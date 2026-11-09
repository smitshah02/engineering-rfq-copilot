"""Production capacity: workcentre hours per routed operation and first-fit weekly planning.

Weekly free hours are the stored remaining_hours (nominal - downtime - scheduled).
Nothing is reserved or written; the plan only consumes hours in memory, shared by
the RFQ's own operations.

The start week is an input: turning procurement delay into a start week, the ready date
and lead business days are lead-time estimation.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.entities import Part
from rfq_copilot.models import OperationRouting
from rfq_copilot.repositories import (
    RuleVersionDetails,
    WorkcentreCapacity,
    get_workcentre_capacity,
    list_part_routing,
)
from rfq_copilot.seeding.transform import decimal_value
from rfq_copilot.services.inventory import InventoryCheck
from rfq_copilot.services.materials import require_manufacturing_rules

# Hours repeat (x / 60, weld length / 65), so they cannot be exact. A fixed context, the
# reference's Decimal default, keeps results identical whatever the caller's context is.
PLANNING = Context(prec=28, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation])
MINUTES_PER_HOUR = Decimal(60)
# TD-003: reference-only constants, in no versioned rule document. They belong to the
# dataset v1.1.0 rule set, identified by its pricing version; any other pricing version
# must define them explicitly instead of inheriting them silently.
REFERENCE_PRICING_VERSIONS = frozenset({"price-v1.0"})
FINISH_SIDES = Decimal(2)
FINISH_MINUTES_PER_M2 = Decimal("0.30")
PACK_MINUTES_PER_UNIT = Decimal("0.75")

CapacityResult = Literal["feasible", "infeasible", "needs_clarification", "not_manufacturable"]
Reason = Literal["NO_CAPACITY_IN_HORIZON", "MISSING_CAPACITY_DATA"]


@dataclass(frozen=True)
class OperationLoad:
    """Hours one routed operation needs; week fields are None when it was not planned."""

    part_id: str
    operation_id: str
    workcentre_id: str
    run_minutes: Decimal
    setup_minutes: Decimal
    required_hours: Decimal
    week_index: int | None
    week_start: date | None


@dataclass(frozen=True)
class WeekLoad:
    """A workcentre week this plan touched: free hours before and hours planned in it."""

    workcentre_id: str
    week_start: date
    remaining_hours: Decimal
    planned_hours: Decimal


@dataclass(frozen=True)
class CapacityCheck:
    result: CapacityResult
    reason: Reason | None
    operations: tuple[OperationLoad, ...]
    unscheduled: OperationLoad | None
    weeks: tuple[WeekLoad, ...]
    horizon_start: date
    start_week: int
    final_week_start: date | None
    capacity_snapshot_ids: tuple[str, ...]
    inventory: InventoryCheck
    pricing_rule_version: str
    pricing_rule_fingerprint: str


def _number(value: object, location: str) -> Decimal:
    return decimal_value(value, location, 12, 6)


def require_reference_constants(pricing_rule_version: str) -> None:
    """Refuse reference-only constants under a pricing version that did not define them."""
    if pricing_rule_version not in REFERENCE_PRICING_VERSIONS:
        raise ValueError(
            f"Reference-only constants are not defined for {pricing_rule_version} (TD-003)."
        )


def operation_minutes(
    part: Part,
    blank_area_m2: Decimal,
    pricing: RuleVersionDetails,
    manufacturing: RuleVersionDetails,
) -> dict[str, Decimal]:
    """Run minutes for the whole quantity, per operation (reference operation_minutes)."""
    require_reference_constants(pricing.rule_version)
    rates = pricing.document
    rule = pricing.rule_version
    holes = (
        manufacturing.document["defaults"]["hole_count"]
        if part.hole_count is None
        else part.hole_count
    )
    bends = manufacturing.document["bend_counts"].get(part.part_type, 0)
    q = Decimal(part.quantity)
    length, width = part.length_mm, part.width_mm
    height = part.height_mm or Decimal(0)
    weld = part.weld_length_mm or Decimal(0)
    with localcontext(PLANNING):
        perimeter = 2 * (length + width)
        perimeter += {"l_bracket": 2, "u_bracket": 4, "enclosure": 8}.get(
            part.part_type, 0
        ) * height
        if part.part_type == "fabricated_assembly":
            perimeter *= 2
        perimeter += holes * _number(rates["hole_cut_extra_mm"], f"{rule}.hole_cut_extra_mm")
        return {
            "laser": q * (perimeter / _number(rates["cutting_mm_per_min"], f"{rule}.cutting")),
            "bend": q * bends * _number(rates["bend_minutes_per_bend"], f"{rule}.bend"),
            "weld": q * weld / _number(rates["weld_mm_per_min"], f"{rule}.weld"),
            "finish": q * blank_area_m2 * FINISH_SIDES * FINISH_MINUTES_PER_M2,
            "inspect": q * _number(rates["inspection_minutes_per_unit"], f"{rule}.inspect"),
            "pack": q * PACK_MINUTES_PER_UNIT,
        }


def _validate(
    parts: Sequence[Part],
    inventory: InventoryCheck,
    manufacturing: RuleVersionDetails,
    pricing: RuleVersionDetails,
    horizon_start: date,
    start_week: int,
) -> None:
    require_manufacturing_rules(manufacturing)
    if not isinstance(pricing, RuleVersionDetails) or pricing.rule_kind != "pricing":
        raise ValueError("Capacity checks require a pricing rule version.")
    if not isinstance(inventory, InventoryCheck):
        raise ValueError("Capacity checks require the inventory check.")
    if not parts or len(parts) != len(inventory.manufacturing):
        raise ValueError("The inventory check must cover exactly these parts.")
    if (inventory.pricing_rule_version, inventory.pricing_rule_fingerprint) != (
        pricing.rule_version,
        pricing.fingerprint,
    ) or any(m.rule_fingerprint != manufacturing.fingerprint for m in inventory.manufacturing):
        raise ValueError("The inventory check used different rule versions.")
    if type(horizon_start) is not date or horizon_start.weekday() != 0:
        raise ValueError("The horizon must start on a Monday calendar date.")
    if type(start_week) is not int or start_week < 0:
        raise ValueError("The start week must be a non-negative integer.")


def evaluate_capacity(
    parts: Sequence[Part],
    inventory: InventoryCheck,
    routing: Mapping[str, Sequence[OperationRouting]],
    capacity: Mapping[str, WorkcentreCapacity],
    *,
    manufacturing_rules: RuleVersionDetails,
    pricing_rules: RuleVersionDetails,
    horizon_start: date,
    start_week: int = 0,
) -> CapacityCheck:
    """Pure first-fit plan: each operation takes the first week, at or after the previous
    operation's week, whose free hours cover it. The week cursor never moves back, across
    operations and across parts, and hours are shared by the whole RFQ."""
    parts = tuple(parts)
    _validate(parts, inventory, manufacturing_rules, pricing_rules, horizon_start, start_week)

    def check(result, reason=None, planned=(), unscheduled=None, weeks=(), final=None, snaps=()):
        return CapacityCheck(
            result,
            reason,
            tuple(planned),
            unscheduled,
            tuple(weeks),
            horizon_start,
            start_week,
            final,
            tuple(snaps),
            inventory,
            pricing_rules.rule_version,
            pricing_rules.fingerprint,
        )

    if inventory.result == "needs_clarification":
        return check("needs_clarification")
    if inventory.result == "not_manufacturable":
        return check("not_manufacturable")

    setups = pricing_rules.document["setup_minutes"]
    free: dict[tuple[str, int], Decimal] = {}
    horizons: dict[str, int] = {}
    snapshots: set[str] = set()
    for workcentre_id, reading in capacity.items():
        horizons[workcentre_id] = reading.workcentre.planning_horizon_weeks
        for row in reading.weeks:
            index, offset = divmod((row.week_start - horizon_start).days, 7)
            if offset == 0 and 0 <= index:
                free[(workcentre_id, index)] = row.remaining_hours
                snapshots.add(row.snapshot_id)
    remaining_before = dict(free)
    planned_hours: dict[tuple[str, int], Decimal] = {}

    def weeks_touched():
        return [
            WeekLoad(
                wc, horizon_start + timedelta(weeks=i), remaining_before.get((wc, i), Decimal(0)), h
            )
            for (wc, i), h in sorted(
                planned_hours.items(), key=lambda item: (item[0][1], item[0][0])
            )
        ]

    planned: list[OperationLoad] = []
    cursor = start_week
    for part, requirement, validation in zip(
        parts, inventory.parts, inventory.manufacturing, strict=True
    ):
        minutes = operation_minutes(
            part, requirement.blank_area_m2, pricing_rules, manufacturing_rules
        )
        stations = {
            step.operation_id: step.workcentre_id for step in routing.get(part.part_type, ())
        }
        for operation in validation.operations:
            if operation not in stations or operation not in setups:
                raise ValueError(f"No workcentre or setup is configured for {operation}.")
            workcentre_id = stations[operation]
            if workcentre_id not in capacity:
                raise ValueError(f"No capacity reading was supplied for {workcentre_id}.")
            setup = _number(setups[operation], f"{pricing_rules.rule_version}.setup_minutes")
            with localcontext(PLANNING):
                hours = (minutes[operation] + setup) / MINUTES_PER_HOUR
            load = OperationLoad(
                part.part_id, operation, workcentre_id, minutes[operation], setup, hours, None, None
            )
            horizon = horizons[workcentre_id]
            week = next(
                (
                    w
                    for w in range(cursor, horizon)
                    if free.get((workcentre_id, w), Decimal(0)) >= hours
                ),
                None,
            )
            if week is None:
                has_data = any(wc == workcentre_id and w < horizon for wc, w in free)
                reason = "NO_CAPACITY_IN_HORIZON" if has_data else "MISSING_CAPACITY_DATA"
                final = horizon_start + timedelta(weeks=cursor) if planned else None
                return check(
                    "infeasible", reason, planned, load, weeks_touched(), final, sorted(snapshots)
                )
            with localcontext(PLANNING):
                free[(workcentre_id, week)] -= hours
                key = (workcentre_id, week)
                planned_hours[key] = planned_hours.get(key, Decimal(0)) + hours
            cursor = week
            week_start = horizon_start + timedelta(weeks=week)
            planned.append(
                OperationLoad(
                    part.part_id,
                    operation,
                    workcentre_id,
                    minutes[operation],
                    setup,
                    hours,
                    week,
                    week_start,
                )
            )
    return check(
        "feasible",
        planned=planned,
        weeks=weeks_touched(),
        final=horizon_start + timedelta(weeks=cursor),
        snaps=sorted(snapshots),
    )


async def check_capacity(
    session: AsyncSession,
    parts: Sequence[Part],
    inventory: InventoryCheck,
    *,
    manufacturing_rules: RuleVersionDetails,
    pricing_rules: RuleVersionDetails,
    horizon_start: date,
    start_week: int = 0,
) -> CapacityCheck:
    """Read routing and weekly capacity for the routed workcentres, then plan in memory."""
    parts = tuple(parts)
    _validate(parts, inventory, manufacturing_rules, pricing_rules, horizon_start, start_week)
    routing: dict[str, tuple[OperationRouting, ...]] = {}
    capacity: dict[str, WorkcentreCapacity] = {}
    if inventory.result in ("sufficient", "insufficient"):
        for part in parts:
            if part.part_type not in routing:
                routing[part.part_type] = await list_part_routing(session, part.part_type)
        for part, validation in zip(parts, inventory.manufacturing, strict=True):
            stations = {s.operation_id: s.workcentre_id for s in routing[part.part_type]}
            for operation in validation.operations:
                workcentre_id = stations.get(operation)
                if workcentre_id is not None and workcentre_id not in capacity:
                    reading = await get_workcentre_capacity(
                        session, workcentre_id, week_start=horizon_start
                    )
                    if reading is None:
                        raise ValueError(f"Workcentre {workcentre_id} is not in the catalog.")
                    capacity[workcentre_id] = reading
    return evaluate_capacity(
        parts,
        inventory,
        routing,
        capacity,
        manufacturing_rules=manufacturing_rules,
        pricing_rules=pricing_rules,
        horizon_start=horizon_start,
        start_week=start_week,
    )
