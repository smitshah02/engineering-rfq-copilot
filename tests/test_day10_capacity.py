"""Capacity planning against the guarded, seeded, rolled-back test database.

Parity sources: dev/validation ground truth expected_schedule and the reference
capacity_engine.estimate_schedule, loaded as a test oracle only. Both turn procurement
days into a start week; production code leaves that to lead-time estimation, so the tests derive the
start week with the reference's own weekday_advance.
"""

import csv
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from test_database_queries import populated as populated
from test_database_queries import statements
from test_database_seeding import SOURCE
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day07_materials import reference_module
from test_day08_manufacturing import base_parts
from test_day09_inventory import PANEL, pinned, truth_files

from rfq_copilot.entities import Part
from rfq_copilot.models import ProductionCapacity
from rfq_copilot.services.capacity import check_capacity
from rfq_copilot.services.inventory import check_inventory

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

MONDAY = date(2026, 11, 9)
CAPACITY = list(csv.DictReader((SOURCE / "operations_data/production_capacity.csv").open()))
D = Decimal


def start_week(reference, procurement_days):
    earliest = reference.weekday_advance(MONDAY, procurement_days)
    return max(0, ((earliest - MONDAY).days + 6) // 7)


async def plan(session, sources, rule_set, week=0):
    parts = [Part.from_mapping(s) for s in sources]
    inventory = await check_inventory(session, parts, **rule_set)
    return await check_capacity(
        session, parts, inventory, **rule_set, horizon_start=MONDAY, start_week=week
    )


def ours(check):
    return [
        {
            "part_id": op.part_id,
            "operation": op.operation_id,
            "workcentre_id": op.workcentre_id,
            "week_start": op.week_start.isoformat(),
            "required_hours": str(op.required_hours),
        }
        for op in check.operations
    ]


async def test_ground_truth_schedules(populated):
    reference = reference_module("capacity_engine")
    rule_set = await pinned(populated)
    compared, infeasible = 0, 0
    for rfq_id, truth in truth_files():
        expected = truth.get("expected_schedule")
        if expected is None:
            continue
        week = start_week(reference, expected["procurement_business_days"])
        check = await plan(populated, truth["expected_extraction"], rule_set, week)
        assert ours(check) == expected["operations"], rfq_id  # hours compared as exact strings
        assert (check.result == "feasible") is expected["feasible"], rfq_id
        assert check.reason == expected["reason"], rfq_id
        if not expected["feasible"]:
            assert check.unscheduled.operation_id == expected["missing_operation"]
            infeasible += 1
        else:
            last = max(op.week_start for op in check.operations)
            assert check.final_week_start == last
        compared += 1
    assert (compared, infeasible) == (100, 2)


def scaled(base, quantity, part_id=None):
    source = {**base, "quantity": quantity}
    if part_id:
        source["part_id"] = part_id
    return source


async def test_reference_parity_over_quantities_mixes_and_start_weeks(populated):
    reference = reference_module("capacity_engine")
    rule_set = await pinned(populated)
    bases = list(base_parts().values())
    cases = [[scaled(b, q)] for b in bases for q in (1, 100, 1000)]
    cases.append([scaled(b, 200, f"MIX-{i}") for i, b in enumerate(bases)])
    cases.append([scaled(b, 1000, f"BIG-{i}") for i, b in enumerate(bases)])
    cases += [[scaled(b, 1000, "A"), scaled(b, 1000, "B"), scaled(b, 1000, "C")] for b in bases]
    results = set()
    compared = 0
    for sources in cases:
        for days in (0, 7, 19, 45):
            week = start_week(reference, days)
            check = await plan(populated, sources, rule_set, week)
            expected = reference.estimate_schedule(
                sources, CAPACITY, MONDAY.isoformat(), [{"procurement_business_days": days}]
            )
            assert ours(check) == expected["operations"], (sources, days)
            assert (check.result == "feasible") is expected["feasible"], (sources, days)
            assert check.reason == expected["reason"]
            if not expected["feasible"]:
                assert check.unscheduled.operation_id == expected["missing_operation"]
            results.add(check.result)
            compared += 1
    assert compared == (6 * 3 + 2 + 6) * 4
    assert results == {"feasible", "infeasible"}


async def set_free_hours(session, workcentre_id, hours_by_week):
    for week, free in hours_by_week.items():
        await session.execute(
            update(ProductionCapacity)
            .where(
                ProductionCapacity.workcentre_id == workcentre_id,
                ProductionCapacity.week_start == MONDAY + timedelta(weeks=week),
            )
            .values(
                scheduled_hours=ProductionCapacity.nominal_hours
                - ProductionCapacity.downtime_hours
                - free
            )
        )
    session.expunge_all()  # remaining_hours is computed by PostgreSQL; re-read it


async def test_exact_capacity_boundary_in_the_database(populated):
    rule_set = await pinned(populated)
    # 10 flat panels: laser 10 x 3000 / 2500 = 12 min + 18 setup = 30 min = 0.5 h.
    for free, week in [(D("0.5"), 0), (D("0.4999"), 1)]:
        await set_free_hours(populated, "WC-LAS", {0: free})
        check = await plan(populated, [PANEL], rule_set)
        laser = check.operations[0]
        assert (laser.operation_id, laser.required_hours, laser.week_index) == (
            "laser",
            D("0.5"),
            week,
        )
        if week == 0:
            assert check.weeks[0].remaining_hours == free


async def test_full_workcentre_makes_the_rfq_infeasible(populated):
    rule_set = await pinned(populated)
    await set_free_hours(populated, "WC-QA", dict.fromkeys(range(10), D(0)))
    check = await plan(populated, [PANEL], rule_set)
    assert (check.result, check.reason) == ("infeasible", "NO_CAPACITY_IN_HORIZON")
    assert [op.operation_id for op in check.operations] == ["laser"]
    assert check.unscheduled.workcentre_id == "WC-QA"


async def test_inventory_from_other_parts_is_rejected(populated):
    rule_set = await pinned(populated)
    parts = [Part.from_mapping(PANEL)]
    inventory = await check_inventory(populated, parts + parts, **rule_set)
    with pytest.raises(ValueError, match="exactly these parts"):
        await check_capacity(populated, parts, inventory, **rule_set, horizon_start=MONDAY)


async def test_reads_only_bounded_reproducible_with_provenance(populated, seed_database):
    rule_set = await pinned(populated)
    table = ProductionCapacity.__table__
    before = (await populated.execute(select(table).order_by(table.c.capacity_id))).all()
    parts = [Part.from_mapping(base_parts()["l_bracket"])]
    inventory = await check_inventory(populated, parts, **rule_set)
    populated.expunge_all()
    with statements(seed_database) as captured:
        first = await check_capacity(populated, parts, inventory, **rule_set, horizon_start=MONDAY)
    # 1 routing read + (workcentre + weeks) for each of laser, bend, inspect, pack.
    assert len(captured) == 10  # includes the capacity snapshot selection
    assert all(s.lstrip().upper().startswith("SELECT") for s in captured)
    again = await check_capacity(populated, parts, inventory, **rule_set, horizon_start=MONDAY)
    assert again == first and first.result == "feasible"
    assert first.capacity_snapshot_ids and all(first.capacity_snapshot_ids)
    after = (await populated.execute(select(table).order_by(table.c.capacity_id))).all()
    assert after == before
