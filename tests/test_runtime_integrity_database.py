"""Opt-in runtime integrity audit (``integrity --runtime``) against the guarded test database.

The runtime world is built only through the audited paths (operator bootstrap, customer
provisioning, binding grant, POST /api/v1/rfqs) inside the rolled-back seeded fixture
transaction, on customers whose seeded account status is already eligible, so no seed row
changes. Each tampering case then adds one forged or altered row and must fail closed.
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from test_attachments import ROW, bom
from test_auth import bearer
from test_auth import trust as trust
from test_auth_database import call
from test_auth_database import database as database
from test_customer_access_database import v3
from test_customer_rfq_create_database import body
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_operator_assignment_admin_database import admin, first_operator, staff

from rfq_copilot import identity_admin
from rfq_copilot import operator_admin as operators
from rfq_copilot.integrity import IntegrityVerificationError, audit_integrity
from rfq_copilot.models import (
    RFQ,
    AuditEvent,
    Customer,
    IdempotencyRecord,
    RFQAttachment,
    RFQLineItem,
    User,
    UserCustomerBinding,
)
from rfq_copilot.repositories.identity import ELIGIBLE_ACCOUNT_STATUSES
from rfq_copilot.seeding.policy import register_policy

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


def eligible(plan, n=2):
    return [c["customer_id"] for c in plan.rows["customers"] if c["account_status"] in ELIGIBLE_ACCOUNT_STATUSES][:n]  # fmt: skip


async def world(populated, seed_plan, trust, database, user_id="U-CUST-RT"):
    """Operator, customer account, binding and one RFQ (plus its replay) via the real paths."""
    mine, _ = eligible(seed_plan)
    await register_policy(populated, "rbac-policy-v3")
    operator = await first_operator(populated)
    provisioned = await operators.provision_customer(
        populated, new_user=user_id, display_name="Runtime Buyer", email=f"{user_id}@customer.invalid",
        region="NA", subject=f"auth|{user_id}", verification_ref="SEC-301",
        approver=await admin(populated), requester=operator, execute=True,
    )  # fmt: skip
    assert provisioned.result == "customer_provisioned"
    granted = await identity_admin.grant_binding(populated, operator=operator, user=user_id, customer=mine, reason="onboarding", execute=True)  # fmt: skip
    assert granted.result == "granted"
    headers = {**bearer(trust.mint(sub=f"auth|{user_id}")), "Idempotency-Key": "rt-1"}
    app, request = v3(trust, database), await body(populated)
    created = await call(app, "POST", "/api/v1/rfqs", json=request, headers=headers)
    assert created.status_code == 201
    replay = await call(app, "POST", "/api/v1/rfqs", json=request, headers=headers)
    assert replay.headers.get("Idempotent-Replay") == "true"  # writes nothing new
    return user_id, mine, operator, created.json()["rfq_id"]


async def runtime_audit(populated, seed_database, seed_plan):
    return await audit_integrity(populated, seed_database.config, seed_plan, runtime=True)


async def fails(populated, seed_database, seed_plan, rule):
    with pytest.raises(IntegrityVerificationError) as error:
        await runtime_audit(populated, seed_database, seed_plan)
    assert rule in str(error.value)


async def forge_rfq(populated, user, customer, later=timedelta(0)):
    """An RFQ, line, receipt and creation event that agree with each other, written directly."""
    at = await populated.scalar(select(func.now())) + later
    rfq_id = f"RFQ-{uuid.uuid4().hex}"
    populated.add(RFQ(rfq_id=rfq_id, customer_id=customer, source_type="customer_portal", part_ids=["P-1"], status="draft", created_by=user, submitted_at=at))  # fmt: skip
    await populated.flush()
    populated.add(RFQLineItem(rfq_id=rfq_id, line_number=1, part_id="P-1", quantity=1))
    populated.add(IdempotencyRecord(actor_id=user, customer_id=customer, operation="create_rfq", idempotency_key=f"forged-{rfq_id}", request_sha256="0" * 64, result_json={"rfq_id": rfq_id, "status": "draft"}, correlation_id=rfq_id, committed_at=at))  # fmt: skip
    populated.add(AuditEvent(actor_id=user, customer_id=customer, rfq_id=rfq_id, operation="rfq.create", outcome="success", correlation_id=rfq_id, occurred_at=at, event_data={"source_type": "customer_portal", "line_count": 1}))  # fmt: skip
    await populated.flush()
    return rfq_id


# --- Valid runtime activity -------------------------------------------------------------------


async def test_seed_only_database_passes_both_modes(populated, seed_database, seed_plan):
    strict = await audit_integrity(populated, seed_database.config, seed_plan)
    assert "mode" not in strict and "runtime_records" not in strict  # strict report unchanged
    report = await runtime_audit(populated, seed_database, seed_plan)
    assert report["mode"] == "runtime" and set(report["runtime_records"].values()) == {0}


async def test_audited_customer_activity_passes_runtime_and_strict_still_refuses(populated, seed_database, seed_plan, trust, database):  # fmt: skip
    await world(populated, seed_plan, trust, database)
    report = await runtime_audit(populated, seed_database, seed_plan)
    assert report["status"] == "PASS" and report["policy_overlay"] == "applied:rbac-policy-v3"
    assert report["runtime_records"] == {
        "services": 0, "workflow_runs": 0, "quotes": 0, "operators": 1, "customers": 1, "bindings": 1, "revoked_bindings": 0, "rfqs": 1,
        "rfq_line_items": 2, "rfq_attachments": 0, "idempotency_records": 1, "audit_events": 4,
    }  # fmt: skip
    with pytest.raises(IntegrityVerificationError, match="source count differs"):
        await audit_integrity(populated, seed_database.config, seed_plan)  # default: strict


async def test_later_revocation_keeps_historical_rfq_valid(populated, seed_database, seed_plan, trust, database):  # fmt: skip
    user, mine, operator, _ = await world(populated, seed_plan, trust, database)
    revoked = await identity_admin.revoke_binding(populated, operator=operator, user=user, customer=mine, reason="offboarding", execute=True)  # fmt: skip
    assert revoked.result == "revoked"
    report = await runtime_audit(populated, seed_database, seed_plan)
    assert report["runtime_records"]["revoked_bindings"] == 1


# --- Tampering and refusal cases --------------------------------------------------------------


async def test_altered_seed_record_fails(populated, seed_database, seed_plan, trust, database):
    _, mine, _, _ = await world(populated, seed_plan, trust, database)
    await populated.execute(update(Customer).where(Customer.customer_id == mine).values(company_name="Altered"))  # fmt: skip
    await fails(populated, seed_database, seed_plan, "source value differs")


async def test_forged_user_without_provisioning_fails(populated, seed_database, seed_plan, trust, database):  # fmt: skip
    await world(populated, seed_plan, trust, database)
    populated.add(User(user_id="U-FORGED", display_name="Forged", role="customer", email="f@customer.invalid", active=True, assigned_region="NA", authentication_subject="auth|U-FORGED"))  # fmt: skip
    await populated.flush()
    await fails(populated, seed_database, seed_plan, "runtime user without provisioning event")


async def test_binding_without_grant_history_fails(populated, seed_database, seed_plan, trust, database):  # fmt: skip
    await world(populated, seed_plan, trust, database)
    _, theirs = eligible(seed_plan)
    populated.add(UserCustomerBinding(user_id=(await staff(populated)).user_id, customer_id=theirs))
    await populated.flush()
    await fails(populated, seed_database, seed_plan, "binding without matching grant history")


async def test_rfq_for_an_unbound_customer_fails(populated, seed_database, seed_plan, trust, database):  # fmt: skip
    user, _, _, _ = await world(populated, seed_plan, trust, database)
    _, theirs = eligible(seed_plan)
    await forge_rfq(populated, user, theirs)
    await fails(populated, seed_database, seed_plan, "no active binding at creation")


async def test_rfq_after_revocation_fails(populated, seed_database, seed_plan, trust, database):
    user, mine, operator, _ = await world(populated, seed_plan, trust, database)
    await identity_admin.revoke_binding(populated, operator=operator, user=user, customer=mine, reason="offboarding", execute=True)  # fmt: skip
    await forge_rfq(populated, user, mine, later=timedelta(minutes=1))
    await fails(populated, seed_database, seed_plan, "no active binding at creation")


async def test_orphan_receipt_fails(populated, seed_database, seed_plan, trust, database):
    user, mine, _, _ = await world(populated, seed_plan, trust, database)
    populated.add(IdempotencyRecord(actor_id=user, customer_id=mine, operation="create_rfq", idempotency_key="orphan", request_sha256="0" * 64, result_json={"rfq_id": "RFQ-NONE", "status": "draft"}, correlation_id="RFQ-NONE"))  # fmt: skip
    await populated.flush()
    await fails(populated, seed_database, seed_plan, "orphan receipt")


async def test_duplicate_creation_event_fails(populated, seed_database, seed_plan, trust, database):
    user, mine, _, rfq_id = await world(populated, seed_plan, trust, database)
    populated.add(AuditEvent(actor_id=user, customer_id=mine, rfq_id=rfq_id, operation="rfq.create", outcome="success", correlation_id=rfq_id, event_data={"source_type": "customer_portal", "line_count": 2}))  # fmt: skip
    await populated.flush()
    await fails(populated, seed_database, seed_plan, "RFQ without exactly one creation event")


async def test_unexplained_audit_event_fails(populated, seed_database, seed_plan, trust, database):
    user, mine, _, _ = await world(populated, seed_plan, trust, database)
    populated.add(AuditEvent(actor_id=user, customer_id=mine, operation="rfq.delete", outcome="success", correlation_id="forged"))  # fmt: skip
    await populated.flush()
    await fails(populated, seed_database, seed_plan, "unexplained audit event")


async def upload(trust, database, user, rfq_id, storage):
    """One CSV through the real customer upload route (no Idempotency-Key, as the frontend)."""
    app = v3(trust, database, attachment_dir=storage)
    files = {"file": ("bom.csv", bom(ROW), "text/csv")}
    response = await call(app, "POST", f"/api/v1/rfqs/{rfq_id}/attachments", files=files, headers=bearer(trust.mint(sub=f"auth|{user}")))  # fmt: skip
    assert response.status_code == 201, response.text
    return response.json()["attachment_id"]


def forged_attachment(rfq_id, **changes):
    values = dict(attachment_id=f"ATT-{uuid.uuid4().hex.upper()}", rfq_id=rfq_id, filename="bom.csv", document_type="csv", source_role="customer_supplied", is_trusted_instruction=False, sha256="0" * 64)  # fmt: skip
    values = {**values, "storage_ref": f"{values['attachment_id']}.csv", **changes}
    return RFQAttachment(**values)


async def test_portal_upload_passes_runtime_and_strict_still_refuses(populated, seed_database, seed_plan, trust, database, tmp_path):  # fmt: skip
    user, _, _, rfq_id = await world(populated, seed_plan, trust, database)
    await upload(trust, database, user, rfq_id, tmp_path / "store")
    report = await runtime_audit(populated, seed_database, seed_plan)
    assert report["status"] == "PASS" and report["runtime_records"]["rfq_attachments"] == 1
    with pytest.raises(IntegrityVerificationError, match="source count differs"):
        await audit_integrity(populated, seed_database.config, seed_plan)  # default: strict


async def test_attachment_on_a_seeded_rfq_fails(populated, seed_database, seed_plan, trust, database):  # fmt: skip
    await world(populated, seed_plan, trust, database)
    populated.add(forged_attachment(seed_plan.rows["rfqs"][0]["rfq_id"]))
    await populated.flush()
    await fails(populated, seed_database, seed_plan, "runtime attachment outside the customer-portal upload contract")  # fmt: skip


@pytest.mark.parametrize(
    "changes",
    [
        {"storage_ref": "../elsewhere.csv"},
        {"source_role": "internal"},
        {"revision": "B"},
        {"document_type": "md", "storage_ref": None},
        {"attachment_id": "ATT-forged", "storage_ref": "ATT-forged.csv"},
    ],
)
async def test_attachment_outside_the_upload_shape_fails(populated, seed_database, seed_plan, trust, database, changes):  # fmt: skip
    _, _, _, rfq_id = await world(populated, seed_plan, trust, database)
    row = forged_attachment(rfq_id, **changes)
    if row.storage_ref is None:
        row.storage_ref = f"{row.attachment_id}.{row.document_type}"  # consistent ref, wrong type
    populated.add(row)
    await populated.flush()
    await fails(populated, seed_database, seed_plan, "runtime attachment outside the customer-portal upload contract")  # fmt: skip


async def test_runtime_rows_outside_runtime_tables_still_fail(populated, seed_database, seed_plan, trust, database):  # fmt: skip
    user, mine, operator, _ = await world(populated, seed_plan, trust, database)
    revoked = await identity_admin.revoke_tokens(populated, operator=operator, user=user, reason="security", token=None, execute=True)  # fmt: skip
    assert revoked.result == "user_tokens_revoked"
    await fails(populated, seed_database, seed_plan, "source count differs")  # token_revocations
