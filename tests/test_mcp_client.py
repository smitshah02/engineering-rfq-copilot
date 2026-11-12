"""Day 29: MCP client, REST -> MCP delegation, and a live loopback host smoke test.

The credential provider here is TEST-ONLY: it signs MCP-audience tokens with the test
trust key, standing in for an identity-provider token exchange that does not exist yet.
Production ships no provider, so the MCP-backed REST route is not mounted.
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from dotenv import dotenv_values
from sqlalchemy import insert, update
from test_auth import AUDIENCE as REST_AUDIENCE
from test_auth import ISSUER, bearer
from test_auth import trust as trust
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day14_quotation import insert_valid_runtime_rfq
from test_day20_auth_database import call
from test_day20_auth_database import database as database
from test_mcp_auth import BASE, MCP_AUDIENCE, settings, world
from test_mcp_host import Factories, body, host_settings, modern_headers, running
from test_mcp_host import fake_identity as fake_identity

from rfq_copilot.api.app import create_app
from rfq_copilot.api.auth import Principal
from rfq_copilot.api.mcp_integration import McpIntegration
from rfq_copilot.mcp import client as mc
from rfq_copilot.mcp import contracts as c
from rfq_copilot.mcp import host as h
from rfq_copilot.mcp.client import McpCallError, McpToolClient
from rfq_copilot.models import TokenRevocation, User
from rfq_copilot.services.materials import get_material_spec

ROOT = Path(__file__).resolve().parents[1]
PLAN = {
    "parts": [
        {
            "part_id": "PANEL",
            "part_type": "flat_panel",
            "material_id": "MAT-002",
            "finish_id": "none",
            "length_mm": 1000,
            "width_mm": 500,
            "thickness_mm": "2.0",
            "tolerance_mm": "0.5",
            "quantity": 10,
        }
    ],
    "horizon_start": "2026-11-09",
}


class TestIssuer:
    """TEST-ONLY delegated credentials: MCP audience, the principal's own subject."""

    __test__ = False

    def __init__(self, trust, **claims):
        self.trust, self.claims, self.issued = trust, claims, []

    async def credential_for(self, principal):
        token = self.trust.mint(**{"sub": principal.subject, "aud": MCP_AUDIENCE, **self.claims})
        self.issued.append(token)
        return token


class Unused:
    """A transport that must never be reached (local rejections)."""

    async def handle_async_request(self, request):
        raise AssertionError("no network call expected")


def status_app(status, hits):
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        hits.append(status)
        while (await receive()).get("more_body"):
            pass
        payload = b'{"error":"x","secret":"sk-FAKE-123"}'
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    return app


# --- client: local validation and failure mapping (no database) ------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "request_", "token", "code"),
    [
        ("drop_tables", {}, "t", "invalid_input"),  # not an approved tool
        ("get_material", {"material_id": "M", "customer_id": "C"}, "t", "invalid_input"),
        ("check_inventory", {**PLAN, "inventory": {"result": "sufficient"}}, "t", "invalid_input"),
        ("get_material", {"material_id": "M"}, "", "unauthenticated"),
    ],
    ids=["unknown-tool", "forged-customer", "upstream-result", "no-token"],
)
async def test_client_rejects_locally_without_network(tool, request_, token, code):
    client = McpToolClient(BASE, transport=Unused())
    with pytest.raises(McpCallError) as caught:
        await client.call(tool, request_, token=token)
    assert caught.value.code == code


def test_client_refuses_arbitrary_destinations():
    for url in ("http://127.0.0.1:8000/mcp", "ftp://x", "127.0.0.1:8000"):
        with pytest.raises(ValueError):
            McpToolClient(url)
    assert set(mc.SERVER_PATHS) == {t.server for t in c.TOOLS}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "unauthenticated"),
        (429, "busy"),
        (503, "busy"),
        (504, "timeout"),
        (421, "unavailable"),
        (403, "unavailable"),
        (400, "unavailable"),
        (500, "unavailable"),
    ],
)
async def test_http_failures_map_to_safe_codes_without_retry(status, code):
    hits = []
    client = McpToolClient(BASE, transport=httpx2.ASGITransport(app=status_app(status, hits)))
    with pytest.raises(McpCallError) as caught:
        await client.call("get_material", {"material_id": "M"}, token="t")
    assert caught.value.code == code and str(caught.value) == code
    assert hits == [status]  # exactly one attempt: no retry loop


@pytest.mark.asyncio
async def test_unreachable_host_and_timeout():
    with socket.socket() as probe:  # a port with nothing listening
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(McpCallError) as refused:
        await McpToolClient(f"http://127.0.0.1:{port}").call("get_material", {"material_id": "M"}, token="t")  # fmt: skip

    async def slow(scope, receive, send):
        await asyncio.sleep(5)

    client = McpToolClient(BASE, timeout_seconds=0.2, transport=httpx2.ASGITransport(app=slow))
    with pytest.raises(McpCallError) as late:
        await client.call("get_material", {"material_id": "M"}, token="t")
    assert (refused.value.code, late.value.code) == ("unavailable", "timeout")


# --- client against the Day 28 host (faked identity rows; no database) ----------------


def faked_gateway(trust, **overrides):
    return h.create_mcp_host(
        host_settings(rate_limit_per_minute=1000, **overrides),
        settings(trust),
        Factories(identity=object(), snapshot=object()),
    )


@pytest.mark.asyncio
async def test_client_concurrent_identities_and_rbac_through_the_host(trust, fake_identity):
    gateway = faked_gateway(trust)
    client = McpToolClient(BASE, transport=httpx2.ASGITransport(app=gateway))
    subjects = [f"S{i % 3}" for i in range(9)]
    async with running(gateway):
        pages = await asyncio.gather(*(client.call("search_materials", {}, token=trust.mint(sub=s, aud=MCP_AUDIENCE)) for s in subjects))  # fmt: skip
        with pytest.raises(McpCallError) as denied:  # fake grants are view_catalog only
            await client.call("estimate_costs", PLAN, token=trust.mint(sub="S0", aud=MCP_AUDIENCE))  # fmt: skip
        with pytest.raises(McpCallError) as rest_token:
            await client.call("search_materials", {}, token=trust.mint(sub="S0"))  # REST audience
    assert [p.items[0].material_id for p in pages] == [f"CUST-{s}" for s in subjects]
    assert (denied.value.code, rest_token.value.code) == ("forbidden", "unauthenticated")


@pytest.mark.asyncio
async def test_invalid_structured_output_is_refused(trust, fake_identity, monkeypatch):
    stricter = replace(mc.CONTRACTS["search_materials"], output_model=c.MaterialSpecOutput)
    monkeypatch.setitem(mc.CONTRACTS, "search_materials", stricter)
    gateway = faked_gateway(trust)
    client = McpToolClient(BASE, transport=httpx2.ASGITransport(app=gateway))
    async with running(gateway):
        with pytest.raises(McpCallError) as caught:
            await client.call("search_materials", {}, token=trust.mint(sub="S", aud=MCP_AUDIENCE))
    assert caught.value.code == "invalid_output"


# --- delegation guard (no database, no network) ----------------------------------------


class Fixed:
    def __init__(self, token=None, error=None):
        self.token, self.error = token, error

    async def credential_for(self, principal):
        if self.error:
            raise self.error
        return self.token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["passthrough", "rest-audience", "other-subject", "provider-error", "not-a-jwt", "no-subject"],
)
async def test_delegation_guard_fails_closed(trust, case):
    rest = trust.mint(sub="auth|alice")
    provider = {
        "passthrough": Fixed(rest),
        "rest-audience": Fixed(trust.mint(sub="auth|alice", aud=REST_AUDIENCE, jti="x")),
        "other-subject": Fixed(trust.mint(sub="auth|mallory", aud=MCP_AUDIENCE)),
        "provider-error": Fixed(error=RuntimeError("idp down sk-FAKE")),
        "not-a-jwt": Fixed("garbage"),
        "no-subject": Fixed(trust.mint(sub="auth|alice", aud=MCP_AUDIENCE)),
    }[case]
    integration = McpIntegration(
        McpToolClient(BASE, transport=Unused()), provider, MCP_AUDIENCE, REST_AUDIENCE
    )
    principal = Principal(
        "CUST-A", frozenset(), user_id="U-1", subject=None if case == "no-subject" else "auth|alice"
    )
    with pytest.raises(McpCallError) as caught:
        await integration.call(
            principal, "get_material", {"material_id": "M"}, rest_credential=rest
        )
    assert caught.value.code == "unauthenticated"


def test_integration_requires_a_provider_and_a_distinct_audience(trust):
    client = McpToolClient(BASE)
    with pytest.raises(TypeError):
        McpIntegration(client, None, MCP_AUDIENCE, REST_AUDIENCE)
    with pytest.raises(ValueError):
        McpIntegration(client, TestIssuer(trust), REST_AUDIENCE, REST_AUDIENCE)


def test_without_an_integration_the_mcp_route_is_not_mounted(trust):
    app = create_app(trust.settings())
    paths = {getattr(r, "path", "") for r in app.router.routes}
    assert not any("specification" in p for p in paths) and app.state.mcp is None


# --- REST -> MCP on the seeded test database ---------------------------------------------


def hardened(trust):
    return {
        "deployment_profile": "hardened",
        "environment": "production",
        "permission_policy": "v2",
        "token_revocation": "enforced",
        "upload_idempotency": "enabled",
    }


def stack(trust, populated, database, issuer):
    gateway = h.create_mcp_host(
        host_settings(rate_limit_per_minute=1000),
        settings(trust),
        Factories(identity=populated, snapshot=populated),
    )
    client = McpToolClient(BASE, transport=httpx2.ASGITransport(app=gateway))
    integration = McpIntegration(client, issuer, MCP_AUDIENCE, REST_AUDIENCE)
    app = create_app(trust.settings(**hardened(trust)), database=database, mcp=integration)
    return gateway, client, app


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_rest_to_mcp_material_specification(trust, populated, seed_plan, database):
    _, _, user_a, _ = await world(populated, seed_plan)
    issuer = TestIssuer(trust)
    gateway, _, app = stack(trust, populated, database, issuer)
    rest_token = trust.mint(sub=user_a.authentication_subject)
    auth = bearer(rest_token)
    async with running(gateway):
        ok = await call(app, "GET", "/api/v1/materials/MAT-002/specification", headers=auth)
        missing = await call(app, "GET", "/api/v1/materials/NOPE-404/specification", headers=auth)
        anonymous = await call(app, "GET", "/api/v1/materials/MAT-002/specification")
        spoofed = await call(app, "GET", "/api/v1/materials/MAT-002/specification",
                             headers={**auth, "X-RFQ-Customer-Context": "OTHER", "X-User-Id": "U-9"})  # fmt: skip
    direct = await get_material_spec(populated, "MAT-002")
    assert ok.status_code == 200, ok.text
    data = ok.json()
    assert data["material_id"] == "MAT-002" and data["density_kg_m3"] == str(direct.density_kg_m3)
    assert all(isinstance(v, str) for v in data["supported_thickness_mm"])
    assert (missing.status_code, missing.json()) == (404, {"detail": "Not found."})
    assert anonymous.status_code == 401 and spoofed.status_code == 200
    assert issuer.issued and rest_token not in issuer.issued  # the REST token never left
    claims = [json.loads(__import__("base64").urlsafe_b64decode(t.split(".")[1] + "==")) for t in issuer.issued]  # fmt: skip
    assert {(x["aud"], x["sub"]) for x in claims} == {(MCP_AUDIENCE, user_a.authentication_subject)}  # fmt: skip


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_rest_rbac_and_revoked_delegated_token(trust, populated, seed_plan, database):
    _, _, user_a, _ = await world(populated, seed_plan)
    fixed = trust.mint(sub=user_a.authentication_subject, aud=MCP_AUDIENCE)
    gateway, _, app = stack(trust, populated, database, Fixed(fixed))
    auth = bearer(trust.mint(sub=user_a.authentication_subject))
    path = "/api/v1/materials/MAT-002/specification"
    async with running(gateway):
        assert (await call(app, "GET", path, headers=auth)).status_code == 200
        import hashlib

        await populated.execute(insert(TokenRevocation).values(revocation_id=uuid.uuid4(), user_id=user_a.user_id, token_sha256=hashlib.sha256(fixed.encode()).hexdigest(), created_by=user_a.user_id, reason="security"))  # fmt: skip
        revoked = await call(app, "GET", path, headers=auth)
        await populated.execute(update(User).where(User.user_id == user_a.user_id).values(role="auditor"))  # fmt: skip
        populated.expunge_all()
        no_catalog = await call(app, "GET", path, headers=auth)  # auditor: view_quote only
    assert (revoked.status_code, revoked.json()) == (503, {"detail": "Service unavailable."})
    assert no_catalog.status_code == 403  # REST v2 RBAC refuses before any MCP call


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_client_reaches_all_three_servers_with_customer_scope(trust, populated, seed_plan):
    a, _, user_a, user_b = await world(populated, seed_plan)
    await insert_valid_runtime_rfq(populated, a)
    gateway, client, _ = stack(trust, populated, None, TestIssuer(trust))
    token_a = trust.mint(sub=user_a.authentication_subject, aud=MCP_AUDIENCE)
    token_b = trust.mint(sub=user_b.authentication_subject, aud=MCP_AUDIENCE)
    stored = {"rfq_id": "TEST-DAY14-RFQ", "horizon_start": "2026-11-09"}
    async with running(gateway):
        material = await client.call("get_material", {"material_id": "MAT-002"}, token=token_a)
        inventory = await client.call("check_inventory", PLAN, token=token_a)
        quote = await client.call("evaluate_quotation", stored, token=token_a)
        with pytest.raises(McpCallError) as foreign:
            await client.call("evaluate_quotation", stored, token=token_b)
        again = await client.call("evaluate_quotation", stored, token=token_a)
    assert isinstance(material, c.MaterialSpecOutput) and inventory.result == "sufficient"
    assert quote.result == "quoted" and [ln.line_number for ln in quote.lines] == [10, 20]
    assert again == quote  # deterministic
    assert foreign.value.code == "not_found"


# --- live loopback host (real uvicorn process) -------------------------------------------


@contextmanager
def live_host(trust, **extra_env):
    """A real ``uvicorn --factory rfq_copilot.mcp.host:create_app_from_env`` process on a
    free loopback port, guarded test DB config. Yields (base, secrets, log) where ``log``
    receives the process output after a SIGTERM shutdown that must complete cleanly."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env_file = dotenv_values(ROOT / ".env") if (ROOT / ".env").is_file() else {}
    secrets = [v for v in env_file.values() if v and len(v) >= 6]
    env = {
        **os.environ,
        **{k: v for k, v in env_file.items() if v is not None},
        "RFQ_MCP_RESOURCE_SERVER_URL": f"{base}/mcp",
        "RFQ_MCP_JWT_ISSUER": ISSUER,
        "RFQ_MCP_JWT_AUDIENCE": MCP_AUDIENCE,
        "RFQ_MCP_JWT_ALGORITHM": trust.algorithm,
        "RFQ_MCP_JWT_PUBLIC_KEY_FILE": str(trust.key_file),
        "RFQ_API_JWT_AUDIENCE": REST_AUDIENCE,
        "RFQ_MCP_DATABASE_TARGET": "test",
        "RFQ_MCP_HOST_PUBLIC_BASE_URL": base,
        "RFQ_MCP_HOST_ALLOWED_HOSTS": f"127.0.0.1:{port}",
        "RFQ_MCP_HOST_ALLOWED_ORIGINS": base,
        **extra_env,
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "rfq_copilot.mcp.host:create_app_from_env",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "--no-proxy-headers",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log = []
    try:
        with httpx2.Client(base_url=base, timeout=5) as http:
            deadline = time.monotonic() + 20
            while True:
                try:
                    if http.get("/.well-known/oauth-protected-resource/mcp/manufacturing").status_code == 200:  # fmt: skip
                        break
                except httpx2.TransportError:
                    pass
                assert process.poll() is None, "host exited during startup"
                assert time.monotonic() < deadline, "host did not start"
                time.sleep(0.2)
        yield base, secrets, log
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        log.append(output)
    assert process.returncode in (0, -signal.SIGTERM)
    assert "Application startup complete" in output and "Application shutdown complete" in output
    assert not any(s in output for s in secrets)  # nothing secret logged either


@pytest.mark.integration
def test_live_uvicorn_host_smoke(trust):
    """Unauthenticated and rejection paths only: a separate process cannot see the rolled-back
    seeded fixtures. Authenticated live execution: tests/test_mcp_live_e2e.py (Day 30)."""
    with live_host(trust) as (base, secrets, _):
        with httpx2.Client(base_url=base, timeout=5) as http:
            responses = []
            for path in ("/mcp/manufacturing", "/mcp/supply", "/mcp/pricing"):
                meta = http.get(f"/.well-known/oauth-protected-resource{path}")
                assert meta.json()["resource"] == f"{base}{path}"
                anon = http.post(path, json=body(), headers=modern_headers())
                assert anon.status_code == 401 and f"/.well-known/oauth-protected-resource{path}" in anon.headers["www-authenticate"]  # fmt: skip
                responses += [meta, anon]
            checks = {
                "rest-audience": (401, {"Authorization": f"Bearer {trust.mint()}"}),
                "bad-signature": (
                    401,
                    {"Authorization": f"Bearer {trust.mint(aud=MCP_AUDIENCE)[:-4]}AAAA"},
                ),  # fmt: skip
                "unknown-subject": (
                    401,
                    {"Authorization": f"Bearer {trust.mint(aud=MCP_AUDIENCE, sub='auth|none')}"},
                ),  # fmt: skip
                "evil-host": (421, {"Host": "evil.test"}),
                "evil-origin": (403, {"Origin": "http://evil.test"}),
            }
            for name, (status, extra) in checks.items():
                r = http.post(
                    "/mcp/manufacturing", json=body(), headers={**modern_headers(), **extra}
                )
                assert r.status_code == status, name
                responses.append(r)
            for status, kwargs in [
                (400, {"content": b"{not json", "headers": modern_headers()}),
                (
                    400,
                    {
                        "json": body(version="2025-06-18"),
                        "headers": modern_headers(version="2025-06-18"),
                    },
                ),  # fmt: skip
                (404, {"json": body(), "headers": modern_headers(), "url": "/api/v1/rfqs"}),
                (405, {"headers": modern_headers(), "method": "GET"}),
            ]:
                method, url = kwargs.pop("method", "POST"), kwargs.pop("url", "/mcp/manufacturing")
                r = http.request(method, url, **kwargs)
                assert r.status_code == status
                responses.append(r)
        for r in responses:  # no configured secret, path or trace in any response
            blob = r.text + json.dumps(dict(r.headers))
            assert not any(s in blob for s in secrets)
            assert "Traceback" not in blob and str(ROOT) not in blob


def test_auth_and_host_settings_share_the_environment(trust):
    # Day 29 regression: RFQ_MCP_HOST_* and RFQ_MCP_DATABASE_TARGET broke McpAuthSettings.
    env = {
        "RFQ_MCP_RESOURCE_SERVER_URL": f"{BASE}/mcp",
        "RFQ_MCP_JWT_ISSUER": ISSUER,
        "RFQ_MCP_JWT_AUDIENCE": MCP_AUDIENCE,
        "RFQ_MCP_JWT_ALGORITHM": trust.algorithm,
        "RFQ_MCP_JWT_PUBLIC_KEY_FILE": str(trust.key_file),
        "RFQ_MCP_DATABASE_TARGET": "test",
        "RFQ_MCP_HOST_PUBLIC_BASE_URL": BASE,
        "RFQ_MCP_HOST_ALLOWED_HOSTS": "127.0.0.1:8000",
    }
    from rfq_copilot.mcp.security import McpAuthSettings

    assert McpAuthSettings.from_env(env).jwt_audience == MCP_AUDIENCE
    assert h.McpHostSettings.from_env(env).base == BASE
    with pytest.raises(Exception):  # a misspelled security key still fails closed
        McpAuthSettings.from_env({**env, "RFQ_MCP_RFQ_ASIGNMENTS": "off"})
