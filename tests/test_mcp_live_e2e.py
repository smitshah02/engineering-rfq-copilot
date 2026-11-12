"""Day 30: resource-binding model, hardened REST profile, authenticated live end to end.

Live test process boundary: pytest runs the REST app in process (ASGI) -> its MCP client
connects over real loopback TCP -> ``uvicorn --factory rfq_copilot.mcp.host:create_app_from_env``
as a SEPARATE OS process with its own read-only engine on the guarded test database.

Data: a separate process cannot see a rolled-back transaction, so ``live`` COMMITS the
seeded dataset, the rbac-policy-v2 overlay and synthetic identities to ``rfq_copilot_test``
(``seed_database`` requires it empty before it migrates) and truncates every application
table in teardown. ``seed_database`` then asserts the tables empty, downgrades to base and
checks no object is left. The development database is never opened.

Tokens are TEST-ONLY: ``trust.mint`` / ``TestIssuer`` sign with a throwaway key in a pytest
temp directory. The REST app is given only a credential-provider object, never a signing
key; production ships no provider, so the MCP-backed route stays unmounted there.
"""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta

import pytest
import pytest_asyncio
from conftest import connection_config
from sqlalchemy import func, insert, select, text, update
from test_auth import AUDIENCE as REST_AUDIENCE
from test_auth import bearer
from test_auth import trust as trust
from test_database_queries import customer_id, other_customer
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day09_inventory import pinned
from test_day14_quotation import insert_valid_runtime_rfq
from test_day20_auth_database import app_with, bind, call
from test_day20_auth_database import database as database
from test_day21_rbac_database import bound, grants, one_active_user_per_role, own_rfq
from test_mcp_auth import MCP_AUDIENCE, granted_user, settings
from test_mcp_client import PLAN, TestIssuer, hardened, live_host
from test_phase35_completion_database import files, operator_with, policy_v2

from rfq_copilot import identity_admin
from rfq_copilot.api.app import create_app
from rfq_copilot.api.mcp_integration import McpIntegration
from rfq_copilot.database import Database
from rfq_copilot.mcp import contracts as c
from rfq_copilot.mcp import security
from rfq_copilot.mcp.client import McpCallError, McpToolClient
from rfq_copilot.mcp.security import McpTokenVerifier
from rfq_copilot.models import Customer, TokenRevocation, User
from rfq_copilot.seeding.loader import execute_seed
from rfq_copilot.seeding.policy import register_policy
from rfq_copilot.services.materials import get_material_spec
from rfq_copilot.services.quotation import quote_rfq

ENDPOINTS = ("manufacturing", "supply", "pricing")
STORED = {"rfq_id": "TEST-DAY14-RFQ", "horizon_start": "2026-11-09"}
SPEC = "/api/v1/materials/MAT-002/specification"
ONE_TOOL_PER_SERVER = {
    "get_material": {"material_id": "MAT-002"},
    "check_inventory": PLAN,
    "calculate_price": PLAN,
}


# --- resource binding: one shared protected resource (no database) ---------------------


@asynccontextmanager
async def no_session():
    yield None


def endpoint_verifier(trust, monkeypatch, endpoint):
    class Row:
        user_id = "U-1"

    async def resolve(session, credential, token, **kwargs):
        return Row(), "CUST-A", frozenset({"view_catalog"})

    monkeypatch.setattr(security, "resolve_identity", resolve)
    url = f"http://127.0.0.1:8000/mcp/{endpoint}"
    return McpTokenVerifier.from_settings(settings(trust, resource_server_url=url), no_session)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_tokens_are_bound_to_the_mcp_audience_not_an_endpoint(trust, monkeypatch, endpoint):
    verifier = endpoint_verifier(trust, monkeypatch, endpoint)
    principal = await verifier.verify_token(trust.mint(sub="auth|a", aud=MCP_AUDIENCE))
    # The principal reports what the issuer bound (aud), never a URL copied from settings.
    assert principal is not None and principal.resource == MCP_AUDIENCE
    assert verifier.settings.auth_settings().validate_token_resource is False
    # An RFC 8707 per-endpoint audience is NOT accepted: the issuer must mint the MCP
    # audience for every endpoint (deployment requirement, documented in the report).
    url = str(verifier.settings.resource_server_url)
    assert await verifier.verify_token(trust.mint(sub="auth|a", aud=url)) is None


# --- hardened REST profile: configuration and enforced behaviour -----------------------


def test_hardened_profile_never_mounts_rest_to_mcp_delegation(trust):
    app = create_app(trust.settings(**hardened(trust)))
    paths = {getattr(r, "path", "") for r in app.router.routes}
    assert app.state.settings.deployment_profile == "hardened" and app.state.mcp is None
    assert not any(p.endswith("/specification") for p in paths)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_hardened_profile_enforces_revocation_and_upload_idempotency(
    trust, database, populated, seed_plan, tmp_path
):
    await policy_v2(populated)
    user = (await one_active_user_per_role(populated))[0]  # sales_representative
    mine = customer_id(seed_plan)
    await bound(populated, user, mine)
    rfq = await own_rfq(populated, mine)
    app = app_with(trust, database, attachment_dir=tmp_path / "store", **hardened(trust))
    assert app.state.settings.deployment_profile == "hardened"
    token = trust.mint(sub=user.authentication_subject, jti="h-1")
    path = f"/api/v1/rfqs/{rfq}/attachments"
    keyed = {**bearer(token), "Idempotency-Key": "hardened-1"}
    first = await call(app, "POST", path, headers=keyed, files=files())
    again = await call(app, "POST", path, headers=keyed, files=files())
    conflict = await call(app, "POST", path, headers=keyed, files=files("Other"))
    assert first.status_code == again.status_code == 201, first.text
    assert again.json() == first.json() and again.headers["idempotent-replay"] == "true"
    assert conflict.status_code == 409 and len(list((tmp_path / "store").iterdir())) == 1
    operator = await operator_with(populated, "revoke_tokens", exclude=user.user_id)
    outcome = await identity_admin.revoke_tokens(populated, operator=operator.user_id, user=user.user_id, reason="security", token=token, execute=True)  # fmt: skip
    assert outcome.result == "token_revoked"
    revoked = await call(app, "GET", "/api/v1/customers/me", headers=bearer(token))
    fresh = await call(app, "GET", "/api/v1/customers/me", headers=bearer(trust.mint(sub=user.authentication_subject, jti="h-2")))  # fmt: skip
    assert (revoked.status_code, fresh.status_code) == (401, 200)


# --- authenticated live end to end (separate uvicorn process) --------------------------


@pytest_asyncio.fixture(loop_scope="module")
async def live(seed_database, seed_plan):
    """Committed synthetic world on the guarded test DB; truncated again in teardown."""
    a, b = customer_id(seed_plan), other_customer(seed_plan)
    try:
        async with seed_database.session() as s, s.begin():
            await execute_seed(s, seed_database.config, seed_plan, "apply", confirm_database="rfq_copilot_test")  # fmt: skip
            await register_policy(s, "rbac-policy-v2")
            await s.execute(update(Customer).where(Customer.customer_id.in_([a, b])).values(account_status="active"))  # fmt: skip
            user_a = await granted_user(s)
            user_b = await granted_user(s, exclude={user_a.user_id})
            restricted = next(u for u in await one_active_user_per_role(s) if u.role == "manufacturing_engineer")  # fmt: skip
            taken = {user_a.user_id, user_b.user_id, restricted.user_id}
            rest = (await s.scalars(select(User).where(User.active.is_(True), User.user_id.not_in(taken)).order_by(User.user_id).limit(3))).all()  # fmt: skip
            operator, revoked, inactive = rest
            await s.execute(update(User).where(User.user_id == operator.user_id).values(role="identity_operator"))  # fmt: skip
            await s.execute(update(User).where(User.user_id == inactive.user_id).values(active=False))  # fmt: skip
            await s.execute(insert(TokenRevocation).values(revocation_id=uuid.uuid4(), user_id=revoked.user_id, not_before=func.now() + timedelta(minutes=5), created_by=user_a.user_id, reason="security"))  # fmt: skip
            await bind(s, user_b.user_id, b)
            for user in (user_a, restricted, operator, revoked, inactive):
                await bind(s, user.user_id, a)
            await insert_valid_runtime_rfq(s, a)
            subjects = {
                name: u.authentication_subject
                for name, u in [("a", user_a), ("b", user_b), ("restricted", restricted),
                                ("operator", operator), ("revoked", revoked), ("inactive", inactive)]
            }  # fmt: skip
            held = {"restricted": await grants(s, "manufacturing_engineer"), "operator": await grants(s, "identity_operator")}  # fmt: skip
            direct = await quote_rfq(s, STORED["rfq_id"], customer_id=a, **await pinned(s), horizon_start=date(2026, 11, 9))  # fmt: skip
            material = await get_material_spec(s, "MAT-002")
        yield {"subjects": subjects, "held": held, "direct": direct, "material": material, "b": b}
    finally:
        # Teardown independent of the body: every application table, FK-safe in one
        # statement. seed_database then asserts empty, downgrades and checks user_objects.
        async with seed_database.session() as s, s.begin():
            tables = (await s.scalars(text("SELECT quote_ident(schemaname)||'.'||quote_ident(tablename) FROM pg_tables WHERE schemaname IN ('business','orchestration')"))).all()  # fmt: skip
            if tables:
                await s.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))


async def outcome(client, tool, request, token):
    try:
        return await client.call(tool, request, token=token)
    except McpCallError as error:
        return error.code


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_authenticated_live_end_to_end(trust, live):
    sub = live["subjects"]

    def mcp_token(name, **claims):
        return trust.mint(sub=sub[name], aud=MCP_AUDIENCE, **claims)

    minted = []
    with live_host(trust, RFQ_MCP_HOST_RATE_LIMIT_PER_MINUTE="1000") as (base, secrets, log):
        client = McpToolClient(base, timeout_seconds=20)

        # 1. REST caller -> FastAPI (hardened) -> test provider -> live host -> Manufacturing
        issuer = TestIssuer(trust)
        async with Database(connection_config("test"), read_only=True) as rest_db:
            integration = McpIntegration(client, issuer, MCP_AUDIENCE, REST_AUDIENCE)
            app = create_app(trust.settings(**hardened(trust)), database=rest_db, mcp=integration)
            rest_a = trust.mint(sub=sub["a"])
            ok = await call(app, "GET", SPEC, headers=bearer(rest_a))
            spoofed = await call(app, "GET", SPEC, headers={**bearer(rest_a), "X-RFQ-Customer-Context": live["b"], "X-User-Id": "U-9"})  # fmt: skip
            missing = await call(app, "GET", "/api/v1/materials/NOPE-404/specification", headers=bearer(rest_a))  # fmt: skip
            anonymous = await call(app, "GET", SPEC)
            operator_rest = await call(app, "GET", SPEC, headers=bearer(trust.mint(sub=sub["operator"])))  # fmt: skip
            revoked_rest = await call(app, "GET", SPEC, headers=bearer(trust.mint(sub=sub["revoked"])))  # fmt: skip
        assert ok.status_code == 200, ok.text
        assert ok.json()["density_kg_m3"] == str(live["material"].density_kg_m3)
        assert spoofed.status_code == 200 and spoofed.json() == ok.json()
        assert (missing.status_code, anonymous.status_code) == (404, 401)
        assert (operator_rest.status_code, revoked_rest.status_code) == (403, 401)
        assert issuer.issued and rest_a not in issuer.issued  # the REST token never left
        minted += issuer.issued

        # 2. Direct authenticated client calls: all three servers with one token
        token_a = mcp_token("a")
        token_b = mcp_token("b")
        minted += [token_a, token_b]
        material = await client.call("get_material", {"material_id": "MAT-002"}, token=token_a)
        inventory = await client.call("check_inventory", PLAN, token=token_a)
        lead = await client.call("estimate_lead_time", PLAN, token=token_a)
        price = await client.call("calculate_price", PLAN, token=token_a)
        quote = await client.call("evaluate_quotation", STORED, token=token_a)
        assert isinstance(material, c.MaterialSpecOutput) and inventory.result == "sufficient"
        assert isinstance(lead, c.LeadTimeOutput) and isinstance(price, c.PriceOutput)
        direct = live["direct"]
        assert (quote.result, quote.quote_total_usd, quote.calculation_id) == (
            direct.result,
            direct.quote_total_usd,
            direct.calculation_id,
        )
        assert str(quote.quote_total_usd) == str(direct.quote_total_usd)  # exact Decimal text
        assert [ln.line_number for ln in quote.lines] == [10, 20]

        # 3. Customer isolation, including forged claims and concurrency
        forged = mcp_token("b", customer_id="CUST-A", role="administrator", permissions=["view_quote"], scope="view_quote")  # fmt: skip
        minted.append(forged)
        isolation = await asyncio.gather(
            *[outcome(client, "evaluate_quotation", STORED, t) for t in [token_a, token_b] * 3],
            outcome(client, "evaluate_quotation", {**STORED, "rfq_id": "RFQ-NONE"}, token_b),
            outcome(client, "evaluate_quotation", STORED, forged),
        )
        assert [r if isinstance(r, str) else r.calculation_id for r in isolation] == [
            direct.calculation_id, "not_found"
        ] * 3 + ["not_found", "not_found"]  # fmt: skip

        # 4. Database-derived permissions: restricted role and operator, per server
        for name in ("restricted", "operator"):
            token = mcp_token(
                name, role="administrator", permissions=sorted({t.permission for t in c.TOOLS})
            )
            minted.append(token)
            for tool, request in ONE_TOOL_PER_SERVER.items():
                permission = next(t.permission for t in c.TOOLS if t.name == tool)
                result = await outcome(client, tool, request, token)
                if permission in live["held"][name]:
                    assert not isinstance(result, str), (name, tool, result)
                else:
                    assert result == "forbidden", (name, tool, result)
        assert not live["held"]["operator"] & {t.permission for t in c.TOOLS}
        assert "view_quote" not in live["held"]["restricted"]

        # 5. Invalid credentials never execute a tool, on every server
        now = int(time.time())
        invalid = {
            "revoked": mcp_token("revoked"),
            "inactive": mcp_token("inactive"),
            "rest-audience": trust.mint(sub=sub["a"]),
            "expired": mcp_token("a", iat=now - 600, exp=now - 300),
            "bad-signature": token_a[:-4] + ("AAAA" if not token_a.endswith("AAAA") else "BBBB"),
            "unknown": trust.mint(sub="auth|nobody", aud=MCP_AUDIENCE),
        }
        minted += list(invalid.values())
        for name, token in invalid.items():
            for tool, request in ONE_TOOL_PER_SERVER.items():
                assert await outcome(client, tool, request, token) == "unauthenticated", (name, tool)  # fmt: skip

    output = log[0]
    assert "Traceback" not in output
    assert not any(t in output for t in minted)  # no bearer token logged
    assert "MCP identity resolution failed" not in output  # no fail-closed DB errors
