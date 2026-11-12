"""Day 28 MCP host: edge policy, mounted servers, F1-F4 through the full host.

In-memory ASGI transport only (no listener). The host's ASGI lifespan is driven
explicitly, so the three SDK session managers start and stop as under uvicorn.
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError
from sqlalchemy import func, insert, update
from sqlalchemy import text as sql
from test_auth import AUDIENCE as REST_AUDIENCE
from test_auth import trust as trust
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day14_quotation import insert_valid_runtime_rfq
from test_mcp_auth import ARGS, BASE, MCP_AUDIENCE, NOT_FOUND, settings, world
from test_mcp_manufacturing import text

from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.schemas.reads import MaterialPage, MaterialSummary
from rfq_copilot.mcp import contracts as c
from rfq_copilot.mcp import host as h
from rfq_copilot.mcp import manufacturing as m
from rfq_copilot.mcp import security
from rfq_copilot.models import RfqAssignment, TokenRevocation, User

HOST = "127.0.0.1:8000"
ORIGIN = "http://127.0.0.1:8000"
PATHS = {"manufacturing": "/mcp/manufacturing", "supply": "/mcp/supply", "pricing": "/mcp/pricing"}
PATH_OF = {t.name: PATHS[t.server] for t in c.TOOLS}


def host_settings(**overrides):
    values = {"public_base_url": BASE, "allowed_hosts": (HOST,), "allowed_origins": (ORIGIN,)}
    return h.McpHostSettings(**{**values, **overrides})


class Factories:
    """Session factories for the host. ``session`` serves identity reads and ``snapshot``
    business evaluations; both default to "never used" for edge-only tests."""

    def __init__(self, identity=None, snapshot=None):
        self.entered = []
        self._identity, self._snapshot = identity, snapshot

    @asynccontextmanager
    async def session(self):
        assert self._identity is not None, "identity read not expected"
        yield self._identity

    @asynccontextmanager
    async def snapshot_session(self):
        assert self._snapshot is not None, "business snapshot not expected"
        self.entered.append(1)
        if callable(self._snapshot):
            async with self._snapshot() as s:
                yield s
        else:
            yield self._snapshot


@asynccontextmanager
async def running(app):
    """Drive the ASGI lifespan like a server: startup, serve, shutdown."""
    inbox, outbox = asyncio.Queue(), asyncio.Queue()
    task = asyncio.create_task(app({"type": "lifespan"}, inbox.get, outbox.put))
    await inbox.put({"type": "lifespan.startup"})
    assert (await outbox.get())["type"] == "lifespan.startup.complete"
    try:
        yield app
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        assert (await outbox.get())["type"] == "lifespan.shutdown.complete"
        await task


@asynccontextmanager
async def http(app, token=None, **headers):
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=BASE, headers=headers
    ) as client:
        yield client


async def tool(app, token, name, request):
    async with http(app, token) as client:
        url = f"{BASE}{PATH_OF[name]}"
        async with Client(streamable_http_client(url, http_client=client), mode="2026-07-28") as mc:
            return await mc.call_tool(name, {"request": request})


def body(method="tools/call", name="get_material", version="2026-07-28", **extra):
    params = {
        "name": name,
        "arguments": {"request": {"material_id": "X"}},
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": version,
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params, **extra}


def modern_headers(method="tools/call", name="get_material", version="2026-07-28"):
    return {
        "MCP-Protocol-Version": version,
        "Mcp-Method": method,
        "Mcp-Name": name,
        "Accept": "application/json, text/event-stream",
    }


def edge_host(trust, **overrides):
    return h.create_mcp_host(host_settings(**overrides), settings(trust), Factories())


# --- fail-closed configuration ---------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"allowed_hosts": ("*",)},
        {"allowed_origins": ("*",)},
        {"allowed_hosts": ("other:1",)},  # must contain the public host
        {"public_base_url": "http://10.0.0.5:8000", "allowed_hosts": ("10.0.0.5:8000",)},
        {"public_base_url": "https://mcp.example.test", "allowed_hosts": ("mcp.example.test",)},
        {
            "public_base_url": "https://mcp.example.test",
            "allowed_hosts": ("mcp.example.test",),
            "tls_terminating_proxy": True,
        },  # no trusted proxy address
        {"public_base_url": f"{BASE}/mcp"},
        {"max_body_bytes": 10 * 1024 * 1024},
    ],
    ids=[
        "host-wildcard",
        "origin-wildcard",
        "host-mismatch",
        "plain-http-public",
        "https-no-proxy",
        "proxy-not-trusted",
        "path-in-base",
        "body-limit",
    ],
)
def test_unsafe_host_settings_are_refused(override):
    with pytest.raises(ValidationError):
        host_settings(**override)


def test_declared_tls_proxy_boundary_is_accepted():
    s = host_settings(
        public_base_url="https://mcp.example.test",
        allowed_hosts=("mcp.example.test",),
        allowed_origins=(),
        tls_terminating_proxy=True,
        trusted_proxies=("10.0.0.2",),
    )
    assert s.base == "https://mcp.example.test"


@pytest.mark.parametrize("problem", ["no-database", "resource-elsewhere", "env-missing"])
def test_host_refuses_to_build_without_safe_auth_and_factories(trust, problem, monkeypatch):
    if problem == "no-database":
        with pytest.raises(TypeError):
            h.create_mcp_host(host_settings(), settings(trust), None)
    elif problem == "resource-elsewhere":
        with pytest.raises(ValueError):
            h.create_mcp_host(host_settings(), settings(trust, resource_server_url="http://evil.test/mcp"), Factories())  # fmt: skip
    else:
        for key in list(__import__("os").environ):
            if key.startswith("RFQ_MCP_"):
                monkeypatch.delenv(key)
        with pytest.raises(ValidationError):
            h.create_app_from_env()


def test_mcp_auth_cannot_be_weakened_for_the_host(trust):
    for override in (
        {"permission_policy": "v1"},
        {"token_revocation": "off"},
        {"rest_audience": MCP_AUDIENCE},
        {"jwt_public_key_file": Path("/nonexistent/key.pem")},
    ):
        with pytest.raises((ValidationError, ValueError, OSError)):
            h.create_mcp_host(host_settings(), settings(trust, **override), Factories())


@pytest.mark.parametrize(
    ("overrides", "ok"),
    [
        ({}, True),  # standard profile: approved defaults unchanged
        (
            {
                "deployment_profile": "hardened",
                "permission_policy": "v2",
                "token_revocation": "enforced",
                "upload_idempotency": "enabled",
            },
            True,
        ),
        (
            {
                "deployment_profile": "hardened",
                "token_revocation": "enforced",
                "upload_idempotency": "enabled",
            },
            False,
        ),  # v1
        (
            {
                "deployment_profile": "hardened",
                "permission_policy": "v2",
                "upload_idempotency": "enabled",
            },
            False,
        ),  # revocation off
        (
            {
                "deployment_profile": "hardened",
                "permission_policy": "v2",
                "token_revocation": "enforced",
            },
            False,
        ),  # idempotency off
        (
            {
                "deployment_profile": "hardened",
                "permission_policy": "v2",
                "token_revocation": "enforced",
                "upload_idempotency": "enabled",
                "environment": "development",
            },
            False,
        ),
    ],
    ids=["standard", "hardened", "v1", "no-revocation", "no-idempotency", "development"],
)
def test_rest_hardened_profile_refuses_insecure_settings(trust, overrides, ok):
    values = {**trust.settings().model_dump(exclude={"environment"}), **overrides}
    values.setdefault("environment", "production")
    if ok:
        assert ApiSettings(**values)
    else:
        with pytest.raises(ValidationError):
            ApiSettings(**values)


def test_rest_hardened_profile_requires_jwt():
    with pytest.raises(ValidationError):
        ApiSettings(
            deployment_profile="hardened",
            permission_policy="v2",
            token_revocation="enforced",
            upload_idempotency="enabled",
        )


# --- edge policy (no database; rejected before any SDK app or snapshot) -----------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method", "headers", "payload", "status"),
    [
        ("/mcp", "POST", {}, body(), 404),
        ("/mcp/manufacturing/", "POST", modern_headers(), body(), 404),  # no slash variant
        ("/api/v1/rfqs", "GET", {}, None, 404),
        ("/mcp/admin", "POST", modern_headers(), body(), 404),
        ("/mcp/manufacturing", "GET", modern_headers(), None, 405),  # no legacy GET stream
        ("/mcp/manufacturing", "DELETE", modern_headers(), None, 405),
        ("/mcp/manufacturing", "POST", {}, body(), 400),  # no version header: legacy
        (
            "/mcp/manufacturing",
            "POST",
            modern_headers(version="2025-06-18"),
            body(version="2025-06-18"),
            400,
        ),  # fmt: skip
        ("/mcp/manufacturing", "POST", modern_headers(version="2025-11-25"), body(), 400),
        ("/mcp/manufacturing", "POST", modern_headers(version="2099-01-01"), body(), 400),
        (
            "/mcp/manufacturing",
            "POST",
            modern_headers(method="initialize"),
            body(method="initialize"),
            400,
        ),  # fmt: skip
        ("/mcp/manufacturing", "POST", modern_headers(), [body()], 400),  # batch
        ("/mcp/manufacturing", "POST", modern_headers(), "not json {", 400),
        ("/mcp/manufacturing", "POST", modern_headers(method="tools/list"), body(), 400),
        ("/mcp/manufacturing", "POST", modern_headers(name="calculate_price"), body(), 400),
        ("/mcp/manufacturing", "POST", modern_headers(), body(version="2025-11-25"), 400),
        ("/mcp/manufacturing", "POST", modern_headers(), {**body(), "jsonrpc": "1.0"}, 400),
    ],
    ids=[
        "root",
        "slash",
        "rest",
        "unknown",
        "get",
        "delete",
        "no-version",
        "legacy",
        "handshake-version",
        "unknown-version",
        "initialize",
        "batch",
        "malformed",
        "method-mismatch",
        "name-mismatch",
        "meta-mismatch",
        "not-jsonrpc",
    ],
)
async def test_edge_rejects_before_the_sdk(trust, path, method, headers, payload, status):
    app = edge_host(trust)
    content = payload if isinstance(payload, str) else json.dumps(payload) if payload else None
    async with running(app), http(app, **headers) as client:
        response = await client.request(method, path, content=content)
    assert response.status_code == status
    assert response.json() == {"error": h._REJECT[status]}  # fixed text, nothing echoed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Host": "evil.test"}, 421),
        ({"Origin": "http://evil.test"}, 403),
        ({"Host": "127.0.0.1:9999"}, 421),
    ],
    ids=["dns-rebinding-host", "foreign-origin", "other-port"],
)
async def test_host_and_origin_allowlists(trust, headers, status):
    app = edge_host(trust)
    async with running(app), http(app, **modern_headers(), **headers) as client:
        response = await client.post("/mcp/manufacturing", json=body())
    assert response.status_code == status


@pytest.mark.asyncio
async def test_oversized_bodies_are_rejected(trust):
    app = edge_host(trust, max_body_bytes=2048)
    big = body(padding="x" * 4096)
    async with running(app), http(app, **modern_headers()) as client:
        declared = await client.post("/mcp/manufacturing", json=big)

        async def chunks():  # no content-length: counted while streaming
            yield json.dumps(big).encode()

        streamed = await client.post("/mcp/manufacturing", content=chunks())
    assert declared.status_code == streamed.status_code == 413


@pytest.mark.asyncio
async def test_modern_request_without_token_gets_401_and_per_server_metadata(trust):
    app = edge_host(trust)
    async with running(app), http(app, **modern_headers()) as client:
        for server, path in PATHS.items():
            response = await client.post(path, json=body())
            assert response.status_code == 401, server
            challenge = response.headers["www-authenticate"]
            expected = f"{BASE}/.well-known/oauth-protected-resource{path}"
            assert f'resource_metadata="{expected}"' in challenge
            meta = await client.get(f"/.well-known/oauth-protected-resource{path}")
            assert meta.status_code == 200 and meta.json()["resource"] == f"{BASE}{path}"


# --- authenticated paths with faked identity rows (no database) --------------------------


@pytest.fixture
def fake_identity(monkeypatch):
    """Real JWT verification; identity rows faked per subject; tool reports its principal."""

    class Row:
        def __init__(self, subject):
            self.user_id = f"U-{subject}"

    async def resolve(session, credential, token, **kwargs):
        await asyncio.sleep(0.001 * (hash(token.subject) % 5))
        return Row(token.subject), f"CUST-{token.subject}", frozenset({"view_catalog"})

    delay = {"seconds": 0.0}

    async def page(session, query):
        from mcp.server.auth.middleware.auth_context import get_access_token

        await asyncio.sleep(delay["seconds"])
        seen = get_access_token().customer_id
        return MaterialPage(
            items=(MaterialSummary(material_id=seen, category="x", grade="y"),),
            limit=query.limit,
            offset=0,
            has_more=False,
        )

    monkeypatch.setattr(security, "resolve_identity", resolve)
    monkeypatch.setattr(m, "material_page", page)
    return delay


def faked_host(trust, **overrides):
    return h.create_mcp_host(
        host_settings(**overrides), settings(trust), Factories(identity=object(), snapshot=object())
    )


async def search(app, token):
    return await tool(app, token, "search_materials", {})


@pytest.mark.asyncio
async def test_concurrent_identities_stay_isolated_through_the_host(trust, fake_identity):
    app = faked_host(trust, rate_limit_per_minute=1000)
    subjects = [f"S{i % 4}" for i in range(12)]
    async with running(app):
        results = await asyncio.gather(*(search(app, trust.mint(sub=s, aud=MCP_AUDIENCE)) for s in subjects))  # fmt: skip
    assert [r.structured_content["items"][0]["material_id"] for r in results] == [f"CUST-{s}" for s in subjects]  # fmt: skip


@pytest.mark.asyncio
async def test_rate_limit(trust, fake_identity):
    app = faked_host(trust, rate_limit_per_minute=3)
    token = trust.mint(sub="S1", aud=MCP_AUDIENCE)
    async with running(app), http(app, token, **modern_headers()) as client:
        statuses = [(await client.post("/mcp/manufacturing", json=body())).status_code for _ in range(5)]  # fmt: skip
    assert statuses[3:] == [429, 429] and 429 not in statuses[:3]


@pytest.mark.asyncio
async def test_forwarded_for_is_ignored_unless_from_a_trusted_proxy(trust):
    app = edge_host(trust, rate_limit_per_minute=2)
    async with running(app), http(app, **modern_headers()) as client:
        spoofed = [(await client.post("/mcp/manufacturing", json=body(), headers={"X-Forwarded-For": f"10.0.0.{i}"})).status_code for i in range(3)]  # fmt: skip
    assert spoofed[-1] == 429  # one peer, however many forwarded addresses it claims


@pytest.mark.asyncio
async def test_concurrency_limit_and_timeout(trust, fake_identity):
    fake_identity["seconds"] = 0.5
    app = faked_host(trust, max_concurrent_requests=1, request_timeout_seconds=0.2)
    token = trust.mint(sub="S1", aud=MCP_AUDIENCE)
    async with running(app), http(app, token, **modern_headers(name="search_materials")) as client:
        payload = body(name="search_materials")
        payload["params"]["arguments"] = {"request": {}}
        slow = asyncio.create_task(client.post("/mcp/manufacturing", json=payload))
        await asyncio.sleep(0.05)
        busy = await client.post("/mcp/manufacturing", json=payload)
        timed_out = await slow
    assert busy.status_code == 503
    assert timed_out.status_code == 504 and timed_out.json() == {"error": h._REJECT[504]}


# --- full host on the seeded test database ----------------------------------------------


def seeded_host(populated, trust, entered=None, **auth):
    factories = Factories(identity=populated, snapshot=populated)
    app = h.create_mcp_host(host_settings(rate_limit_per_minute=1000), settings(trust, **auth), factories)  # fmt: skip
    return app, factories


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_host_serves_exactly_ten_tools_with_independent_permissions(
    populated, seed_plan, trust
):
    _, _, user_a, _ = await world(populated, seed_plan)
    token = trust.mint(sub=user_a.authentication_subject, aud=MCP_AUDIENCE)
    app, factories = seeded_host(populated, trust)
    listed = {}
    async with running(app):
        async with http(app, token) as client:
            for server, path in PATHS.items():
                url = f"{BASE}{path}"
                async with Client(streamable_http_client(url, http_client=client), mode="2026-07-28") as mc:  # fmt: skip
                    listed[server] = sorted(t.name for t in (await mc.list_tools()).tools)
        for contract in c.TOOLS:
            result = await tool(app, token, contract.name, ARGS[contract.name])
            assert not result.is_error, (contract.name, text(result))
        # Same token, reduced role: each server re-checks its own permission.
        await populated.execute(update(User).where(User.user_id == user_a.user_id).values(role="manufacturing_engineer"))  # fmt: skip
        populated.expunge_all()
        assert not (await tool(app, token, "get_material", ARGS["get_material"])).is_error
        assert text(await tool(app, token, "calculate_price", PLAN_ARGS)) == "PERMISSION_DENIED: Permission denied."  # fmt: skip
    assert listed == {s: sorted(t.name for t in c.TOOLS if t.server == s) for s in PATHS}
    assert sum(map(len, listed.values())) == 10
    assert len(factories.entered) == 10 + 1  # one business snapshot per successful call


PLAN_ARGS = ARGS["calculate_price"]


@pytest.mark.parametrize(
    "case", ["missing", "expired", "rest-audience", "revoked", "bad-signature"]
)
@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_rejected_credentials_never_open_a_snapshot(populated, seed_plan, trust, case):
    _, _, user_a, _ = await world(populated, seed_plan)
    sub = user_a.authentication_subject
    import time as clock

    now = int(clock.time())
    token = {
        "missing": None,
        "expired": trust.mint(sub=sub, aud=MCP_AUDIENCE, iat=now - 600, exp=now - 300),
        "rest-audience": trust.mint(sub=sub, aud=REST_AUDIENCE),
        "revoked": trust.mint(sub=sub, aud=MCP_AUDIENCE),
        "bad-signature": trust.mint(sub=sub, aud=MCP_AUDIENCE)[:-4] + "AAAA",
    }[case]
    if case == "revoked":
        await populated.execute(insert(TokenRevocation).values(revocation_id=uuid.uuid4(), user_id=user_a.user_id, not_before=func.now() + timedelta(minutes=1), created_by=user_a.user_id, reason="security"))  # fmt: skip
    app, factories = seeded_host(populated, trust)
    async with running(app), http(app, token, **modern_headers()) as client:
        for path in PATHS.values():
            response = await client.post(path, json=body())
            assert response.status_code == 401
    assert factories.entered == []


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_customer_isolation_and_assignments_survive_the_host(populated, seed_plan, trust):
    a, _, user_a, user_b = await world(populated, seed_plan)
    await insert_valid_runtime_rfq(populated, a)
    request = {"rfq_id": "TEST-DAY14-RFQ", "horizon_start": "2026-11-09"}
    token_a = trust.mint(sub=user_a.authentication_subject, aud=MCP_AUDIENCE)
    token_b = trust.mint(sub=user_b.authentication_subject, aud=MCP_AUDIENCE)
    app, _ = seeded_host(populated, trust)
    async with running(app):
        own = await tool(app, token_a, "evaluate_quotation", request)
        foreign = await tool(app, token_b, "evaluate_quotation", request)
        missing = await tool(app, token_b, "evaluate_quotation", {**request, "rfq_id": "NONE"})
    assert not own.is_error and own.structured_content["result"] == "quoted"
    assert text(foreign) == text(missing) == NOT_FOUND
    assigned_app, _ = seeded_host(populated, trust, rfq_assignments="required")
    async with running(assigned_app):
        unassigned = await tool(assigned_app, token_a, "check_inventory", request)
        await populated.execute(insert(RfqAssignment).values(rfq_id="TEST-DAY14-RFQ", user_id=user_a.user_id, assigned_by=user_b.user_id))  # fmt: skip
        assigned = await tool(assigned_app, token_a, "check_inventory", request)
    assert text(unassigned) == NOT_FOUND and not assigned.is_error


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_host_snapshot_is_read_only_repeatable_read(
    populated, seed_plan, seed_database, trust
):
    # Identity from the seeded transaction; business snapshot from the real
    # Database.snapshot_session() on the guarded test database (migrated, empty catalog).
    _, _, user_a, _ = await world(populated, seed_plan)
    seen = []

    @asynccontextmanager
    async def real_snapshot():
        async with seed_database.snapshot_session() as s:
            seen.append(
                (
                    await s.scalar(sql("SHOW transaction_isolation")),
                    await s.scalar(sql("SHOW transaction_read_only")),
                )
            )
            yield s

    factories = Factories(identity=populated, snapshot=real_snapshot)
    app = h.create_mcp_host(host_settings(), settings(trust), factories)
    token = trust.mint(sub=user_a.authentication_subject, aud=MCP_AUDIENCE)
    async with running(app):
        result = await search(app, token)
    assert not result.is_error and result.structured_content["items"] == []
    assert seen == [("repeatable read", "on")]
