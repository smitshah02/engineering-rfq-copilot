"""Capacity planning decisions without a database: hours, first-fit weeks, boundaries."""

from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta
from decimal import Decimal, localcontext
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_inventory_service import PRICING, decide
from test_manufacturing_service import BRACKET
from test_material_service import RULES

from rfq_copilot.models import OperationRouting, ProductionCapacity, Workcentre
from rfq_copilot.repositories import WorkcentreCapacity
from rfq_copilot.services.capacity import (
    check_capacity,
    evaluate_capacity,
    operation_minutes,
)

D = Decimal
MONDAY = date(2026, 11, 9)
STATIONS = {
    "laser": "WC-LAS",
    "bend": "WC-BND",
    "weld": "WC-WLD",
    "finish": "WC-FIN",
    "inspect": "WC-QA",
    "pack": "WC-PCK",
}
ROUTES = {
    "l_bracket": ("laser", "bend", "finish", "inspect", "pack"),
    "flat_panel": ("laser", "finish", "inspect", "pack"),
    "fabricated_assembly": ("laser", "weld", "finish", "inspect", "pack"),
}
ROUTING = {
    part_type: tuple(
        OperationRouting(
            operation_id=op,
            workcentre_id=STATIONS[op],
            sequence=i + 1,
            required_when="finish_not_none" if op == "finish" else "always",
        )
        for i, op in enumerate(ops)
    )
    for part_type, ops in ROUTES.items()
}
# RFQ-0001 ground truth hours, which the reference computes in default Decimal context.
BRACKET_HOURS = {
    "laser": "1.682666666666666666666666667",
    "bend": "0.6866666666666666666666666667",
    "inspect": "2.258333333333333333333333333",
    "pack": "1.145833333333333333333333333",
}


def reading(workcentre_id, hours_by_week, horizon=10):
    weeks = tuple(
        ProductionCapacity(
            workcentre_id=workcentre_id,
            week_start=MONDAY + timedelta(weeks=w),
            remaining_hours=D(h),
            snapshot_id="SNAP-CAP",
        )
        for w, h in sorted(hours_by_week.items())
    )
    workcentre = Workcentre(
        workcentre_id=workcentre_id,
        operation_id=next(op for op, wc in STATIONS.items() if wc == workcentre_id),
        planning_horizon_weeks=horizon,
    )
    return WorkcentreCapacity(workcentre, weeks, sum((w.remaining_hours for w in weeks), D(0)))


def plenty(**overrides):
    capacity = {wc: reading(wc, dict.fromkeys(range(10), "500")) for wc in STATIONS.values()}
    capacity.update(overrides)
    return capacity


def plan(parts=(BRACKET,), capacity=None, inventory=None, start_week=0, **kwargs):
    inventory = inventory or decide(list(parts))
    return evaluate_capacity(
        list(parts),
        inventory,
        kwargs.pop("routing", ROUTING),
        capacity or plenty(),
        manufacturing_rules=kwargs.pop("manufacturing_rules", RULES),
        pricing_rules=kwargs.pop("pricing_rules", PRICING),
        horizon_start=kwargs.pop("horizon_start", MONDAY),
        start_week=start_week,
    )


def weeks(check):
    return [(op.part_id, op.operation_id, op.week_index) for op in check.operations]


def test_bracket_hours_match_ground_truth_digit_for_digit():
    check = plan()
    assert {op.operation_id: str(op.required_hours) for op in check.operations} == BRACKET_HOURS
    laser = check.operations[0]
    assert (laser.run_minutes, laser.setup_minutes, laser.workcentre_id) == (
        D("82.96"),  # 85 x (2 x (450 + 490) + 2 x 170 + 11 x 20) / 2500
        D(18),
        "WC-LAS",
    )


def test_operation_minutes_per_part_type():
    rate = PRICING
    panel = replace(BRACKET, part_type="flat_panel", hole_count=None, quantity=10)
    minutes = operation_minutes(panel, D("0.2205"), rate, RULES)
    assert minutes["laser"] == 10 * D(2 * (450 + 490)) / 2500  # no height, default 0 holes
    assert minutes["bend"] == 0
    assert minutes["finish"] == 10 * D("0.2205") * 2 * D("0.30")
    assert (minutes["inspect"], minutes["pack"]) == (D(15), D("7.5"))
    enclosure = replace(BRACKET, part_type="enclosure", hole_count=0, quantity=1)
    assert operation_minutes(enclosure, D(1), rate, RULES)["laser"] == D(1880 + 8 * 170) / 2500
    assert operation_minutes(enclosure, D(1), rate, RULES)["bend"] == 4 * D("0.32")
    assembly = replace(
        BRACKET, part_type="fabricated_assembly", hole_count=0, quantity=3, weld_length_mm=D(234)
    )
    welded = operation_minutes(assembly, D(1), rate, RULES)
    assert welded["laser"] == 3 * (D(2 * 1880) / 2500)
    with localcontext() as ctx:
        ctx.prec = 28
        assert welded["weld"] == D(3) * D(234) / D(65)


def test_feasible_plan_with_plenty_of_capacity():
    check = plan()
    assert (check.result, check.reason, check.unscheduled) == ("feasible", None, None)
    assert [w for _, _, w in weeks(check)] == [0, 0, 0, 0]
    assert check.final_week_start == MONDAY
    assert check.capacity_snapshot_ids == ("SNAP-CAP",)
    assert check.pricing_rule_version == "price-v1.0"


def test_exact_capacity_boundary_fits():
    laser = D(BRACKET_HOURS["laser"])
    exact = plan(capacity=plenty(**{"WC-LAS": reading("WC-LAS", {0: laser, 1: "500"})}))
    assert exact.operations[0].week_index == 0
    short = plan(
        capacity=plenty(**{"WC-LAS": reading("WC-LAS", {0: laser - D("0.0001"), 1: "500"})})
    )
    assert short.operations[0].week_index == 1


def test_week_cursor_never_moves_back():
    tight = plenty(**{"WC-LAS": reading("WC-LAS", {0: "1", 1: "1", 2: "500"})})
    check = plan(capacity=tight)
    assert [w for _, _, w in weeks(check)] == [2, 2, 2, 2]  # bend had room in week 0
    assert check.final_week_start == MONDAY + timedelta(weeks=2)


def test_capacity_is_shared_by_the_rfq_and_the_cursor_spans_parts():
    second = replace(BRACKET, part_id="P2")
    hours = D(BRACKET_HOURS["laser"])
    capacity = plenty(**{"WC-LAS": reading("WC-LAS", {0: hours * 3 / 2, 1: "500"})})
    check = plan((BRACKET, second), capacity=capacity)
    assert weeks(check) == [
        ("P1", "laser", 0),
        ("P1", "bend", 0),
        ("P1", "inspect", 0),
        ("P1", "pack", 0),
        ("P2", "laser", 1),
        ("P2", "bend", 1),
        ("P2", "inspect", 1),
        ("P2", "pack", 1),
    ]
    laser_week0 = next(w for w in check.weeks if w.workcentre_id == "WC-LAS")
    assert (laser_week0.remaining_hours, laser_week0.planned_hours) == (hours * 3 / 2, hours)


def test_no_capacity_in_horizon_reports_the_unscheduled_operation():
    capacity = plenty(**{"WC-BND": reading("WC-BND", dict.fromkeys(range(10), "0.5"))})
    check = plan(capacity=capacity)
    assert (check.result, check.reason) == ("infeasible", "NO_CAPACITY_IN_HORIZON")
    assert weeks(check) == [("P1", "laser", 0)]
    assert (check.unscheduled.operation_id, check.unscheduled.week_start) == ("bend", None)
    assert check.unscheduled.required_hours == D(BRACKET_HOURS["bend"])


def test_missing_week_rows_count_as_zero_hours():
    check = plan(capacity=plenty(**{"WC-LAS": reading("WC-LAS", {3: "500"})}))
    assert check.operations[0].week_index == 3


def test_workcentre_without_any_capacity_rows():
    check = plan(capacity=plenty(**{"WC-BND": reading("WC-BND", {})}))
    assert (check.result, check.reason) == ("infeasible", "MISSING_CAPACITY_DATA")


def test_horizon_comes_from_the_workcentre():
    late = plenty(**{"WC-LAS": reading("WC-LAS", {0: "0", 2: "500"}, horizon=2)})
    assert plan(capacity=late).reason == "NO_CAPACITY_IN_HORIZON"
    # Rows exist only beyond the horizon: nothing usable was ever recorded.
    beyond = plenty(**{"WC-LAS": reading("WC-LAS", {2: "500"}, horizon=2)})
    assert plan(capacity=beyond).reason == "MISSING_CAPACITY_DATA"
    assert plan(capacity=plenty(**{"WC-LAS": reading("WC-LAS", {2: "500"}, horizon=3)})).result == (
        "feasible"
    )


def test_start_week_is_respected():
    check = plan(start_week=4)
    assert [w for _, _, w in weeks(check)] == [4, 4, 4, 4]
    assert plan(start_week=10).reason == "NO_CAPACITY_IN_HORIZON"


def test_finish_operation_uses_its_workcentre():
    finished = replace(BRACKET, finish_id="passivated")
    check = plan((finished,))
    assert [(op.operation_id, op.workcentre_id) for op in check.operations][2] == (
        "finish",
        "WC-FIN",
    )
    with localcontext() as ctx:
        ctx.prec = 28
        expected = (85 * D("0.3038") * 2 * D("0.30") + 20) / 60
    assert check.operations[2].required_hours == expected


def test_blocked_inventory_results_pass_through_without_planning():
    incomplete = replace(BRACKET, tolerance_mm=None)
    assert plan((incomplete,), capacity={}).result == "needs_clarification"
    zero = replace(BRACKET, quantity=0)
    check = plan((zero,), capacity={})
    assert (check.result, check.operations) == ("not_manufacturable", ())


def test_insufficient_stock_still_plans_capacity():
    short = decide([BRACKET], available="1")
    assert short.result == "insufficient"
    assert plan(inventory=short).result == "feasible"


def test_results_do_not_depend_on_the_callers_decimal_context():
    expected = plan()
    with localcontext() as ctx:
        ctx.prec = 5
        assert plan() == expected


def test_results_are_immutable_and_reproducible():
    assert plan() == plan()
    with pytest.raises(FrozenInstanceError):
        plan().result = "feasible"


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"horizon_start": date(2026, 11, 10)}, "Monday"),
        ({"start_week": -1}, "start week"),
        ({"start_week": True}, "start week"),
        ({"pricing_rules": RULES}, "pricing rule"),
        ({"manufacturing_rules": PRICING}, "manufacturing rule"),
        ({"pricing_rules": replace(PRICING, fingerprint="0" * 64)}, "different rule"),
        ({"manufacturing_rules": replace(RULES, fingerprint="0" * 64)}, "different rule"),
        ({"parts": (BRACKET, BRACKET)}, "exactly these parts"),
        ({"routing": {}}, "No workcentre"),
        ({"capacity": {"WC-LAS": reading("WC-LAS", {0: "500"})}}, "No capacity reading"),
    ],
)
def test_malformed_inputs_fail(kwargs, message):
    inventory = decide([BRACKET])
    with pytest.raises(ValueError, match=message):
        plan(inventory=inventory, **kwargs)


def test_operation_without_setup_minutes_fails():
    document = {**PRICING.document, "setup_minutes": {"laser": "18"}}
    pricing = replace(PRICING, document=document)
    inventory = decide([BRACKET], pricing=pricing)
    with pytest.raises(ValueError, match="setup"):
        plan(inventory=inventory, pricing_rules=pricing)


@pytest.mark.asyncio
async def test_check_arguments_fail_before_sql():
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError):
        await check_capacity(
            session,
            [BRACKET],
            decide([BRACKET]),
            manufacturing_rules=RULES,
            pricing_rules=PRICING,
            horizon_start=date(2026, 11, 11),
        )
    session.scalar.assert_not_awaited()
    session.scalars.assert_not_awaited()
