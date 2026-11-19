"""Offline corruption cases for the runtime lifecycle reconstruction; no database."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from rfq_copilot import quote_approval as approval
from rfq_copilot.api.auth import Principal
from rfq_copilot.canonical import fingerprint
from rfq_copilot.integrity import IntegrityVerificationError
from rfq_copilot.runtime_quote_lifecycle import verify_lifecycles

AT = datetime(2026, 11, 18, 15, tzinfo=UTC)
Q, R, C = "QUOTE-RT", "RFQ-RT", "CUST-001"


def history_world(target="released"):
    quote = dict(
        quote_id=Q,
        rfq_id=R,
        status=target,
        current_version=1,
        row_version=1,
        created_by="SERVICE",
        created_at=AT,
        **dict.fromkeys(
            (
                "submitted_by",
                "approved_version",
                "approver_id",
                "approval_timestamp",
                "rejection_reason",
                "released_by",
                "released_at",
                "legacy_idempotency_key",
            )
        ),
    )
    version = dict(
        quote_id=Q,
        quote_version_id=f"{Q}-V1",
        version=1,
        is_current=True,
        approval_valid=False,
        rfq_id=R,
        created_by="SERVICE",
        created_at=AT,
        approval_rule_version="approval-v1.2",
        calculation_record_id="CALC-RT",
        rfq_source_sha256="a" * 64,
        requested_delivery_date=None,
        source_snapshot_versions={"inventory": "I-1", "capacity": "C-1"},
    )
    extras = dict(
        quotes=[quote],
        quote_versions=[version],
        quote_line_items=[],
        pricing_calculations=[
            dict(
                calculation_record_id="CALC-RT",
                rfq_id=R,
                created_by="SERVICE",
                created_at=AT,
                source_snapshot_versions=version["source_snapshot_versions"],
            )
        ],
        rfqs=[dict(rfq_id=R, customer_id=C)],
        approval_requests=[],
        approval_history=[],
        audit_events=[],
        idempotency_records=[],
    )
    users = {
        u: dict(active=True, role="sales_manager", authentication_subject=f"human|{u}")
        for u in ("REQUESTER", "APPROVER", "RELEASER")
    }
    authority = dict(
        user_customer_bindings=[
            dict(user_id=u, customer_id=C, created_at=AT, revoked_at=None) for u in users
        ],
        role_permissions=[
            dict(role="sales_manager", permission=op, allowed=True) for op in approval.AUDIT
        ],
    )
    if target == "draft":
        quote["status"] = "draft"
        return extras, users, authority
    request = dict(
        approval_request_id="APR-RT",
        quote_id=Q,
        quote_version=1,
        required_role="sales_manager",
        approval_rule_version="approval-v1.2",
        requested_by="REQUESTER",
        requested_at=AT + timedelta(seconds=1),
        status="pending",
        resolved_by=None,
        resolved_at=None,
    )
    extras["approval_requests"].append(request)
    actions = ["submitted"]
    if target in {"approved", "released", "rejected"}:
        actions.append("rejected" if target == "rejected" else "approved")
    if target == "released":
        actions.append("released")
    actors = {
        "submitted": "REQUESTER",
        "approved": "APPROVER",
        "rejected": "APPROVER",
        "released": "RELEASER",
    }
    for index, action in enumerate(actions, 1):
        actor, operation = (
            actors[action],
            approval.SUBMIT
            if action == "submitted"
            else {
                "approved": approval.APPROVE,
                "rejected": approval.REJECT,
                "released": approval.RELEASE,
            }[action],
        )
        at, extra = AT + timedelta(seconds=index), {}
        data = dict(
            approval_request_id="APR-RT",
            assessment_fingerprint="b" * 64,
            classification="policy_conditions_met",
        )
        reason = "classification=policy_conditions_met"
        if action == "submitted":
            quote.update(status="pending_approval", submitted_by=actor)
            data["reviews"] = ["human_approval"]
        elif action in {"approved", "rejected"}:
            request.update(status=action, resolved_by=actor, resolved_at=at)
            quote["status"] = action
            if action == "approved":
                attested = dict(
                    rfq_source_sha256="a" * 64, requested_delivery_date=None, no_contradictions=True
                )
                reason = approval.ATTESTED + fingerprint(attested)
                data.update(attestation=attested, manager_review_reasons=[])
                extra = {"attestation": attested}
                version["approval_valid"] = True
                quote.update(approved_version=1, approver_id=actor, approval_timestamp=at)
            else:
                import hashlib

                reason = "Needs revision"
                extra = {"reason_sha256": hashlib.sha256(reason.encode()).hexdigest()}
                data.update(extra)
                quote["rejection_reason"] = reason
        else:
            reason = "released"
            data.update(approver_id="APPROVER", approval_rule_version="approval-v1.2")
            quote.update(status="released", released_by=actor, released_at=at)
        quote["row_version"] += 1
        extras["approval_history"].append(
            dict(
                approval_id=f"APH-{index}",
                quote_id=Q,
                quote_version=1,
                action=action,
                actor_id=actor,
                action_at=at,
                reason=reason,
                approval_request_id="APR-RT",
            )
        )
        extras["audit_events"].append(
            dict(
                event_id=f"EVENT-{index}",
                quote_id=Q,
                quote_version=1,
                rfq_id=R,
                actor_id=actor,
                customer_id=C,
                occurred_at=at,
                agent_name=None,
                correlation_id=f"step-{index}",
                operation=approval.AUDIT[operation],
                outcome="success",
                event_data=data,
            )
        )
        result = approval.QuoteDecision(
            quote_id=Q,
            rfq_id=R,
            customer_id=C,
            version=1,
            action=action,
            status=quote["status"],
            approval_request_id="APR-RT",
            quote_row_version=quote["row_version"],
            actor_id=actor,
            assessment_fingerprint="b" * 64,
        ).to_json()
        extras["idempotency_records"].append(
            dict(
                receipt_id=f"RECEIPT-{index}",
                actor_id=actor,
                customer_id=C,
                committed_at=at,
                correlation_id=f"step-{index}",
                operation=operation,
                idempotency_key="key",
                request_sha256=approval.decision_digest(
                    Principal(C, user_id=actor), Q, 1, operation, extra
                ),
                result_json=result,
            )
        )
    return extras, users, authority


@pytest.mark.parametrize(
    "status", ["draft", "pending_approval", "approved", "rejected", "released"]
)
def test_supported_histories_reconstruct_original_draft_without_altering_rows(status):
    extras, users, authority = history_world(status)
    before = deepcopy(extras)
    explained = set()
    initial = verify_lifecycles(extras, users, authority, {}, explained)
    assert initial["quotes"][0]["status"] == "draft"
    assert initial["quotes"][0]["row_version"] == 1
    assert initial["quote_versions"][0]["approval_valid"] is False
    assert extras == before
    assert explained == {e["event_id"] for e in extras["audit_events"]}
    # A replay adds no rows; another audit sees exactly the same explanation.
    assert verify_lifecycles(extras, users, authority, {}, set()) == initial


@pytest.mark.parametrize(
    "table,field,value",
    [
        ("quotes", "status", "approved"),
        ("quotes", "current_version", 2),
        ("quotes", "row_version", 9),
        ("quotes", "released_by", "APPROVER"),
        ("quotes", "released_at", None),
        ("quotes", "approved_version", 9),
        ("quote_versions", "is_current", False),
        ("quote_versions", "approval_valid", False),
        ("approval_requests", "quote_version", 2),
        ("approval_requests", "resolved_by", "REQUESTER"),
        ("approval_history", "actor_id", "APPROVER"),
        ("approval_history", "approval_request_id", "APR-FORGED"),
        ("audit_events", "actor_id", "REQUESTER"),
        ("audit_events", "quote_version", 7),
        ("idempotency_records", "actor_id", "REQUESTER"),
        ("idempotency_records", "request_sha256", "0" * 64),
    ],
)
def test_corruption_is_refused(table, field, value):
    extras, users, authority = history_world()
    # Alter the final row for event/history/receipt tables.
    extras[table][-1][field] = value
    with pytest.raises(IntegrityVerificationError):
        verify_lifecycles(extras, users, authority, {}, set())


@pytest.mark.parametrize(
    "table", ["approval_history", "approval_requests", "audit_events", "idempotency_records"]
)
def test_missing_or_duplicate_records_fail(table):
    extras, users, authority = history_world()
    removed = extras[table].pop()
    with pytest.raises(IntegrityVerificationError):
        verify_lifecycles(extras, users, authority, {}, set())
    extras[table].extend([removed, deepcopy(removed)])
    with pytest.raises(IntegrityVerificationError):
        verify_lifecycles(extras, users, authority, {}, set())


@pytest.mark.parametrize(
    "corruption",
    [
        "attestation",
        "numeric_attestation",
        "early_release",
        "binding",
        "grant",
        "machine_actor",
        "self_approval",
    ],
)
def test_security_relationships_fail_closed(corruption):
    extras, users, authority = history_world()
    if corruption == "attestation":
        extras["audit_events"][1]["event_data"]["attestation"]["no_contradictions"] = False
    elif corruption == "numeric_attestation":
        extras["audit_events"][1]["event_data"]["attestation"]["no_contradictions"] = 1
    elif corruption == "early_release":
        extras["approval_history"][-1]["action_at"] = AT
    elif corruption == "binding":
        authority["user_customer_bindings"] = []
    elif corruption == "grant":
        authority["role_permissions"] = []
    elif corruption == "machine_actor":
        users["APPROVER"]["authentication_subject"] = "service|forged"
    else:
        extras["quotes"][0]["created_by"] = "APPROVER"
    with pytest.raises(IntegrityVerificationError):
        verify_lifecycles(extras, users, authority, {}, set())


def test_causal_receipt_order_allows_overlapping_transaction_timestamps():
    extras, users, authority = history_world()
    # A release transaction may have begun before the approval transaction and then
    # waited on its quote lock. Its timestamp is its start time, not commit order.
    at = AT + timedelta(seconds=1, microseconds=500)
    extras["approval_history"][-1]["action_at"] = at
    extras["audit_events"][-1]["occurred_at"] = at
    extras["idempotency_records"][-1]["committed_at"] = at
    extras["quotes"][0]["released_at"] = at
    verify_lifecycles(extras, users, authority, {}, set())
    extras["idempotency_records"][-1]["result_json"]["quote_row_version"] = 3
    with pytest.raises(IntegrityVerificationError):
        verify_lifecycles(extras, users, authority, {}, set())
