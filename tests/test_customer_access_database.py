"""External customer accounts under rbac-policy-v3 (Milestone 2 Step 1; guarded test DB).

Everything runs in the seeded fixture transaction and is rolled back. A customer account is
provisioned and bound only through the audited operator paths (``operator_admin``,
``identity_admin``), then exercised through the real bearer-token path of ``create_app``.
"""

import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select, update
from test_auth import AUDIENCE, bearer
from test_auth import trust as trust
from test_auth_database import app_with, call
from test_auth_database import database as database
from test_database_queries import customer_id, other_customer
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_operator_assignment_admin_database import admin, first_operator, staff
from test_rbac_database import bound, own_rfq, release_one

from rfq_copilot import assignment_admin as assignments
from rfq_copilot import identity_admin
from rfq_copilot import operator_admin as operators
from rfq_copilot.api.auth import Principal
from rfq_copilot.identity_admin import Refused
from rfq_copilot.models import RFQ, AuditEvent, Customer, Quote, RolePermission
from rfq_copilot.quote_approval import submit_for_review
from rfq_copilot.quote_drafts import DraftRefused, create_quote_draft
from rfq_copilot.seeding.policy import register_policy

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]
RELEASED_FIELDS = {"quote_id", "rfq_id", "status", "released_at", "version", "currency", "total_usd", "expiry_date", "estimated_lead_business_days", "lines"}  # fmt: skip


async def customer_account(populated, customer, user_id="U-CUST-1"):
    """Provision (operator requests, administrator approves) and bind through audited paths."""
    await register_policy(populated, "rbac-policy-v3")
    operator = await first_operator(populated)
    outcome = await operators.provision_customer(
        populated, new_user=user_id, display_name="Customer Buyer", email=f"{user_id}@customer.invalid",
        region="NA", subject=f"auth|{user_id}", verification_ref="SEC-202",
        approver=await admin(populated), requester=operator, execute=True,
    )  # fmt: skip
    assert outcome.result == "customer_provisioned"
    await populated.execute(update(Customer).where(Customer.customer_id == customer).values(account_status="active"))  # fmt: skip
    granted = await identity_admin.grant_binding(populated, operator=operator, user=user_id, customer=customer, reason="onboarding", execute=True)  # fmt: skip
    assert granted.result == "granted"
    return user_id, f"auth|{user_id}", operator


def v3(trust, database, **settings):
    return app_with(trust, database, permission_policy="v3", rfq_assignments="required", **settings)


# --- 1, 9: a valid customer token reads its own data, with staff assignments enforced -------


async def test_customer_reads_own_profile_rfqs_and_released_quote_with_assignments_on(populated, seed_plan, trust, database):  # fmt: skip
    quote_id, owner = await release_one(populated)
    quote_rfq = await populated.scalar(select(Quote.rfq_id).where(Quote.quote_id == quote_id))
    _, subject, _ = await customer_account(populated, owner)
    app, auth = v3(trust, database), bearer(trust.mint(sub=subject))
    profile = await call(app, "GET", "/api/v1/customers/me", headers=auth)
    assert profile.status_code == 200 and profile.json()["customer_id"] == owner
    page = await call(app, "GET", "/api/v1/rfqs", headers=auth)
    owned = set((await populated.scalars(select(RFQ.rfq_id).where(RFQ.customer_id == owner))).all())  # fmt: skip
    listed = {r["rfq_id"] for r in page.json()["items"]}
    assert page.status_code == 200 and listed and listed <= owned  # paginated: own RFQs only
    assert (await call(app, "GET", f"/api/v1/rfqs/{quote_rfq}", headers=auth)).status_code == 200
    assert (await call(app, "GET", f"/api/v1/rfqs/{quote_rfq}/attachments", headers=auth)).status_code == 200  # fmt: skip
    quote = await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=auth)
    assert quote.status_code == 200 and set(quote.json()) == RELEASED_FIELDS  # no cost/margin field
    unreleased = await populated.scalar(select(Quote.quote_id).join(RFQ, RFQ.rfq_id == Quote.rfq_id).where(RFQ.customer_id == owner, Quote.status != "released").limit(1))  # fmt: skip
    if unreleased:
        assert (await call(app, "GET", f"/api/v1/quotes/{unreleased}", headers=auth)).status_code == 404  # fmt: skip


async def test_staff_assignment_enforcement_is_unchanged_under_v3(populated, seed_plan, trust, database):  # fmt: skip
    quote_id, owner = await release_one(populated)
    quote_rfq = await populated.scalar(select(Quote.rfq_id).where(Quote.quote_id == quote_id))
    _, _, operator = await customer_account(populated, owner)
    rep = await staff(populated)
    await bound(populated, rep, owner)
    app, auth = v3(trust, database), bearer(trust.mint(sub=rep.authentication_subject))
    assert (await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=auth)).status_code == 404  # unassigned  # fmt: skip
    assert (await call(app, "GET", f"/api/v1/rfqs/{quote_rfq}", headers=auth)).status_code == 404
    await assignments.grant_assignment(populated, operator=operator, user=rep.user_id, rfq=quote_rfq, reason="onboarding", execute=True)  # fmt: skip
    assert (await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=auth)).status_code == 200
    # Staff catalog access (view_catalog) is untouched by v3.
    assert (await call(app, "GET", "/api/v1/materials", headers=auth)).status_code == 200


# --- 2, 3: bad or missing credentials ---------------------------------------------------------


async def test_missing_expired_forged_and_wrong_audience_tokens_get_401(populated, seed_plan, trust, database):  # fmt: skip
    _, subject, _ = await customer_account(populated, customer_id(seed_plan))
    app, now = v3(trust, database), int(time.time())
    other_key = ec.generate_private_key(ec.SECP256R1())
    tokens = [
        trust.mint(sub=subject, iat=now - 1000, exp=now - 500),  # expired
        trust.mint(sub=subject, key=other_key),  # invalid signature
        trust.mint(sub=subject, aud=f"{AUDIENCE}-other"),  # wrong audience
    ]
    assert (await call(app, "GET", "/api/v1/customers/me")).status_code == 401
    for token in tokens:
        assert (await call(app, "GET", "/api/v1/customers/me", headers=bearer(token))).status_code == 401  # fmt: skip
    assert (await call(app, "GET", "/api/v1/customers/me", headers=bearer(trust.mint(sub=subject)))).status_code == 200  # fmt: skip


# --- 4, 6: no staff, catalog, draft, review, decision or admin authority ---------------------


async def test_customer_gets_403_on_catalog_and_under_staff_policies(populated, seed_plan, trust, database):  # fmt: skip
    mine = customer_id(seed_plan)
    _, subject, _ = await customer_account(populated, mine)
    rfq, auth = await own_rfq(populated, mine), bearer(trust.mint(sub=subject))
    assert (await call(app_with(trust, database, permission_policy="v3"), "GET", "/api/v1/materials", headers=auth)).status_code == 403  # fmt: skip
    for policy in ("v1", "v2"):  # customer_* permissions mean nothing outside v3: no inheritance
        app = app_with(trust, database, permission_policy=policy)
        for path in (f"/api/v1/rfqs/{rfq}", "/api/v1/rfqs", "/api/v1/materials", "/api/v1/quotes/Q-1"):  # fmt: skip
            assert (await call(app, "GET", path, headers=auth)).status_code == 403, (policy, path)


async def test_customer_cannot_draft_submit_or_administer(populated, seed_plan):
    mine = customer_id(seed_plan)
    user_id, subject, _ = await customer_account(populated, mine)
    held = frozenset((await populated.scalars(select(RolePermission.permission).where(RolePermission.role == "customer"))).all())  # fmt: skip
    principal = Principal(mine, held, user_id=user_id, subject=subject)
    with pytest.raises(DraftRefused) as refused:
        await create_quote_draft(populated, principal, await own_rfq(populated, mine), idempotency_key="cust-draft-1", correlation_id="c-1")  # fmt: skip
    assert refused.value.reason == "permission_denied"
    quote = await populated.scalar(select(Quote.quote_id).join(RFQ, RFQ.rfq_id == Quote.rfq_id).where(RFQ.customer_id == mine).limit(1))  # fmt: skip
    if quote:
        with pytest.raises(DraftRefused) as refused:
            await submit_for_review(populated, principal, quote, expected_version=1, idempotency_key="cust-submit-1", correlation_id="c-2")  # fmt: skip
        assert refused.value.reason == "permission_denied"
    with pytest.raises(Refused):  # no binding administration
        await identity_admin.grant_binding(populated, operator=user_id, user=user_id, customer=other_customer(seed_plan), reason="onboarding", execute=True)  # fmt: skip


# --- 5, 7: cross-customer access is concealed; request-supplied identity is ignored ---------


async def test_cross_customer_access_is_404_and_identity_cannot_be_forged(populated, seed_plan, trust, database):  # fmt: skip
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    _, subject, _ = await customer_account(populated, mine)
    their_rfq = await own_rfq(populated, theirs)
    app = v3(trust, database)
    token = trust.mint(sub=subject, customer_id=theirs, role="sales_manager", permissions=["view_catalog", "approve_quote"])  # fmt: skip
    forged = {**bearer(token), "X-Customer-Id": theirs, "X-RFQ-Customer-Context": theirs}
    profile = await call(app, "GET", "/api/v1/customers/me", headers=forged, params={"customer_id": theirs})  # fmt: skip
    assert profile.status_code == 200 and profile.json()["customer_id"] == mine
    if their_rfq:
        for template in ("/api/v1/rfqs/{rfq_id}", "/api/v1/rfqs/{rfq_id}/attachments"):
            hidden = await call(app, "GET", template.replace("{rfq_id}", their_rfq), headers=forged)
            absent = await call(
                app, "GET", template.replace("{rfq_id}", "RFQ-NOPE"), headers=forged
            )
            assert hidden.status_code == absent.status_code == 404
            assert hidden.json() == absent.json()
    assert (await call(app, "GET", "/api/v1/materials", headers=forged)).status_code == 403
    page = await call(app, "GET", "/api/v1/rfqs", headers=forged)
    assert their_rfq not in {r["rfq_id"] for r in page.json()["items"]}


# --- provisioning rules -----------------------------------------------------------------------


async def test_customer_provisioning_needs_overlay_operator_and_is_audited(populated, seed_plan):
    profile = dict(new_user="U-CUST-9", display_name="Buyer", email="b9@customer.invalid", region="NA", subject="auth|cust9", verification_ref="SEC-9")  # fmt: skip
    approver = await admin(populated)
    with pytest.raises(Refused) as refused:  # no v3 overlay: no customer role row
        await operators.provision_customer(populated, **profile, approver=approver, requester=None, execute=True)  # fmt: skip
    assert refused.value.code == "customer_role_not_provisioned"
    await register_policy(populated, "rbac-policy-v3")
    with pytest.raises(Refused) as refused:  # no bootstrap path for customer accounts
        await operators.provision_customer(populated, **profile, approver=approver, requester=None, execute=True)  # fmt: skip
    assert refused.value.code == "operator_required"
    operator = await first_operator(populated)
    with pytest.raises(Refused) as refused:
        await operators.provision_customer(populated, **{**profile, "subject": "svc|robot"}, approver=approver, requester=operator, execute=True)  # fmt: skip
    assert refused.value.code == "machine_identity_refused"
    before = await populated.scalar(select(func.count()).select_from(AuditEvent))
    await operators.provision_customer(populated, **profile, approver=approver, requester=operator, execute=True)  # fmt: skip
    assert await populated.scalar(select(func.count()).select_from(AuditEvent)) == before + 1
    with pytest.raises(Refused) as refused:  # never reuses an identity
        await operators.provision_customer(populated, **{**profile, "new_user": "U-CUST-10"}, approver=approver, requester=operator, execute=True)  # fmt: skip
    assert refused.value.code == "identity_conflict"
