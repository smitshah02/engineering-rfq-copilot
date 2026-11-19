"""approval policy: what approval of one persisted quotation version requires. Assessment only.

    Principal + quote id + expected current version
        -> assess_quote_approval -> QuoteApprovalAssessment (frozen, non-binding)

It decides nothing on anyone's behalf and writes nothing: no approval, rejection, approval
request, release, audit event or version change. ``ready_for_binding_approval``,
``approval_decision`` and ``released`` are typed ``Literal[False]`` / ``Literal["none"]`` to
document intent only: Python does not enforce annotations, and any caller can construct
this dataclass. The security property is architectural instead. An assessment is output,
never input: no module accepts one (a test enforces it), and integrity must recompute it
itself, inside its own approval transaction and lock, and must never accept an assessment
object, its fingerprint or any serialized copy from a caller, agent or earlier request as
approval authority.

Authority: ``view_assigned_rfqs``, the established read permission for quotes (TD-014),
re-read from the database with the customer binding (as draft persistence/52). Assessing is not
approving, so ``approve_quote`` is neither required nor implied.

Binding: only the quotation's current version is assessed. The quote row is locked FOR
SHARE, so no revision can commit until the caller's transaction ends, and the version,
lines and audit facts read afterwards belong to that version. A different
``expected_version`` is ``stale_version``; a pointer that disagrees with ``is_current`` is
``version_inconsistent``. The result carries the assessed version, the quote's
``row_version`` and an ``assessment_fingerprint`` for integrity to compare.

Policy: never re-derived here. The stored RFQ is re-evaluated by ``evaluate_stored_rfq``
under the server-pinned, manifest-verified rules (``approval-v1.2``), which must equal the
version's recorded rules (else ``rule_version_changed``), in the version's recorded
evaluation context (audit event: horizon, requested date; version: snapshot pins). Its
outcome, manager-review reasons, customer standing, findings, borderline lines and
unevaluated conditions are mapped to typed requirements. Required roles come from the
approved contracts: approval requests require ``sales_manager`` (``approval_requests``
rule_02, holding ``approve_quote``); engineering review requires a role the database grants
``engineering_review``. Nothing undefined (approver counts, ordering, delegation) is added.

Provenance: the recomputation is compared with what the version stores (evaluation
fingerprint and calculation id from its audit event; total, lines with their part and
internal cost breakdown, snapshots and lead time from its rows). The evaluation fingerprint has no protected home: audit rows are mutable
(K2), versions are ``priced_partial`` without a calculation row or validity (K3), and the
evaluation context is application-supplied, not authenticated evidence (L04/L21/L26). Each
is a ``binding_approval_blockers`` code derived from the data, so a valid policy assessment
is never presented as approval-ready evidence. Agent or Supervisor output is never read.

integrity (K3). A ``complete`` version keeps its evidence in its own K2-protected columns:
its evaluation fingerprint, calculation id, horizon and requested delivery date are read
from the version (the audit event is still parsed, and must agree: ``EVIDENCE_MISMATCH``).
Its calculation record is re-read and compared with today's recomputation
(``CALCULATION_RECORD_MISMATCH``), its RFQ source and rule-pin digests are recomputed
(``EVIDENCE_MISMATCH``), and its expiry is recomputed from its pinned workflow rule and its
creation time (``VALIDITY_MISMATCH``). An expired version (database time, UTC) is
``QUOTE_EXPIRED`` and not eligible for human review. For such a version the K3 blockers
(``SNAPSHOT_NOT_COMPLETE``, ``NO_PERSISTED_CALCULATION``, ``NO_QUOTE_VALIDITY``,
``FINGERPRINT_ONLY_IN_MUTABLE_AUDIT``) are gone; ``EVALUATION_CONTEXT_NOT_AUTHENTICATED``
always stays: a digest proves consistency, not authentic origin (54C).
"""

import hmac
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from rfq_copilot import quote_drafts as drafts
from rfq_copilot import quote_evidence as k3
from rfq_copilot import quote_versions as versions
from rfq_copilot.api.auth import Principal
from rfq_copilot.canonical import fingerprint
from rfq_copilot.mcp.runtime import pinned_rules
from rfq_copilot.models import (
    ApprovalRequest,
    PricingCalculation,
    QuoteLineItem,
    QuoteVersion,
    RolePermission,
)
from rfq_copilot.quote_drafts import DraftRefused
from rfq_copilot.repositories import get_rfq
from rfq_copilot.repositories.queries import _identifier
from rfq_copilot.services.evaluation import (
    ENGINEERING_REVIEW_FINDINGS,
    Outcome,
    RfqEvaluation,
    evaluate_stored_rfq,
)
from rfq_copilot.services.pricing import (
    calculation_record_content,
    canonical_part,
    display_quote,
)

PERMISSION = "view_assigned_rfqs"  # TD-014: the read permission for quotes
APPROVE_PERMISSION = "approve_quote"
APPROVER_ROLE = "sales_manager"  # approval_requests rule_02: required_role = 'sales_manager'
ENGINEERING_PERMISSION = "engineering_review"
ASSESSABLE = frozenset({"draft", "revised_draft"})
ASSESSMENT_VERSION = "quote-approval-assessment-v1"

Classification = Literal[
    "integrity_mismatch",  # stored version content disagrees with its reproducible evaluation
    "evaluation_changed",  # today's facts no longer reproduce the version: revise first
    "clarification_required",
    "unsupported",
    "commercially_blocked",  # blocked customer status (approval-v1.2 blocked_customer_statuses)
    "unresolved_conditions",  # a policy condition could not be evaluated or reviewed
    "reviews_required",  # manager and/or engineering review, then human approval
    "policy_conditions_met",  # only the mandatory human approval remains
]
REVIEWABLE = frozenset({"reviews_required", "policy_conditions_met"})
# Integrity findings about today's evaluation; every other finding is about stored content.
EVALUATION_FINDINGS = frozenset({"EVALUATION_CHANGED", "QUOTATION_NOT_PRICED"})
BLOCKING_OUTCOMES = {
    "clarification_required": "clarification_required",
    "unsupported_declined": "unsupported",
    "customer_hold_blocked_release": "commercially_blocked",
}


@dataclass(frozen=True)
class ReviewRequirement:
    """One human review the policy requires. Never a record that it happened."""

    kind: Literal["human_approval", "manager_review", "engineering_review"]
    permission: str  # the database permission a reviewer must hold
    roles: tuple[str, ...]  # roles that may perform it, from approved contracts and grants
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PolicyRequirements:
    """approval-v1.2 applied to one evaluation, as reported by the evaluation service."""

    outcome: Outcome
    escalation_reason: str | None
    reviews: tuple[ReviewRequirement, ...]
    commercial_conditions: tuple[str, ...]  # CUSTOMER_ACCOUNT_RESTRICTION, CREDIT_LIMIT_EXCEEDED
    engineering_review_lines: tuple[int, ...]  # borderline bend line numbers
    blockers: tuple[str, ...]  # escalation reason of a blocking outcome
    unresolved: tuple[str, ...]  # unevaluated policy conditions, missing reviewer roles


@dataclass(frozen=True)
class QuoteApprovalAssessment:
    """What approving one exact version would require. Customer-safe: no cost or margin."""

    quote_id: str
    rfq_id: str
    customer_id: str
    assessed_version: int
    quote_row_version: int  # freshness: integrity must find the same version and row_version
    quote_status: Literal["draft", "revised_draft", "pending_approval", "approved"]
    rule_versions: tuple[str, ...]  # manufacturing, pricing, approval, workflow (recorded)
    approval_rule_fingerprint: str
    evaluation_fingerprint: str  # recomputed now
    recorded_evaluation_fingerprint: str  # from the version's (mutable) audit event
    calculation_id: str | None  # recomputed now; None when nothing was priced
    policy: PolicyRequirements
    integrity_findings: tuple[str, ...]
    classification: Classification
    human_review: Literal["eligible", "not_eligible"]
    binding_approval_blockers: tuple[str, ...]
    assessment_fingerprint: str
    ready_for_binding_approval: Literal[False] = False
    approval_decision: Literal["none"] = "none"
    released: Literal[False] = False


def policy_requirements(
    evaluation: RfqEvaluation, approvers: tuple[str, ...], engineers: tuple[str, ...]
) -> PolicyRequirements:
    """Typed requirements from a trusted evaluation; nothing recalculated, nothing assumed."""
    quotation, customer = evaluation.quotation, evaluation.customer
    findings = set(quotation.findings) if quotation is not None else set()
    engineering = tuple(f for f in ENGINEERING_REVIEW_FINDINGS if f in findings)
    if evaluation.borderline_bend_lines:
        engineering += ("BORDERLINE_BEND_RADIUS",)
    commercial = tuple(
        code
        for code, applies in (
            ("CUSTOMER_ACCOUNT_RESTRICTION", customer.action != "eligible"),
            ("CREDIT_LIMIT_EXCEEDED", bool(customer.credit_breach)),
        )
        if applies
    )
    if evaluation.human_release_approval_required is not True:  # enforced upstream; re-checked
        raise ValueError("Every quotation needs human approval.")
    approval = ("HUMAN_APPROVAL_REQUIRED",)
    reviews = [ReviewRequirement("human_approval", APPROVE_PERMISSION, approvers, approval)]
    if evaluation.manager_review_reasons:
        manager = evaluation.manager_review_reasons
        reviews.append(ReviewRequirement("manager_review", APPROVE_PERMISSION, approvers, manager))
    if engineering:
        reviews.append(
            ReviewRequirement("engineering_review", ENGINEERING_PERMISSION, engineers, engineering)
        )
    unresolved = list(evaluation.unevaluated_review_conditions)
    if not approvers:
        unresolved.append("NO_APPROVER_ROLE_GRANTED")
    if engineering and not engineers:
        unresolved.append("NO_ENGINEERING_REVIEWER_GRANTED")
    blocking = evaluation.outcome in BLOCKING_OUTCOMES
    return PolicyRequirements(
        outcome=evaluation.outcome,
        escalation_reason=evaluation.escalation_reason,
        reviews=tuple(reviews),
        commercial_conditions=commercial,
        engineering_review_lines=evaluation.borderline_bend_lines,
        blockers=(evaluation.escalation_reason or evaluation.outcome,) if blocking else (),
        unresolved=tuple(unresolved),
    )


def classify(policy: PolicyRequirements, integrity: tuple[str, ...]) -> Classification:
    """Most severe first: integrity, changed evaluation, blocking outcome, unresolved, reviews.

    Any stored-content mismatch is ``integrity_mismatch`` even when the evaluation also
    changed: a financial or structural discrepancy is never reported as a mere change."""
    if set(integrity) - EVALUATION_FINDINGS:
        return "integrity_mismatch"
    if integrity:
        return "evaluation_changed"
    if policy.outcome in BLOCKING_OUTCOMES:
        return BLOCKING_OUTCOMES[policy.outcome]
    if policy.unresolved:
        return "unresolved_conditions"
    if any(r.kind != "human_approval" for r in policy.reviews):
        return "reviews_required"
    return "policy_conditions_met"


def binding_blockers(
    version: QuoteVersion, integrity: tuple[str, ...], expired: bool = False
) -> tuple[str, ...]:
    """Why no binding approval may rest on this version today (K2, K3, L04/L21/L26).

    A version that is not ``complete`` has no persisted calculation, no governed validity
    (a seeded ``expiry_date`` is a dataset fact, not a workflow rule's) and only its audit
    event as provenance. A complete version's own evidence is checked by the integrity
    findings, so only expiry is added. 54C: the evaluation context counts as server-dated
    only when the version's K2-protected ``horizon_start`` is the server horizon of its own
    creation time (``EVALUATION_CONTEXT_NOT_AUTHENTICATED`` otherwise); even then the
    requested delivery date and the absence of contradictions are facts only a human can
    attest, at decision time (``CONTEXT_ATTESTATION_REQUIRED``, cleared by
    ``quote_approval.approve_quote`` after verifying the attestation)."""
    complete = version.snapshot_completeness == k3.COMPLETE
    server_dated = complete and version.horizon_start == k3.server_horizon(version.created_at)
    found = [
        code
        for code, applies in (
            ("SNAPSHOT_NOT_COMPLETE", not complete),
            ("NO_PERSISTED_CALCULATION", not complete or version.calculation_id is None),
            ("NO_QUOTE_VALIDITY", not complete or version.expiry_date is None),
            ("FINGERPRINT_ONLY_IN_MUTABLE_AUDIT", not complete),
            ("QUOTE_EXPIRED", expired),
            ("EVALUATION_CONTEXT_NOT_AUTHENTICATED", not server_dated),
            ("CONTEXT_ATTESTATION_REQUIRED", server_dated),
        )
        if applies
    ]
    return (*found, *integrity)


def recorded_fingerprint(event: dict, version: QuoteVersion) -> str:
    """The evaluation fingerprint the version stands for: its own column when complete
    (K2-protected), else the creation audit event's."""
    if version.snapshot_completeness == k3.COMPLETE:
        return version.evaluation_fingerprint
    return event["evaluation_fingerprint"]


def _holds(row, expected: dict) -> bool:
    """True when the row exists and every named attribute equals its expected value."""
    return row is not None and all(getattr(row, name) == value for name, value in expected.items())


def evidence_findings(
    *,
    quote_rfq_id,
    version,
    record,
    event,
    context,
    evaluation,
    rfq_source,
    rules_digest,
    expected_expiry,
    pricing_rules,
) -> tuple[str, ...]:
    """K3 checks of a complete version; codes only, never a stored or recomputed value."""
    quotation = evaluation.quotation
    pricing = quotation.pricing if quotation is not None else None
    record_ok = _holds(record, {"rfq_id": quote_rfq_id, "requirement_sha256": rfq_source})
    if record_ok and pricing is not None and pricing.result == "priced":
        lines, content, manufacturing = calculation_record_content(
            pricing, pricing_rules, quotation.parts
        )
        recomputed = {
            "calculation_id": pricing.calculation_id,
            "lines": lines,
            "content_sha256": content,
            "quote_total_usd": pricing.quote_total_usd,
            "pricing_rule_version": pricing.pricing_rule_version,
            "manufacturing_rule_version": manufacturing,
        }
        record_ok = _holds(record, recomputed)
    recorded = {
        "rfq_id": quote_rfq_id,
        "rfq_source_sha256": rfq_source,
        "rules_sha256": rules_digest,
        "evaluation_fingerprint": event["evaluation_fingerprint"],
        "calculation_id": event["calculation_id"],
        "horizon_start": context["horizon_start"],
        "requested_delivery_date": context["requested_delivery_date"],
    }
    evidence_ok = _holds(version, recorded)
    checks = (
        ("CALCULATION_RECORD_MISMATCH", not record_ok),
        ("EVIDENCE_MISMATCH", not evidence_ok),
        ("VALIDITY_MISMATCH", version.expiry_date != expected_expiry),
    )
    return tuple(code for code, applies in checks if applies)


def integrity_findings(
    evaluation, event: dict, version: QuoteVersion, lines, pricing_rules
) -> tuple[str, ...]:
    """The recomputation against the version's stored facts; empty when all agree.

    ``lines`` are (line_number, part_id, quantity, line_total_usd, part_json,
    cost_breakdown_json) rows. part_json and cost_breakdown_json are rebuilt with the same
    helpers ``version_rows`` wrote them with (``canonical_part``, ``display_quote``: text,
    integers and lists only, so a JSONB round trip is exact). Only finding codes leave this
    function: never a stored or recomputed value, so no cost reaches a result or message."""
    found = []
    recorded = recorded_fingerprint(event, version)
    if not hmac.compare_digest(evaluation.evaluation_fingerprint, recorded):
        found.append("EVALUATION_CHANGED")
    quotation = evaluation.quotation
    pricing = quotation.pricing if quotation is not None else None
    if quotation is None or quotation.result != "quoted" or pricing is None:
        return (*found, "QUOTATION_NOT_PRICED")
    snapshots = quotation.lead_time.snapshots
    selected = None
    if snapshots is not None:
        selected = {
            "inventory": snapshots.inventory.snapshot_id,
            "capacity": snapshots.capacity.snapshot_id,
            "dataset_version": snapshots.inventory.dataset_version,
        }
    stored = [tuple(line[:4]) for line in lines]
    expected = [
        (number, line.cost.part_id, line.cost.quantity, line.line_total_usd)
        for number, line in zip(quotation.line_numbers, pricing.lines, strict=True)
    ]
    parts = [canonical_part(part) for part in quotation.parts]
    breakdowns = display_quote(pricing, pricing_rules)["lines"]
    total = (pricing.currency, quotation.quote_total_usd)
    lead = version.estimated_lead_business_days
    complete = version.snapshot_completeness == k3.COMPLETE
    recorded_calculation = version.calculation_id if complete else event["calculation_id"]
    checks = (
        ("CALCULATION_CHANGED", pricing.calculation_id != recorded_calculation),
        ("TOTAL_MISMATCH", total != (version.currency, version.total_usd)),
        ("LINE_MISMATCH", expected != stored),
        ("PART_MISMATCH", parts != [line[4] for line in lines]),
        ("COST_BREAKDOWN_MISMATCH", breakdowns != [line[5] for line in lines]),
        ("SNAPSHOT_MISMATCH", selected != version.source_snapshot_versions),
        ("LEAD_TIME_MISMATCH", quotation.lead_business_days != lead),
    )
    return (*found, *(code for code, applies in checks if applies))


def _recorded_context(event: dict, version: QuoteVersion, recorded: tuple) -> dict:
    """The version's evaluation context, exactly as recorded; never filled in. Audit rows
    are mutable (K2), so every field is type-checked: malformed is ``provenance_missing``,
    well-formed but different rule versions are ``version_inconsistent``."""
    names = event.get("rule_versions")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise DraftRefused("provenance_missing")
    if tuple(names) != recorded:
        raise DraftRefused("version_inconsistent")
    try:
        if not drafts._SHA256.fullmatch(event["evaluation_fingerprint"]):
            raise ValueError  # also keeps non-ASCII text away from hmac.compare_digest
        delivery = event["requested_delivery_date"]
        snapshots = version.source_snapshot_versions
        context = {
            "horizon_start": date.fromisoformat(event["horizon_start"]),
            "requested_delivery_date": None if delivery is None else date.fromisoformat(delivery),
            "inventory_snapshot": snapshots["inventory"],
            "capacity_snapshot": snapshots["capacity"],
        }
        named = (
            snapshots["dataset_version"],
            event["calculation_id"],
            context["inventory_snapshot"],
            context["capacity_snapshot"],
        )
        count = event["contradiction_count"]
        # A draft is never created with contradictions, so the recorded count must be 0.
        if type(count) is not int or count != 0 or not all(isinstance(v, str) for v in named):
            raise ValueError
        _identifier(context["inventory_snapshot"])
        _identifier(context["capacity_snapshot"])
    except (KeyError, TypeError, ValueError):
        raise DraftRefused("provenance_missing") from None
    return context


async def _granted_roles(session, permission: str) -> tuple[str, ...]:
    rows = await session.scalars(
        select(RolePermission.role)
        .where(RolePermission.permission == permission, RolePermission.allowed.is_(True))
        .order_by(RolePermission.role)
    )
    return tuple(rows.all())


async def assess_quote_approval(
    session: AsyncSession, principal: Principal, quote_id: str, *, expected_version: int
) -> QuoteApprovalAssessment:
    """Assess the current version (read only; FOR SHARE until the caller's transaction ends)."""
    assessment, _, _, _ = await _assessed(session, principal, quote_id, expected_version)
    return assessment


async def _assessed(
    session: AsyncSession,
    principal: Principal,
    quote_id: str,
    expected_version: int,
    *,
    permission: str = PERMISSION,
    share: bool = True,
    statuses: frozenset = ASSESSABLE,
    next_states: tuple[str, ...] = ("pending_approval",),
    pending_required: bool = False,
    approved: bool = False,
):
    """The assessment plus the locked quote, its current version and the pending request.

    approval policy assesses unsubmitted drafts (FOR SHARE, no pending request). integrity recomputes
    the same assessment inside its decision transaction (FOR NO KEY UPDATE, status
    ``pending_approval``, exactly one pending request for the expected version). human decision
    release recomputes it for an ``approved`` quote (``approved``): the expected version
    must be the approved one, its ``approval_valid`` set and nothing pending.
    """
    try:
        if not isinstance(principal, Principal) or principal.user_id is None:
            raise ValueError
        _identifier(principal.user_id)
        _identifier(quote_id)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError
    except ValueError:
        raise DraftRefused("invalid_request") from None
    customer = principal.customer_id

    await drafts._authorize(session, principal, permission)
    quote = await versions._locked_quote(session, principal, quote_id, share=share)
    if quote is None:
        raise DraftRefused("not_found")  # absent or another customer's: identical
    details = await get_rfq(
        session, quote.rfq_id, customer_id=customer, assigned_user=principal.assigned_user
    )
    if details is None:
        raise DraftRefused("not_found")  # unassigned RFQ
    if quote.current_version != expected_version:
        raise DraftRefused("stale_version")  # a historical version is never a candidate
    if (
        quote.status not in statuses
        or (quote.approved_version is not None) is not approved
        or (approved and quote.approved_version != expected_version)
        or quote.released_at is not None
    ):
        raise DraftRefused("not_assessable")
    version = await session.scalar(
        select(QuoteVersion)
        .where(QuoteVersion.quote_id == quote_id, QuoteVersion.version == expected_version)
        .options(raiseload("*"))
        .execution_options(populate_existing=True)
    )
    if version is None or not version.is_current:
        raise DraftRefused("version_inconsistent")
    pending = await session.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.quote_id == quote_id, ApprovalRequest.status == "pending")
        .options(raiseload("*"))
        .execution_options(populate_existing=True)
        .limit(1)
    )
    if pending_required and (pending is None or pending.quote_version != expected_version):
        raise DraftRefused("not_pending")  # nothing (or another version) awaits a decision
    if version.approval_valid is not approved or not details.runtime_lines:
        raise DraftRefused("not_assessable")
    if not pending_required and pending is not None:
        raise DraftRefused("not_assessable")
    # The version's own workflow rule must list the step(s) this assessment leads to.
    workflow = await pinned_rules(session, versions.recorded_workflow_version(version))
    allowed = workflow.document.get("transitions", {}).get(quote.status, ())
    if not set(next_states) <= set(allowed):
        raise DraftRefused("not_assessable")

    rules = await drafts.pinned_evaluation_rules(session)
    recorded = (
        version.manufacturing_rule_version,
        version.pricing_rule_version,
        version.approval_rule_version,
        version.workflow_rule_version,
    )
    if drafts.rule_versions(rules, workflow) != recorded:
        raise DraftRefused("rule_version_changed")  # never moved to another rule silently
    event = await versions.recorded_event(session, quote_id, expected_version)
    audited = _recorded_context(event, version, recorded)  # always parsed and type-checked
    complete = version.snapshot_completeness == k3.COMPLETE
    context = audited
    if complete:  # the version's own K2-protected context, compared with the audit below
        context = {
            **audited,
            "horizon_start": version.horizon_start,
            "requested_delivery_date": version.requested_delivery_date,
        }
    evaluation = await evaluate_stored_rfq(
        session, quote.rfq_id, customer_id=customer, **rules, **context, contradictions=()
    )
    if evaluation is None:
        raise DraftRefused("not_found")
    columns = (
        QuoteLineItem.line_number,
        QuoteLineItem.part_id,
        QuoteLineItem.quantity,
        QuoteLineItem.line_total_usd,
        QuoteLineItem.part_json,
        QuoteLineItem.cost_breakdown_json,  # compared only; never returned
    )
    statement = (
        select(*columns)
        .where(QuoteLineItem.quote_id == quote_id, QuoteLineItem.quote_version == expected_version)
        .order_by(QuoteLineItem.line_number)
    )
    lines = (await session.execute(statement)).all()

    integrity = integrity_findings(evaluation, event, version, lines, rules["pricing_rules"])
    expired = False
    if complete:
        record = await session.scalar(
            select(PricingCalculation)
            .where(PricingCalculation.calculation_record_id == version.calculation_record_id)
            .options(raiseload("*"))
            .execution_options(populate_existing=True)
        )
        dates = k3.validity_dates(workflow)
        expected_expiry = None if dates is None else k3.expiry_date(version.created_at, dates)
        integrity += evidence_findings(
            quote_rfq_id=quote.rfq_id,
            version=version,
            record=record,
            event=event,
            context=audited,
            evaluation=evaluation,
            rfq_source=k3.rfq_source_sha256(quote.rfq_id, customer, details.runtime_lines),
            rules_digest=k3.rules_sha256(rules, workflow),
            expected_expiry=expected_expiry,
            pricing_rules=rules["pricing_rules"],
        )
        expired = k3.is_expired(version.expiry_date, await k3.database_now(session))
    granted = await _granted_roles(session, APPROVE_PERMISSION)
    approvers = tuple(role for role in granted if role == APPROVER_ROLE)
    engineers = await _granted_roles(session, ENGINEERING_PERMISSION)
    policy = policy_requirements(evaluation, approvers, engineers)
    classification = classify(policy, integrity)
    reviewable = classification in REVIEWABLE and not expired
    pricing = evaluation.quotation.pricing if evaluation.quotation is not None else None
    fields = dict(
        quote_id=quote_id,
        rfq_id=quote.rfq_id,
        customer_id=customer,
        assessed_version=expected_version,
        quote_row_version=quote.row_version,
        quote_status=quote.status,
        rule_versions=recorded,
        approval_rule_fingerprint=evaluation.approval_rule_fingerprint,
        evaluation_fingerprint=evaluation.evaluation_fingerprint,
        recorded_evaluation_fingerprint=recorded_fingerprint(event, version),
        calculation_id=pricing.calculation_id if pricing is not None else None,
        policy=policy,
        integrity_findings=integrity,
        classification=classification,
        human_review="eligible" if reviewable else "not_eligible",
        binding_approval_blockers=binding_blockers(version, integrity, expired),
    )
    digest = fingerprint({"assessment_version": ASSESSMENT_VERSION, **fields})
    return QuoteApprovalAssessment(**fields, assessment_fingerprint=digest), quote, version, pending
