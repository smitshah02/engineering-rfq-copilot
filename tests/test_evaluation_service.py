"""Business gates without a database: outcome, escalation reason, reviews, draft gate."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_capacity_service import MONDAY, plenty, reading
from test_inventory_service import PRICING
from test_material_service import RULES
from test_quotation_service import FIRST, SECOND, quote

from rfq_copilot.models import Customer
from rfq_copilot.repositories import RuleVersionDetails
from rfq_copilot.services.evaluation import (
    customer_action,
    evaluate_outcome,
    evaluate_rfq,
    review_threshold,
)

D = Decimal
APPROVAL_FILE = (
    Path(__file__).resolve().parents[1] / "data/rfq_synthetic_data/config/approval_policies.json"
)
APPROVAL = RuleVersionDetails(
    rule_version="approval-v1.1",
    rule_kind="approval",
    sha256=hashlib.sha256(APPROVAL_FILE.read_bytes()).hexdigest(),
    fingerprint="a" * 64,
    document=json.loads(APPROVAL_FILE.read_text(), parse_float=Decimal),
)
NO_CAPACITY = plenty(**{"WC-BND": reading("WC-BND", dict.fromkeys(range(10), "0.5"))})


def customer(status="active", limit="25000", exposure="0"):
    return Customer(
        customer_id="CUST-T",
        account_status=status,
        credit_limit_usd=D(limit),
        credit_exposure_usd=D(exposure),
    )


def evaluate(q=None, cust=None, contradictions=(), approval=APPROVAL):
    q = quote((FIRST,)) if q is None and not contradictions else q
    return evaluate_outcome(cust or customer(), q, approval, contradictions)


def with_total(total, **kwargs):
    return replace(quote((FIRST,), **kwargs), quote_total_usd=D(total))


def test_clean_quote_is_ready_for_approval_and_never_agent_releasable():
    e = evaluate()
    assert (e.outcome, e.escalation_reason, e.draft_creation_allowed) == (
        "quote_draft_ready_for_approval",
        None,
        True,
    )
    assert (e.manager_review_required, e.manager_review_reasons) == (False, ())
    assert (e.human_release_approval_required, e.approval_must_be_human) == (True, True)
    assert e.quote_release_authorized_for_agent is False
    assert (e.approval_rule_version, e.customer.credit_breach) == ("approval-v1.1", False)


@pytest.mark.parametrize("total,review", [("7499.99", False), ("7500.00", True), ("9000", True)])
def test_manager_review_threshold_is_at_or_above(total, review):
    e = evaluate(with_total(total))
    assert e.manager_review_required is review
    assert e.outcome == "quote_draft_ready_for_approval"  # review is separate from the outcome


@pytest.mark.parametrize(
    "exposure,breach", [("22619.26", False), ("22619.27", True)]
)  # quote total 2380.74; limit 25000
def test_credit_breach_is_strictly_above_the_limit(exposure, breach):
    e = evaluate(cust=customer(exposure=exposure))
    assert e.customer.credit_breach is breach
    if breach:
        assert (e.outcome, e.escalation_reason) == (
            "commercial_review_required",
            "CREDIT_LIMIT_EXCEEDED",
        )
        assert e.manager_review_reasons == ("CREDIT_LIMIT_BREACH",)
        assert e.draft_creation_allowed is True  # a commercial review does not block drafting
    else:
        assert e.outcome == "quote_draft_ready_for_approval"


@pytest.mark.parametrize("status", ["pending_verification", "credit_restricted"])
def test_commercial_review_statuses(status):
    e = evaluate(cust=customer(status))
    assert (e.outcome, e.escalation_reason, e.customer.action) == (
        "commercial_review_required",
        "CUSTOMER_ACCOUNT_RESTRICTION",
        "commercial_review",
    )
    assert e.manager_review_reasons == ("CUSTOMER_NOT_ACTIVE",)


@pytest.mark.parametrize("status", ["on_hold", "inactive"])
def test_blocked_statuses_block_release_even_with_capacity_problems(status):
    e = evaluate(quote((FIRST,), hours=NO_CAPACITY), cust=customer(status))
    assert (e.outcome, e.escalation_reason) == (
        "customer_hold_blocked_release",
        "CUSTOMER_ACCOUNT_RESTRICTION",
    )
    assert e.draft_creation_allowed is False  # the capacity finding still blocks the draft


def test_unknown_account_status_fails_closed():
    with pytest.raises(ValueError, match="not covered"):
        customer_action("suspended", APPROVAL)


def test_capacity_and_delivery_problems_need_engineering_review():
    infeasible = evaluate(quote((FIRST,), hours=NO_CAPACITY))
    assert (
        infeasible.outcome,
        infeasible.escalation_reason,
        infeasible.draft_creation_allowed,
    ) == (
        "engineering_review_required",
        "CAPACITY_OUT_OF_HORIZON",
        False,
    )
    late = evaluate(quote((FIRST,), requested=date(2026, 11, 12)))
    assert (late.outcome, late.escalation_reason) == (
        "engineering_review_required",
        "DELIVERY_DATE_INFEASIBLE",
    )


def test_escalation_reason_precedence_differs_from_outcome_precedence():
    breach = customer(exposure="24000")
    # Late delivery outranks the credit breach as the reason ...
    late = evaluate(quote((FIRST,), requested=date(2026, 11, 12)), cust=breach)
    assert (late.outcome, late.escalation_reason) == (
        "engineering_review_required",
        "DELIVERY_DATE_INFEASIBLE",
    )
    # ... but the credit breach outranks a capacity problem (dataset labels).
    infeasible = evaluate(quote((FIRST,), hours=NO_CAPACITY), cust=breach)
    assert (infeasible.outcome, infeasible.escalation_reason) == (
        "engineering_review_required",
        "CREDIT_LIMIT_EXCEEDED",
    )


def test_incomplete_and_unsupported_requests():
    missing = evaluate(quote((FIRST, replace(SECOND, tolerance_mm=None))))
    assert (missing.outcome, missing.escalation_reason, missing.draft_creation_allowed) == (
        "clarification_required",
        "MISSING_CRITICAL_SPECIFICATION",
        False,
    )
    assert (missing.manager_review_required, missing.customer.credit_breach) == (False, None)
    unsupported = evaluate(quote((replace(FIRST, quantity=0),)))
    assert (unsupported.outcome, unsupported.escalation_reason) == (
        "unsupported_declined",
        "ENGINEERING_UNSUPPORTED",
    )


def test_inactive_customer_needs_manager_review_even_without_a_price():
    e = evaluate(quote((replace(FIRST, tolerance_mm=None),)), cust=customer("on_hold"))
    assert e.outcome == "clarification_required"
    assert e.manager_review_reasons == ("CUSTOMER_NOT_ACTIVE",)


def test_contradictions_stop_evaluation_before_pricing():
    e = evaluate(contradictions=("QUANTITY_CONFLICT",))
    assert (e.outcome, e.escalation_reason, e.quotation, e.draft_creation_allowed) == (
        "clarification_required",
        "CONTRADICTORY_DOCUMENTS",
        None,
        False,
    )
    with pytest.raises(ValueError, match="nonblank"):
        evaluate(contradictions=(" ",))
    with pytest.raises(ValueError, match="needs a quotation"):
        evaluate_outcome(customer(), None, APPROVAL)


def test_policy_integrity():
    assert review_threshold(APPROVAL) == D("7500.00")
    split = replace(
        APPROVAL, document={**APPROVAL.document, "manager_review_threshold_usd": "5000"}
    )
    with pytest.raises(ValueError, match="inconsistent"):
        evaluate(approval=split)
    policy = {**APPROVAL.document["manager_review_policy"], "also_required_for": ["new_customer"]}
    unknown = replace(APPROVAL, document={**APPROVAL.document, "manager_review_policy": policy})
    with pytest.raises(ValueError, match="not enforced"):
        evaluate(approval=unknown)
    with pytest.raises(ValueError, match="approval rule"):
        evaluate(approval=PRICING)


def test_evaluation_is_immutable_and_reproducible():
    assert evaluate() == evaluate()
    with pytest.raises(FrozenInstanceError):
        evaluate().outcome = "quote_draft_ready_for_approval"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parts,approval", [([FIRST], PRICING), ([FIRST, FIRST], APPROVAL), ([], APPROVAL)]
)
async def test_arguments_fail_before_sql(parts, approval):
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError):
        await evaluate_rfq(
            session,
            parts,
            customer_id="CUST-001",
            manufacturing_rules=RULES,
            pricing_rules=PRICING,
            approval_rules=approval,
            horizon_start=MONDAY,
        )
    session.scalar.assert_not_awaited()
    session.scalars.assert_not_awaited()
