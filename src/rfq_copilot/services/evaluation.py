"""RFQ business evaluation: the quotation plus the customer and commercial gates.

Combines the multi-part quotation with the verified customer's account status
and credit position under the pinned approval policy (approval-v1.1), and returns the
business outcome: whether a draft may be created, which review is required and why.

It decides; it executes nothing. No draft is persisted, no approval is requested or
granted, nothing is released. Every release stays a human action (policy and contract).
Workflow failures (retries, outages) belong to the orchestration layer, and document
contradictions are detected by Intake; they arrive here as an input.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.entities import Part
from rfq_copilot.models import Customer
from rfq_copilot.repositories import RuleVersionDetails, get_customer, get_rfq
from rfq_copilot.seeding.transform import decimal_value
from rfq_copilot.services.quotation import Quotation, quote_parts, validate_lines

Outcome = Literal[
    "clarification_required",
    "unsupported_declined",
    "customer_hold_blocked_release",
    "engineering_review_required",
    "commercial_review_required",
    "quote_draft_ready_for_approval",
]
CustomerAction = Literal["eligible", "commercial_review", "blocked_release"]


@dataclass(frozen=True)
class CustomerStanding:
    customer_id: str
    account_status: str
    action: CustomerAction
    credit_limit_usd: Decimal
    credit_exposure_usd: Decimal
    credit_breach: bool | None  # None when nothing was priced


@dataclass(frozen=True)
class RfqEvaluation:
    outcome: Outcome
    escalation_reason: str | None
    draft_creation_allowed: bool
    manager_review_required: bool
    manager_review_reasons: tuple[str, ...]
    human_release_approval_required: bool
    approval_must_be_human: bool
    quote_release_authorized_for_agent: bool
    customer: CustomerStanding
    contradictions: tuple[str, ...]
    quotation: Quotation | None
    approval_rule_version: str
    approval_rule_fingerprint: str


def _approval(rules: RuleVersionDetails) -> None:
    if not isinstance(rules, RuleVersionDetails) or rules.rule_kind != "approval":
        raise ValueError("Business evaluation requires an approval rule version.")


def customer_action(status: str, approval: RuleVersionDetails) -> CustomerAction:
    """Account status under the approval policy; an unknown status fails closed."""
    _approval(approval)
    if status in approval.document["blocked_customer_statuses"]:
        return "blocked_release"
    if status in approval.document["commercial_review_customer_statuses"]:
        return "commercial_review"
    if status == "active":
        return "eligible"
    raise ValueError(f"Account status {status!r} is not covered by {approval.rule_version}.")


def review_threshold(approval: RuleVersionDetails) -> Decimal:
    """The manager-review quote total; the policy states it twice and both must agree."""
    document, rule = approval.document, approval.rule_version
    policy = decimal_value(
        document["manager_review_policy"]["quote_total_at_or_above_usd"],
        f"{rule}.manager_review_policy.quote_total_at_or_above_usd",
        18,
        2,
    )
    summary = decimal_value(
        document["manager_review_threshold_usd"], f"{rule}.manager_review_threshold_usd", 18, 2
    )
    if policy != summary or policy < 0:
        raise ValueError(f"{rule} states inconsistent manager-review thresholds.")
    return policy


def evaluate_outcome(
    customer: Customer,
    quotation: Quotation | None,
    approval_rules: RuleVersionDetails,
    contradictions: Sequence[str] = (),
) -> RfqEvaluation:
    """Pure gate logic. quotation is None only when contradictions stopped evaluation."""
    _approval(approval_rules)
    contradictions = tuple(contradictions)
    if not all(isinstance(c, str) and c.strip() for c in contradictions):
        raise ValueError("Contradiction findings must be nonblank text.")
    if quotation is None and not contradictions:
        raise ValueError("Without contradictions, the evaluation needs a quotation.")
    document = approval_rules.document
    action = customer_action(customer.account_status, approval_rules)
    priced = quotation is not None and quotation.result == "quoted"
    total = quotation.quote_total_usd if priced else None
    breach = (
        None if total is None else customer.credit_exposure_usd + total > customer.credit_limit_usd
    )
    standing = CustomerStanding(
        customer.customer_id,
        customer.account_status,
        action,
        customer.credit_limit_usd,
        customer.credit_exposure_usd,
        breach,
    )
    findings = set(quotation.findings) if quotation is not None else set()
    capacity_problem = bool(findings & {"CAPACITY_OUT_OF_HORIZON", "CAPACITY_DATA_MISSING"})
    late = "DELIVERY_DATE_INFEASIBLE" in findings
    missing = quotation is not None and quotation.result == "needs_clarification"
    unsupported = quotation is not None and quotation.result == "not_manufacturable"

    if contradictions or missing:
        outcome = "clarification_required"
    elif unsupported:
        outcome = "unsupported_declined"
    elif action == "blocked_release":
        outcome = "customer_hold_blocked_release"
    elif capacity_problem or late:
        outcome = "engineering_review_required"
    elif action == "commercial_review" or breach:
        outcome = "commercial_review_required"
    else:
        outcome = "quote_draft_ready_for_approval"

    # The reason has its own precedence (dataset labels), distinct from the outcome's.
    reasons = [
        ("CONTRADICTORY_DOCUMENTS", bool(contradictions)),
        ("MISSING_CRITICAL_SPECIFICATION", missing),
        ("ENGINEERING_UNSUPPORTED", unsupported),
        # The generator keys this on its scenario category; at runtime the status decides.
        ("CUSTOMER_ACCOUNT_RESTRICTION", action != "eligible"),
        ("DELIVERY_DATE_INFEASIBLE", late),
        ("CREDIT_LIMIT_EXCEEDED", bool(breach)),
        ("CAPACITY_OUT_OF_HORIZON", "CAPACITY_OUT_OF_HORIZON" in findings),
        ("CAPACITY_DATA_MISSING", "CAPACITY_DATA_MISSING" in findings),
    ]
    reason = next((code for code, applies in reasons if applies), None)

    also = set(document["manager_review_policy"]["also_required_for"])
    unknown = also - {"credit_limit_breach", "customer_not_active"}
    if unknown:
        raise ValueError(
            f"{approval_rules.rule_version} has review conditions not enforced: {sorted(unknown)}."
        )
    review = []
    if total is not None and total >= review_threshold(approval_rules):
        review.append("QUOTE_TOTAL_AT_OR_ABOVE_THRESHOLD")
    if breach and "credit_limit_breach" in also:
        review.append("CREDIT_LIMIT_BREACH")
    if action != "eligible" and "customer_not_active" in also:
        review.append("CUSTOMER_NOT_ACTIVE")

    human_release = document["human_release_approval_required"]
    if type(human_release) is not bool:
        raise ValueError(
            f"{approval_rules.rule_version}.human_release_approval_required must be boolean."
        )
    return RfqEvaluation(
        outcome=outcome,
        escalation_reason=reason,
        draft_creation_allowed=priced and not contradictions and not findings,
        manager_review_required=bool(review),
        manager_review_reasons=tuple(review),
        human_release_approval_required=human_release,
        approval_must_be_human=True,
        quote_release_authorized_for_agent="release_quote"
        not in document["agent_forbidden_actions"]
        and not human_release,
        customer=standing,
        contradictions=contradictions,
        quotation=quotation,
        approval_rule_version=approval_rules.rule_version,
        approval_rule_fingerprint=approval_rules.fingerprint,
    )


async def evaluate_rfq(
    session: AsyncSession,
    parts: Sequence[Part],
    *,
    customer_id: str,
    manufacturing_rules: RuleVersionDetails,
    pricing_rules: RuleVersionDetails,
    approval_rules: RuleVersionDetails,
    horizon_start: date,
    line_numbers: Sequence[int] | None = None,
    requested_delivery_date: date | None = None,
    contradictions: Sequence[str] = (),
    inventory_snapshot: str | None = None,
    capacity_snapshot: str | None = None,
) -> RfqEvaluation | None:
    """Evaluate an RFQ's lines for a verified customer; None if the customer is unknown."""
    _approval(approval_rules)
    parts = tuple(parts)
    numbers = tuple(range(1, len(parts) + 1)) if line_numbers is None else tuple(line_numbers)
    validate_lines(parts, numbers)
    details = await get_customer(session, customer_id)
    if details is None:
        return None
    quotation = None
    if not contradictions:  # contradictory specifications are not priced (dataset policy)
        quotation = await quote_parts(
            session,
            parts,
            line_numbers=numbers,
            manufacturing_rules=manufacturing_rules,
            pricing_rules=pricing_rules,
            horizon_start=horizon_start,
            requested_delivery_date=requested_delivery_date,
            inventory_snapshot=inventory_snapshot,
            capacity_snapshot=capacity_snapshot,
        )
    return evaluate_outcome(details.customer, quotation, approval_rules, contradictions)


async def evaluate_stored_rfq(
    session: AsyncSession,
    rfq_id: str,
    *,
    customer_id: str,
    manufacturing_rules: RuleVersionDetails,
    pricing_rules: RuleVersionDetails,
    approval_rules: RuleVersionDetails,
    horizon_start: date,
    requested_delivery_date: date | None = None,
    contradictions: Sequence[str] = (),
    inventory_snapshot: str | None = None,
    capacity_snapshot: str | None = None,
) -> RfqEvaluation | None:
    """Evaluate a stored RFQ's runtime lines; None if absent or not this customer's."""
    _approval(approval_rules)
    details = await get_rfq(session, rfq_id, customer_id=customer_id)
    if details is None:
        return None
    if not details.runtime_lines:
        raise ValueError(f"RFQ {rfq_id} has no runtime lines to evaluate.")
    return await evaluate_rfq(
        session,
        [Part.from_line(line) for line in details.runtime_lines],
        line_numbers=[line.line_number for line in details.runtime_lines],
        customer_id=customer_id,
        manufacturing_rules=manufacturing_rules,
        pricing_rules=pricing_rules,
        approval_rules=approval_rules,
        horizon_start=horizon_start,
        requested_delivery_date=requested_delivery_date,
        contradictions=contradictions,
        inventory_snapshot=inventory_snapshot,
        capacity_snapshot=capacity_snapshot,
    )
