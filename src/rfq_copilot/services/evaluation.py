"""RFQ business evaluation: the quotation plus the customer and commercial gates.

Combines the multi-part quotation with the verified customer's account status
and credit position under the pinned approval policy (approval-v1.1 or v1.2), and returns the
business outcome: whether a draft may be created, which review is required and why.

It decides; it executes nothing. No draft is persisted, no approval is requested or
granted, nothing is released. Every release stays a human action: that is an architectural
invariant enforced in code, not a policy setting. A policy that would let an agent release
or approve a quotation is rejected, and quote_release_authorized_for_agent is always False.
Workflow failures (retries, outages) belong to the orchestration layer, and document
contradictions are detected by Intake; they arrive here as an input.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.canonical import fingerprint
from rfq_copilot.entities import Part
from rfq_copilot.models import Customer
from rfq_copilot.repositories import RuleVersionDetails, get_customer, get_rfq
from rfq_copilot.seeding.transform import decimal_value
from rfq_copilot.services.manufacturing import EXACT
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

# Invariant, not configuration: no policy value can make this True.
AGENT_MAY_RELEASE_QUOTE = False
# Actions every approval policy must forbid to agents.
AGENT_MUST_NOT = frozenset({"approve_quote", "release_quote"})
# engineering_review_conditions this service knows. The two gate-covered ones are
# enforced by a stronger gate that already outranks engineering review: unsupported or
# incomplete geometry is declined or sent for clarification, and a specification conflict
# (Intake's contradictions) stops evaluation. borderline_bend_radius is evaluated when the
# policy states its threshold (approval-v1.2, TD-004): a bent line is borderline when
# minimum <= stated radius < ratio x minimum; an unstated radius is not borderline. Under a
# policy without the threshold (approval-v1.1) it cannot be evaluated, and when it could
# apply (a line is bent) it is reported in unevaluated_review_conditions, never as passed.
GATE_COVERED_CONDITIONS = frozenset({"unsupported_or_unknown_geometry", "unresolved_spec_conflict"})
THRESHOLD_CONDITIONS = frozenset({"borderline_bend_radius"})
EVALUATION_FINGERPRINT_VERSION = "rfq-evaluation-v1"


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
    unevaluated_review_conditions: tuple[str, ...]
    borderline_bend_lines: tuple[int, ...]  # line numbers needing engineering review
    parts: tuple[Part, ...]
    line_numbers: tuple[int, ...]
    # sha256 over every input and result above (canonical JSON); see evaluation_fingerprint.
    evaluation_fingerprint: str


def _approval(rules: RuleVersionDetails) -> None:
    if not isinstance(rules, RuleVersionDetails) or rules.rule_kind != "approval":
        raise ValueError("Business evaluation requires an approval rule version.")


def require_human_release(approval: RuleVersionDetails) -> None:
    """Reject any approval policy that would let an agent approve or release a quotation."""
    _approval(approval)
    document, rule = approval.document, approval.rule_version
    forbidden = document.get("agent_forbidden_actions")
    if isinstance(forbidden, str) or not isinstance(forbidden, Sequence):
        raise ValueError(f"{rule}.agent_forbidden_actions must be a list.")
    missing = AGENT_MUST_NOT - set(forbidden)
    if missing:
        raise ValueError(
            f"{rule} would let an agent {sorted(missing)}; human release is mandatory."
        )
    if document.get("human_release_approval_required") is not True:
        raise ValueError(f"{rule}.human_release_approval_required must be true.")


def engineering_conditions(approval: RuleVersionDetails) -> frozenset[str]:
    """The policy's engineering-review conditions; an unknown one fails closed."""
    declared = approval.document.get("engineering_review_conditions", ())
    if isinstance(declared, str) or not isinstance(declared, Sequence):
        raise ValueError(f"{approval.rule_version}.engineering_review_conditions must be a list.")
    unknown = set(declared) - GATE_COVERED_CONDITIONS - THRESHOLD_CONDITIONS
    if unknown:
        raise ValueError(
            f"{approval.rule_version} has engineering review conditions not enforced: "
            f"{sorted(unknown)}."
        )
    return frozenset(declared)


def borderline_ratio(approval: RuleVersionDetails) -> Decimal | None:
    """The policy's borderline-bend ratio (> 1), or None when the policy states none."""
    rule = approval.rule_version
    thresholds = approval.document.get("engineering_review_thresholds")
    if thresholds is None:
        return None
    if not hasattr(thresholds, "keys") or set(thresholds) - THRESHOLD_CONDITIONS:
        raise ValueError(f"{rule}.engineering_review_thresholds names unknown conditions.")
    if set(thresholds) - engineering_conditions(approval):
        raise ValueError(f"{rule} sets a threshold for a condition it does not declare.")
    setting = thresholds.get("borderline_bend_radius")
    if setting is None:
        return None
    ratio = decimal_value(
        setting.get("radius_below_minimum_ratio") if hasattr(setting, "get") else None,
        f"{rule}.engineering_review_thresholds.borderline_bend_radius",
        12,
        6,
    )
    if ratio <= 1:
        raise ValueError(f"{rule} borderline bend ratio must be greater than 1.")
    return ratio


def borderline_bend_lines(quotation: Quotation, ratio: Decimal) -> tuple[int, ...]:
    """Lines whose stated bend radius is at least the minimum but below ratio x minimum."""
    lines = []
    for line, part in zip(quotation.lines, quotation.parts, strict=True):
        minimum = line.manufacturing.min_bend_radius_mm
        if minimum is None or part.bend_radius_mm is None:
            continue
        with localcontext(EXACT):
            if minimum <= part.bend_radius_mm < minimum * ratio:
                lines.append(line.line_number)
    return tuple(lines)


def evaluation_fingerprint(fields: dict[str, object]) -> str:
    """Deterministic identity of one evaluation: parts, customer facts, rule fingerprints,
    snapshot selection, horizon and every result. Unlike calculation_id (parts and pricing
    version only) it changes whenever anything that influenced the outcome changes."""
    return fingerprint({"fingerprint_version": EVALUATION_FINGERPRINT_VERSION, **fields})


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
    *,
    parts: Sequence[Part] | None = None,
    line_numbers: Sequence[int] | None = None,
) -> RfqEvaluation:
    """Pure gate logic. quotation is None only when contradictions stopped evaluation;
    then parts (and line_numbers) identify what was asked, for the fingerprint."""
    require_human_release(approval_rules)
    conditions = engineering_conditions(approval_rules)
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

    if quotation is not None:
        if parts is not None and tuple(parts) != quotation.parts:
            raise ValueError("The parts differ from the quotation's parts.")
        parts, line_numbers = quotation.parts, quotation.line_numbers
    else:
        parts = tuple(parts or ())
        line_numbers = (
            tuple(range(1, len(parts) + 1)) if line_numbers is None else tuple(line_numbers)
        )
        if parts:
            validate_lines(parts, line_numbers)
    ratio = borderline_ratio(approval_rules)  # validated even when unused
    bent = quotation is not None and any(
        "bend" in line.manufacturing.operations for line in quotation.lines
    )
    unevaluated = (
        ("borderline_bend_radius",)
        if bent and "borderline_bend_radius" in conditions and ratio is None
        else ()
    )
    borderline = borderline_bend_lines(quotation, ratio) if priced and ratio is not None else ()

    if contradictions or missing:
        outcome = "clarification_required"
    elif unsupported:
        outcome = "unsupported_declined"
    elif action == "blocked_release":
        outcome = "customer_hold_blocked_release"
    elif capacity_problem or late or borderline:
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
        ("BORDERLINE_BEND_RADIUS", bool(borderline)),  # approval-v1.2; ranks last
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

    result = dict(
        outcome=outcome,
        escalation_reason=reason,
        draft_creation_allowed=priced and not contradictions and not findings,
        manager_review_required=bool(review),
        manager_review_reasons=tuple(review),
        human_release_approval_required=True,  # require_human_release enforced it
        approval_must_be_human=True,
        quote_release_authorized_for_agent=AGENT_MAY_RELEASE_QUOTE,
        customer=standing,
        contradictions=contradictions,
        quotation=quotation,
        approval_rule_version=approval_rules.rule_version,
        approval_rule_fingerprint=approval_rules.fingerprint,
        unevaluated_review_conditions=unevaluated,
        borderline_bend_lines=borderline,
        parts=parts,
        line_numbers=line_numbers,
    )
    return RfqEvaluation(**result, evaluation_fingerprint=evaluation_fingerprint(result))


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
    return evaluate_outcome(
        details.customer,
        quotation,
        approval_rules,
        contradictions,
        parts=parts,
        line_numbers=numbers,
    )


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
