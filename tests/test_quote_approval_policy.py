"""Day 53 approval-policy assessment: offline checks (no database, network or model).

The policy mapping is exercised on real evaluations from ``evaluate_outcome`` under the
registered approval-v1.2 document (and approval-v1.1 for an unevaluable condition), using
the existing offline quotation fixtures. Nothing here re-derives a threshold.
"""

import csv
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evaluation_service import APPROVAL, customer  # APPROVAL is approval-v1.1
from test_hardening_services import APPROVAL_V12, FLAT, bent, minimum
from test_quotation_service import FIRST, quote
from test_quote_drafts import AGENT_MODULES, INITIAL, SRC, called_names, imported_modules

import rfq_copilot.quote_approval_policy as policy_module
from rfq_copilot.api.auth import Principal
from rfq_copilot.quote_approval_policy import (
    QuoteApprovalAssessment,
    assess_quote_approval,
    binding_blockers,
    classify,
    policy_requirements,
)
from rfq_copilot.quote_drafts import DraftRefused
from rfq_copilot.services.evaluation import evaluate_outcome, review_threshold

D = Decimal
DATASET = SRC.parents[1] / "data" / "rfq_synthetic_data"
APPROVERS, ENGINEERS = ("sales_manager",), ("manufacturing_engineer",)
PRINCIPAL = Principal("CUST-001", frozenset(), user_id="USER-001")
# Bent, but clearly above 1.25 x the minimum radius: never borderline under approval-v1.2.
CLEAR = bent((minimum() * D("2")).quantize(D("0.001")))


def with_total(total):
    return replace(CLEAR, quote_total_usd=D(total))


def assess(q=None, cust=None, approval=APPROVAL_V12, approvers=APPROVERS, engineers=ENGINEERS):
    evaluation = evaluate_outcome(cust or customer(), q or CLEAR, approval)
    return policy_requirements(evaluation, approvers, engineers)


def kinds(requirements):
    return {r.kind: r for r in requirements.reviews}


# --- Verified approval-v1.2 values (never assumed) --------------------------------------------


def test_the_registered_policy_states_the_verified_values():
    document = APPROVAL_V12.document
    assert review_threshold(APPROVAL_V12) == D("7500.00")  # both statements agree
    assert set(document["manager_review_policy"]["also_required_for"]) == {"credit_limit_breach", "customer_not_active"}  # fmt: skip
    assert set(document["blocked_customer_statuses"]) == {"on_hold", "inactive"}
    assert set(document["commercial_review_customer_statuses"]) == {"pending_verification", "credit_restricted"}  # fmt: skip
    assert "borderline_bend_radius" in document["engineering_review_conditions"]
    assert document["human_release_approval_required"] is True
    assert {"approve_quote", "release_quote"} <= set(document["agent_forbidden_actions"])


# --- Commercial thresholds -------------------------------------------------------------------


@pytest.mark.parametrize(
    "total,manager",
    [("7499.99", False), ("7500.00", True), ("9000", True)],
)
def test_manager_review_at_or_above_the_threshold(total, manager):
    requirements = assess(with_total(total))
    reviews = kinds(requirements)
    assert reviews["human_approval"].roles == APPROVERS  # every quotation needs a human
    assert reviews["human_approval"].permission == "approve_quote"
    if manager:
        assert reviews["manager_review"].reasons == ("QUOTE_TOTAL_AT_OR_ABOVE_THRESHOLD",)
        assert reviews["manager_review"].roles == APPROVERS
        assert classify(requirements, ()) == "reviews_required"
    else:
        assert "manager_review" not in reviews
        assert classify(requirements, ()) == "policy_conditions_met"
    assert requirements.blockers == requirements.unresolved == ()


def test_commercial_review_status_needs_manager_review_not_rejection():
    requirements = assess(cust=customer(status="credit_restricted"))
    assert requirements.outcome == "commercial_review_required"
    assert requirements.commercial_conditions == ("CUSTOMER_ACCOUNT_RESTRICTION",)
    assert kinds(requirements)["manager_review"].reasons == ("CUSTOMER_NOT_ACTIVE",)
    assert requirements.blockers == ()
    assert classify(requirements, ()) == "reviews_required"


def test_credit_limit_breach_needs_manager_review():
    requirements = assess(with_total("600"), cust=customer(limit="1000", exposure="500"))
    assert requirements.commercial_conditions == ("CREDIT_LIMIT_EXCEEDED",)
    assert kinds(requirements)["manager_review"].reasons == ("CREDIT_LIMIT_BREACH",)
    assert classify(requirements, ()) == "reviews_required"
    within = assess(with_total("500"), cust=customer(limit="1000", exposure="500"))  # == limit
    assert within.commercial_conditions == () and "manager_review" not in kinds(within)


@pytest.mark.parametrize("status", ["on_hold", "inactive"])
def test_blocked_customer_statuses_block_review(status):
    requirements = assess(cust=customer(status=status))
    assert requirements.outcome == "customer_hold_blocked_release"
    assert requirements.blockers == ("CUSTOMER_ACCOUNT_RESTRICTION",)
    assert kinds(requirements)["manager_review"].reasons == ("CUSTOMER_NOT_ACTIVE",)
    assert classify(requirements, ()) == "commercially_blocked"


# --- Engineering -----------------------------------------------------------------------------


def test_borderline_bend_needs_engineering_review():
    requirements = assess(bent(minimum()))
    review = kinds(requirements)["engineering_review"]
    assert (review.permission, review.roles, review.reasons) == ("engineering_review", ENGINEERS, ("BORDERLINE_BEND_RADIUS",))  # fmt: skip
    assert requirements.engineering_review_lines == (1,)
    assert classify(requirements, ()) == "reviews_required"
    clear = assess(bent((minimum() * D("1.25")).quantize(D("0.001"))))  # the boundary is clear
    assert "engineering_review" not in kinds(clear)


def test_engineering_and_manager_reviews_are_both_reported():
    requirements = assess(replace(bent(minimum()), quote_total_usd=D("9000")))
    assert set(kinds(requirements)) == {"human_approval", "manager_review", "engineering_review"}
    assert classify(requirements, ()) == "reviews_required"


def test_capacity_and_delivery_findings_are_engineering_reasons():
    findings = ("DELIVERY_DATE_INFEASIBLE", "CAPACITY_DATA_MISSING")
    requirements = assess(replace(CLEAR, findings=findings))
    assert requirements.outcome == "engineering_review_required"
    assert kinds(requirements)["engineering_review"].reasons == findings


def test_unsupported_geometry_is_not_reviewable():
    requirements = assess(bent(minimum() - D("0.01")))
    assert (requirements.outcome, requirements.blockers) == ("unsupported_declined", ("ENGINEERING_UNSUPPORTED",))  # fmt: skip
    assert classify(requirements, ()) == "unsupported"


def test_contradictions_need_clarification():
    evaluation = evaluate_outcome(customer(), None, APPROVAL_V12, ("QUANTITY_CONFLICT",), parts=[FIRST])  # fmt: skip
    requirements = policy_requirements(evaluation, APPROVERS, ENGINEERS)
    assert requirements.blockers == ("CONTRADICTORY_DOCUMENTS",)
    assert classify(requirements, ()) == "clarification_required"


# --- Unresolved conditions are never treated as satisfied --------------------------------------


def test_an_unevaluable_policy_condition_stays_unresolved():
    requirements = assess(bent(minimum()), approval=APPROVAL)  # no borderline threshold
    assert requirements.unresolved == ("borderline_bend_radius",)
    assert classify(requirements, ()) == "unresolved_conditions"
    flat = assess(quote((FLAT,)), approval=APPROVAL)  # nothing bent: nothing to evaluate
    assert flat.unresolved == ()


def test_missing_reviewer_grants_are_unresolved():
    assert assess(approvers=()).unresolved == ("NO_APPROVER_ROLE_GRANTED",)
    no_engineer = assess(bent(minimum()), engineers=())
    assert no_engineer.unresolved == ("NO_ENGINEERING_REVIEWER_GRANTED",)
    assert classify(no_engineer, ()) == "unresolved_conditions"


def test_tampered_policy_content_fails_closed():
    inconsistent = replace(APPROVAL_V12, document={**APPROVAL_V12.document, "manager_review_threshold_usd": "1.00"})  # fmt: skip
    with pytest.raises(ValueError, match="inconsistent manager-review thresholds"):
        evaluate_outcome(customer(), quote((FIRST,)), inconsistent)
    releasing = replace(APPROVAL_V12, document={**APPROVAL_V12.document, "agent_forbidden_actions": []})  # fmt: skip
    with pytest.raises(ValueError, match="human release is mandatory"):
        evaluate_outcome(customer(), quote((FIRST,)), releasing)


# --- Integrity and binding readiness --------------------------------------------------------


def test_integrity_findings_outrank_policy():
    clean = assess()
    assert classify(clean, ("TOTAL_MISMATCH",)) == "integrity_mismatch"
    assert classify(clean, ("EVALUATION_CHANGED",)) == "evaluation_changed"
    assert classify(clean, ("EVALUATION_CHANGED", "QUOTATION_NOT_PRICED")) == "evaluation_changed"


@pytest.mark.parametrize(
    "content",
    ["TOTAL_MISMATCH", "LINE_MISMATCH", "PART_MISMATCH", "COST_BREAKDOWN_MISMATCH",
     "CALCULATION_CHANGED", "SNAPSHOT_MISMATCH", "LEAD_TIME_MISMATCH"],
)  # fmt: skip
def test_a_content_mismatch_is_never_reported_as_a_mere_change(content):
    # Even when today's evaluation also changed, stored financial or structural content
    # that disagrees is an integrity mismatch (and every finding stays a binding blocker).
    findings = ("EVALUATION_CHANGED", content)
    assert classify(assess(), findings) == "integrity_mismatch"
    partial = SimpleNamespace(snapshot_completeness="priced_partial", calculation_id=None, expiry_date=None)  # fmt: skip
    assert binding_blockers(partial, findings)[-2:] == findings


def test_no_module_accepts_an_assessment_as_input():
    """Literal annotations are not enforced at runtime, so authority must never come from a
    caller-built assessment: nothing outside the producing module may name the type."""
    users = [
        path.relative_to(SRC)
        for path in SRC.rglob("*.py")
        if path.name != "quote_approval_policy.py" and "QuoteApprovalAssessment" in path.read_text()
    ]
    assert users == []


def test_no_version_is_ever_ready_for_binding_approval():
    partial = SimpleNamespace(snapshot_completeness="priced_partial", calculation_id=None, expiry_date=None)  # fmt: skip
    assert binding_blockers(partial, ()) == (
        "SNAPSHOT_NOT_COMPLETE", "NO_PERSISTED_CALCULATION", "NO_QUOTE_VALIDITY",
        "FINGERPRINT_ONLY_IN_MUTABLE_AUDIT", "EVALUATION_CONTEXT_NOT_AUTHENTICATED")  # fmt: skip
    created = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)  # a Wednesday: server horizon 11-16
    complete = SimpleNamespace(snapshot_completeness="complete", calculation_id="CALC-1", expiry_date="2027-01-01",
                               created_at=created, horizon_start=date(2026, 11, 9))  # fmt: skip
    # K3: a complete version's evidence is K2-protected and checked by the integrity
    # findings; 54C: a caller-dated horizon is never an authenticated context: never ready.
    assert binding_blockers(complete, ("LINE_MISMATCH",)) == (
        "EVALUATION_CONTEXT_NOT_AUTHENTICATED", "LINE_MISMATCH")  # fmt: skip
    assert binding_blockers(complete, ()) == ("EVALUATION_CONTEXT_NOT_AUTHENTICATED",)
    assert binding_blockers(complete, (), expired=True) == (
        "QUOTE_EXPIRED", "EVALUATION_CONTEXT_NOT_AUTHENTICATED")  # fmt: skip
    # 54C: a server-dated context still needs the human attestation at decision time.
    dated = SimpleNamespace(**{**vars(complete), "horizon_start": date(2026, 11, 16)})
    assert binding_blockers(dated, ()) == ("CONTEXT_ATTESTATION_REQUIRED",)
    assert binding_blockers(dated, (), expired=True) == ("QUOTE_EXPIRED", "CONTEXT_ATTESTATION_REQUIRED")  # fmt: skip
    late = SimpleNamespace(**{**vars(dated), "created_at": datetime(2026, 11, 23, 0, 30, tzinfo=UTC)})  # fmt: skip
    assert binding_blockers(late, ()) == ("EVALUATION_CONTEXT_NOT_AUTHENTICATED",)  # next week
    # A seeded legacy version's dataset expiry_date is not governed validity.
    seeded = SimpleNamespace(snapshot_completeness="priced_partial", calculation_id=None, expiry_date="2026-12-01")  # fmt: skip
    assert "NO_QUOTE_VALIDITY" in binding_blockers(seeded, ())


def test_the_assessment_type_cannot_express_an_approval():
    names = {f.name: f for f in fields(QuoteApprovalAssessment)}
    assert names["ready_for_binding_approval"].default is False
    assert names["approval_decision"].default == "none"
    assert names["released"].default is False
    assert not {n for n in names if "cost" in n or "margin" in n or "breakdown" in n}
    assert "approved" not in names and "approver_id" not in names
    requirements = assess()
    with pytest.raises(FrozenInstanceError):
        requirements.blockers = ()  # type: ignore[misc]


# --- Request validation and approved contracts --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "principal,quote_id,expected",
    [(PRINCIPAL, "QUOTE-1", 0), (PRINCIPAL, "QUOTE-1", True), (PRINCIPAL, "QUOTE-1", "1"),
     (PRINCIPAL, "", 1), (PRINCIPAL, None, 1), (Principal("CUST-001"), "QUOTE-1", 1),
     ("USER-001", "QUOTE-1", 1)],
)  # fmt: skip
async def test_invalid_requests_are_refused_without_a_session(principal, quote_id, expected):
    with pytest.raises(DraftRefused) as refused:
        await assess_quote_approval(None, principal, quote_id, expected_version=expected)
    assert refused.value.reason == "invalid_request"


def test_contracts_come_from_the_dataset_and_the_approved_migration():
    with (DATASET / "master_data/roles_permissions.csv").open(newline="") as handle:
        granted = {(r["role"], r["permission"]) for r in csv.DictReader(handle) if r["allowed"] == "True"}  # fmt: skip
    readers = {role for role, permission in granted if permission == policy_module.PERMISSION}
    assert readers == {"sales_representative", "estimator", "manufacturing_engineer", "sales_manager"}  # fmt: skip
    assert {role for role, p in granted if p == "approve_quote"} == {"sales_manager"}
    assert {role for role, p in granted if p == "engineering_review"} == {"manufacturing_engineer"}
    assert policy_module.PERMISSION != "approve_quote"  # assessing is not approving
    assert "required_role = 'sales_manager'" in INITIAL.read_text()  # approval_requests rule_02
    assert policy_module.APPROVER_ROLE == "sales_manager"


def test_service_never_writes_commits_logs_or_imports_agents():
    service = Path(policy_module.__file__)
    writes = {"commit", "flush", "add", "add_all", "add_receipt", "begin_nested", "update",
              "insert", "delete", "print", "getLogger", "with_for_update"}  # fmt: skip
    assert not called_names(service) & writes  # the share lock lives in quote_versions
    modules = imported_modules(service)
    assert not {m for m in modules if m == "logging" or m.startswith("logging.")}
    assert not modules & AGENT_MODULES


@pytest.mark.parametrize(
    "module",
    ["supervisor.py", "graph.py", "agent_integration.py", "recovery.py", "ownership.py",
     "observability.py", "escalation.py", "quotation_agent.py"],
)  # fmt: skip
def test_advisory_modules_cannot_reach_the_assessment(module):
    assert "rfq_copilot.quote_approval_policy" not in imported_modules(SRC / module)
