"""Material availability for an RFQ: kilograms required versus free stock, read only.

Formulas follow the dataset's reference: blank area by part type, net mass from
thickness and catalog density, a scrap allowance from the pinned pricing rule, summed
per (material, thickness). Free stock is the stored quantity_kg - reserved_kg. Nothing
is reserved, deducted or ordered; procurement facts are passed on for lead-time estimation.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, DivisionByZero, Inexact, InvalidOperation, localcontext
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.entities import Part
from rfq_copilot.repositories import (
    MaterialAvailability,
    RuleVersionDetails,
    get_material_availability,
)
from rfq_copilot.seeding.transform import decimal_value
from rfq_copilot.services.manufacturing import ManufacturingCheck, check_manufacturing_capability

# Exact arithmetic: any result that would need rounding raises instead of drifting.
EXACT = Context(prec=60, traps=[Inexact, InvalidOperation, DivisionByZero])
MM2_PER_M2 = Decimal(1_000_000)
MM_PER_M = Decimal(1000)

LineStatus = Literal["sufficient", "insufficient", "unavailable"]
InventoryResult = Literal["sufficient", "insufficient", "needs_clarification", "not_manufacturable"]
StockKey = tuple[str, Decimal]


@dataclass(frozen=True)
class PartRequirement:
    """Per-part material mass, in m2 and kg, exact (no display rounding)."""

    part_id: str
    material_id: str
    thickness_mm: Decimal
    quantity: int
    blank_area_m2: Decimal
    net_kg_per_unit: Decimal
    allowance_kg_per_unit: Decimal
    required_kg: Decimal


@dataclass(frozen=True)
class MaterialRequirement:
    """Demand versus free stock for one (material, thickness), plus lead-time facts."""

    material_id: str
    thickness_mm: Decimal
    part_ids: tuple[str, ...]
    required_kg: Decimal
    available_kg: Decimal
    shortage_kg: Decimal
    status: LineStatus
    availability_class: str
    inventory_ids: tuple[str, ...]
    snapshot_ids: tuple[str, ...]
    supplier_lead_business_days: tuple[int, ...]


@dataclass(frozen=True)
class InventoryCheck:
    result: InventoryResult
    parts: tuple[PartRequirement, ...]
    lines: tuple[MaterialRequirement, ...]
    manufacturing: tuple[ManufacturingCheck, ...]
    scrap_fraction: Decimal
    pricing_rule_version: str
    pricing_rule_fingerprint: str


def scrap_fraction(pricing: RuleVersionDetails) -> Decimal:
    """The pinned pricing rule's scrap allowance, an exact fraction in [0, 1)."""
    if not isinstance(pricing, RuleVersionDetails) or pricing.rule_kind != "pricing":
        raise ValueError("Material requirements require a pricing rule version.")
    value = pricing.document.get("scrap_fraction")
    fraction = decimal_value(value, f"{pricing.rule_version}.scrap_fraction", 12, 6)
    if not Decimal(0) <= fraction < 1:
        raise ValueError(f"{pricing.rule_version}.scrap_fraction must be in [0, 1).")
    return fraction


def blank_area_m2(part: Part) -> Decimal:
    """Flat blank area of one unit, in square metres."""
    length, width = part.length_mm, part.width_mm
    height = part.height_mm or Decimal(0)
    with localcontext(EXACT):
        if part.part_type in ("flat_panel", "mounting_plate"):
            mm2 = length * width
        elif part.part_type == "l_bracket":
            mm2 = (length + height) * width
        elif part.part_type == "u_bracket":
            mm2 = (width + 2 * height) * length
        elif part.part_type == "enclosure":
            mm2 = length * width + 2 * height * (length + width)
        elif part.part_type == "fabricated_assembly":
            mm2 = 2 * length * width
        else:
            raise ValueError(f"No blank-area formula for part type {part.part_type!r}.")
        return mm2 / MM2_PER_M2


def part_requirement(part: Part, density_kg_m3: Decimal, scrap: Decimal) -> PartRequirement:
    area = blank_area_m2(part)
    with localcontext(EXACT):
        net = area * part.thickness_mm / MM_PER_M * density_kg_m3
        allowance = net * (1 + scrap)
        required = allowance * part.quantity
    return PartRequirement(
        part.part_id,
        part.material_id,
        part.thickness_mm,
        part.quantity,
        area,
        net,
        allowance,
        required,
    )


def evaluate_inventory(
    parts: Sequence[Part],
    manufacturing: Sequence[ManufacturingCheck],
    stock: Mapping[StockKey, MaterialAvailability],
    pricing: RuleVersionDetails,
) -> InventoryCheck:
    """Pure decision. stock holds the availability read for every (material, thickness)."""
    scrap = scrap_fraction(pricing)
    if not parts or len(parts) != len(manufacturing):
        raise ValueError("Each part needs exactly one manufacturing check.")

    def check(result: InventoryResult, requirements=(), lines=()) -> InventoryCheck:
        return InventoryCheck(
            result,
            requirements,
            lines,
            tuple(manufacturing),
            scrap,
            pricing.rule_version,
            pricing.fingerprint,
        )

    results = {m.result for m in manufacturing}
    if "needs_clarification" in results:
        return check("needs_clarification")
    if "unsupported" in results:
        return check("not_manufacturable")

    requirements = tuple(
        part_requirement(part, m.material.material.density_kg_m3, scrap)
        for part, m in zip(parts, manufacturing, strict=True)
    )
    grouped: dict[StockKey, list[PartRequirement]] = {}
    for requirement in requirements:
        grouped.setdefault((requirement.material_id, requirement.thickness_mm), []).append(
            requirement
        )
    specs = {m.material.material.material_id: m.material.material for m in manufacturing}
    lines = []
    for key in sorted(grouped):
        members = grouped[key]
        if key not in stock:
            raise ValueError(f"No stock read was supplied for {key[0]} at {key[1]} mm.")
        availability = stock[key]
        with localcontext(EXACT):
            required = sum((r.required_kg for r in members), Decimal(0))
            available = availability.available_kg
            shortage = max(required - available, Decimal(0))
        status = (
            "sufficient"
            if available >= required
            else "insufficient"
            if available > 0
            else "unavailable"
        )
        rows = availability.inventory
        lines.append(
            MaterialRequirement(
                material_id=key[0],
                thickness_mm=members[0].thickness_mm,
                part_ids=tuple(r.part_id for r in members),
                required_kg=required,
                available_kg=available,
                shortage_kg=shortage,
                status=status,
                availability_class=specs[key[0]].availability_class,
                inventory_ids=tuple(row.inventory_id for row in rows),
                snapshot_ids=tuple(sorted({row.snapshot_id for row in rows})),
                supplier_lead_business_days=tuple(row.supplier_lead_business_days for row in rows),
            )
        )
    result = "sufficient" if all(line.status == "sufficient" for line in lines) else "insufficient"
    return check(result, requirements, tuple(lines))


async def check_inventory(
    session: AsyncSession,
    parts: Sequence[Part],
    *,
    manufacturing_rules: RuleVersionDetails,
    pricing_rules: RuleVersionDetails,
) -> InventoryCheck:
    """Validate each part for manufacturing, then read free stock per (material, thickness)."""
    scrap_fraction(pricing_rules)
    parts = tuple(parts)
    if not parts or not all(isinstance(part, Part) for part in parts):
        raise ValueError("Inventory checks require one or more Parts.")
    manufacturing = tuple(
        [await check_manufacturing_capability(session, p, manufacturing_rules) for p in parts]
    )
    stock: dict[StockKey, MaterialAvailability] = {}
    if all(m.result == "valid" for m in manufacturing):
        for part in parts:
            key = (part.material_id, part.thickness_mm)
            if key not in stock:
                availability = await get_material_availability(
                    session, part.material_id, thickness_mm=part.thickness_mm
                )
                if availability is None:
                    raise ValueError(f"Material {part.material_id} disappeared during the check.")
                stock[key] = availability
    return evaluate_inventory(parts, manufacturing, stock, pricing_rules)
