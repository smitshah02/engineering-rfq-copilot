"""Milestone 2 Step 2: POST /api/v1/rfqs against the guarded test database.

Everything runs in the seeded fixture transaction and is rolled back. Customer accounts are
provisioned and bound through the audited operator paths (test_customer_access_database),
then exercised through the real bearer-token path of ``create_app`` under policy v3.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, insert, select, update
from test_auth import bearer
from test_auth import trust as trust
from test_auth_database import call
from test_auth_database import database as database
from test_customer_access_database import customer_account, v3
from test_database_queries import customer_id, other_customer
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_operator_assignment_admin_database import admin, staff
from test_rbac_database import bound

import rfq_copilot.rfq_submission as rfq_submission
from rfq_copilot import identity_admin
from rfq_copilot import operator_admin as operators
from rfq_copilot.models import (
    RFQ,
    AuditEvent,
    Customer,
    Finish,
    IdempotencyRecord,
    Material,
    RFQLineItem,
    RolePermission,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


async def body(populated, **part):
    material = await populated.scalar(select(Material.material_id).order_by(Material.material_id).limit(1))  # fmt: skip
    return {
        "parts": [
            {"part_id": "P-100", "part_type": "flat_panel", "material_id": material,
             "length_mm": "120.5", "width_mm": "80", "thickness_mm": "2", "quantity": 25, **part},
            {"part_id": "P-200", "quantity": 4},
        ],
        "raw_request": "Two panels, see drawing.",
    }  # fmt: skip


async def second_account(populated, customer, operator, user_id="U-CUST-2"):
    """Another customer account via the same audited paths (customer_account bootstraps)."""
    outcome = await operators.provision_customer(
        populated, new_user=user_id, display_name="Other Buyer", email=f"{user_id}@customer.invalid",
        region="NA", subject=f"auth|{user_id}", verification_ref="SEC-203",
        approver=await admin(populated), requester=operator, execute=True,
    )  # fmt: skip
    assert outcome.result == "customer_provisioned"
    granted = await identity_admin.grant_binding(populated, operator=operator, user=user_id, customer=customer, reason="onboarding", execute=True)  # fmt: skip
    assert granted.result == "granted"
    return f"auth|{user_id}"


async def bound_customer(populated, customer):
    """An eligible account status, as customer_account sets for its own customer."""
    await populated.execute(update(Customer).where(Customer.customer_id == customer).values(account_status="active"))  # fmt: skip


async def written(populated):
    """(runtime RFQs, their lines, create_rfq receipts, rfq.create audit events)."""
    runtime = select(RFQ.rfq_id).where(RFQ.source_type == rfq_submission.SOURCE_TYPE)
    return (
        await populated.scalar(select(func.count()).select_from(runtime.subquery())),
        await populated.scalar(
            select(func.count()).select_from(RFQLineItem).where(RFQLineItem.rfq_id.in_(runtime))
        ),  # fmt: skip
        await populated.scalar(
            select(func.count())
            .select_from(IdempotencyRecord)
            .where(IdempotencyRecord.operation == "create_rfq")
        ),  # fmt: skip
        await populated.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.operation == "rfq.create")
        ),  # fmt: skip
    )


@pytest.fixture
def app(trust, database):
    return v3(trust, database)


@pytest.fixture
def auth(trust):
    def headers(subject, key="key-1"):
        token = bearer(trust.mint(sub=subject))
        return token if key is None else {**token, "Idempotency-Key": key}

    return headers


async def test_customer_creates_an_rfq_and_reads_it_back(populated, seed_plan, app, auth):
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    user, subject, operator = await customer_account(populated, mine)
    await bound_customer(populated, theirs)
    other_subject = await second_account(populated, theirs, operator)
    request = await body(populated)
    created = await call(app, "POST", "/api/v1/rfqs", json=request, headers=auth(subject))
    assert created.status_code == 201
    receipt = created.json()
    assert set(receipt) == {"rfq_id", "status"} and receipt["status"] == "draft"

    rfq = await populated.scalar(select(RFQ).where(RFQ.rfq_id == receipt["rfq_id"]))
    assert (rfq.customer_id, rfq.created_by, rfq.source_type, rfq.status) == (mine, user, "customer_portal", "draft")  # fmt: skip
    assert rfq.part_ids == ["P-100", "P-200"] and rfq.raw_request == request["raw_request"]

    detail = await call(app, "GET", f"/api/v1/rfqs/{rfq.rfq_id}", headers=auth(subject, None))
    assert detail.status_code == 200 and detail.json()["status"] == "draft"
    lines = detail.json()["lines"]
    assert [line["line_number"] for line in lines] == [1, 2]
    assert (
        Decimal(lines[0]["part"]["length_mm"]) == Decimal("120.5")
        and lines[0]["part"]["quantity"] == 25
    )
    listed = await call(app, "GET", "/api/v1/rfqs", headers=auth(subject, None))
    assert rfq.rfq_id in {r["rfq_id"] for r in listed.json()["items"]}

    # Another customer: indistinguishable from a missing RFQ, and absent from its list.
    assert (await call(app, "GET", f"/api/v1/rfqs/{rfq.rfq_id}", headers=auth(other_subject, None))).status_code == 404  # fmt: skip
    theirs_list = await call(app, "GET", "/api/v1/rfqs", headers=auth(other_subject, None))
    assert rfq.rfq_id not in {r["rfq_id"] for r in theirs_list.json()["items"]}

    event = await populated.scalar(select(AuditEvent).where(AuditEvent.operation == "rfq.create"))
    assert (event.actor_id, event.customer_id, event.rfq_id, event.outcome) == (user, mine, rfq.rfq_id, "success")  # fmt: skip
    assert event.event_data == {"source_type": "customer_portal", "line_count": 2}  # no raw content
    receipt_row = await populated.scalar(select(IdempotencyRecord).where(IdempotencyRecord.operation == "create_rfq"))  # fmt: skip
    assert (receipt_row.actor_id, receipt_row.customer_id, receipt_row.result_json) == (user, mine, receipt)  # fmt: skip
    assert await written(populated) == (1, 2, 1, 1)


async def test_same_key_replays_and_a_different_request_conflicts(populated, seed_plan, app, auth):
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    _, subject, operator = await customer_account(populated, mine)
    await bound_customer(populated, theirs)
    other_subject = await second_account(populated, theirs, operator)
    request = await body(populated)
    first = await call(app, "POST", "/api/v1/rfqs", json=request, headers=auth(subject))
    again = await call(app, "POST", "/api/v1/rfqs", json=request, headers=auth(subject))
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() and again.headers["Idempotent-Replay"] == "true"
    assert await written(populated) == (1, 2, 1, 1)

    changed = {**request, "raw_request": "Changed."}
    conflict = await call(app, "POST", "/api/v1/rfqs", json=changed, headers=auth(subject))
    assert conflict.status_code == 409 and conflict.json()["reason"] == "idempotency_key_conflict"
    assert "Changed" not in conflict.text
    assert await written(populated) == (1, 2, 1, 1)

    # Receipts are scoped to actor and customer: the same key elsewhere is a new RFQ.
    other = await call(app, "POST", "/api/v1/rfqs", json=request, headers=auth(other_subject))
    assert other.status_code == 201 and other.json()["rfq_id"] != first.json()["rfq_id"]
    assert await written(populated) == (2, 4, 2, 2)


@pytest.mark.parametrize(
    "change,key,status,reason",
    [({"customer_id": "CUST-FORGED"}, "k", 422, None),
     ({"created_by": "U-FORGED"}, "k", 422, None),
     ({}, None, 422, None),  # the header is required
     ({}, "x" * 257, 422, "idempotency_key_invalid"),
     ({"part": {"material_id": "MAT-NOPE"}}, "k", 422, "unknown_material"),
     ({"part": {"finish_id": "FIN-NOPE"}}, "k", 422, "unknown_finish"),
     ({"part": {"bend_radius_mm": "0"}}, "k", 422, "rejected_by_constraints"),
     ({"requested_delivery_date": "2027-01-15"}, "k", 422, "delivery_date_not_stored"),
     ({"part": {"part_id": "P-200"}}, "k", 422, None)],  # duplicate part_id
)  # fmt: skip
async def test_refused_requests_write_nothing(populated, seed_plan, app, auth, change, key, status, reason):  # fmt: skip
    _, subject, _ = await customer_account(populated, customer_id(seed_plan))
    change = dict(change)
    request = {**(await body(populated, **change.pop("part", {}))), **change}
    sent = auth(subject, key)
    response = await call(app, "POST", "/api/v1/rfqs", json=request, headers=sent)
    assert response.status_code == status
    if reason is not None:
        assert response.json()["reason"] == reason
    for value in ("CUST-FORGED", "U-FORGED", "MAT-NOPE", "FIN-NOPE"):
        assert value not in response.text  # nothing submitted is echoed
    assert await written(populated) == (0, 0, 0, 0)


async def test_a_failing_audit_write_rolls_back_rfq_lines_and_receipt(populated, seed_plan, app, auth, monkeypatch):  # fmt: skip
    _, subject, _ = await customer_account(populated, customer_id(seed_plan))
    real = rfq_submission.AuditEvent
    monkeypatch.setattr(rfq_submission, "AuditEvent", lambda **kw: real(**{**kw, "outcome": "forged"}))  # fmt: skip
    response = await call(app, "POST", "/api/v1/rfqs", json=await body(populated), headers=auth(subject))  # fmt: skip
    assert response.status_code == 422 and response.json()["reason"] == "rejected_by_constraints"
    assert await written(populated) == (0, 0, 0, 0)


async def test_staff_even_with_the_grant_and_revoked_customers_are_denied(populated, seed_plan, app, auth):  # fmt: skip
    mine = customer_id(seed_plan)
    user, subject, operator = await customer_account(populated, mine)
    rep = await staff(populated)
    await bound(populated, rep, mine)
    request = await body(populated)
    send = lambda s, k: call(app, "POST", "/api/v1/rfqs", json=request, headers=auth(s, k))  # noqa: E731
    assert (await send(rep.authentication_subject, "s-1")).status_code == 403  # no grant
    # A staff role holding customer_create_rfq is still not a customer account.
    await populated.execute(insert(RolePermission).values(role=rep.role, permission="customer_create_rfq", allowed=True))  # fmt: skip
    assert (await send(rep.authentication_subject, "s-2")).status_code == 403
    no_token = await call(app, "POST", "/api/v1/rfqs", json=request, headers={"Idempotency-Key": "k"})  # fmt: skip
    assert no_token.status_code == 401
    revoked = await identity_admin.revoke_binding(populated, operator=operator, user=user, customer=mine, reason="offboarding", execute=True)  # fmt: skip
    assert revoked.result == "revoked"
    assert (await send(subject, "c-1")).status_code == 403
    assert await written(populated) == (0, 0, 0, 0)


async def test_unknown_finish_or_material_is_checked_against_the_catalog(populated, seed_plan, app, auth):  # fmt: skip
    _, subject, _ = await customer_account(populated, customer_id(seed_plan))
    finish = await populated.scalar(select(Finish.finish_id).order_by(Finish.finish_id).limit(1))
    ok = await call(app, "POST", "/api/v1/rfqs", json=await body(populated, finish_id=finish), headers=auth(subject))  # fmt: skip
    assert ok.status_code == 201
    line = await populated.scalar(select(RFQLineItem).where(RFQLineItem.rfq_id == ok.json()["rfq_id"], RFQLineItem.line_number == 1))  # fmt: skip
    assert line.finish_id == finish
