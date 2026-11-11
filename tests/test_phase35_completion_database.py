"""Phase 3.5 features against the guarded PostgreSQL test database at head 20261111_06.

The seeded fixture database already has the activated schema (02, 03, 04, 06). The
pinned rbac-policy-v2 overlay and the deferred 20261111_05 are applied inside the
seeded fixture transaction and rolled back with it; downgrades are exercised the same
way with Alembic's Operations API. Real ES256 tokens go through the real get_principal.
Expectations come from database grants.
"""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.util import CommandError
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_attachments import pdf
from test_auth import bearer
from test_auth import trust as trust
from test_database_queries import customer_id, other_customer, statements
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day20_auth_database import app_with, bind, call
from test_day20_auth_database import database as database
from test_day21_rbac_database import bound, grants, one_active_user_per_role, own_rfq, release_one
from test_phase35_completion import V2

from rfq_copilot import identity_admin
from rfq_copilot.api.auth import ROUTE_PERMISSIONS
from rfq_copilot.models import (
    RFQ,
    AuditEvent,
    Customer,
    IdempotencyRecord,
    RfqAssignment,
    RFQAttachment,
    RolePermission,
    TokenRevocation,
    User,
    UserCustomerBinding,
)
from rfq_copilot.reconcile_attachments import missing_attachment_ids

rfq_assignments, token_revocations = RfqAssignment.__table__, TokenRevocation.__table__

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
DEFERRED = Path(__file__).resolve().parents[1] / "alembic" / "proposed" / "deferred"
PHASE35 = ["20261111_02", "20261111_03", "20261111_04", "20261111_06"]
REFERENCE_V2_GRANTS = {  # derived from the dataset's reference tool policy (rbac-policy-v2)
    ("sales_representative", "view_catalog"),
    ("estimator", "view_catalog"),
    ("manufacturing_engineer", "view_catalog"),
    ("estimator", "view_quote"),
    ("sales_representative", "view_quote"),
    ("sales_manager", "view_quote"),
    ("auditor", "view_quote"),
    ("sales_representative", "attach_rfq_document"),
    ("estimator", "attach_rfq_document"),
}


OPERATOR_GRANTS = {
    ("identity_operator", "manage_customer_bindings"),
    ("identity_operator", "revoke_tokens"),
}


def migration(revision):
    (path,) = [*VERSIONS.glob(f"{revision}_*.py"), *DEFERRED.glob(f"{revision}_*.py")]
    spec = importlib.util.spec_from_file_location(f"proposed_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def migrate(populated, *revisions, direction="upgrade"):
    def run(sync_session):
        context = MigrationContext.configure(sync_session.connection())
        with Operations.context(context):
            for revision in revisions:
                getattr(migration(revision), direction)()

    await populated.run_sync(run)


async def policy_v2(populated):
    """The pinned rbac-policy-v2 overlay, in the fixture transaction (rolled back)."""
    from rfq_copilot.seeding.policy import register_policy

    assert await register_policy(populated, "rbac-policy-v2") == "applied"


# --- Migrations: chain, upgrade, downgrade ----------------------------------------------


async def test_active_chain_ends_at_06_and_05_stays_deferred():
    previous = "20261110_01"
    for revision in PHASE35:
        module = migration(revision)
        assert (module.revision, module.down_revision) == (revision, previous)
        assert list(VERSIONS.glob(f"{revision}_*.py"))  # activated
        previous = revision
    deferred = migration("20261111_05")
    assert deferred.down_revision == "20261111_06"
    assert not list(VERSIONS.glob("20261111_05_*")) and list(DEFERRED.glob("20261111_05_*"))
    assert not list((DEFERRED.parent / "approved").glob("*.py"))


async def test_phase35_revisions_downgrade_and_reupgrade_cleanly(populated):
    tables = "SELECT tablename FROM pg_tables WHERE schemaname='business'"
    assert {"rfq_assignments", "token_revocations"} <= set((await populated.scalars(text(tables))).all())  # fmt: skip
    await migrate(populated, *reversed(PHASE35), direction="downgrade")
    assert not {"rfq_assignments", "token_revocations"} & set((await populated.scalars(text(tables))).all())  # fmt: skip
    await migrate(populated, *PHASE35)
    assert {"rfq_assignments", "token_revocations"} <= set((await populated.scalars(text(tables))).all())  # fmt: skip
    await migrate(populated, "20261111_05")
    await migrate(populated, "20261111_05", direction="downgrade")  # deferred: clean both ways


async def test_downgrades_refuse_to_drop_provisioned_records(populated, seed_plan):
    await migrate(populated, "20261111_05")
    users = await one_active_user_per_role(populated)
    mine = customer_id(seed_plan)
    rfq = await own_rfq(populated, mine)
    await populated.execute(insert(rfq_assignments).values(rfq_id=rfq, user_id=users[0].user_id, assigned_by=users[1].user_id))  # fmt: skip
    await populated.execute(insert(token_revocations).values(revocation_id=__import__("uuid").uuid4(), user_id=users[0].user_id, not_before=func.now(), created_by=users[1].user_id, reason="security"))  # fmt: skip
    await bind(populated, users[2].user_id, mine)
    await bind(populated, users[2].user_id, other_customer(seed_plan))  # several (05 applied)
    for revision in ("20261111_05", "20261111_03", "20261111_02"):
        savepoint = await populated.begin_nested()
        with pytest.raises(CommandError):
            await migrate(populated, revision, direction="downgrade")
        await savepoint.rollback()


async def test_token_revocation_constraints(populated):
    users = await one_active_user_per_role(populated)
    base = {"user_id": users[0].user_id, "created_by": users[1].user_id, "reason": "security"}
    for values in ({}, {"token_sha256": "a" * 64, "not_before": func.now()}, {"token_sha256": "NOT-HEX"}):  # fmt: skip
        savepoint = await populated.begin_nested()
        with pytest.raises(IntegrityError):
            await populated.execute(insert(token_revocations).values(revocation_id=__import__("uuid").uuid4(), **base, **values))  # fmt: skip
        await savepoint.rollback()


# --- Decision A: exact v2 grants and the per-role route matrix ------------------------------


async def test_policy_v2_grants_exactly_the_reference_derived_set(populated):
    before = set((await populated.execute(select(RolePermission.role, RolePermission.permission))).all())  # fmt: skip
    await policy_v2(populated)
    after = set((await populated.execute(select(RolePermission.role, RolePermission.permission))).all())  # fmt: skip
    assert after - before == REFERENCE_V2_GRANTS | OPERATOR_GRANTS  # nothing else
    assert before <= after  # no existing grant removed or changed
    holders = set((await populated.scalars(select(RolePermission.role).where(RolePermission.permission.in_(["manage_customer_bindings", "revoke_tokens"])))).all())  # fmt: skip
    assert holders == {"identity_operator"}  # never administrators or any other role
    operators = await populated.scalar(select(func.count()).select_from(User).where(User.role == "identity_operator"))  # fmt: skip
    assert operators == 0  # the overlay provisions no operator account


async def test_every_role_gets_exactly_the_v2_routes_its_grants_allow(trust, database, populated, tmp_path):  # fmt: skip
    await policy_v2(populated)
    released, mine = await release_one(populated)
    rfq = await own_rfq(populated, mine)
    app = app_with(trust, database, attachment_dir=tmp_path / "store", permission_policy="v2")
    roles = await one_active_user_per_role(populated)
    assert len(roles) == 6
    for user in roles:
        await bound(populated, user, mine)
        held = await grants(populated, user.role)
        auth = bearer(trust.mint(sub=user.authentication_subject))
        for (method, template), required in V2.items():
            path = template.replace("{rfq_id}", rfq).replace("{quote_id}", released)
            kwargs = {"files": {"file": ("s.pdf", pdf("S"), "application/pdf")}} if method == "POST" else {}  # fmt: skip
            response = await call(app, method, path, headers=auth, **kwargs)
            assert (response.status_code in (200, 201)) == (required in held), (user.role, template, response.status_code)  # fmt: skip
            assert response.status_code in (200, 201, 403)


async def test_v1_v2_differences_are_exactly_the_documented_ones(populated):
    await policy_v2(populated)
    roles = [u.role for u in await one_active_user_per_role(populated)]
    differences = {}
    for role in roles:
        held = await grants(populated, role)
        for operation in ROUTE_PERMISSIONS["v1"]:
            v1 = ROUTE_PERMISSIONS["v1"][operation] in held
            v2 = ROUTE_PERMISSIONS["v2"][operation] in held
            if v1 != v2:
                differences[(role, operation)] = "gains" if v2 else "loses"
    assert differences == {
        ("sales_manager", "catalog_read"): "loses",
        ("manufacturing_engineer", "quote_read"): "loses",
        ("auditor", "quote_read"): "gains",
    }


async def test_v2_without_the_overlay_fails_closed(trust, database, populated, seed_plan):
    user = (await one_active_user_per_role(populated))[0]
    await bound(populated, user, customer_id(seed_plan))
    app = app_with(trust, database, permission_policy="v2")
    auth = bearer(trust.mint(sub=user.authentication_subject))
    assert (await call(app, "GET", "/api/v1/materials", headers=auth)).status_code == 403


# --- Decision B: per-RFQ assignments ------------------------------------------------------


async def test_assignments_narrow_customer_scope_and_never_widen_it(trust, database, populated, seed_plan, tmp_path, seed_database):  # fmt: skip
    await policy_v2(populated)
    roles = await one_active_user_per_role(populated)
    reader, operator = roles[0], roles[1]
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    await bound(populated, reader, mine)
    own = (await populated.scalars(select(RFQ.rfq_id).where(RFQ.customer_id == mine).order_by(RFQ.rfq_id).limit(2))).all()  # fmt: skip
    foreign = await own_rfq(populated, theirs)
    app = app_with(trust, database, attachment_dir=tmp_path / "store", rfq_assignments="required")
    auth = bearer(trust.mint(sub=reader.authentication_subject))

    async def assign(rfq):
        await populated.execute(insert(rfq_assignments).values(rfq_id=rfq, user_id=reader.user_id, assigned_by=operator.user_id))  # fmt: skip

    assert (await call(app, "GET", f"/api/v1/rfqs/{own[0]}", headers=auth)).status_code == 404
    assert (await call(app, "GET", "/api/v1/rfqs", headers=auth)).json()["items"] == []
    await assign(own[0])
    await assign(foreign)  # an assignment to another customer's RFQ grants nothing
    with statements(seed_database) as captured:
        assert (await call(app, "GET", f"/api/v1/rfqs/{own[0]}", headers=auth)).status_code == 200
    assert any("business.rfq_assignments" in s for s in captured)  # enforced in SQL
    listing = (await call(app, "GET", "/api/v1/rfqs", headers=auth)).json()["items"]
    assert [item["rfq_id"] for item in listing] == [own[0]]
    for rfq in (own[1], foreign):
        assert (await call(app, "GET", f"/api/v1/rfqs/{rfq}", headers=auth)).status_code == 404
        assert (await call(app, "GET", f"/api/v1/rfqs/{rfq}/attachments", headers=auth)).status_code == 404  # fmt: skip
    upload = await call(app, "POST", f"/api/v1/rfqs/{own[1]}/attachments", headers=auth,
                        files={"file": ("s.pdf", pdf("S"), "application/pdf")})  # fmt: skip
    assert upload.status_code == 404 and not (tmp_path / "store").exists() or list((tmp_path / "store").iterdir()) == []  # fmt: skip
    await populated.execute(update(rfq_assignments).values(revoked_at=func.now()))
    assert (await call(app, "GET", f"/api/v1/rfqs/{own[0]}", headers=auth)).status_code == 404


async def test_assignments_required_without_the_table_fail_closed(trust, database, populated, seed_plan):  # fmt: skip
    user = (await one_active_user_per_role(populated))[0]
    await bound(populated, user, customer_id(seed_plan))
    await populated.execute(text("DROP TABLE business.rfq_assignments"))  # rolled back
    app = app_with(trust, database, rfq_assignments="required")
    response = await call(app, "GET", "/api/v1/rfqs", headers=bearer(trust.mint(sub=user.authentication_subject)))  # fmt: skip
    assert response.status_code in (500, 503)  # the test wrapper surfaces the missing relation
    assert "rfq_assignments" not in response.text


# --- Decision B: multi-customer staff with explicit context ---------------------------------


async def test_multi_customer_user_must_name_a_bound_customer(trust, database, populated, seed_plan):  # fmt: skip
    await migrate(populated, "20261111_05")  # deferred revision, this transaction only
    user = (await one_active_user_per_role(populated))[0]
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    await populated.execute(update(Customer).where(Customer.customer_id.in_([mine, theirs])).values(account_status="active"))  # fmt: skip
    await bind(populated, user.user_id, mine)
    await bind(populated, user.user_id, theirs)
    auth = bearer(trust.mint(sub=user.authentication_subject))
    single = app_with(trust, database)
    assert (await call(single, "GET", "/api/v1/customers/me", headers=auth)).status_code == 403
    selectable = app_with(trust, database, customer_context="selectable")
    assert (await call(selectable, "GET", "/api/v1/customers/me", headers=auth)).status_code == 403
    for customer in (mine, theirs):
        response = await call(selectable, "GET", "/api/v1/customers/me", headers={**auth, "X-RFQ-Customer-Context": customer})  # fmt: skip
        assert response.json()["customer_id"] == customer
    stranger = await populated.scalar(select(Customer.customer_id).where(Customer.customer_id.not_in([mine, theirs])).limit(1))  # fmt: skip
    response = await call(selectable, "GET", "/api/v1/customers/me", headers={**auth, "X-RFQ-Customer-Context": stranger})  # fmt: skip
    assert response.status_code == 403
    await populated.execute(update(UserCustomerBinding).where(UserCustomerBinding.customer_id == theirs).values(revoked_at=func.now()))  # fmt: skip
    response = await call(selectable, "GET", "/api/v1/customers/me", headers={**auth, "X-RFQ-Customer-Context": theirs})  # fmt: skip
    assert response.status_code == 403  # a revoked binding is not selectable


# --- TD-024: token revocation ---------------------------------------------------------------


async def operator_with(populated, permission, *, exclude):
    """A separately provisioned human operator account (test transaction only)."""
    held = await populated.scalar(select(func.count()).select_from(RolePermission).where(RolePermission.role == "identity_operator", RolePermission.permission == permission))  # fmt: skip
    assert held == 1  # granted by the overlay, to the dedicated role only
    await populated.execute(
        insert(User).values(
            user_id="U-OPERATOR-TEST", display_name="Test Operator", role="identity_operator",
            email="operator@test.invalid", active=True, assigned_region="NA",
            authentication_subject="auth|operator-test",
        )
    )  # fmt: skip
    assert exclude != "U-OPERATOR-TEST"
    return await populated.scalar(select(User).where(User.user_id == "U-OPERATOR-TEST"))


async def test_revoking_one_token_or_all_earlier_tokens(trust, database, populated, seed_plan):
    import time

    await policy_v2(populated)
    user = (await one_active_user_per_role(populated))[0]
    await bound(populated, user, customer_id(seed_plan))
    operator = await operator_with(populated, "revoke_tokens", exclude=user.user_id)
    app = app_with(trust, database, token_revocation="enforced")
    first, second = trust.mint(sub=user.authentication_subject, jti="a"), trust.mint(sub=user.authentication_subject, jti="b")  # fmt: skip
    for token in (first, second):
        assert (
            await call(app, "GET", "/api/v1/customers/me", headers=bearer(token))
        ).status_code == 200
    outcome = await identity_admin.revoke_tokens(populated, operator=operator.user_id, user=user.user_id, reason="security", token=first, execute=True)  # fmt: skip
    assert outcome.result == "token_revoked"
    assert (
        await call(app, "GET", "/api/v1/customers/me", headers=bearer(first))
    ).status_code == 401
    assert (
        await call(app, "GET", "/api/v1/customers/me", headers=bearer(second))
    ).status_code == 200
    stored = (await populated.scalars(select(token_revocations.c.token_sha256))).all()
    assert first not in str(stored) and len(stored[0]) == 64  # digest only
    await identity_admin.revoke_tokens(populated, operator=operator.user_id, user=user.user_id, reason="offboarding", token=None, execute=True)  # fmt: skip
    assert (
        await call(app, "GET", "/api/v1/customers/me", headers=bearer(second))
    ).status_code == 401
    now = await populated.scalar(select(func.clock_timestamp()))
    later = trust.mint(sub=user.authentication_subject, iat=int(now.timestamp()) + 5, exp=int(time.time()) + 300)  # fmt: skip
    assert (
        await call(app, "GET", "/api/v1/customers/me", headers=bearer(later))
    ).status_code == 200
    audited = (await populated.scalars(select(AuditEvent.operation).where(AuditEvent.actor_id == operator.user_id))).all()  # fmt: skip
    assert audited.count("token.revoke") == 2


async def test_revocation_enforced_without_the_table_fails_closed(trust, database, populated, seed_plan):  # fmt: skip
    user = (await one_active_user_per_role(populated))[0]
    await bound(populated, user, customer_id(seed_plan))
    await populated.execute(text("DROP TABLE business.token_revocations"))  # rolled back
    app = app_with(trust, database, token_revocation="enforced")
    response = await call(app, "GET", "/api/v1/customers/me", headers=bearer(trust.mint(sub=user.authentication_subject)))  # fmt: skip
    assert response.status_code == 503


# --- Decision C: operator binding provisioning ---------------------------------------------


async def test_binding_provisioning_authority_conflicts_repeats_and_audit(populated, seed_plan):
    await policy_v2(populated)
    target = (await one_active_user_per_role(populated))[0]
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    await populated.execute(update(Customer).where(Customer.customer_id.in_([mine, theirs])).values(account_status="active"))  # fmt: skip
    candidate = await populated.scalar(select(User).where(User.role == "administrator", User.active.is_(True)).limit(1))  # fmt: skip
    call_args = {"operator": candidate.user_id, "user": target.user_id, "reason": "onboarding", "execute": True}  # fmt: skip
    with pytest.raises(identity_admin.Refused) as refused:  # administrators are not operators
        await identity_admin.grant_binding(populated, customer=mine, **call_args)
    assert refused.value.code == "operator_not_authorized"
    operator = await operator_with(populated, "manage_customer_bindings", exclude=target.user_id)
    call_args["operator"] = operator.user_id
    with pytest.raises(identity_admin.Refused) as refused:
        await identity_admin.grant_binding(populated, customer=mine, **{**call_args, "user": operator.user_id})  # fmt: skip
    assert refused.value.code == "self_administration_refused"
    assert (
        await identity_admin.grant_binding(populated, customer=mine, **call_args)
    ).result == "granted"
    assert (
        await identity_admin.grant_binding(populated, customer=mine, **call_args)
    ).result == "already_bound"
    with pytest.raises(identity_admin.Refused) as refused:
        await identity_admin.grant_binding(populated, customer=theirs, **call_args)
    assert refused.value.code == "conflicting_active_binding"
    assert (
        await identity_admin.revoke_binding(populated, customer=mine, **call_args)
    ).result == "revoked"
    assert (
        await identity_admin.revoke_binding(populated, customer=mine, **call_args)
    ).result == "already_revoked"
    assert (
        await identity_admin.grant_binding(populated, customer=mine, **call_args)
    ).result == "reactivated"
    await populated.execute(update(Customer).where(Customer.customer_id == theirs).values(account_status="inactive"))  # fmt: skip
    await identity_admin.revoke_binding(populated, customer=mine, **call_args)
    with pytest.raises(identity_admin.Refused) as refused:
        await identity_admin.grant_binding(populated, customer=theirs, **call_args)
    assert refused.value.code == "customer_not_eligible"
    await populated.execute(update(User).where(User.user_id == target.user_id).values(active=False))
    with pytest.raises(identity_admin.Refused) as refused:
        await identity_admin.grant_binding(populated, customer=mine, **call_args)
    assert refused.value.code == "user_not_active"
    events = (await populated.execute(select(AuditEvent.operation, AuditEvent.outcome, AuditEvent.event_data).where(AuditEvent.actor_id == operator.user_id).order_by(AuditEvent.occurred_at))).all()  # fmt: skip
    assert [e.operation for e in events].count("customer_binding.grant") == 2
    assert [e.operation for e in events].count("customer_binding.revoke") == 2
    assert all(e.outcome == "success" and e.event_data["target_user_id"] == target.user_id for e in events)  # fmt: skip


async def test_refused_execution_is_audited_separately(populated, database):
    await policy_v2(populated)
    target = (await one_active_user_per_role(populated))[0]
    args = type(
        "Args", (), {"operator": "U-NOT-A-USER", "user": target.user_id, "command": "grant"}
    )()
    await identity_admin._audit_denial(database, args, "operator_not_authorized")
    denied = (await populated.execute(select(AuditEvent.actor_id, AuditEvent.outcome, AuditEvent.event_data).where(AuditEvent.operation == "customer_binding.grant"))).all()  # fmt: skip
    assert denied == [(None, "denied", {"target_user_id": target.user_id, "refusal": "operator_not_authorized"})]  # fmt: skip


async def test_concurrent_style_grants_cannot_create_two_active_bindings(populated, seed_plan):
    # The partial unique index is the backstop if two operators pass the checks at once.
    target = (await one_active_user_per_role(populated))[0]
    await bind(populated, target.user_id, customer_id(seed_plan))
    savepoint = await populated.begin_nested()
    with pytest.raises(IntegrityError):
        await bind(populated, target.user_id, other_customer(seed_plan))
    await savepoint.rollback()


# --- Upload idempotency ---------------------------------------------------------------------


async def uploader(trust, database, populated, seed_plan, tmp_path):
    await policy_v2(populated)
    user = (await one_active_user_per_role(populated))[0]  # sales_representative: create_rfq
    mine = customer_id(seed_plan)
    await bound(populated, user, mine)
    app = app_with(trust, database, attachment_dir=tmp_path / "store", upload_idempotency="enabled")
    return app, bearer(trust.mint(sub=user.authentication_subject)), await own_rfq(populated, mine)


def files(text_="Spec"):
    return {"file": ("spec.pdf", pdf(text_), "application/pdf")}


async def test_retry_with_the_same_key_replays_without_a_duplicate(trust, database, populated, seed_plan, tmp_path):  # fmt: skip
    app, auth, rfq = await uploader(trust, database, populated, seed_plan, tmp_path)
    keyed = {**auth, "Idempotency-Key": "retry-1"}
    first = await call(app, "POST", f"/api/v1/rfqs/{rfq}/attachments", headers=keyed, files=files())
    again = await call(app, "POST", f"/api/v1/rfqs/{rfq}/attachments", headers=keyed, files=files())
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() and again.headers["idempotent-replay"] == "true"
    assert len(list((tmp_path / "store").iterdir())) == 1
    assert await populated.scalar(select(func.count()).select_from(RFQAttachment).where(RFQAttachment.rfq_id == rfq)) >= 1  # fmt: skip
    rows = await populated.scalar(select(func.count()).select_from(RFQAttachment).where(RFQAttachment.attachment_id == first.json()["attachment_id"]))  # fmt: skip
    assert rows == 1
    reused = await call(app, "POST", f"/api/v1/rfqs/{rfq}/attachments", headers=keyed, files=files("Other"))  # fmt: skip
    assert reused.status_code == 409 and reused.json()["reason"] == "idempotency_key_conflict"
    assert len(list((tmp_path / "store").iterdir())) == 1


async def test_retry_after_a_lost_commit_acknowledgement_replays(trust, database, populated, seed_plan, tmp_path, monkeypatch):  # fmt: skip
    app, auth, rfq = await uploader(trust, database, populated, seed_plan, tmp_path)
    keyed = {**auth, "Idempotency-Key": "ack-lost"}
    real_commit = AsyncSession.commit

    async def committed_then_lost(self):
        await real_commit(self)
        raise ConnectionResetError("acknowledgement lost")

    monkeypatch.setattr(AsyncSession, "commit", committed_then_lost)
    lost = await call(app, "POST", f"/api/v1/rfqs/{rfq}/attachments", headers=keyed, files=files())
    monkeypatch.undo()
    assert lost.status_code == 503
    retry = await call(app, "POST", f"/api/v1/rfqs/{rfq}/attachments", headers=keyed, files=files())
    assert retry.status_code == 201 and retry.headers["idempotent-replay"] == "true"
    receipts = (await populated.scalars(select(IdempotencyRecord.result_json))).all()
    assert len(receipts) == 1 and retry.json() == receipts[0]
    assert len(list((tmp_path / "store").iterdir())) == 1  # kept file, no second upload


async def test_racing_retry_loses_cleanly_and_replays_the_winner(trust, database, populated, seed_plan, tmp_path, monkeypatch):  # fmt: skip
    import rfq_copilot.repositories.idempotency as idempotency

    app, auth, rfq = await uploader(trust, database, populated, seed_plan, tmp_path)
    keyed = {**auth, "Idempotency-Key": "race"}
    winner = await call(
        app, "POST", f"/api/v1/rfqs/{rfq}/attachments", headers=keyed, files=files()
    )
    real_find = idempotency.find_receipt
    seen = []

    async def not_yet_visible(session, **kwargs):  # the loser's first lookup ran before commit
        seen.append(1)
        return None if len(seen) == 1 else await real_find(session, **kwargs)

    monkeypatch.setattr(idempotency, "find_receipt", not_yet_visible)
    loser = await call(app, "POST", f"/api/v1/rfqs/{rfq}/attachments", headers=keyed, files=files())
    assert loser.status_code == 201 and loser.json() == winner.json()
    assert len(list((tmp_path / "store").iterdir())) == 1  # the loser's file was removed
    assert await populated.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1


async def test_keys_are_scoped_per_customer(trust, database, populated, seed_plan, tmp_path):
    app, auth, rfq = await uploader(trust, database, populated, seed_plan, tmp_path)
    users = await one_active_user_per_role(populated)
    other = await populated.scalar(select(User).where(User.role == users[0].role, User.user_id != users[0].user_id, User.active.is_(True)).limit(1))  # fmt: skip
    theirs = other_customer(seed_plan)
    await bound(populated, other, theirs)
    their_rfq = await own_rfq(populated, theirs)
    one = await call(app, "POST", f"/api/v1/rfqs/{rfq}/attachments", headers={**auth, "Idempotency-Key": "shared"}, files=files())  # fmt: skip
    two = await call(app, "POST", f"/api/v1/rfqs/{their_rfq}/attachments", headers={**bearer(trust.mint(sub=other.authentication_subject)), "Idempotency-Key": "shared"}, files=files())  # fmt: skip
    assert one.status_code == two.status_code == 201
    assert (
        one.json()["attachment_id"] != two.json()["attachment_id"]
    )  # never another customer's replay


# --- Missing-file metadata ------------------------------------------------------------------


async def test_rows_whose_file_vanished_are_listed_never_changed(trust, database, populated, seed_plan, tmp_path):  # fmt: skip
    user = (await one_active_user_per_role(populated))[0]
    mine = customer_id(seed_plan)
    await bound(populated, user, mine)
    store = tmp_path / "store"
    app = app_with(trust, database, attachment_dir=store)
    response = await call(app, "POST", f"/api/v1/rfqs/{await own_rfq(populated, mine)}/attachments",
                          headers=bearer(trust.mint(sub=user.authentication_subject)), files=files())  # fmt: skip
    attachment_id = response.json()["attachment_id"]
    (store / next(iter(p.name for p in store.iterdir()))).unlink()  # lost on disk
    refs = (await populated.scalars(select(RFQAttachment.storage_ref))).all()
    assert missing_attachment_ids(store, refs) == (attachment_id,)
    assert await populated.scalar(select(func.count()).select_from(RFQAttachment).where(RFQAttachment.attachment_id == attachment_id)) == 1  # fmt: skip
