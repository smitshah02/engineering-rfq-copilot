"""Day 27 MCP authentication and authorization (F1-F4).

The real bearer path is exercised over the SDK's Streamable HTTP ASGI app with an
in-memory httpx2 transport (no listener): JWT signed by the test trust key ->
McpTokenVerifier (api.tokens verifier + shared resolve_identity) -> request-scoped
McpPrincipal -> runtime._authorized -> tool. Identities are synthetic changes inside the
seeded, rolled-back test database transaction.
"""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError
from sqlalchemy import func, insert, update
from test_auth import AUDIENCE as REST_AUDIENCE
from test_auth import ISSUER
from test_auth import trust as trust
from test_database_queries import customer_id, other_customer
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day09_inventory import pinned
from test_day14_quotation import insert_valid_runtime_rfq
from test_day20_auth_database import bind
from test_day21_rbac_database import grants, one_active_user_per_role
from test_mcp_manufacturing import ALL_PERMISSIONS, acting_as, text
from test_mcp_supply import FACTORY, PLAN, spy

from rfq_copilot.api.auth import CREATE_RFQ, VIEW_ASSIGNED_RFQS, VIEW_CUSTOMER
from rfq_copilot.api.schemas.reads import MaterialPage, MaterialSummary
from rfq_copilot.mcp import contracts as c
from rfq_copilot.mcp import manufacturing as m
from rfq_copilot.mcp import security
from rfq_copilot.mcp.manufacturing import create_manufacturing_server
from rfq_copilot.mcp.pricing import create_pricing_server
from rfq_copilot.mcp.security import McpAuthSettings, McpTokenVerifier
from rfq_copilot.models import (
    Customer,
    RfqAssignment,
    TokenRevocation,
    User,
    UserCustomerBinding,
)
from rfq_copilot.seeding.policy import register_policy
from rfq_copilot.services.quotation import quote_rfq

BASE = "http://127.0.0.1:8000"  # loopback: the SDK's default Host allowlist
MCP_AUDIENCE = "rfq-copilot-mcp"
ARGS = {
    "search_materials": {},
    "get_material": {"material_id": "MAT-002"},
    "check_material_suitability": {"part": PLAN["parts"][0]},
    "check_manufacturing_feasibility": {"part": PLAN["parts"][0]},
    **{
        name: PLAN
        for name in ("check_inventory", "check_capacity", "estimate_costs", "calculate_price")
    },
    "estimate_lead_time": PLAN,
    "evaluate_quotation": PLAN,
}
UNAUTHENTICATED = "UNAUTHENTICATED: Authentication required."
DENIED = "PERMISSION_DENIED: Permission denied."
NOT_FOUND = "NOT_FOUND: Resource not found or not visible."
PLAN_MONDAY = date(2026, 11, 9)


def settings(trust, **overrides):
    values = {
        "resource_server_url": f"{BASE}/mcp",
        "jwt_issuer": ISSUER,
        "jwt_audience": MCP_AUDIENCE,
        "jwt_algorithm": trust.algorithm,
        "jwt_public_key_file": trust.key_file,
    }
    return McpAuthSettings(**{**values, **overrides})


async def call_tool(name, request, *, snapshot=None):
    snapshot = snapshot or spy()[0]
    async with Client(FACTORY[name](snapshot)) as client:
        return await client.call_tool(name, {"request": request})


# --- central enforcement (in-memory, no database) --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(ARGS))
async def test_every_tool_requires_a_principal_before_any_snapshot(name):
    snapshot, entered = spy()
    with acting_as(permissions=None):
        result = await call_tool(name, ARGS[name], snapshot=snapshot)
    assert text(result) == UNAUTHENTICATED and entered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", c.TOOLS, ids=lambda t: t.name)
async def test_every_tool_enforces_its_declared_v2_permission(contract):
    snapshot, entered = spy()
    with acting_as(permissions=ALL_PERMISSIONS - {contract.permission}):
        result = await call_tool(contract.name, ARGS[contract.name], snapshot=snapshot)
    assert text(result) == DENIED and entered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [t.name for t in c.TOOLS if t.server != "supply"])
async def test_v1_grants_never_substitute_for_v2_permissions(name):
    # v1 routes used view_assigned_rfqs for catalog and quotes; MCP has no v1 table.
    with acting_as(permissions={VIEW_ASSIGNED_RFQS, VIEW_CUSTOMER, CREATE_RFQ}):
        assert text(await call_tool(name, ARGS[name])) == DENIED


def test_permission_mapping_matches_the_v2_policy():
    expected = {
        "manufacturing": "view_catalog",
        "supply": "view_assigned_rfqs",
        "pricing": "view_quote",
    }
    assert all(t.permission == expected[t.server] for t in c.TOOLS)


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"permission_policy": "v1"}, "v1 refused"),
        ({"token_revocation": "off"}, "revocation cannot be disabled"),
        ({"rest_audience": MCP_AUDIENCE}, "REST audience reuse"),
        ({"jwt_public_key_file": Path("relative.pem")}, "relative key path"),
        ({"jwt_algorithm": "HS256"}, "symmetric algorithm"),
    ],
)
def test_mcp_settings_fail_closed(trust, override, why):
    with pytest.raises(ValidationError):
        settings(trust, **override)


def test_settings_from_env_and_rest_audience_check(trust):
    env = {
        "RFQ_MCP_RESOURCE_SERVER_URL": f"{BASE}/mcp",
        "RFQ_MCP_JWT_ISSUER": ISSUER,
        "RFQ_MCP_JWT_AUDIENCE": MCP_AUDIENCE,
        "RFQ_MCP_JWT_ALGORITHM": trust.algorithm,
        "RFQ_MCP_JWT_PUBLIC_KEY_FILE": str(trust.key_file),
        "RFQ_API_JWT_AUDIENCE": REST_AUDIENCE,
    }
    loaded = McpAuthSettings.from_env(env)
    assert (loaded.permission_policy, loaded.token_revocation) == ("v2", "enforced")
    with pytest.raises(ValidationError):
        McpAuthSettings.from_env({**env, "RFQ_MCP_JWT_AUDIENCE": REST_AUDIENCE})


# --- HTTP harness ----------------------------------------------------------------------


@asynccontextmanager
async def http_server(server):
    app = server.streamable_http_app()
    async with server.session_manager.run():
        yield app


@asynccontextmanager
async def http_client(app, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=BASE, headers=headers) as http:
        yield http


async def http_call(app, token, name, request):
    async with http_client(app, token) as http:
        async with Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client:
            return await client.call_tool(name, {"request": request})


@pytest.mark.asyncio
async def test_concurrent_identities_never_leak_between_requests(trust, monkeypatch):
    """Real JWTs, real verifier and SDK auth context; identity rows faked per subject."""

    class Row:
        def __init__(self, subject):
            self.user_id = f"U-{subject}"

    async def resolve(session, credential, token, **kwargs):
        await asyncio.sleep(0.001 * (hash(token.subject) % 5))  # interleave requests
        return Row(token.subject), f"CUST-{token.subject}", frozenset({"view_catalog"})

    async def page(session, query):  # reports the principal the tool body actually saw
        await asyncio.sleep(0.002)
        seen = security_principal()
        return MaterialPage(
            items=(MaterialSummary(material_id=seen, category="x", grade="y"),),
            limit=query.limit,
            offset=0,
            has_more=False,
        )

    @asynccontextmanager
    async def no_session():
        yield None

    monkeypatch.setattr(security, "resolve_identity", resolve)
    monkeypatch.setattr(m, "material_page", page)
    verifier = McpTokenVerifier.from_settings(settings(trust), no_session)
    server = create_manufacturing_server(spy()[0], verifier)
    async with http_server(server) as app:
        subjects = [f"S{i % 4}" for i in range(16)]
        tokens = [trust.mint(sub=s, aud=MCP_AUDIENCE) for s in subjects]
        results = await asyncio.gather(*(http_call(app, t, "search_materials", {}) for t in tokens))
    seen = [r.structured_content["items"][0]["material_id"] for r in results]
    assert seen == [f"CUST-{s}" for s in subjects]


def security_principal():
    from mcp.server.auth.middleware.auth_context import get_access_token

    return get_access_token().customer_id


# --- seeded database: identities, revocation, isolation, assignments -------------------


async def granted_user(session, *, lacking=(), exclude=()):
    """A seeded active user whose role holds every MCP permission except ``lacking``."""
    needed = ALL_PERMISSIONS - set(lacking)
    for user in await one_active_user_per_role(session):
        held = await grants(session, user.role)
        if user.user_id not in exclude and needed <= held and not set(lacking) & held:
            return user
    pytest.fail("no seeded role has that grant combination")


async def world(populated, seed_plan):
    """v2 overlay plus two customers, each with one bound, fully granted user."""
    await register_policy(populated, "rbac-policy-v2")
    a, b = customer_id(seed_plan), other_customer(seed_plan)
    await populated.execute(update(Customer).where(Customer.customer_id.in_([a, b])).values(account_status="active"))  # fmt: skip
    user_a = await granted_user(populated)
    user_b = await granted_user(populated, exclude={user_a.user_id})
    await bind(populated, user_a.user_id, a)
    await bind(populated, user_b.user_id, b)
    return a, b, user_a, user_b


def verifier_for(populated, trust, **overrides):
    @asynccontextmanager
    async def identity():
        yield populated  # an infrastructure read, not the business snapshot

    return McpTokenVerifier.from_settings(settings(trust, **overrides), identity)


def business(populated, entered):
    @asynccontextmanager
    async def snapshot():
        entered.append(1)
        yield populated

    return snapshot


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "missing",
        "bad-signature",
        "expired",
        "not-yet-valid",
        "wrong-issuer",
        "rest-audience",
        "no-subject",
        "unknown-subject",
        "inactive",
        "unbound",
        "revoked",
    ],
)
@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_bearer_token_checks(populated, seed_plan, trust, case):
    a, b, user_a, _ = await world(populated, seed_plan)
    now = int(time.time())
    claims = {"sub": user_a.authentication_subject, "aud": MCP_AUDIENCE}
    token = {
        "valid": lambda: trust.mint(**claims),
        "missing": lambda: None,
        "bad-signature": lambda: trust.mint(key=ec.generate_private_key(ec.SECP256R1()), **claims),
        "expired": lambda: trust.mint(iat=now - 600, exp=now - 300, **claims),
        "not-yet-valid": lambda: trust.mint(nbf=now + 600, **claims),
        "wrong-issuer": lambda: trust.mint(iss="https://evil.invalid/", **claims),
        "rest-audience": lambda: trust.mint(**{**claims, "aud": REST_AUDIENCE}),
        "no-subject": lambda: trust.mint(drop=("sub",), aud=MCP_AUDIENCE),
        "unknown-subject": lambda: trust.mint(sub="auth|nobody", aud=MCP_AUDIENCE),
        "inactive": lambda: trust.mint(**claims),
        "unbound": lambda: trust.mint(**claims),
        "revoked": lambda: trust.mint(**claims),
    }[case]()
    if case == "inactive":
        await populated.execute(update(User).where(User.user_id == user_a.user_id).values(active=False))  # fmt: skip
    if case == "unbound":
        await populated.execute(update(UserCustomerBinding).where(UserCustomerBinding.user_id == user_a.user_id).values(revoked_at=func.now()))  # fmt: skip
    if case == "revoked":
        await populated.execute(insert(TokenRevocation).values(revocation_id=uuid.uuid4(), user_id=user_a.user_id, not_before=func.now() + timedelta(minutes=1), created_by=user_a.user_id, reason="security"))  # fmt: skip
    populated.expunge_all()
    entered = []
    server = create_manufacturing_server(
        business(populated, entered), verifier_for(populated, trust)
    )
    async with http_server(server) as app:
        async with http_client(app, token) as http:
            raw = await http.post("/mcp", json={})
        if case == "valid":
            result = await http_call(app, token, "search_materials", {"limit": 1})
            assert not result.is_error and len(entered) == 1
        else:
            assert raw.status_code == 401 and "resource_metadata" in raw.headers["www-authenticate"]
            assert entered == []


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_stored_rfq_is_customer_scoped_and_matches_quote_rfq(populated, seed_plan, trust):
    a, b, user_a, user_b = await world(populated, seed_plan)
    await insert_valid_runtime_rfq(populated, a)  # TEST-DAY14-RFQ, lines 10 and 20
    request = {"rfq_id": "TEST-DAY14-RFQ", "horizon_start": "2026-11-09"}
    token_a = trust.mint(sub=user_a.authentication_subject, aud=MCP_AUDIENCE)
    token_b = trust.mint(sub=user_b.authentication_subject, aud=MCP_AUDIENCE)
    server = create_pricing_server(business(populated, []), verifier_for(populated, trust))
    async with http_server(server) as app:
        own = await http_call(app, token_a, "evaluate_quotation", request)
        foreign = await http_call(app, token_b, "evaluate_quotation", request)
        missing = await http_call(
            app, token_b, "evaluate_quotation", {**request, "rfq_id": "RFQ-NONE"}
        )
    assert not own.is_error, text(own)
    direct = await quote_rfq(
        populated,
        "TEST-DAY14-RFQ",
        customer_id=a,
        **await pinned(populated),
        horizon_start=PLAN_MONDAY,
    )
    out = c.QuotationOutput.model_validate(own.structured_content)
    assert (out.result, out.quote_total_usd, out.calculation_id) == (
        direct.result,
        direct.quote_total_usd,
        direct.calculation_id,
    )
    assert [ln.line_number for ln in out.lines] == [10, 20] == list(direct.line_numbers)
    assert text(foreign) == text(missing) == NOT_FOUND
    assert foreign.model_dump(mode="json")["content"] == missing.model_dump(mode="json")["content"]


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_assignment_enforcement_for_stored_rfqs(populated, seed_plan, trust):
    a, _, user_a, user_b = await world(populated, seed_plan)
    await insert_valid_runtime_rfq(populated, a)
    mine = "TEST-DAY14-RFQ"
    token = trust.mint(sub=user_a.authentication_subject, aud=MCP_AUDIENCE)
    request = {"rfq_id": mine, "horizon_start": "2026-11-09"}
    server = create_pricing_server(
        business(populated, []), verifier_for(populated, trust, rfq_assignments="required")
    )
    async with http_server(server) as app:
        unassigned = await http_call(app, token, "estimate_costs", request)
        await populated.execute(insert(RfqAssignment).values(rfq_id=mine, user_id=user_a.user_id, assigned_by=user_b.user_id))  # fmt: skip
        assigned = await http_call(app, token, "estimate_costs", request)
    assert text(unassigned) == NOT_FOUND
    assert not assigned.is_error and assigned.structured_content["result"] == "costed"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_database_roles_decide_and_operators_get_no_business_tools(
    populated, seed_plan, trust
):
    a, _, user_a, _ = await world(populated, seed_plan)
    token = trust.mint(sub=user_a.authentication_subject, aud=MCP_AUDIENCE)
    verifier = verifier_for(populated, trust)
    for role in ("manufacturing_engineer", "identity_operator"):
        await populated.execute(update(User).where(User.user_id == user_a.user_id).values(role=role))  # fmt: skip
        populated.expunge_all()
        held = await grants(populated, role)
        if role == "identity_operator":
            assert not held & ALL_PERMISSIONS  # identity administration only
        else:
            assert "view_catalog" in held and "view_quote" not in held
        for contract in c.TOOLS:
            server = FACTORY[contract.name](business(populated, []), verifier)
            async with http_server(server) as app:
                result = await http_call(app, token, contract.name, ARGS[contract.name])
            if contract.permission in held:
                assert not result.is_error, (role, contract.name, text(result))
            else:
                assert text(result) == DENIED, (role, contract.name)
