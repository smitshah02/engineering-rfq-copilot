"""Quotation and Review Agent A5 (Day 41): a verified, non-persisted quotation draft and the
review it needs under the pinned approval policy. Never an approval, a release or a record.

    ManufacturingAssessment (A2) + SupplyAssessment (A3) + PricingAssessment (A4)
        + trusted price-v1.0 and approval-v1.2 RuleVersionDetails + trusted ToolExecutor
        -> evaluate_quotation_review -> QuotationReviewAssessment

- Upstream gate (no tool is called otherwise): one source and RFQ across A2-A4. A2/A3 asking
  the customer -> ``clarification_required``; A2/A3 ``requires_review`` -> ``requires_review``;
  any other unpriceable upstream -> ``not_evaluated``. A4 ``priced`` / ``requires_review`` is
  re-derived by replaying A4 itself (``evaluate_pricing``) over A4's own tool evidence: the
  replay re-runs A3's gate (A2 evidence bound to its specifications, A3's evidence checks) and
  every A4 check, and must reproduce A4's assessment exactly, else
  ``pricing_assessment_invalid``. A4 ``requires_review`` (rule drift, contradiction) stays
  ``requires_review`` and no amount, raw evidence included, is ever copied.
- Authority: one read-only MCP tool, ``evaluate_quotation`` (``services.quotation.quote_parts``,
  RBAC v2 ``view_quote``), once, no retries, one timeout, with A2's specifications and A3's
  trusted horizon and verified delivery date. It runs in its own snapshot (not A3's or A4's);
  its lines, totals, calculation id, ready date and findings must agree with A3 and A4, else
  ``invalid_evidence``. Amounts are A4's; A5 computes no price, tax, freight, discount, limit,
  validity or delivery commitment.
- Approval policy: the pure ``services.evaluation`` helpers under the trusted
  ``approval-v1.2`` (``require_human_release``, ``review_threshold``, ``engineering_conditions``,
  ``borderline_ratio``, ``borderline_bend_lines``). Customer account status and credit need
  facts no agent tool exposes, so they are reported as unevaluated, never as passed.
- Human boundary: ``approved`` and ``released`` are ``Literal[False]``; every draft needs human
  approval by ``APPROVER_ROLE``; the policy's release prerequisites are listed as outstanding.
  Nothing is written, reserved, signed or sent; there is no quote number.
- No model. Explanations are fixed templates without customer text.
"""

import asyncio
import hashlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from rfq_copilot.intake_validation import ToolExecutor
from rfq_copilot.manufacturing_agent import ManufacturingAssessment
from rfq_copilot.mcp.client import FailureCode, McpCallError
from rfq_copilot.mcp.contracts import (
    NEW_EVALUATION_APPROVAL_RULE_VERSION,
    DeliveryPlanningInput,
    ExactDecimal,
    QuotationOutput,
)
from rfq_copilot.pricing_agent import (
    FLAGS,
    PRICEABLE,
    PricingAssessment,
    _trusted,
    evaluate_pricing,
)
from rfq_copilot.repositories import RuleVersionDetails
from rfq_copilot.services.evaluation import (
    borderline_bend_lines,
    borderline_ratio,
    engineering_conditions,
    require_human_release,
    review_threshold,
)
from rfq_copilot.services.lead_time import procurement_business_days
from rfq_copilot.supply_agent import SupplyAssessment

TOOL = "evaluate_quotation"
ALLOWED_TOOLS = frozenset({TOOL})  # a subset of graph.READ_ONLY_TOOLS (tested)
DEFAULT_TIMEOUT = 60.0
# The only role an approval request may require (models.approvals rule_02,
# repositories.queries request default); no other reviewer role is defined by policy.
APPROVER_ROLE = "sales_manager"
# Policy conditions needing customer facts (account status, credit) no agent tool returns.
CUSTOMER_CONDITIONS = ("customer_account_status", "credit_limit")
NOTICE = "Draft for human review: not a quote, not approved, not released, not a commitment."

Status = Literal[
    "draft_ready_for_review",
    "requires_review",
    "clarification_required",
    "not_evaluated",
    "error",
]
ErrorCode = FailureCode | Literal["invalid_evidence"]
_STRICT = ConfigDict(extra="forbid", frozen=True)


class QuotationEvidence(BaseModel):
    """Provenance: the arguments digest, the A3/A4 digests it is bound to, the exact result."""

    model_config = _STRICT
    arguments_sha256: str  # of the DeliveryPlanningInput sent (equals A3's)
    pricing_arguments_sha256: str  # A4's evidence digest
    quotation: QuotationOutput  # selling amounts only; no internal cost


class DraftLine(BaseModel):
    """One quotation line: selling amounts from A4, line facts from the quotation service."""

    model_config = _STRICT
    index: int  # A2 / Intake part index
    line_number: int
    part_id: str
    quantity: int
    operations: tuple[str, ...]
    quoted_unit_price_usd: ExactDecimal  # advisory until a human approves
    line_total_usd: ExactDecimal
    stock_status: str | None  # never concealed
    procurement_business_days: int | None


class CustomerLine(BaseModel):
    model_config = _STRICT
    part_id: str
    quantity: int
    quoted_unit_price_usd: ExactDecimal
    line_total_usd: ExactDecimal


class CustomerQuotationDraft(BaseModel):
    """The customer-safe projection: selling prices only. No cost, margin, evidence, rule
    fingerprint or review detail. Still a draft until a human approves and releases it."""

    model_config = _STRICT
    source_id: str
    rfq_id: str | None
    currency: str
    lines: tuple[CustomerLine, ...]
    quote_total_usd: ExactDecimal
    excludes: tuple[str, ...]
    estimated_ready_date: date | None  # an estimate, not a delivery commitment
    requested_delivery_date: date | None
    delivery_feasible: bool | None
    # D41-02: True while anything beyond ordinary human approval and release is open: a review
    # reason, a mandatory manager review, or a policy check A5 could not evaluate (customer
    # account status and credit always are, today). False means only that approval by the
    # approver role remains; it is never approval itself (see notice).
    conditional: bool
    notice: Literal["Draft for human review: not a quote, not approved, not released, not a commitment."] = NOTICE  # fmt: skip


class QuotationReviewAssessment(BaseModel):
    """A5 result: an internal review record. Never an approval, release or persisted quote."""

    model_config = _STRICT
    source_id: str
    rfq_id: str | None
    status: Status
    supply_status: str | None = None  # A3's, unchanged
    pricing_status: str | None = None  # A4's, unchanged
    approval_rule_version: str | None = None
    approval_rule_fingerprint: str | None = None
    pricing_rule_version: str | None = None
    calculation_id: str | None = None  # A4's deterministic id: not a quote number
    currency: str | None = None
    lines: tuple[DraftLine, ...] = ()
    quote_total_usd: ExactDecimal | None = None  # selling total; internal cost is not copied
    excludes: tuple[str, ...] = ()  # e.g. tax, freight, shipping, discounts
    estimated_ready_date: date | None = None  # A3's planning estimate
    lead_business_days: int | None = None
    requested_delivery_date: date | None = None  # A3's verified Intake value
    delivery_feasible: bool | None = None
    findings: tuple[str, ...] = ()  # the quotation service's, equal to A3's lead-time findings
    clarifications: tuple[tuple[int, str], ...] = ()  # (part index, field) to ask the customer
    review_reasons: tuple[str, ...] = ()
    manager_review_required: bool = False
    manager_review_reasons: tuple[str, ...] = ()
    manager_review_threshold_usd: ExactDecimal | None = None
    borderline_bend_lines: tuple[int, ...] = ()
    unevaluated_review_conditions: tuple[str, ...] = ()  # never reported as passed
    required_approver_role: str | None = None
    release_prerequisites: tuple[str, ...] = ()  # the policy's; none is verified by A5
    human_approval_required: Literal[True] = True
    approved: Literal[False] = False
    released: Literal[False] = False
    explanation: str
    evidence: QuotationEvidence | None = None
    not_evaluated_reason: str | None = None
    error: ErrorCode | None = None

    def customer_view(self) -> CustomerQuotationDraft | None:
        """The customer-safe draft, or None when nothing verified was assembled."""
        if not self.lines or self.quote_total_usd is None:
            return None
        conditional = (
            self.status != "draft_ready_for_review"
            or bool(self.review_reasons)
            or self.manager_review_required
            or bool(self.unevaluated_review_conditions)
        )
        return CustomerQuotationDraft(
            source_id=self.source_id,
            rfq_id=self.rfq_id,
            currency=self.currency,
            lines=tuple(
                CustomerLine(
                    part_id=line.part_id,
                    quantity=line.quantity,
                    quoted_unit_price_usd=line.quoted_unit_price_usd,
                    line_total_usd=line.line_total_usd,
                )
                for line in self.lines
            ),
            quote_total_usd=self.quote_total_usd,
            excludes=self.excludes,
            estimated_ready_date=self.estimated_ready_date,
            requested_delivery_date=self.requested_delivery_date,
            delivery_feasible=self.delivery_feasible,
            conditional=conditional,
        )


def _sha(model: BaseModel) -> str:
    return hashlib.sha256(model.model_dump_json().encode()).hexdigest()


def _approval_trusted(rules: object) -> bool:
    """The registered approval-v1.2 document, unchanged; it forbids agent approval/release."""
    if not _trusted(rules, NEW_EVALUATION_APPROVAL_RULE_VERSION):
        return False
    require_human_release(rules)  # the trusted document always passes; enforced, not assumed
    return True


async def _pricing_verified(a2, a3, a4: PricingAssessment, rules) -> bool:
    """A4's assessment is exactly what A4 derives from its own evidence (A2/A3 gate included)."""
    ev = a4.evidence
    if type(a4) is not PricingAssessment or ev is None:
        return False
    outputs = {"estimate_costs": ev.costs, "calculate_price": ev.price}

    async def replay(tool, request):  # no network: A4's recorded tool results
        return outputs[tool]

    try:
        return await evaluate_pricing(a2, a3, replay, pricing_rules=rules) == a4
    except Exception:  # malformed upstream objects cannot be verified
        return False


def _inconsistent(q: QuotationOutput, a2: ManufacturingAssessment, a3, a4) -> bool:
    """True when the quotation result is not the quotation service's view of exactly the parts,
    amounts and schedule A2-A4 verified. Comparisons only; nothing is recalculated."""
    lead = a3.evidence.lead_time
    if q.result != "quoted" or q.blocking_lines:
        return True
    if [line.line_number for line in q.lines] != list(range(1, len(a2.parts) + 1)):
        return True
    if [line.part_id for line in q.lines] != [p.part_id for p in a2.parts]:
        return True
    # D41-01: a line's procurement delay is the lead-time service's procurement_business_days
    # of the stock line covering that part (assemble_quotation keys it by material and
    # thickness, so parts sharing stock share it); the RFQ delay is their maximum (plan_start).
    # A3's per-part stock line, re-verified by the A4 replay, is the authority. The service
    # function reads only the line's status and supplier lead days, which the output carries.
    stock = {p.part_id: p.stock for p in a3.parts}
    delays = []
    for line, p, priced in zip(q.lines, a2.parts, a4.lines, strict=True):
        if line.manufacturing_result != "valid" or line.findings != p.evidence.output.findings:
            return True
        if (line.stock_status, line.line_total_usd) != (priced.stock_status, priced.line_total_usd):  # fmt: skip
            return True
        covering = stock.get(p.part_id)
        if covering is None or line.procurement_business_days is None:
            return True  # a quoted line always has a stock line and a delay
        if line.procurement_business_days != procurement_business_days(covering):
            return True
        delays.append(line.procurement_business_days)
    if max(delays) != lead.procurement_business_days:
        return True  # the RFQ delay is the longest line delay
    if (q.calculation_id, q.quote_total_usd, q.currency) != (a4.calculation_id, a4.quote_total_usd, a4.currency):  # fmt: skip
        return True
    return (q.estimated_ready_date, q.lead_business_days, q.findings) != (
        lead.estimated_ready_date,
        lead.lead_business_days,
        lead.findings,
    )


def _upstream(a2: ManufacturingAssessment, a3: SupplyAssessment, a4: PricingAssessment):
    """(status, reason) when the upstream results cannot be assembled, else None."""
    if {(a.source_id, a.rfq_id) for a in (a2, a3, a4)} != {(a2.source_id, a2.rfq_id)}:
        return "not_evaluated", "upstream_identity_mismatch"
    if a2.status == "needs_clarification":
        return "clarification_required", "manufacturing_needs_clarification"
    if a3.status == "needs_clarification":
        return "clarification_required", a3.not_evaluated_reason or "supply_needs_clarification"
    if a2.status == "requires_review":
        return "requires_review", "manufacturing_requires_review"
    if a2.status != "feasible":
        return "not_evaluated", f"manufacturing_{a2.status}"  # infeasible is never quoted
    if a3.status == "requires_review":
        return "requires_review", "supply_requires_review"
    if a3.status not in PRICEABLE:
        return "not_evaluated", f"supply_{a3.status}"
    if a4.status not in ("priced", "requires_review"):
        return "not_evaluated", f"pricing_{a4.status}"
    return None


def _explain(f: dict, n: int, threshold: Decimal) -> str:
    cur = f["currency"]
    text = (
        f"Quotation draft assembled by {TOOL} and checked against the pricing and supply "
        f"assessments (calculation {f['calculation_id']}, {f['pricing_rule_version']}, "
        f"{f['approval_rule_version']}): {n} line(s), selling total {f['quote_total_usd']} {cur}"
    )
    if f["excludes"]:
        text += f", excluding {', '.join(f['excludes'])}"
    if f["estimated_ready_date"] is None:
        text += "; no ready date could be estimated"
    else:
        text += (
            f"; estimated ready date {f['estimated_ready_date']} ({f['lead_business_days']} "
            "business days), a planning estimate, not a delivery commitment"
        )
    if f["requested_delivery_date"] is not None:
        achievable = "achievable" if f["delivery_feasible"] else "not achievable"
        text += f"; requested delivery {f['requested_delivery_date']} is {achievable}"
    text += "."
    if f["review_reasons"]:
        text += f" Review required: {', '.join(f['review_reasons'])}."
    if f["manager_review_required"]:
        text += f" Manager review: the total is at or above {threshold} {cur}."
    text += (
        f" Not evaluated (no trusted source): {', '.join(f['unevaluated_review_conditions'])}."
        f" Not approved and not released: human approval by {APPROVER_ROLE} is required."
    )
    return text


async def evaluate_quotation_review(
    a2: ManufacturingAssessment,
    a3: SupplyAssessment,
    a4: PricingAssessment,
    execute: ToolExecutor | None,
    *,
    pricing_rules: RuleVersionDetails | None,
    approval_rules: RuleVersionDetails | None,
    timeout_seconds: float = DEFAULT_TIMEOUT,
) -> QuotationReviewAssessment:
    """A verified quotation draft and its review requirements; never approval or release.

    ``execute`` must be a trusted, principal-bound executor (``graph.mcp_executor``);
    ``pricing_rules`` / ``approval_rules`` the trusted ``price-v1.0`` / ``approval-v1.2``
    rules (``mcp.runtime.pinned_rules``)."""
    if not (
        isinstance(a2, ManufacturingAssessment)
        and isinstance(a3, SupplyAssessment)
        and isinstance(a4, PricingAssessment)
    ):
        raise ValueError("evaluate_quotation_review needs A2, A3 and A4 assessments")
    base = {
        "source_id": a2.source_id,
        "rfq_id": a2.rfq_id,
        "supply_status": a3.status,
        "pricing_status": a4.status,
    }

    def blocked(status: Status, reason=None, error=None, **extra) -> QuotationReviewAssessment:
        text = f"No quotation draft ({reason or error}); nothing approved or released."
        return QuotationReviewAssessment(**base, status=status, explanation=text, not_evaluated_reason=reason, error=error, **extra)  # fmt: skip

    if not _trusted(pricing_rules):
        return blocked("not_evaluated", "pricing_rules_untrusted")
    if not _approval_trusted(approval_rules):
        return blocked("not_evaluated", "approval_rules_untrusted")
    upstream = _upstream(a2, a3, a4)
    if upstream:
        status, reason = upstream
        if status == "clarification_required":
            asks = tuple((p.index, f) for p in a2.parts for f in p.clarifications)
            return blocked(status, reason, clarifications=asks)
        if status == "requires_review":
            return blocked(status, reason, review_reasons=(reason,))
        return blocked(status, reason)
    if not await _pricing_verified(a2, a3, a4, pricing_rules):
        return blocked("not_evaluated", "pricing_assessment_invalid")
    if a4.status == "requires_review":  # drift or contradiction: no amount is promoted
        return blocked("requires_review", "pricing_requires_review", review_reasons=a4.review_reasons)  # fmt: skip
    if execute is None:
        return blocked("error", error="unauthenticated")
    specs = tuple(p.specification for p in a2.parts)
    delivery = a3.requested_delivery_date  # validated by A4's replayed gate
    request = DeliveryPlanningInput(parts=specs, horizon_start=a3.horizon_start, requested_delivery_date=delivery)  # fmt: skip
    try:
        async with asyncio.timeout(timeout_seconds):  # once, no retries
            raw = await execute(TOOL, request)
            data = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw
            q = QuotationOutput.model_validate(data)
    except McpCallError as error:
        return blocked("error", error=error.code)
    except ValidationError:
        return blocked("error", error="invalid_output")
    except TimeoutError:
        return blocked("error", error="timeout")
    except Exception:  # an executor fault: its text never surfaces
        return blocked("error", error="unavailable")
    if _inconsistent(q, a2, a3, a4):
        return blocked("error", error="invalid_evidence")  # unverifiable results are not kept

    # Approval policy (approval-v1.2), the services' own helpers.
    document = approval_rules.document
    threshold = review_threshold(approval_rules)
    manager = ("QUOTE_TOTAL_AT_OR_ABOVE_THRESHOLD",) if a4.quote_total_usd >= threshold else ()
    ratio = borderline_ratio(approval_rules)
    bent = any("bend" in line.operations for line in a4.lines)
    unevaluated = CUSTOMER_CONDITIONS
    if bent and ratio is None and "borderline_bend_radius" in engineering_conditions(approval_rules):  # fmt: skip
        unevaluated += ("borderline_bend_radius",)
    borderline = ()
    if ratio is not None:
        # The attributes borderline_bend_lines reads from a Quotation: line numbers, A2's
        # verified minimum radii and A2's specifications. One rule, one implementation.
        rows = [SimpleNamespace(line_number=ln.line_number, manufacturing=p.evidence.output) for ln, p in zip(q.lines, a2.parts, strict=True)]  # fmt: skip
        view = SimpleNamespace(lines=rows, parts=[p.specification for p in a2.parts])
        borderline = borderline_bend_lines(view, ratio)
    reasons = (*a4.review_reasons, *q.findings, *(("BORDERLINE_BEND_RADIUS",) if borderline else ()))  # fmt: skip
    lead = a3.evidence.lead_time
    fields = {
        **base,
        "status": "requires_review" if reasons else "draft_ready_for_review",
        "approval_rule_version": approval_rules.rule_version,
        "approval_rule_fingerprint": approval_rules.fingerprint,
        "pricing_rule_version": a4.pricing_rule_version,
        "calculation_id": a4.calculation_id,
        "currency": a4.currency,
        "lines": tuple(
            DraftLine(
                index=p.index,
                line_number=line.line_number,
                part_id=p.part_id,
                quantity=priced.quantity,
                operations=priced.operations,
                quoted_unit_price_usd=priced.quoted_unit_price_usd,
                line_total_usd=priced.line_total_usd,
                stock_status=priced.stock_status,
                procurement_business_days=line.procurement_business_days,
            )
            for p, line, priced in zip(a2.parts, q.lines, a4.lines, strict=True)
        ),
        "quote_total_usd": a4.quote_total_usd,
        "excludes": (
            *(f.removesuffix("_included") for f in FLAGS if not getattr(a4, f)),
            "discounts",
        ),  # fmt: skip
        "estimated_ready_date": lead.estimated_ready_date,
        "lead_business_days": lead.lead_business_days,
        "requested_delivery_date": a3.requested_delivery_date,
        "delivery_feasible": lead.delivery_feasible,
        "findings": q.findings,
        "review_reasons": reasons,
        "manager_review_required": bool(manager),
        "manager_review_reasons": manager,
        "manager_review_threshold_usd": threshold,
        "borderline_bend_lines": borderline,
        "unevaluated_review_conditions": unevaluated,
        "required_approver_role": APPROVER_ROLE,
        "release_prerequisites": tuple(document["quote_release_requires"]),
        "evidence": QuotationEvidence(
            arguments_sha256=_sha(request),
            pricing_arguments_sha256=a4.evidence.arguments_sha256,
            quotation=q,
        ),
    }
    return QuotationReviewAssessment(**fields, explanation=_explain(fields, len(a4.lines), threshold))  # fmt: skip
