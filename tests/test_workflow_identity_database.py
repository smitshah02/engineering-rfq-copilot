"""Checkpoint A2: the internal workflow service identity, RBAC v4 and MCP security.

Rolled-back seeded fixture transaction on the guarded test database. Service identities are
provisioned only through ``operator_admin.provision_service`` (operator requests,
administrator approves, one binding in the same transaction). MCP calls go through the real
Day 28 host (in memory, ASGI), the real ``McpTokenVerifier`` (signature, issuer, MCP
audience, lifetime, revocation, binding, v2 grants) and the per-tool permission wrapper;
credentials come from the development ``DevMcpCredentialProvider`` with a throwaway key.
"""

import time
from datetime import date

import httpx2
import jwt
import pytest
from sqlalchemy import delete, func, select, update
from test_auth import AUDIENCE as REST_AUDIENCE
from test_auth import trust as trust
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day20_auth_database import database as database
from test_mcp_auth import BASE, MCP_AUDIENCE, settings
from test_mcp_host import Factories, host_settings, running
from test_operator_assignment_admin_database import admin
from test_runtime_integrity_database import fails, runtime_audit
from test_workflow_queue_database import activate, customer, submit

from rfq_copilot import dev_token, identity_admin, workflow_worker
from rfq_copilot import operator_admin as operators
from rfq_copilot.api.auth import Principal
from rfq_copilot.api.mcp_integration import McpIntegration
from rfq_copilot.mcp import host as h
from rfq_copilot.mcp.client import McpCallError, McpToolClient
from rfq_copilot.mcp.contracts import PlanningInput
from rfq_copilot.models import AuditEvent, RolePermission, User, UserCustomerBinding
from rfq_copilot.quote_drafts import DraftRefused
from rfq_copilot.quote_drafts import _authorize as authorize_quote_operation
from rfq_copilot.repositories import granted_permissions
from rfq_copilot.seeding import SeedError
from rfq_copilot.seeding.policy import policy_rows, register_policy
from rfq_copilot.seeding.rules import register_rule_version

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

SERVICE_GRANTS = {"view_catalog", "view_assigned_rfqs", "view_quote", "create_quote_draft"}
NEVER = {"approve_quote", "reject_quote", "release_quote", "request_approval", "edit_quote_draft", "engineering_review"}  # fmt: skip
MONDAY = date(2026, 11, 16)


async def service(populated, customer_id, operator, user_id="U-SVC-1", **over):
    values = dict(
        new_user=user_id, display_name="RFQ workflow", email=f"{user_id}@service.invalid",
        region="NA", subject=f"svc|rfq-workflow-{user_id}", verification_ref="CHG-501",
        customer=customer_id, approver=await admin(populated), requester=operator, execute=True,
    )  # fmt: skip
    return await operators.provision_service(populated, **{**values, **over})


async def world(populated, seed_plan, trust, database):
    """Customer A (queue active, one RFQ) and customer B, each with its own service identity."""
    await register_rule_version(populated, "approval-v1.2")  # the worker's pinned approval rule
    user, mine, subject, operator = await customer(populated, seed_plan)
    _, theirs, their_subject, _ = await customer(populated, seed_plan, user_id="U-CUST-B", nth=1)
    await activate(populated)
    rfq_a = (await submit(populated, trust, database, subject, "a2-a")).json()["rfq_id"]
    rfq_b = (await submit(populated, trust, database, their_subject, "a2-b")).json()["rfq_id"]
    assert (await service(populated, mine, operator)).result == "service_provisioned"
    assert (await service(populated, theirs, operator, user_id="U-SVC-2")).result == "service_provisioned"  # fmt: skip
    return {"a": mine, "b": theirs, "rfq_a": rfq_a, "rfq_b": rfq_b, "operator": operator, "customer_subject": subject}  # fmt: skip


@pytest.fixture
def signing(tmp_path, trust):
    """A throwaway Ed25519 pair (dev_token.keygen) and MCP settings trusting its public key."""
    private, public = dev_token.keygen(tmp_path / "keys")
    return private, settings(trust, jwt_algorithm="EdDSA", jwt_public_key_file=public)


def provider(signing, **over):
    private, auth = signing
    return dev_token.DevMcpCredentialProvider(auth, private, environment=over.pop("environment", "development"), **over)  # fmt: skip


def stack(populated, signing, credentials=None):
    gateway = h.create_mcp_host(host_settings(rate_limit_per_minute=1000), signing[1], Factories(identity=populated, snapshot=populated))  # fmt: skip
    client = McpToolClient(BASE, transport=httpx2.ASGITransport(app=gateway))
    integration = McpIntegration(client, credentials or provider(signing), MCP_AUDIENCE, REST_AUDIENCE)  # fmt: skip
    return gateway, client, integration


async def principal_of(populated, user_id):
    user = await populated.scalar(select(User).where(User.user_id == user_id))
    binding = await populated.scalar(select(UserCustomerBinding.customer_id).where(UserCustomerBinding.user_id == user_id, UserCustomerBinding.revoked_at.is_(None)))  # fmt: skip
    return Principal(binding or "NONE", await granted_permissions(populated, user), user_id=user_id, subject=user.authentication_subject)  # fmt: skip


def planning(rfq_id):
    return PlanningInput(rfq_id=rfq_id, horizon_start=MONDAY)


# --- provisioning ---------------------------------------------------------------------------


async def test_service_provisioning_is_audited_and_binds_exactly_one_customer(populated, seed_plan, trust, database):  # fmt: skip
    w = await world(populated, seed_plan, trust, database)
    user = await populated.scalar(select(User).where(User.user_id == "U-SVC-1"))
    assert (user.role, user.active, user.authentication_subject) == ("rfq_workflow_agent", True, "svc|rfq-workflow-U-SVC-1")  # fmt: skip
    bindings = (await populated.execute(select(UserCustomerBinding.customer_id, UserCustomerBinding.revoked_at).where(UserCustomerBinding.user_id == "U-SVC-1"))).all()  # fmt: skip
    assert bindings == [(w["a"], None)]
    event = await populated.scalar(select(AuditEvent).where(AuditEvent.operation == "service.provision", AuditEvent.event_data["new_user_id"].astext == "U-SVC-1"))  # fmt: skip
    assert event.actor_id == w["operator"] and event.event_data == {"new_user_id": "U-SVC-1", "approved_by": await admin(populated), "requested_by": w["operator"], "verification_ref": "CHG-501", "customer_id": w["a"]}  # fmt: skip
    assert set(await granted_permissions(populated, user)) == SERVICE_GRANTS


@pytest.mark.parametrize(
    "over,code",
    [
        ({"subject": "auth|human-user"}, "service_subject_required"),
        ({"subject": "svc|"}, "service_subject_required"),
        ({"verification_ref": "none"}, "verification_reference_required"),
        ({"requester": None}, "requester_required"),
        ({"new_user": "U-OP-1"}, "identity_conflict"),
        ({"customer": "CUST-NOPE"}, "customer_not_eligible"),
    ],
)
async def test_service_provisioning_refusals(populated, seed_plan, trust, database, over, code):
    _, mine, _, operator = await customer(populated, seed_plan)
    before = await populated.scalar(select(func.count()).select_from(User))
    if over.get("new_user") == "U-OP-1":
        over = {**over, "requester": operator, "new_user": "U-CUST-Q"}  # an existing account
    async with populated.begin_nested() as nested:
        with pytest.raises(identity_admin.Refused, match=code):
            await service(populated, mine, operator, **over)
        await nested.rollback()
    assert await populated.scalar(select(func.count()).select_from(User)) == before


async def test_service_role_needs_the_v4_overlay(populated, seed_plan):
    _, mine, _, operator = await customer(populated, seed_plan, policy="rbac-policy-v3")
    with pytest.raises(identity_admin.Refused, match="service_role_not_provisioned"):
        await service(populated, mine, operator)


# --- RBAC v4 regression ---------------------------------------------------------------------


async def test_v4_adds_only_the_service_role_and_never_decision_authority():
    v3, v4 = policy_rows("rbac-policy-v3"), policy_rows("rbac-policy-v4")
    added = [g for g in v4["role_permissions"] if g not in v3["role_permissions"]]
    assert all(r in v4[t] for t in v3 for r in v3[t])  # a strict superset: v3 access unchanged
    assert {g["role"] for g in added} == {"rfq_workflow_agent"}
    assert {g["permission"] for g in added} == SERVICE_GRANTS and not SERVICE_GRANTS & NEVER
    customer = {g["permission"] for g in v4["role_permissions"] if g["role"] == "customer"}
    assert customer == {g["permission"] for g in v3["role_permissions"] if g["role"] == "customer"}
    assert not customer & (SERVICE_GRANTS | {"view_assigned_rfqs"})  # customers: no MCP tool


async def test_v4_upgrades_a_complete_v3_and_refuses_a_damaged_one(populated, seed_plan):
    await register_policy(populated, "rbac-policy-v3")
    assert await register_policy(populated, "rbac-policy-v4") == "upgraded"
    assert await register_policy(populated, "rbac-policy-v4") == "already present"
    await populated.execute(delete(RolePermission).where(RolePermission.role == "customer", RolePermission.permission == "customer_view_rfq"))  # fmt: skip
    with pytest.raises(SeedError):
        await register_policy(populated, "rbac-policy-v4")


async def test_service_identity_is_refused_every_decision_operation(populated, seed_plan, trust, database):  # fmt: skip
    await world(populated, seed_plan, trust, database)
    principal = await principal_of(populated, "U-SVC-1")
    assert not principal.permissions & NEVER
    for operation in ("approve_quote", "reject_quote", "release_quote", "request_approval"):
        with pytest.raises(DraftRefused, match="permission_denied"):
            await authorize_quote_operation(populated, principal, operation)


# --- MCP: the service identity through the real host ------------------------------------------


async def test_mcp_host_v2_policy_recognises_the_service_role(populated, seed_plan, trust, database, signing):  # fmt: skip
    w = await world(populated, seed_plan, trust, database)
    gateway, _, integration = stack(populated, signing)
    execute = workflow_worker.mcp_executor(integration, await principal_of(populated, "U-SVC-1"))
    async with running(gateway):
        page = await execute("search_materials", {})
        inventory = await execute("check_inventory", planning(w["rfq_a"]))  # own customer's RFQ
        with pytest.raises(McpCallError) as foreign:
            await execute("check_inventory", planning(w["rfq_b"]))  # bound elsewhere
    assert page.items and inventory is not None
    assert foreign.value.code == "not_found"  # indistinguishable from a missing RFQ


async def test_customer_principal_cannot_use_any_mcp_tool(populated, seed_plan, trust, database, signing):  # fmt: skip
    w = await world(populated, seed_plan, trust, database)
    customer_principal = await principal_of(populated, "U-CUST-Q")
    with pytest.raises(dev_token.DevTokenError):  # never minted for a human subject
        await provider(signing).credential_for(customer_principal)
    gateway, client, _ = stack(populated, signing)
    private, auth = signing
    key = dev_token._private_key(private)
    now = int(time.time())
    token = jwt.encode({"iss": auth.jwt_issuer, "aud": MCP_AUDIENCE, "sub": w["customer_subject"], "iat": now, "exp": now + 120}, key, algorithm="EdDSA")  # fmt: skip
    async with running(gateway):
        for tool, request in (("search_materials", {}), ("check_inventory", planning(w["rfq_a"]))):
            with pytest.raises(McpCallError) as denied:
                await client.call(tool, request, token=token)
            assert denied.value.code == "forbidden"  # v3 customer grants hold no tool permission


def forged(signing, subject, **claims):
    private, auth = signing
    now = int(time.time())
    values = {"iss": auth.jwt_issuer, "aud": MCP_AUDIENCE, "sub": subject, "iat": now, "exp": now + 120, **claims}  # fmt: skip
    return jwt.encode(values, dev_token._private_key(private), algorithm="EdDSA")


@pytest.mark.parametrize(
    "case",
    ["rest_audience", "expired", "other_key", "unknown_subject", "lifetime_too_long"],
)
async def test_mcp_refuses_bad_service_credentials(populated, seed_plan, trust, database, signing, case):  # fmt: skip
    await world(populated, seed_plan, trust, database)
    gateway, client, _ = stack(populated, signing)
    subject, now = "svc|rfq-workflow-U-SVC-1", int(time.time())
    if case == "rest_audience":
        token = forged(signing, subject, aud=REST_AUDIENCE)
    elif case == "expired":
        token = forged(signing, subject, iat=now - 600, exp=now - 300)
    elif case == "other_key":
        other, _ = dev_token.keygen(signing[0].parent.parent / "other")
        token = jwt.encode({"iss": signing[1].jwt_issuer, "aud": MCP_AUDIENCE, "sub": subject, "iat": now, "exp": now + 120}, dev_token._private_key(other), algorithm="EdDSA")  # fmt: skip
    elif case == "unknown_subject":
        token = forged(signing, "svc|rfq-workflow-nobody")
    else:
        token = forged(signing, subject, exp=now + 7200)
    async with running(gateway):
        with pytest.raises(McpCallError) as refused:
            await client.call("search_materials", {}, token=token)
    assert refused.value.code == "unauthenticated"


async def test_integration_never_sends_a_token_for_the_wrong_audience_or_subject(populated, seed_plan, trust, database, signing):  # fmt: skip
    await world(populated, seed_plan, trust, database)

    class Wrong:
        def __init__(self, token):
            self.token = token

        async def credential_for(self, principal):
            return self.token

    principal = await principal_of(populated, "U-SVC-1")
    for token in (forged(signing, principal.subject, aud=REST_AUDIENCE), forged(signing, "svc|rfq-workflow-U-SVC-2")):  # fmt: skip
        _, _, integration = stack(populated, signing, credentials=Wrong(token))
        with pytest.raises(McpCallError) as refused:  # McpIntegration._check, before any request
            await workflow_worker.mcp_executor(integration, principal)("search_materials", {})
        assert refused.value.code == "unauthenticated"


async def test_revoked_service_token_and_revoked_binding_are_refused(populated, seed_plan, trust, database, signing):  # fmt: skip
    w = await world(populated, seed_plan, trust, database)
    gateway, client, _ = stack(populated, signing)
    principal = await principal_of(populated, "U-SVC-1")
    token = await provider(signing).credential_for(principal)
    async with running(gateway):
        assert (await client.call("search_materials", {}, token=token)).items
        revoked = await identity_admin.revoke_tokens(populated, operator=w["operator"], user="U-SVC-1", reason="security", token=token, execute=True)  # fmt: skip
        assert revoked.result == "token_revoked"
        with pytest.raises(McpCallError) as after_revocation:
            await client.call("search_materials", {}, token=token)
        fresh = await provider(signing).credential_for(principal)
        assert (await client.call("search_materials", {}, token=fresh)).items
        await identity_admin.revoke_binding(populated, operator=w["operator"], user="U-SVC-1", customer=w["a"], reason="offboarding", execute=True)  # fmt: skip
        with pytest.raises(McpCallError) as unbound:
            await client.call("search_materials", {}, token=fresh)
    assert (after_revocation.value.code, unbound.value.code) == ("unauthenticated", "unauthenticated")  # fmt: skip


async def test_tool_permissions_are_still_enforced_per_tool(populated, seed_plan, trust, database, signing):  # fmt: skip
    w = await world(populated, seed_plan, trust, database)
    await populated.execute(delete(RolePermission).where(RolePermission.role == "rfq_workflow_agent", RolePermission.permission == "view_quote"))  # fmt: skip
    gateway, _, integration = stack(populated, signing)
    execute = workflow_worker.mcp_executor(integration, await principal_of(populated, "U-SVC-1"))
    async with running(gateway):
        assert (await execute("search_materials", {})).items
        with pytest.raises(McpCallError) as denied:
            await execute("estimate_costs", planning(w["rfq_a"]))
    assert denied.value.code == "forbidden"


async def test_provider_refuses_outside_development_and_unsafe_settings(signing, trust):
    with pytest.raises(dev_token.DevTokenError, match="not development"):
        provider(signing, environment="production")
    with pytest.raises(dev_token.DevTokenError, match="EdDSA"):
        dev_token.DevMcpCredentialProvider(settings(trust), signing[0], environment="development")
    with pytest.raises(dev_token.DevTokenError, match="ttl"):
        provider(signing, ttl=3600)
    assert "PRIVATE" not in repr(provider(signing)) and "key" not in repr(provider(signing)).lower().replace("audience", "")  # fmt: skip


# --- worker revalidation and runtime integrity ----------------------------------------------


class Session:
    """``Database``-shaped wrapper over the rolled-back fixture session (no commits)."""

    def __init__(self, session):
        self._session = session

    def session(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def scope():
            yield _Nested(self._session)

        return scope()


class _Nested:
    def __init__(self, session):
        self._s = session

    def __getattr__(self, name):
        return getattr(self._s, name)

    def begin(self):
        return self._s.begin_nested()


async def test_worker_resolves_only_the_bound_service_identity(populated, seed_plan, trust, database):  # fmt: skip
    from rfq_copilot.models import WorkflowRun

    w = await world(populated, seed_plan, trust, database)
    run = await populated.scalar(select(WorkflowRun).where(WorkflowRun.rfq_id == w["rfq_a"]))
    claim = workflow_worker.Claim(run.workflow_run_id, run.rfq_id, run.customer_id, 1, 1)
    prepared = await workflow_worker.prepare(Session(populated), claim)
    assert (prepared.principal.user_id, prepared.principal.customer_id) == ("U-SVC-1", w["a"])
    assert prepared.principal.subject.startswith("svc|") and not prepared.principal.permissions & NEVER  # fmt: skip
    await identity_admin.revoke_binding(populated, operator=w["operator"], user="U-SVC-1", customer=w["a"], reason="offboarding", execute=True)  # fmt: skip
    with pytest.raises(workflow_worker.Unavailable, match="service_identity_unavailable"):
        await workflow_worker.prepare(Session(populated), claim)
    foreign = workflow_worker.Claim(
        run.workflow_run_id, w["rfq_b"], w["a"], 1, 1
    )  # B's RFQ, A's run
    with pytest.raises(workflow_worker.Unavailable, match="rfq_unavailable"):
        await workflow_worker.prepare(Session(populated), foreign)


async def test_runtime_integrity_accepts_services_and_detects_forgeries(populated, seed_database, seed_plan, trust, database):  # fmt: skip
    await world(populated, seed_plan, trust, database)
    report = await runtime_audit(populated, seed_database, seed_plan)
    assert report["runtime_records"]["services"] == 2 and report["policy_overlay"] == "applied:rbac-policy-v4"  # fmt: skip
    await populated.execute(update(User).where(User.user_id == "U-SVC-1").values(authentication_subject="auth|looks-human"))  # fmt: skip
    await fails(populated, seed_database, seed_plan, "service account without a service subject")


async def test_runtime_integrity_refuses_a_second_service_binding(populated, seed_database, seed_plan, trust, database):  # fmt: skip
    w = await world(populated, seed_plan, trust, database)
    await identity_admin.revoke_binding(populated, operator=w["operator"], user="U-SVC-1", customer=w["a"], reason="correction", execute=True)  # fmt: skip
    granted = await identity_admin.grant_binding(populated, operator=w["operator"], user="U-SVC-1", customer=w["b"], reason="correction", execute=True)  # fmt: skip
    assert granted.result == "granted"
    await fails(
        populated, seed_database, seed_plan, "service binding differs from its provisioning"
    )
