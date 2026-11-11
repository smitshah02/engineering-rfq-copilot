"""Operator provisioning and RFQ assignment administration (guarded PostgreSQL test DB).

Everything runs in the seeded fixture transaction (head 20261111_06) with the pinned
overlay applied there, and is rolled back. No real account, binding or assignment is
created anywhere else.
"""

import asyncio

import pytest
from sqlalchemy import func, select, update
from test_auth import bearer
from test_auth import trust as trust
from test_database_queries import customer_id, other_customer
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day20_auth_database import app_with, call
from test_day20_auth_database import database as database
from test_day21_rbac_database import bound, own_rfq, release_one

from rfq_copilot import assignment_admin as assignments
from rfq_copilot import operator_admin as operators
from rfq_copilot.identity_admin import Refused
from rfq_copilot.models import RFQ, AuditEvent, Quote, RfqAssignment, User
from rfq_copilot.seeding.policy import register_policy

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


async def admin(populated, nth=0):
    return (await populated.scalars(select(User.user_id).where(User.role == "administrator", User.active.is_(True)).order_by(User.user_id))).all()[nth]  # fmt: skip


def profile(user_id, subject, email):
    return dict(new_user=user_id, display_name="Operator", email=email, region="NA", subject=subject,
                verification_ref="SEC-101")  # fmt: skip


async def first_operator(populated, user_id="U-OP-1"):
    approver = await admin(populated)
    outcome = await operators.provision_operator(
        populated, **profile(user_id, f"auth|{user_id}", f"{user_id}@test.invalid"),
        approver=approver, requester=None, bootstrap=True, execute=True,
    )  # fmt: skip
    assert outcome.result == "operator_provisioned"
    return user_id


# --- Task 1: human operator provisioning -----------------------------------------------------


async def test_operator_provisioning_needs_the_overlay(populated):
    with pytest.raises(Refused) as refused:
        await operators.provision_operator(populated, **profile("U-OP-1", "auth|op1", "op1@x.invalid"),
                                           approver=await admin(populated), requester=None, bootstrap=True, execute=True)  # fmt: skip
    assert refused.value.code == "operator_role_not_provisioned"


async def test_bootstrap_then_two_person_rule(populated):
    await register_policy(populated, "rbac-policy-v2")
    first = await first_operator(populated)
    created = await populated.scalar(select(User).where(User.user_id == first))
    assert (created.role, created.active) == ("identity_operator", True)
    approver, other_admin = await admin(populated), await admin(populated, 1)
    with pytest.raises(Refused) as refused:  # bootstrap only while no operator exists
        await operators.provision_operator(populated, **profile("U-OP-2", "auth|op2", "op2@x.invalid"),
                                           approver=approver, requester=None, bootstrap=True, execute=True)  # fmt: skip
    assert refused.value.code == "bootstrap_only_without_operators"
    for requester, code in ((None, "requester_required"), (approver, "two_person_rule"), (other_admin, "requester_not_an_operator")):  # fmt: skip
        with pytest.raises(Refused) as refused:
            await operators.provision_operator(populated, **profile("U-OP-2", "auth|op2", "op2@x.invalid"),
                                               approver=approver, requester=requester, bootstrap=False, execute=True)  # fmt: skip
        assert refused.value.code == code
    outcome = await operators.provision_operator(populated, **profile("U-OP-2", "auth|op2", "op2@x.invalid"),
                                                 approver=approver, requester=first, bootstrap=False, execute=True)  # fmt: skip
    assert outcome.result == "operator_provisioned"
    audit = (await populated.execute(select(AuditEvent.operation, AuditEvent.event_data).where(AuditEvent.operation == "operator.provision"))).all()  # fmt: skip
    assert len(audit) == 2 and audit[1].event_data["requested_by"] == first
    assert audit[1].event_data["verification_ref"] == "SEC-101"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"verification_ref": "none"}, "verification_reference_required"),
        ({"subject": "agent|quote-bot"}, "machine_identity_refused"),
        ({"subject": "svc|pipeline"}, "machine_identity_refused"),
        ({"subject": "abc@clients"}, "machine_identity_refused"),
        ({"approver": "non-admin"}, "approval_requires_active_administrator"),
        ({"conflict": "user_id"}, "identity_conflict"),
        ({"conflict": "subject"}, "identity_conflict"),
        ({"conflict": "email"}, "identity_conflict"),
    ],
)
async def test_operator_refusals(populated, changes, code):
    await register_policy(populated, "rbac-policy-v2")
    existing = await populated.scalar(
        select(User).where(User.user_id == await admin(populated, 1))
    )  # not the approver
    args = {**profile("U-OP-1", "auth|op1", "op1@x.invalid"), "approver": await admin(populated)}
    conflict = changes.pop("conflict", None)
    if conflict == "user_id":
        args["new_user"] = existing.user_id  # an existing administrator is never converted
    elif conflict == "subject":
        args["subject"] = existing.authentication_subject
    elif conflict == "email":
        args["email"] = existing.email.upper()
    if changes.get("approver") == "non-admin":
        changes["approver"] = await populated.scalar(select(User.user_id).where(User.role == "estimator").limit(1))  # fmt: skip
    args.update(changes)
    with pytest.raises(Refused) as refused:
        await operators.provision_operator(
            populated, **args, requester=None, bootstrap=True, execute=True
        )
    assert refused.value.code == code
    assert await populated.scalar(select(func.count()).select_from(User).where(User.role == "identity_operator")) == 0  # fmt: skip


async def test_deactivating_operators_only(populated):
    await register_policy(populated, "rbac-policy-v2")
    op = await first_operator(populated)
    approver = await admin(populated)
    with pytest.raises(Refused) as refused:  # never touches non-operator accounts
        await operators.deactivate_operator(populated, operator_user=approver, approver=approver, requester=None, reason="offboarding", execute=True)  # fmt: skip
    assert refused.value.code == "not_an_operator"
    result = await operators.deactivate_operator(populated, operator_user=op, approver=approver, requester=None, reason="offboarding", execute=True)  # fmt: skip
    assert result.result == "operator_deactivated"
    again = await operators.deactivate_operator(populated, operator_user=op, approver=approver, requester=None, reason="offboarding", execute=True)  # fmt: skip
    assert again.result == "already_inactive"


# --- Task 2: RFQ assignments -----------------------------------------------------------------


async def staff(populated, role="sales_representative"):
    return await populated.scalar(select(User).where(User.role == role, User.active.is_(True)).order_by(User.user_id).limit(1))  # fmt: skip


async def test_assignment_validation_and_audit(populated, seed_plan):
    await register_policy(populated, "rbac-policy-v2")
    op = await first_operator(populated)
    rep = await staff(populated)
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    rfq, foreign = await own_rfq(populated, mine), await own_rfq(populated, theirs)
    args = {"operator": op, "user": rep.user_id, "reason": "onboarding", "execute": True}
    with pytest.raises(Refused) as refused:  # no binding yet
        await assignments.grant_assignment(populated, rfq=rfq, **args)
    assert refused.value.code == "no_active_binding_for_rfq_customer"
    await bound(populated, rep, mine)
    with pytest.raises(Refused) as refused:  # another customer's RFQ: never crosses customers
        await assignments.grant_assignment(populated, rfq=foreign, **args)
    assert refused.value.code == "no_active_binding_for_rfq_customer"
    with pytest.raises(Refused) as refused:
        await assignments.grant_assignment(populated, rfq="RFQ-NOPE", **args)
    assert refused.value.code == "rfq_unknown"
    assert (await assignments.grant_assignment(populated, rfq=rfq, **args)).result == "assigned"
    assert (
        await assignments.grant_assignment(populated, rfq=rfq, **args)
    ).result == "already_assigned"
    assert (await assignments.revoke_assignment(populated, rfq=rfq, **args)).result == "revoked"
    assert (
        await assignments.revoke_assignment(populated, rfq=rfq, **args)
    ).result == "already_revoked"
    assert (await assignments.grant_assignment(populated, rfq=rfq, **args)).result == "reactivated"
    assert await assignments.list_assignments(populated, user=rep.user_id) == [{"rfq_id": rfq, "user_id": rep.user_id, "active": True}]  # fmt: skip
    audit = (
        await populated.scalars(select(AuditEvent.operation).where(AuditEvent.rfq_id == rfq))
    ).all()
    assert audit.count("rfq_assignment.grant") == 2 and audit.count("rfq_assignment.revoke") == 1


async def test_assignment_authority_and_role_checks(populated, seed_plan):
    await register_policy(populated, "rbac-policy-v2")
    op = await first_operator(populated)
    mine = customer_id(seed_plan)
    rfq = await own_rfq(populated, mine)
    administrator = await staff(populated, "administrator")
    await bound(populated, administrator, mine)
    with pytest.raises(Refused) as refused:  # administrators hold no RFQ-scoped permission
        await assignments.grant_assignment(populated, operator=op, user=administrator.user_id, rfq=rfq, reason="onboarding", execute=True)  # fmt: skip
    assert refused.value.code == "role_cannot_use_assignment"
    rep = await staff(populated)
    await bound(populated, rep, mine)
    for operator, code in ((administrator.user_id, "operator_not_authorized"), (op, None)):
        if code:
            with pytest.raises(Refused) as refused:
                await assignments.grant_assignment(populated, operator=operator, user=rep.user_id, rfq=rfq, reason="onboarding", execute=True)  # fmt: skip
            assert refused.value.code == code
    with pytest.raises(Refused) as refused:
        await assignments.grant_assignment(populated, operator=op, user=op, rfq=rfq, reason="onboarding", execute=True)  # fmt: skip
    assert refused.value.code == "self_administration_refused"


async def test_assigned_access_isolation_released_quotes_and_revocation(populated, seed_plan, trust, database, tmp_path):  # fmt: skip
    await register_policy(populated, "rbac-policy-v2")
    op = await first_operator(populated)
    quote_id, owner = await release_one(populated)
    quote_rfq = await populated.scalar(select(Quote.rfq_id).where(Quote.quote_id == quote_id))
    other_rfq = await populated.scalar(select(RFQ.rfq_id).where(RFQ.customer_id == owner, RFQ.rfq_id != quote_rfq).limit(1))  # fmt: skip
    rep = await staff(populated)
    await bound(populated, rep, owner)
    app = app_with(trust, database, rfq_assignments="required", permission_policy="v2")
    auth = bearer(trust.mint(sub=rep.authentication_subject))
    assert (
        await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=auth)
    ).status_code == 404  # unassigned
    await assignments.grant_assignment(populated, operator=op, user=rep.user_id, rfq=quote_rfq, reason="onboarding", execute=True)  # fmt: skip
    assert (
        await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=auth)
    ).status_code == 200  # released + assigned
    assert (await call(app, "GET", f"/api/v1/rfqs/{quote_rfq}", headers=auth)).status_code == 200
    if other_rfq:
        assert (
            await call(app, "GET", f"/api/v1/rfqs/{other_rfq}", headers=auth)
        ).status_code == 404
    unreleased = await populated.scalar(select(Quote.quote_id).where(Quote.rfq_id == quote_rfq, Quote.status != "released").limit(1))  # fmt: skip
    if unreleased:
        assert (
            await call(app, "GET", f"/api/v1/quotes/{unreleased}", headers=auth)
        ).status_code == 404
    await assignments.revoke_assignment(populated, operator=op, user=rep.user_id, rfq=quote_rfq, reason="offboarding", execute=True)  # fmt: skip
    assert (await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=auth)).status_code == 404


async def test_auditor_needs_both_binding_and_assignment(populated, seed_plan, trust, database):
    await register_policy(populated, "rbac-policy-v2")
    op = await first_operator(populated)
    quote_id, owner = await release_one(populated)
    quote_rfq = await populated.scalar(select(Quote.rfq_id).where(Quote.quote_id == quote_id))
    auditor = await staff(populated, "auditor")
    await bound(populated, auditor, owner)
    app = app_with(trust, database, rfq_assignments="required", permission_policy="v2")
    auth = bearer(trust.mint(sub=auditor.authentication_subject))
    assert (
        await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=auth)
    ).status_code == 404  # no bypass
    await assignments.grant_assignment(populated, operator=op, user=auditor.user_id, rfq=quote_rfq, reason="onboarding", execute=True)  # fmt: skip
    assert (await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=auth)).status_code == 200


async def test_verify_reports_stale_assignments_without_changing_them(populated, seed_plan):
    await register_policy(populated, "rbac-policy-v2")
    op = await first_operator(populated)
    rep = await staff(populated)
    mine = customer_id(seed_plan)
    rfq = await own_rfq(populated, mine)
    await bound(populated, rep, mine)
    await assignments.grant_assignment(populated, operator=op, user=rep.user_id, rfq=rfq, reason="onboarding", execute=True)  # fmt: skip
    assert await assignments.verify_assignments(populated) == []
    from rfq_copilot.models import UserCustomerBinding

    await populated.execute(update(UserCustomerBinding).values(revoked_at=func.now()))
    assert await assignments.verify_assignments(populated) == [{"rfq_id": rfq, "user_id": rep.user_id, "problem": "no_active_binding"}]  # fmt: skip
    assert await populated.scalar(select(func.count()).select_from(RfqAssignment).where(RfqAssignment.revoked_at.is_(None))) == 1  # fmt: skip


async def test_concurrent_grants_never_duplicate(populated, seed_plan):
    await register_policy(populated, "rbac-policy-v2")
    op = await first_operator(populated)
    rep = await staff(populated)
    mine = customer_id(seed_plan)
    rfq = await own_rfq(populated, mine)
    await bound(populated, rep, mine)
    args = {
        "operator": op,
        "user": rep.user_id,
        "rfq": rfq,
        "reason": "onboarding",
        "execute": True,
    }
    results = []
    for _ in range(3):  # one connection: sequential, but each call re-checks under row locks
        results.append((await assignments.grant_assignment(populated, **args)).result)
    assert results == ["assigned", "already_assigned", "already_assigned"]
    # A racing grant that read "no row" before the other transaction committed is refused
    # by the primary key (savepoint rolled back), never duplicated.

    class RacedSession:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def execute(self, statement, *args, **kwargs):
            if getattr(statement, "_for_update_arg", None) is not None:
                return type("Empty", (), {"first": lambda self: None})()  # the stale read
            return await self.inner.execute(statement, *args, **kwargs)

    with pytest.raises(Refused) as refused:
        await assignments.grant_assignment(RacedSession(populated), **args)
    assert refused.value.code == "concurrent_change"
    assert await populated.scalar(select(func.count()).select_from(RfqAssignment)) == 1
    await asyncio.sleep(0)
