"""Manufacturing cost per quote line: material, labour, finishing, packaging and setup.

Follows the reference calculate_part up to manufacturing cost; margin, line totals and
the quote total belong to the pricing service. Masses come from the inventory check,
the route from the manufacturing validator and per-unit run minutes from the capacity
service. Every rate comes from the pinned pricing rule.

Values stay unrounded (the reference's 28-digit Decimal context) so pricing works from
the same numbers; display_costs rounds them with the rule's own scales and mode.
"""

import decimal
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.entities import Part
from rfq_copilot.models import Material
from rfq_copilot.repositories import RuleVersionDetails, list_materials
from rfq_copilot.seeding.transform import decimal_value
from rfq_copilot.services.capacity import MINUTES_PER_HOUR, PLANNING, operation_minutes
from rfq_copilot.services.inventory import InventoryCheck
from rfq_copilot.services.materials import require_manufacturing_rules

CostResult = Literal["costed", "needs_clarification", "not_manufacturable"]
# Finishing and packaging are charged per unit and per m2 instead of by labour hour.
NOT_LABOUR = frozenset({"finish", "pack"})
ROUNDING_MODES = frozenset(
    {
        "ROUND_HALF_UP",
        "ROUND_HALF_EVEN",
        "ROUND_HALF_DOWN",
        "ROUND_UP",
        "ROUND_DOWN",
        "ROUND_CEILING",
        "ROUND_FLOOR",
    }
)


@dataclass(frozen=True)
class OperationCost:
    """One routed operation: unit run minutes, hourly rate, unit labour, line setup."""

    operation_id: str
    run_minutes_per_unit: Decimal
    hourly_rate_usd: Decimal
    labour_per_unit_usd: Decimal
    setup_minutes: Decimal
    setup_cost_usd: Decimal


@dataclass(frozen=True)
class LineCost:
    """Unrounded manufacturing cost of one quote line, in USD."""

    part_id: str
    quantity: int
    operations: tuple[str, ...]
    blank_area_m2: Decimal
    net_kg_per_unit: Decimal
    allowance_kg_per_unit: Decimal
    material_cost_usd_per_kg: Decimal
    material_cost_per_unit: Decimal
    labour_cost_per_unit: Decimal
    finish_cost_per_unit: Decimal
    packaging_cost_per_unit: Decimal
    variable_cost_per_unit: Decimal
    setup_cost_per_line: Decimal
    total_manufacturing_cost: Decimal
    operation_costs: tuple[OperationCost, ...]


@dataclass(frozen=True)
class CostEstimate:
    result: CostResult
    lines: tuple[LineCost, ...]
    total_manufacturing_cost: Decimal | None
    inventory: InventoryCheck
    pricing_rule_version: str
    pricing_rule_fingerprint: str


def _pricing(pricing: RuleVersionDetails) -> None:
    if not isinstance(pricing, RuleVersionDetails) or pricing.rule_kind != "pricing":
        raise ValueError("Cost estimates require a pricing rule version.")


def _rate(pricing: RuleVersionDetails, *path: str) -> Decimal:
    value = pricing.document
    for key in path:
        if not hasattr(value, "get") or key not in value:
            raise ValueError(f"{pricing.rule_version} has no {'.'.join(path)}.")
        value = value[key]
    return decimal_value(value, f"{pricing.rule_version}.{'.'.join(path)}", 18, 6)


def line_cost(
    part: Part,
    inventory: InventoryCheck,
    index: int,
    material: Material,
    manufacturing_rules: RuleVersionDetails,
    pricing_rules: RuleVersionDetails,
) -> LineCost:
    """Cost of parts[index]; the order of operations mirrors the reference exactly."""
    requirement = inventory.parts[index]
    route = inventory.manufacturing[index].operations
    if material.material_id != part.material_id:
        raise ValueError("Material identifier mismatch.")
    if material.pricing_rule_version != pricing_rules.rule_version:
        raise ValueError(
            f"Material {material.material_id} is priced under {material.pricing_rule_version}, "
            f"not {pricing_rules.rule_version}."
        )
    unit = operation_minutes(
        replace(part, quantity=1), requirement.blank_area_m2, pricing_rules, manufacturing_rules
    )
    with localcontext(PLANNING):
        material_unit = requirement.allowance_kg_per_unit * material.cost_usd_per_kg
        operations, labour = [], Decimal(0)
        for op in route:
            rate = _rate(pricing_rules, "workcentre_hourly_rates_usd", op)
            setup_minutes = _rate(pricing_rules, "setup_minutes", op)
            op_labour = Decimal(0) if op in NOT_LABOUR else (unit[op] / MINUTES_PER_HOUR) * rate
            labour += op_labour
            setup = setup_minutes * rate / MINUTES_PER_HOUR
            operations.append(OperationCost(op, unit[op], rate, op_labour, setup_minutes, setup))
        finish = Decimal(0)
        if "finish" in route:
            finish = requirement.blank_area_m2 * _rate(
                pricing_rules, "finishing_usd_per_m2", part.finish_id
            )
        packaging = _rate(pricing_rules, "packaging_usd_per_unit")
        variable = material_unit + labour + finish + packaging
        setup_total = sum((o.setup_cost_usd for o in operations), Decimal(0))
        total = part.quantity * variable + setup_total
    return LineCost(
        part_id=part.part_id,
        quantity=part.quantity,
        operations=route,
        blank_area_m2=requirement.blank_area_m2,
        net_kg_per_unit=requirement.net_kg_per_unit,
        allowance_kg_per_unit=requirement.allowance_kg_per_unit,
        material_cost_usd_per_kg=material.cost_usd_per_kg,
        material_cost_per_unit=material_unit,
        labour_cost_per_unit=labour,
        finish_cost_per_unit=finish,
        packaging_cost_per_unit=packaging,
        variable_cost_per_unit=variable,
        setup_cost_per_line=setup_total,
        total_manufacturing_cost=total,
        operation_costs=tuple(operations),
    )


def evaluate_costs(
    parts: Sequence[Part],
    inventory: InventoryCheck,
    materials: Sequence[Material],
    *,
    manufacturing_rules: RuleVersionDetails,
    pricing_rules: RuleVersionDetails,
) -> CostEstimate:
    """Pure cost model over the inventory check and the catalog rows of its materials."""
    parts = tuple(parts)
    require_manufacturing_rules(manufacturing_rules)
    _pricing(pricing_rules)
    if not isinstance(inventory, InventoryCheck) or len(parts) != len(inventory.manufacturing):
        raise ValueError("The inventory check must cover exactly these parts.")
    if (inventory.pricing_rule_version, inventory.pricing_rule_fingerprint) != (
        pricing_rules.rule_version,
        pricing_rules.fingerprint,
    ) or any(
        m.rule_fingerprint != manufacturing_rules.fingerprint for m in inventory.manufacturing
    ):
        raise ValueError("The inventory check used different rule versions.")

    def estimate(result, lines=(), total=None):
        return CostEstimate(
            result,
            tuple(lines),
            total,
            inventory,
            pricing_rules.rule_version,
            pricing_rules.fingerprint,
        )

    if inventory.result in ("needs_clarification", "not_manufacturable"):
        return estimate(inventory.result)
    catalog = {m.material_id: m for m in materials}
    lines = []
    for index, part in enumerate(parts):
        if part.material_id not in catalog:
            raise ValueError(f"No catalog row was supplied for {part.material_id}.")
        lines.append(
            line_cost(
                part,
                inventory,
                index,
                catalog[part.material_id],
                manufacturing_rules,
                pricing_rules,
            )
        )
    with localcontext(PLANNING):
        total = sum((line.total_manufacturing_cost for line in lines), Decimal(0))
    return estimate("costed", lines, total)


def rounding(pricing: RuleVersionDetails) -> tuple[Decimal, Decimal, str]:
    """(unit scale, line scale, rounding mode) declared by the pricing rule."""
    mode = pricing.document.get("rounding")
    if mode not in ROUNDING_MODES:
        raise ValueError(f"{pricing.rule_version} declares an unknown rounding mode {mode!r}.")
    unit = _rate(pricing, "unit_display_scale")
    line = _rate(pricing, "line_total_scale")
    return unit, line, getattr(decimal, mode)


def display_costs(line: LineCost, pricing: RuleVersionDetails) -> dict[str, object]:
    """The line's cost fields as the pricing breakdown displays them (reference scales)."""
    unit, scale, mode = rounding(pricing)

    def show(value: Decimal, quantum: Decimal) -> str:
        # quantize obeys the active context's precision: use the fixed one, not the caller's.
        with localcontext(PLANNING):
            return str(value.quantize(quantum, rounding=mode))

    return {
        "part_id": line.part_id,
        "quantity": line.quantity,
        "operations": list(line.operations),
        "material_cost_per_unit_usd": show(line.material_cost_per_unit, unit),
        "labor_cost_per_unit_usd": show(line.labour_cost_per_unit, unit),
        "finish_cost_per_unit_usd": show(line.finish_cost_per_unit, unit),
        "packaging_cost_per_unit_usd": show(line.packaging_cost_per_unit, unit),
        "variable_cost_per_unit_usd": show(line.variable_cost_per_unit, unit),
        "setup_cost_per_line_usd": show(line.setup_cost_per_line, scale),
        "total_manufacturing_cost_usd": show(line.total_manufacturing_cost, unit),
        "pricing_rule_version": pricing.rule_version,
    }


async def estimate_costs(
    session: AsyncSession,
    parts: Sequence[Part],
    inventory: InventoryCheck,
    *,
    manufacturing_rules: RuleVersionDetails,
    pricing_rules: RuleVersionDetails,
) -> CostEstimate:
    """Read the catalog rows (cost, pricing version) for the parts' materials, then cost."""
    parts = tuple(parts)
    _pricing(pricing_rules)
    if not isinstance(inventory, InventoryCheck) or len(parts) != len(inventory.manufacturing):
        raise ValueError("The inventory check must cover exactly these parts.")
    materials: tuple[Material, ...] = ()
    if inventory.result in ("sufficient", "insufficient"):
        materials = await list_materials(session, [p.material_id for p in parts])
    return evaluate_costs(
        parts,
        inventory,
        materials,
        manufacturing_rules=manufacturing_rules,
        pricing_rules=pricing_rules,
    )
