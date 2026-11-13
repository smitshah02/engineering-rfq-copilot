"""Pricing Agent A4 (Day 40): evidence-backed manufacturing cost and selling price for the
parts A2 found manufacturable and A3 scheduled. Not a quote, not approval, not release.

    ManufacturingAssessment (A2) + SupplyAssessment (A3, available | constrained)
        + trusted pricing RuleVersionDetails + trusted ToolExecutor
        -> evaluate_pricing -> PricingAssessment

- Input gate (no tool is called otherwise, ``not_evaluated``): A2 passes A3's own gate; A3 is
  the assessment of the same source and parts, ``available`` or ``constrained``, and its
  evidence still passes A3's checks (arguments digest bound to A2's specifications, the three
  results consistent, status and explanation those A3 derives from them, stock lines per part)
  under the trusted pricing rule. ``constrained`` is priced (the costing service prices short
  stock) but is never concealed: ``supply_status`` and a ``supply_constrained`` review reason.
- Trusted pricing context: the caller passes the ``price-v1.0`` ``RuleVersionDetails`` it
  loaded with ``mcp.runtime.pinned_rules``. It must match the version-controlled manifest
  (``TRUSTED_RULES``: kind, source sha256, and the document's re-derived fingerprint). Margin,
  currency, scales, rounding mode and inclusion flags are read from it with the services' own
  helpers; none of them is ever an input from the customer or a model.
- Authority: two read-only MCP tools (RBAC v2 ``view_quote``), one RFQ-level call each with
  the specifications A2 verified and A3's trusted horizon: ``estimate_costs``
  (``services.costing``) then ``calculate_price`` (``services.pricing.calculate_quote``).
  Sequential, once each, no retries, one timeout. A principal without ``view_quote`` gets
  ``forbidden`` from the server and sees no cost or price. Nothing is written or reserved.
- No model. Every amount is a tool's; the agent only checks them. Display rounding of cost
  components is ``services.costing.display_costs`` with the trusted rule.
- Evidence checks (fail closed, ``invalid_evidence``): the two results use one rule; cost lines
  are exactly A2's parts in order with their quantity and A2's verified route; components add
  up to the variable unit cost, ``quantity x variable + setup`` is the line cost, line costs
  add up to the RFQ cost; price lines are the cost lines, each line total and unit price is
  ``services.pricing.price_line`` of the tool's line cost under the trusted margin, scales and
  mode, the quote total is the sum of the line totals; currency, margin, inclusion flags and
  calculation id are the trusted rule's. Tool rules other than the trusted rule ->
  ``requires_review``; a costing verdict contradicting A2/A3 -> ``requires_review``.
- No discounts, taxes, freight or shipping: the pricing rule defines none, so none is applied
  and the explanation says so.
"""

import asyncio
import hashlib
from datetime import date
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from rfq_copilot.canonical import fingerprint
from rfq_copilot.entities import Part
from rfq_copilot.intake_validation import ToolExecutor
from rfq_copilot.manufacturing_agent import ManufacturingAssessment
from rfq_copilot.mcp.client import FailureCode, McpCallError
from rfq_copilot.mcp.contracts import (
    CostOutput,
    DeliveryPlanningInput,
    ExactDecimal,
    PlanningInput,
    PriceOutput,
)
from rfq_copilot.mcp.runtime import PRICING_RULE_VERSION
from rfq_copilot.repositories import RuleVersionDetails
from rfq_copilot.repositories.rule_manifest import TRUSTED_RULES
from rfq_copilot.services.capacity import PLANNING
from rfq_copilot.services.costing import _rate, display_costs, rounding
from rfq_copilot.services.pricing import calculation_id, gross_margin, price_line
from rfq_copilot.supply_agent import SupplyAssessment, _explain, _inconsistent, _status
from rfq_copilot.supply_agent import _gate as _a2_gate

TOOLS = ("estimate_costs", "calculate_price")  # call order
ALLOWED_TOOLS = frozenset(TOOLS)  # a subset of graph.READ_ONLY_TOOLS (tested)
DEFAULT_TIMEOUT = 60.0
PRICEABLE = frozenset({"available", "constrained"})  # A3 statuses the costing service prices
FLAGS = ("tax_included", "freight_included", "shipping_included")

Status = Literal["priced", "requires_review", "not_evaluated", "error"]
ErrorCode = FailureCode | Literal["invalid_evidence"]
_STRICT = ConfigDict(extra="forbid", frozen=True)


class PricingEvidence(BaseModel):
    """Provenance: the arguments digest, the A3 evidence it is bound to, the two exact results."""

    model_config = _STRICT
    arguments_sha256: str  # of the exact PlanningInput JSON sent to both tools
    supply_arguments_sha256: str  # A3's evidence digest (A2 specifications + dates)
    inventory_snapshot_id: str | None  # A3's snapshots; the pricing tools do not report theirs
    capacity_snapshot_id: str | None
    costs: CostOutput  # estimate_costs: exact, unrounded components
    price: PriceOutput  # calculate_price: rounded line totals, quote total


class PricingLine(BaseModel):
    """One part's cost (rounded by ``display_costs``) and selling price (tool values)."""

    model_config = _STRICT
    index: int  # A2 / Intake part index
    part_id: str
    quantity: int
    operations: tuple[str, ...]
    material_cost_per_unit_usd: ExactDecimal
    labor_cost_per_unit_usd: ExactDecimal
    finish_cost_per_unit_usd: ExactDecimal
    packaging_cost_per_unit_usd: ExactDecimal
    variable_cost_per_unit_usd: ExactDecimal
    setup_cost_per_line_usd: ExactDecimal
    total_manufacturing_cost_usd: ExactDecimal  # internal cost, not a customer price
    quoted_unit_price_usd: ExactDecimal  # advisory
    line_total_usd: ExactDecimal  # customer-facing selling total
    stock_status: str | None  # A3's stock line status for this part: never concealed


class PricingAssessment(BaseModel):
    """A4 result. A deterministic, non-binding calculation: never a quote, approval or release."""

    model_config = _STRICT
    source_id: str
    rfq_id: str | None
    status: Status
    supply_status: str | None = None  # A3's status, carried unchanged
    pricing_rule_version: str | None = None
    pricing_rule_fingerprint: str | None = None
    currency: str | None = None
    unit_scale: ExactDecimal | None = None  # the trusted rule's display precision
    line_scale: ExactDecimal | None = None
    rounding: str | None = None
    gross_margin: ExactDecimal | None = None
    tax_included: bool | None = None
    freight_included: bool | None = None
    shipping_included: bool | None = None
    calculation_id: str | None = None
    lines: tuple[PricingLine, ...] = ()
    total_manufacturing_cost_usd: ExactDecimal | None = None  # internal, unit scale
    quote_total_usd: ExactDecimal | None = None  # customer-facing, sum of line totals
    review_reasons: tuple[str, ...] = ()
    explanation: str
    evidence: PricingEvidence | None = None  # None: no verified tool results, no conclusion
    not_evaluated_reason: str | None = None
    error: ErrorCode | None = None


def _sha(model: BaseModel) -> str:
    return hashlib.sha256(model.model_dump_json().encode()).hexdigest()


def _trusted(rules: object, version: str = PRICING_RULE_VERSION) -> bool:
    """The rule is the registered ``version`` document (default price-v1.0), unchanged."""
    kind, source_sha256, pinned = TRUSTED_RULES[version]
    if not isinstance(rules, RuleVersionDetails):
        return False
    identity = (rules.rule_version, rules.rule_kind, rules.sha256, rules.fingerprint)
    if identity != (version, kind, source_sha256, pinned):
        return False
    try:
        return fingerprint(rules.document) == pinned
    except Exception:  # a document canonical JSON cannot encode is not the trusted one
        return False


def _gate(a2: ManufacturingAssessment, a3: SupplyAssessment, rules: RuleVersionDetails):
    """Why these assessments cannot be priced, or None."""
    reason = _a2_gate(a2)
    if reason:
        return reason
    if (a3.source_id, a3.rfq_id) != (a2.source_id, a2.rfq_id):
        return "supply_assessment_mismatch"
    if a3.status not in PRICEABLE:
        return f"supply_{a3.status}"
    ev, horizon, delivery = a3.evidence, a3.horizon_start, a3.requested_delivery_date
    if ev is None or type(horizon) is not date:
        return "supply_evidence_missing"
    if [(p.index, p.part_id) for p in a3.parts] != [(p.index, p.part_id) for p in a2.parts]:
        return "supply_parts_mismatch"
    specs = tuple(p.specification for p in a2.parts)
    try:
        request = DeliveryPlanningInput(parts=specs, horizon_start=horizon, requested_delivery_date=delivery)  # fmt: skip
    except ValidationError:
        return "supply_evidence_invalid"
    inv, cap, lead = ev.inventory, ev.capacity, ev.lead_time
    by_part = {part_id: line for line in inv.lines for part_id in line.part_ids}
    if (
        ev.arguments_sha256 != _sha(request)  # A3 evaluated other parts or dates
        or _inconsistent(inv, cap, lead, a2, horizon, delivery)  # A3's own evidence checks
        or _status(inv, cap, lead) != a3.status
        or _explain(inv, cap, lead) != a3.explanation
        or any(p.stock != by_part.get(p.part_id) for p in a3.parts)
    ):
        return "supply_evidence_invalid"
    if (inv.pricing_rule_version, inv.pricing_rule_fingerprint) != (rules.rule_version, rules.fingerprint):  # fmt: skip
        return "pricing_rule_mismatch"  # A3 ran under other pricing rules than the trusted one
    return None


def _costs_inconsistent(costs: CostOutput, a2: ManufacturingAssessment, rules) -> bool:
    """True when the cost result is not one costing of exactly A2's parts. Arithmetic over the
    tool's own components only (the service's PLANNING context); no rate is re-derived."""
    if [line.part_id for line in costs.lines] != [p.part_id for p in a2.parts]:
        return True  # missing, duplicated, extra or reordered lines
    packaging = _rate(rules, "packaging_usd_per_unit")
    with localcontext(PLANNING):
        for line, p in zip(costs.lines, a2.parts, strict=True):
            if line.quantity != p.specification.quantity or line.quantity <= 0:
                return True
            if line.operations != p.evidence.output.operations:  # A2's verified route
                return True
            if line.packaging_cost_per_unit != packaging:
                return True
            if "finish" not in line.operations and line.finish_cost_per_unit != 0:
                return True
            summed = (line.material_cost_per_unit + line.labour_cost_per_unit + line.finish_cost_per_unit + line.packaging_cost_per_unit)  # fmt: skip
            if line.variable_cost_per_unit != summed:
                return True
            if line.total_manufacturing_cost != line.quantity * line.variable_cost_per_unit + line.setup_cost_per_line:  # fmt: skip
                return True
        total = sum((line.total_manufacturing_cost for line in costs.lines), Decimal(0))
    return costs.total_manufacturing_cost != total


def _price_inconsistent(price: PriceOutput, costs: CostOutput, a2, rules) -> bool:
    """True when the price is not the pricing service's result for this cost result under the
    trusted rule (``price_line``: the service's own line formula)."""
    unit, line_scale, mode = rounding(rules)
    margin = gross_margin(rules)
    if price.currency != rules.document.get("currency") or price.gross_margin != margin:
        return True
    if tuple(getattr(price, f) for f in FLAGS) != tuple(rules.document.get(f) for f in FLAGS):
        return True
    parts = [Part.from_mapping(p.specification.model_dump(exclude_none=True)) for p in a2.parts]
    if price.calculation_id != calculation_id(parts, rules.rule_version):
        return True  # priced other parts, or other spellings of them
    if [line.part_id for line in price.lines] != [line.part_id for line in costs.lines]:
        return True
    for priced, cost in zip(price.lines, costs.lines, strict=True):
        expected = price_line(cost.total_manufacturing_cost, cost.quantity, margin, line_scale, unit, mode)  # fmt: skip
        if (priced.line_total_usd, priced.quoted_unit_price_usd) != expected:
            return True
    with localcontext(PLANNING):
        total = sum((line.line_total_usd for line in price.lines), Decimal(0))
    return price.quote_total_usd != total


def _blocked_result(costs: CostOutput, price: PriceOutput) -> bool:
    """A consistent non-priced verdict: both tools agree and carry no amounts."""
    return (
        costs.result == price.result
        and not costs.lines
        and not price.lines
        and costs.total_manufacturing_cost is None
        and price.quote_total_usd is None
        and price.calculation_id is None
    )


def _explain_priced(a: dict, n: int) -> str:
    excluded = [f.removesuffix("_included") for f in FLAGS if not a[f]]
    included = [f.removesuffix("_included") for f in FLAGS if a[f]]
    cur = a["currency"]
    text = (
        f"Calculated by {', '.join(TOOLS)} under {a['pricing_rule_version']} "
        f"({cur}, gross margin {a['gross_margin']}, {a['rounding']}): {n} line(s); "
        f"manufacturing cost {a['total_manufacturing_cost_usd']} {cur} (internal); "
        f"selling total {a['quote_total_usd']} {cur}"
    )
    if excluded:
        text += f", excluding {', '.join(excluded)}"
    if included:
        text += f", including {', '.join(included)}"
    text += (
        "; no discounts are applied (the pricing rules define none). A deterministic, "
        "non-binding calculation: not a quote, not approved, not released."
    )
    if a["supply_status"] == "constrained":
        text += " Supply is constrained (see the supply assessment); review is required."
    return text


async def evaluate_pricing(
    a2: ManufacturingAssessment,
    a3: SupplyAssessment,
    execute: ToolExecutor | None,
    *,
    pricing_rules: RuleVersionDetails | None,
    timeout_seconds: float = DEFAULT_TIMEOUT,
) -> PricingAssessment:
    """Manufacturing cost and selling price for the parts A2 and A3 cleared.

    ``execute`` must be a trusted, principal-bound executor (``graph.mcp_executor``);
    ``pricing_rules`` the trusted ``price-v1.0`` rule (``mcp.runtime.pinned_rules``)."""
    if not isinstance(a2, ManufacturingAssessment) or not isinstance(a3, SupplyAssessment):
        raise ValueError("evaluate_pricing needs a ManufacturingAssessment and a SupplyAssessment")
    base = {"source_id": a2.source_id, "rfq_id": a2.rfq_id, "supply_status": a3.status}

    def blocked(status: Status, reason: str | None = None, error=None) -> PricingAssessment:
        text = f"Pricing was not evaluated ({reason or error}); no cost or price."
        return PricingAssessment(**base, status=status, explanation=text, not_evaluated_reason=reason, error=error)  # fmt: skip

    if not _trusted(pricing_rules):
        return blocked("not_evaluated", "pricing_rules_untrusted")
    reason = _gate(a2, a3, pricing_rules)
    if reason:
        return blocked("not_evaluated", reason)
    if execute is None:
        return blocked("error", error="unauthenticated")
    specs = tuple(p.specification for p in a2.parts)
    request = PlanningInput(parts=specs, horizon_start=a3.horizon_start)  # validated by the gate
    outputs = {"estimate_costs": CostOutput, "calculate_price": PriceOutput}
    results = {}
    try:
        async with asyncio.timeout(timeout_seconds):
            for tool in TOOLS:  # sequential, once each, no retries
                raw = await execute(tool, request)
                data = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw
                results[tool] = outputs[tool].model_validate(data)
    except McpCallError as error:
        return blocked("error", error=error.code)
    except ValidationError:
        return blocked("error", error="invalid_output")
    except TimeoutError:
        return blocked("error", error="timeout")
    except Exception:  # an executor fault: its text never surfaces
        return blocked("error", error="unavailable")
    costs, price = results["estimate_costs"], results["calculate_price"]
    rule = (costs.pricing_rule_version, costs.pricing_rule_fingerprint)
    if rule != (price.pricing_rule_version, price.pricing_rule_fingerprint):
        return blocked("error", error="invalid_evidence")  # two rules: not one evaluation
    lead = a3.evidence.lead_time
    evidence = PricingEvidence(
        arguments_sha256=_sha(request),
        supply_arguments_sha256=a3.evidence.arguments_sha256,
        inventory_snapshot_id=lead.inventory_snapshot_id,
        capacity_snapshot_id=lead.capacity_snapshot_id,
        costs=costs,
        price=price,
    )
    review = {**base, "pricing_rule_version": rule[0], "pricing_rule_fingerprint": rule[1], "evidence": evidence}  # fmt: skip
    if rule != (pricing_rules.rule_version, pricing_rules.fingerprint):
        text = f"The pricing tools ran under {rule[0]}, not the trusted {PRICING_RULE_VERSION}; no amount is reported; review is required."  # fmt: skip
        return PricingAssessment(**review, status="requires_review", review_reasons=("pricing_rule_drift",), explanation=text)  # fmt: skip
    if (costs.result, price.result) != ("costed", "priced"):
        if not _blocked_result(costs, price):
            return blocked("error", error="invalid_evidence")
        text = f"The pricing tools found the parts {costs.result}, contradicting the manufacturing and supply assessments; no amount is reported; review is required."  # fmt: skip
        return PricingAssessment(**review, status="requires_review", review_reasons=("pricing_contradicts_upstream",), explanation=text)  # fmt: skip
    if _costs_inconsistent(costs, a2, pricing_rules) or _price_inconsistent(price, costs, a2, pricing_rules):  # fmt: skip
        return blocked("error", error="invalid_evidence")  # unverifiable evidence is not kept
    unit, line_scale, mode = rounding(pricing_rules)
    stock = {p.part_id: p.stock.status if p.stock else None for p in a3.parts}
    lines = []
    for p, cost, priced in zip(a2.parts, costs.lines, price.lines, strict=True):
        shown = display_costs(cost, pricing_rules)  # the service's display rounding
        lines.append(
            PricingLine(
                index=p.index,
                part_id=p.part_id,
                quantity=cost.quantity,
                operations=cost.operations,
                **{k: v for k, v in shown.items() if k.endswith("_usd")},
                quoted_unit_price_usd=priced.quoted_unit_price_usd,
                line_total_usd=priced.line_total_usd,
                stock_status=stock[p.part_id],
            )
        )
    with localcontext(PLANNING):  # the RFQ cost at the scale display_costs gives line costs
        cost_total = costs.total_manufacturing_cost.quantize(unit, rounding=mode)
    fields = {
        **review,
        "currency": price.currency,
        "unit_scale": unit,
        "line_scale": line_scale,
        "rounding": mode,
        "gross_margin": price.gross_margin,
        **{f: getattr(price, f) for f in FLAGS},
        "calculation_id": price.calculation_id,
        "lines": tuple(lines),
        "total_manufacturing_cost_usd": cost_total,
        "quote_total_usd": price.quote_total_usd,
        "review_reasons": ("supply_constrained",) if a3.status == "constrained" else (),
    }
    return PricingAssessment(**fields, status="priced", explanation=_explain_priced(fields, len(lines)))  # fmt: skip
