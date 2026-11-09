"""Lead-time estimation (estimate_lead_time): procurement delay, start week, ready date.

Runs the deterministic chain: manufacturability and stock (check_inventory), the
procurement delay per short material, weekly capacity (check_capacity) from the resulting
start week, then the ready date (Friday of the final planned week) and lead business
days (Monday-Friday, no holidays, as the dataset specifies). Read only; a planning
estimate, not a commitment.

Exactly one inventory and one capacity snapshot are selected first (select_snapshots) and
used by every read, and their dates are checked against the horizon (planning-date policy):
both snapshots belong to one dataset version, neither was observed after the horizon
starts, and the capacity snapshot still holds weeks on or after the horizon start.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.entities import Part
from rfq_copilot.repositories import (
    RuleVersionDetails,
    SnapshotDetails,
    SnapshotMismatchError,
    get_snapshot,
)
from rfq_copilot.services.capacity import (
    CapacityCheck,
    check_capacity,
    require_reference_constants,
)
from rfq_copilot.services.inventory import InventoryCheck, MaterialRequirement, check_inventory

# TD-002 decision: follow the reference code. A short line waits for the longest
# positive supplier lead among its stock rows; with none (no row, or only zero leads)
# it waits this default: a reference-only constant, guarded by pricing version (TD-003).
DEFAULT_PROCUREMENT_BUSINESS_DAYS = 15
FRIDAY_OFFSET = timedelta(days=4)

LeadTimeResult = Literal["estimated", "infeasible", "needs_clarification", "not_manufacturable"]
CAPACITY_FINDINGS = {
    "NO_CAPACITY_IN_HORIZON": "CAPACITY_OUT_OF_HORIZON",
    "MISSING_CAPACITY_DATA": "CAPACITY_DATA_MISSING",
}


__all__ = ["SnapshotMismatchError"]  # re-exported: defined with the snapshot reads


@dataclass(frozen=True)
class SnapshotSelection:
    """The one inventory and one capacity snapshot a calculation used."""

    inventory: SnapshotDetails
    capacity: SnapshotDetails


@dataclass(frozen=True)
class MaterialDelay:
    material_id: str
    thickness_mm: Decimal
    status: str
    procurement_business_days: int


@dataclass(frozen=True)
class LeadTimeEstimate:
    result: LeadTimeResult
    estimated_ready_date: date | None
    lead_business_days: int | None
    procurement_business_days: int
    material_delays: tuple[MaterialDelay, ...]
    horizon_start: date
    earliest_start_date: date | None
    start_week: int | None
    requested_delivery_date: date | None
    delivery_feasible: bool | None
    findings: tuple[str, ...]
    inventory: InventoryCheck
    capacity: CapacityCheck | None
    snapshots: SnapshotSelection | None = None


def validate_planning_dates(selection: SnapshotSelection, horizon_start: date) -> None:
    """Planning-date policy: reject contradictory, future-dated or exhausted snapshots."""
    if type(horizon_start) is not date or horizon_start.weekday() != 0:
        raise ValueError("The horizon must start on a Monday calendar date.")
    inventory, capacity = selection.inventory, selection.capacity
    if (inventory.snapshot_kind, capacity.snapshot_kind) != ("inventory", "capacity"):
        raise SnapshotMismatchError("Select one inventory and one capacity snapshot.")
    if inventory.dataset_version != capacity.dataset_version:
        raise SnapshotMismatchError("The snapshots belong to different dataset versions.")
    for snapshot in (inventory, capacity):
        if snapshot.as_of_date > horizon_start:
            raise SnapshotMismatchError(
                f"Snapshot {snapshot.snapshot_id} was observed after the horizon starts."
            )
    if capacity.last_week_start is None or capacity.last_week_start < horizon_start:
        raise SnapshotMismatchError(
            f"Snapshot {capacity.snapshot_id} has no capacity on or after {horizon_start}."
        )


async def select_snapshots(
    session: AsyncSession,
    *,
    horizon_start: date,
    inventory_snapshot: str | None = None,
    capacity_snapshot: str | None = None,
) -> SnapshotSelection:
    """Resolve the pins (or the only registered snapshots) and apply the date policy."""
    selection = SnapshotSelection(
        await get_snapshot(session, "inventory", inventory_snapshot),
        await get_snapshot(session, "capacity", capacity_snapshot),
    )
    validate_planning_dates(selection, horizon_start)
    return selection


def add_business_days(start: date, count: int) -> date:
    """The date count Monday-Friday days after start (reference weekday_advance)."""
    if type(count) is not int or count < 0:
        raise ValueError("Business days must be a non-negative integer.")
    day = start
    for _ in range(count):
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return day


def business_days_between(start: date, end: date) -> int:
    """Monday-Friday days in (start, end]."""
    return sum(1 for n in range(1, (end - start).days + 1) if (start + timedelta(n)).weekday() < 5)


def procurement_business_days(line: MaterialRequirement) -> int:
    if line.status == "sufficient":
        return 0
    return max(
        (d for d in line.supplier_lead_business_days if d > 0),
        default=DEFAULT_PROCUREMENT_BUSINESS_DAYS,
    )


@dataclass(frozen=True)
class StartPlan:
    material_delays: tuple[MaterialDelay, ...]
    procurement_business_days: int
    earliest_start_date: date
    start_week: int


def plan_start(inventory: InventoryCheck, horizon_start: date) -> StartPlan:
    """Earliest material-ready date and the first planning week on or after it."""
    if type(horizon_start) is not date or horizon_start.weekday() != 0:
        raise ValueError("The horizon must start on a Monday calendar date.")
    require_reference_constants(inventory.pricing_rule_version)
    delays = tuple(
        MaterialDelay(
            line.material_id, line.thickness_mm, line.status, procurement_business_days(line)
        )
        for line in inventory.lines
    )
    days = max((d.procurement_business_days for d in delays), default=0)
    earliest = add_business_days(horizon_start, days)
    week = -(-(earliest - horizon_start).days // 7)  # ceiling: next Monday unless on one
    return StartPlan(delays, days, earliest, week)


def _check_snapshots(
    inventory: InventoryCheck,
    capacity: CapacityCheck | None,
    inventory_snapshot: str | None,
    capacity_snapshot: str | None,
) -> None:
    for pin, found, kind in (
        (
            inventory_snapshot,
            {s for line in inventory.lines for s in line.snapshot_ids},
            "inventory",
        ),
        (capacity_snapshot, set(capacity.capacity_snapshot_ids) if capacity else set(), "capacity"),
    ):
        if pin is not None and found - {pin}:
            raise SnapshotMismatchError(
                f"Rows came from {kind} snapshots other than the pinned one."
            )


def evaluate_lead_time(
    inventory: InventoryCheck,
    capacity: CapacityCheck | None,
    *,
    horizon_start: date,
    requested_delivery_date: date | None = None,
    inventory_snapshot: str | None = None,
    capacity_snapshot: str | None = None,
    snapshots: SnapshotSelection | None = None,
) -> LeadTimeEstimate:
    """Pure decision from the inventory check and the capacity plan made from plan_start."""
    if requested_delivery_date is not None and type(requested_delivery_date) is not date:
        raise ValueError("The requested delivery date must be a calendar date.")
    if snapshots is not None:
        validate_planning_dates(snapshots, horizon_start)
        selected = (snapshots.inventory.snapshot_id, snapshots.capacity.snapshot_id)
        if inventory_snapshot not in (None, selected[0]) or capacity_snapshot not in (
            None,
            selected[1],
        ):
            raise SnapshotMismatchError("The pins differ from the selected snapshots.")
        inventory_snapshot, capacity_snapshot = selected
        if inventory.snapshot_id not in (None, selected[0]) or (
            capacity is not None and capacity.snapshot_id not in (None, selected[1])
        ):
            raise SnapshotMismatchError("The checks used other snapshots than the selection.")
    start = plan_start(inventory, horizon_start)

    def estimate(result, ready=None, lead=None, feasible=None, findings=(), planned=True):
        return LeadTimeEstimate(
            result,
            ready,
            lead,
            start.procurement_business_days,
            start.material_delays,
            horizon_start,
            start.earliest_start_date if planned else None,
            start.start_week if planned else None,
            requested_delivery_date,
            feasible,
            tuple(sorted(findings)),
            inventory,
            capacity,
            snapshots,
        )

    if inventory.result in ("needs_clarification", "not_manufacturable"):
        return estimate(inventory.result, planned=False)
    if capacity is None or capacity.inventory is not inventory:
        raise ValueError("The capacity plan must be made from this inventory check.")
    if (capacity.horizon_start, capacity.start_week) != (horizon_start, start.start_week):
        raise ValueError("The capacity plan must start at the material-ready week.")
    _check_snapshots(inventory, capacity, inventory_snapshot, capacity_snapshot)
    if capacity.result != "feasible":
        return estimate("infeasible", findings=[CAPACITY_FINDINGS[capacity.reason]])
    ready = max(capacity.final_week_start + FRIDAY_OFFSET, start.earliest_start_date)
    lead = business_days_between(horizon_start, ready)
    feasible = None if requested_delivery_date is None else ready <= requested_delivery_date
    findings = ["DELIVERY_DATE_INFEASIBLE"] if feasible is False else []
    return estimate("estimated", ready, lead, feasible, findings)


async def estimate_lead_time(
    session: AsyncSession,
    parts: Sequence[Part],
    *,
    manufacturing_rules: RuleVersionDetails,
    pricing_rules: RuleVersionDetails,
    horizon_start: date,
    requested_delivery_date: date | None = None,
    inventory_snapshot: str | None = None,
    capacity_snapshot: str | None = None,
) -> LeadTimeEstimate:
    """Stock, then capacity, with the capacity start week taken from the procurement delay."""
    if type(horizon_start) is not date or horizon_start.weekday() != 0:
        raise ValueError("The horizon must start on a Monday calendar date.")
    for pin in (inventory_snapshot, capacity_snapshot):
        if pin is not None and (not isinstance(pin, str) or not pin.strip()):
            raise ValueError("Snapshot pins must be nonblank text.")
    selection = await select_snapshots(
        session,
        horizon_start=horizon_start,
        inventory_snapshot=inventory_snapshot,
        capacity_snapshot=capacity_snapshot,
    )
    reads: dict = {}  # request-scoped catalog memo shared by the stock and capacity checks
    inventory = await check_inventory(
        session,
        parts,
        manufacturing_rules=manufacturing_rules,
        pricing_rules=pricing_rules,
        snapshot=selection.inventory,
        reads=reads,
    )
    capacity = None
    if inventory.result in ("sufficient", "insufficient"):
        capacity = await check_capacity(
            session,
            parts,
            inventory,
            manufacturing_rules=manufacturing_rules,
            pricing_rules=pricing_rules,
            horizon_start=horizon_start,
            start_week=plan_start(inventory, horizon_start).start_week,
            snapshot=selection.capacity,
            reads=reads,
        )
    return evaluate_lead_time(
        inventory,
        capacity,
        horizon_start=horizon_start,
        requested_delivery_date=requested_delivery_date,
        snapshots=selection,
    )
