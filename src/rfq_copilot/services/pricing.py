"""Deterministic quote pricing (calculate_quote): selling totals from manufacturing cost.

line total  = round(manufacturing cost / (1 - gross margin), line scale)
unit price  = round(line total / quantity, unit scale)   (advisory, display only)
quote total = sum of the already rounded line totals
Margin, scales, rounding mode, currency and inclusion flags come from the pinned pricing
rule. Nothing is persisted or approved; draft persistence and approval are out of scope.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.canonical import fingerprint
from rfq_copilot.entities import Part
from rfq_copilot.repositories import RuleVersionDetails, list_operations
from rfq_copilot.seeding.transform import decimal_value
from rfq_copilot.services.capacity import PLANNING
from rfq_copilot.services.costing import CostEstimate, LineCost, display_costs, rounding

PriceResult = Literal["priced", "needs_clarification", "not_manufacturable"]
CALCULATION_PREFIX = "CALC-"
# K3 (integrity): the reusable pricing-content fingerprint of a persisted calculation record.
CALCULATION_CONTENT_VERSION = "calculation-content-v1"
_UNROUNDED = (
    "material_cost_per_unit",
    "labour_cost_per_unit",
    "finish_cost_per_unit",
    "packaging_cost_per_unit",
    "variable_cost_per_unit",
    "setup_cost_per_line",
    "total_manufacturing_cost",
)
# Canonical number spelling for calculation IDs: one value, one spelling. Integral
# dimensions are JSON integers; these fields keep at least this many decimals (the
# dataset's contract convention), so the IDs also match the reference's.
_MIN_DECIMALS = {"thickness_mm": 1, "tolerance_mm": 1, "bend_radius_mm": 2}


@dataclass(frozen=True)
class PricedLine:
    cost: LineCost
    line_total_usd: Decimal
    quoted_unit_price_usd: Decimal


@dataclass(frozen=True)
class PriceCalculation:
    result: PriceResult
    calculation_id: str | None
    currency: str
    gross_margin: Decimal
    lines: tuple[PricedLine, ...]
    quote_total_usd: Decimal | None
    tax_included: bool
    freight_included: bool
    shipping_included: bool
    costs: CostEstimate
    pricing_rule_version: str
    pricing_rule_fingerprint: str


def _canonical_number(name: str, value: Decimal) -> int | str:
    if name not in _MIN_DECIMALS and value == value.to_integral_value():
        return int(value)
    text = format(value.normalize(), "f")
    whole, _, fraction = text.partition(".")
    return f"{whole}.{fraction.ljust(_MIN_DECIMALS.get(name, 1), '0')}"


def canonical_part(part: Part) -> dict[str, object]:
    """The part's stated fields in one canonical spelling; unstated fields are omitted."""
    canonical: dict[str, object] = {}
    for name, value in vars(part).items():
        if value is None:
            continue
        canonical[name] = _canonical_number(name, value) if isinstance(value, Decimal) else value
    return canonical


def calculation_id(parts: Sequence[Part], pricing_rule_version: str) -> str:
    """Stable identifier of (parts in order, pricing version), independent of spelling."""
    payload = json.dumps(
        {"parts": [canonical_part(p) for p in parts], "pricing_rule_version": pricing_rule_version},
        sort_keys=True,
        separators=(",", ":"),
    )
    return CALCULATION_PREFIX + hashlib.sha256(payload.encode()).hexdigest()[:20].upper()


def gross_margin(pricing: RuleVersionDetails) -> Decimal:
    margin = decimal_value(
        pricing.document.get("gross_margin"), f"{pricing.rule_version}.gross_margin", 12, 6
    )
    if not Decimal(0) <= margin < 1:
        raise ValueError(f"{pricing.rule_version}.gross_margin must be in [0, 1).")
    return margin


def _flag(pricing: RuleVersionDetails, key: str) -> bool:
    value = pricing.document.get(key)
    if type(value) is not bool:
        raise ValueError(f"{pricing.rule_version}.{key} must be true or false.")
    return value


def price_line(
    total_cost: Decimal,
    quantity: int,
    margin: Decimal,
    line_scale: Decimal,
    unit_scale: Decimal,
    mode: str,
) -> tuple[Decimal, Decimal]:
    """(line total, advisory unit price) of one costed line, as the module docstring states.
    The one implementation: evaluate_pricing and the Pricing Agent's evidence check use it."""
    with localcontext(PLANNING):
        total = (total_cost / (1 - margin)).quantize(line_scale, rounding=mode)
        unit_price = (total / quantity).quantize(unit_scale, rounding=mode)
    return total, unit_price


def evaluate_pricing(
    parts: Sequence[Part],
    costs: CostEstimate,
    quoted_operations: Mapping[str, bool],
    pricing_rules: RuleVersionDetails,
) -> PriceCalculation:
    """Pure pricing of a cost estimate. quoted_operations maps operation -> is_quoted."""
    parts = tuple(parts)
    if not isinstance(pricing_rules, RuleVersionDetails) or pricing_rules.rule_kind != "pricing":
        raise ValueError("Pricing requires a pricing rule version.")
    if not isinstance(costs, CostEstimate) or (
        costs.pricing_rule_version,
        costs.pricing_rule_fingerprint,
    ) != (pricing_rules.rule_version, pricing_rules.fingerprint):
        raise ValueError("The cost estimate used a different pricing rule version.")
    if not parts or len(parts) != len(costs.inventory.manufacturing):
        raise ValueError("The cost estimate must cover exactly these parts.")
    margin = gross_margin(pricing_rules)
    unit, line_scale, mode = rounding(pricing_rules)
    currency = pricing_rules.document.get("currency")
    if not isinstance(currency, str) or len(currency) != 3:
        raise ValueError(f"{pricing_rules.rule_version}.currency must be a 3-letter code.")
    flags = [
        _flag(pricing_rules, k) for k in ("tax_included", "freight_included", "shipping_included")
    ]

    def calculation(result, identifier=None, lines=(), total=None):
        return PriceCalculation(
            result,
            identifier,
            currency,
            margin,
            tuple(lines),
            total,
            *flags,
            costs,
            pricing_rules.rule_version,
            pricing_rules.fingerprint,
        )

    if costs.result != "costed":
        return calculation(costs.result)
    if [line.part_id for line in costs.lines] != [p.part_id for p in parts]:
        raise ValueError("The cost estimate lines do not match these parts.")
    priced = []
    for line in costs.lines:
        # TD-001: only quoted operations can be priced; anything else is a catalog defect.
        unquoted = [op for op in line.operations if quoted_operations.get(op) is not True]
        if unquoted:
            raise ValueError(f"Operations {unquoted} are not quoted and cannot be priced.")
        total, unit_price = price_line(
            line.total_manufacturing_cost, line.quantity, margin, line_scale, unit, mode
        )
        priced.append(PricedLine(line, total, unit_price))
    with localcontext(PLANNING):
        quote_total = sum((p.line_total_usd for p in priced), Decimal(0))
    return calculation(
        "priced", calculation_id(parts, pricing_rules.rule_version), priced, quote_total
    )


def display_quote(calculation: PriceCalculation, pricing: RuleVersionDetails) -> dict[str, object]:
    """The calculation in the pricing-breakdown shape (money as fixed-scale strings)."""
    lines = []
    for priced in calculation.lines:
        line = display_costs(priced.cost, pricing)
        line.update(
            blank_area_m2=str(priced.cost.blank_area_m2),
            net_material_kg_per_unit=str(priced.cost.net_kg_per_unit),
            material_allowance_kg_per_unit=str(priced.cost.allowance_kg_per_unit),
            line_total_usd=str(priced.line_total_usd),
            quoted_unit_price_usd=str(priced.quoted_unit_price_usd),
        )
        lines.append(line)
    return {
        "calculation_id": calculation.calculation_id,
        "currency": calculation.currency,
        "pricing_rule_version": calculation.pricing_rule_version,
        "lines": lines,
        "quote_total_usd": None
        if calculation.quote_total_usd is None
        else str(calculation.quote_total_usd),
        "tax_included": calculation.tax_included,
        "freight_included": calculation.freight_included,
        "shipping_included": calculation.shipping_included,
    }


def calculation_record_content(
    calculation: PriceCalculation, pricing: RuleVersionDetails, parts: Sequence[Part]
) -> tuple[list[dict[str, object]], str, str]:
    """``(lines, content_sha256, manufacturing_rule_version)`` of a priced calculation (K3).

    ``lines`` is the internal per-line record stored in ``pricing_calculations.lines``: the
    ``display_quote`` line, the canonical part (specification), the material master facts
    used (cost per kg, density), the unrounded cost components,
    every operation's minutes, rate and setup, and the manufacturing rule that routed it.
    Decimals are exact ``str()`` spellings. ``content_sha256`` fingerprints those lines with
    ``calculation_id``, both rule fingerprints, margin, scrap, currency, inclusion flags and
    the total: identical only for identical pricing inputs and results. It holds no RFQ,
    customer, snapshot, horizon or validity: those are version evidence. Internal: costs.
    """
    parts = tuple(parts)
    if calculation.result != "priced" or calculation.quote_total_usd is None:
        raise ValueError("Only a priced calculation has calculation content.")
    if (calculation.pricing_rule_version, calculation.pricing_rule_fingerprint) != (
        pricing.rule_version,
        pricing.fingerprint,
    ):
        raise ValueError("The calculation used a different pricing rule version.")
    checks = calculation.costs.inventory.manufacturing
    routed = {(check.rule_version, check.rule_fingerprint) for check in checks}
    if len(routed) != 1 or len(checks) != len(parts):
        raise ValueError("The calculation must be routed by exactly one manufacturing rule.")
    ((manufacturing_version, manufacturing_fingerprint),) = routed
    shown = display_quote(calculation, pricing)["lines"]
    lines = []
    for part, priced, display, check in zip(parts, calculation.lines, shown, checks, strict=True):
        cost, material = priced.cost, check.material.material
        if (cost.part_id, material.material_id) != (part.part_id, part.material_id):
            raise ValueError("The calculation lines do not match these parts.")
        lines.append(
            {
                **display,
                "part": canonical_part(part),
                # Costing already refused a material priced under another pricing version.
                "material": {
                    "material_id": material.material_id,
                    "cost_usd_per_kg": str(cost.material_cost_usd_per_kg),
                    "density_kg_m3": str(material.density_kg_m3),
                },
                "unrounded": {name: str(getattr(cost, name)) for name in _UNROUNDED},
                "operation_costs": [
                    {
                        "operation_id": op.operation_id,
                        "run_minutes_per_unit": str(op.run_minutes_per_unit),
                        "hourly_rate_usd": str(op.hourly_rate_usd),
                        "labour_per_unit_usd": str(op.labour_per_unit_usd),
                        "setup_minutes": str(op.setup_minutes),
                        "setup_cost_usd": str(op.setup_cost_usd),
                    }
                    for op in cost.operation_costs
                ],
                "manufacturing_rule_version": check.rule_version,
            }
        )
    content = fingerprint(
        {
            "fingerprint_version": CALCULATION_CONTENT_VERSION,
            "calculation_id": calculation.calculation_id,
            "currency": calculation.currency,
            "gross_margin": str(calculation.gross_margin),
            "scrap_fraction": str(calculation.costs.inventory.scrap_fraction),
            "tax_included": calculation.tax_included,
            "freight_included": calculation.freight_included,
            "shipping_included": calculation.shipping_included,
            "pricing_rule": [pricing.rule_version, pricing.fingerprint],  # equal: checked above
            "manufacturing_rule": [manufacturing_version, manufacturing_fingerprint],
            "lines": lines,
            "quote_total_usd": str(calculation.quote_total_usd),
        }
    )
    return lines, content, manufacturing_version


async def calculate_quote(
    session: AsyncSession,
    parts: Sequence[Part],
    costs: CostEstimate,
    *,
    pricing_rules: RuleVersionDetails,
) -> PriceCalculation:
    """Read is_quoted for the routed operations (one SELECT), then price."""
    if not isinstance(costs, CostEstimate):
        raise ValueError("Pricing requires a manufacturing cost estimate.")
    if not isinstance(pricing_rules, RuleVersionDetails) or pricing_rules.rule_kind != "pricing":
        raise ValueError("Pricing requires a pricing rule version.")
    operations = sorted({op for line in costs.lines for op in line.operations})
    quoted = {op.operation_id: op.is_quoted for op in await list_operations(session, operations)}
    return evaluate_pricing(parts, costs, quoted, pricing_rules)
