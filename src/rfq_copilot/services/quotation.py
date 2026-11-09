"""Multi-part quotations: one RFQ's lines evaluated, scheduled, costed and priced together.

Each business service runs exactly once per quotation: lead time (manufacturability,
stock and capacity), costing (on the same stock result) and pricing. Lines share stock and workcentre hours and are taken in RFQ line order, which
is significant: the weekly plan and the calculation_id both depend on it.

A quotation is all or nothing, as in the dataset: if any line needs clarification or
cannot be made, nothing is priced, and the blocking lines are named so only they need
follow-up. Nothing is persisted, reserved or approved.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.entities import Part
from rfq_copilot.repositories import RuleVersionDetails, get_rfq
from rfq_copilot.services.capacity import OperationLoad
from rfq_copilot.services.costing import CostEstimate, estimate_costs
from rfq_copilot.services.lead_time import LeadTimeEstimate, estimate_lead_time
from rfq_copilot.services.manufacturing import ManufacturingCheck
from rfq_copilot.services.pricing import PriceCalculation, PricedLine, calculate_quote

QuotationResult = Literal["quoted", "needs_clarification", "not_manufacturable"]


@dataclass(frozen=True)
class LineOutcome:
    """Everything decided about one RFQ line, for explanation."""

    line_number: int
    part_id: str
    manufacturing: ManufacturingCheck
    material_key: tuple[str, Decimal] | None
    stock_status: str | None
    procurement_business_days: int | None
    operations: tuple[OperationLoad, ...]
    unscheduled: OperationLoad | None
    last_week_start: date | None
    priced: PricedLine | None


@dataclass(frozen=True)
class Quotation:
    result: QuotationResult
    lines: tuple[LineOutcome, ...]
    blocking_lines: tuple[int, ...]
    calculation_id: str | None
    quote_total_usd: Decimal | None
    currency: str | None
    estimated_ready_date: date | None
    lead_business_days: int | None
    findings: tuple[str, ...]
    lead_time: LeadTimeEstimate
    costs: CostEstimate | None
    pricing: PriceCalculation | None


def validate_lines(parts: Sequence[Part], line_numbers: Sequence[int]) -> None:
    if not parts or not all(isinstance(p, Part) for p in parts):
        raise ValueError("A quotation needs one or more Parts.")
    if len(line_numbers) != len(parts) or any(type(n) is not int or n < 1 for n in line_numbers):
        raise ValueError("Each line needs a positive integer line number.")
    if list(line_numbers) != sorted(set(line_numbers)):
        raise ValueError("Line numbers must be unique and in ascending order.")
    ids = [p.part_id for p in parts]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"Part IDs must be unique within a quotation: {duplicates}.")


def assemble_quotation(
    parts: Sequence[Part],
    line_numbers: Sequence[int],
    lead_time: LeadTimeEstimate,
    costs: CostEstimate | None,
    pricing: PriceCalculation | None,
) -> Quotation:
    """Pure: per-line outcomes and the quotation result from the service results."""
    parts, line_numbers = tuple(parts), tuple(line_numbers)
    validate_lines(parts, line_numbers)
    inventory = lead_time.inventory
    if len(inventory.manufacturing) != len(parts):
        raise ValueError("The lead-time estimate must cover exactly these lines.")
    if pricing is not None and pricing.costs is not costs:
        raise ValueError("The pricing must be computed from this cost estimate.")
    if costs is not None and costs.inventory is not inventory:
        raise ValueError("The costs must be computed from this lead-time estimate's stock check.")
    stock = {pid: line for line in inventory.lines for pid in line.part_ids}
    delays = {
        (d.material_id, d.thickness_mm): d.procurement_business_days
        for d in lead_time.material_delays
    }
    capacity = lead_time.capacity
    prices = {p.cost.part_id: p for p in pricing.lines} if pricing is not None else {}
    outcomes = []
    for number, part, check in zip(line_numbers, parts, inventory.manufacturing, strict=True):
        line = stock.get(part.part_id)
        key = None if line is None else (line.material_id, line.thickness_mm)
        planned = (
            tuple(op for op in capacity.operations if op.part_id == part.part_id)
            if capacity
            else ()
        )
        unscheduled = capacity.unscheduled if capacity else None
        outcomes.append(
            LineOutcome(
                number,
                part.part_id,
                check,
                key,
                None if line is None else line.status,
                None if key is None else delays.get(key),
                planned,
                unscheduled
                if unscheduled is not None and unscheduled.part_id == part.part_id
                else None,
                max((op.week_start for op in planned), default=None),
                prices.get(part.part_id),
            )
        )
    clarify = tuple(
        o.line_number for o in outcomes if o.manufacturing.result == "needs_clarification"
    )
    unsupported = tuple(o.line_number for o in outcomes if o.manufacturing.result == "unsupported")
    if clarify:
        result, blocking = "needs_clarification", clarify
    elif unsupported:
        result, blocking = "not_manufacturable", unsupported
    else:
        result, blocking = "quoted", ()
        if pricing is None or pricing.result != "priced":
            raise ValueError("A manufacturable quotation must be priced.")
    priced = result == "quoted"
    return Quotation(
        result,
        tuple(outcomes),
        blocking,
        pricing.calculation_id if priced else None,
        pricing.quote_total_usd if priced else None,
        pricing.currency if priced else None,
        lead_time.estimated_ready_date,
        lead_time.lead_business_days,
        lead_time.findings,
        lead_time,
        costs,
        pricing,
    )


async def quote_parts(
    session: AsyncSession,
    parts: Sequence[Part],
    *,
    manufacturing_rules: RuleVersionDetails,
    pricing_rules: RuleVersionDetails,
    horizon_start: date,
    line_numbers: Sequence[int] | None = None,
    requested_delivery_date: date | None = None,
    inventory_snapshot: str | None = None,
    capacity_snapshot: str | None = None,
) -> Quotation:
    """Evaluate, schedule, cost and price an RFQ's lines, each service exactly once."""
    parts = tuple(parts)
    numbers = tuple(range(1, len(parts) + 1)) if line_numbers is None else tuple(line_numbers)
    validate_lines(parts, numbers)
    rules = {"manufacturing_rules": manufacturing_rules, "pricing_rules": pricing_rules}
    lead_time = await estimate_lead_time(
        session,
        parts,
        **rules,
        horizon_start=horizon_start,
        requested_delivery_date=requested_delivery_date,
        inventory_snapshot=inventory_snapshot,
        capacity_snapshot=capacity_snapshot,
    )
    costs = pricing = None
    if lead_time.inventory.result in ("sufficient", "insufficient"):
        costs = await estimate_costs(session, parts, lead_time.inventory, **rules)
        pricing = await calculate_quote(session, parts, costs, pricing_rules=pricing_rules)
    return assemble_quotation(parts, numbers, lead_time, costs, pricing)


async def quote_rfq(
    session: AsyncSession,
    rfq_id: str,
    *,
    customer_id: str,
    manufacturing_rules: RuleVersionDetails,
    pricing_rules: RuleVersionDetails,
    horizon_start: date,
    requested_delivery_date: date | None = None,
    inventory_snapshot: str | None = None,
    capacity_snapshot: str | None = None,
) -> Quotation | None:
    """Quote a stored RFQ's runtime lines; None if the RFQ is absent or not this customer's."""
    details = await get_rfq(session, rfq_id, customer_id=customer_id)
    if details is None:
        return None
    if not details.runtime_lines:
        raise ValueError(f"RFQ {rfq_id} has no runtime lines to quote.")
    return await quote_parts(
        session,
        [Part.from_line(line) for line in details.runtime_lines],
        line_numbers=[line.line_number for line in details.runtime_lines],
        manufacturing_rules=manufacturing_rules,
        pricing_rules=pricing_rules,
        horizon_start=horizon_start,
        requested_delivery_date=requested_delivery_date,
        inventory_snapshot=inventory_snapshot,
        capacity_snapshot=capacity_snapshot,
    )
