"""Lead-time decisions without a database: business days, procurement delay, ready dates."""

from dataclasses import FrozenInstanceError, replace
from datetime import date
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_capacity_service import MONDAY, plan, plenty, reading
from test_inventory_service import PRICING, decide, stock
from test_manufacturing_service import BRACKET
from test_material_service import RULES

from rfq_copilot.models import Material
from rfq_copilot.repositories import MaterialAvailability
from rfq_copilot.services.lead_time import (
    DEFAULT_PROCUREMENT_BUSINESS_DAYS,
    SnapshotMismatchError,
    add_business_days,
    business_days_between,
    estimate_lead_time,
    evaluate_lead_time,
    plan_start,
)

D = __import__("decimal").Decimal
KEY = ("MAT-009", D("1.0"))


def inventory_with(available, *rows):
    return decide([BRACKET], stock_map={KEY: stock(available, *rows)})


def lead(inventory=None, capacity=None, parts=(BRACKET,), **kwargs):
    inventory = inventory or decide(list(parts))
    if capacity is None:
        week = plan_start(inventory, MONDAY).start_week
        hours = kwargs.pop("hours", None)
        capacity = plan(parts, inventory=inventory, start_week=week, capacity=hours)
    return evaluate_lead_time(inventory, capacity, horizon_start=MONDAY, **kwargs)


@pytest.mark.parametrize(
    "start,count,expected",
    [
        (date(2026, 11, 9), 0, date(2026, 11, 9)),
        (date(2026, 11, 9), 4, date(2026, 11, 13)),
        (date(2026, 11, 9), 5, date(2026, 11, 16)),  # skips the weekend
        (date(2026, 11, 13), 1, date(2026, 11, 16)),  # Friday + 1
        (date(2026, 11, 14), 1, date(2026, 11, 16)),  # from a Saturday
        (date(2026, 11, 9), 7, date(2026, 11, 18)),
        (date(2026, 11, 9), 15, date(2026, 11, 30)),
    ],
)
def test_add_business_days(start, count, expected):
    assert add_business_days(start, count) == expected


def test_business_days_between_excludes_start_and_weekends():
    assert business_days_between(MONDAY, MONDAY) == 0
    assert business_days_between(MONDAY, date(2026, 11, 13)) == 4
    assert business_days_between(MONDAY, date(2026, 11, 20)) == 9
    assert business_days_between(MONDAY, date(2026, 11, 15)) == 4  # Sunday adds nothing


@pytest.mark.parametrize("count", [-1, 1.0, True, "3"])
def test_add_business_days_rejects_bad_counts(count):
    with pytest.raises(ValueError):
        add_business_days(MONDAY, count)


def test_ground_truth_rfq_0001_ready_friday_four_business_days():
    estimate = lead()
    assert (estimate.result, estimate.estimated_ready_date, estimate.lead_business_days) == (
        "estimated",
        date(2026, 11, 13),
        4,
    )
    assert (estimate.procurement_business_days, estimate.start_week, estimate.findings) == (
        0,
        0,
        (),
    )


def test_short_stock_waits_for_the_longest_positive_supplier_lead():
    rows = (("INV-1", "S", 7, "50"), ("INV-2", "S", 19, "50"), ("INV-3", "S", 0, "0"))
    estimate = lead(inventory_with("100", *rows))
    delay = estimate.material_delays[0]
    assert (delay.status, delay.procurement_business_days) == ("insufficient", 19)
    assert estimate.earliest_start_date == date(2026, 12, 4)  # 19 business days
    assert estimate.start_week == 4  # next Monday, 2026-12-07
    assert estimate.estimated_ready_date == date(2026, 12, 11)
    assert estimate.lead_business_days == 24


@pytest.mark.parametrize(
    "rows",
    [
        (),  # no stock record at all
        (("INV-1", "S", 0, "0"),),  # only zero-lead rows (TD-002: reference code wins)
    ],
)
def test_short_stock_without_a_positive_lead_uses_the_default(rows):
    if rows:
        inventory = inventory_with("0", *rows)
    else:
        empty = MaterialAvailability(Material(material_id="MAT-009"), (), D(0))
        inventory = decide([BRACKET], stock_map={KEY: empty})
    estimate = lead(inventory)
    assert estimate.procurement_business_days == DEFAULT_PROCUREMENT_BUSINESS_DAYS == 15
    assert estimate.earliest_start_date == date(2026, 11, 30)  # a Monday: that very week
    assert (estimate.start_week, estimate.estimated_ready_date) == (3, date(2026, 12, 4))


def test_sufficient_stock_has_no_procurement_delay_even_with_long_leads():
    estimate = lead(inventory_with("1000", ("INV-1", "S", 19, "1000")))
    assert (estimate.procurement_business_days, estimate.start_week) == (0, 0)


def test_rfq_delay_is_the_longest_material_delay():
    thick = replace(BRACKET, part_id="P2", thickness_mm=D("4.0"), bend_radius_mm=D(4))
    stock_map = {
        KEY: stock("0", ("INV-1", "S", 7, "0")),
        ("MAT-009", D("4.0")): stock("0", ("INV-2", "S", 11, "0")),
    }
    inventory = decide([BRACKET, thick], stock_map=stock_map)
    estimate = lead(inventory, parts=(BRACKET, thick))
    assert [d.procurement_business_days for d in estimate.material_delays] == [7, 11]
    assert estimate.procurement_business_days == 11


def test_later_capacity_week_moves_the_ready_date():
    hours = plenty(**{"WC-LAS": reading("WC-LAS", {0: "0", 1: "0", 2: "500"})})
    estimate = lead(hours=hours)
    assert (estimate.estimated_ready_date, estimate.lead_business_days) == (date(2026, 11, 27), 14)


@pytest.mark.parametrize(
    "requested,feasible,findings",
    [
        (date(2026, 11, 13), True, ()),  # ready on the requested day is on time
        (date(2026, 12, 1), True, ()),
        (date(2026, 11, 12), False, ("DELIVERY_DATE_INFEASIBLE",)),
        (None, None, ()),
    ],
)
def test_requested_delivery_date(requested, feasible, findings):
    estimate = lead(requested_delivery_date=requested)
    assert (estimate.delivery_feasible, estimate.findings) == (feasible, findings)
    assert estimate.result == "estimated"


def test_no_capacity_means_no_ready_date():
    hours = plenty(**{"WC-BND": reading("WC-BND", dict.fromkeys(range(10), "0"))})
    estimate = lead(hours=hours, requested_delivery_date=date(2027, 6, 1))
    assert (estimate.result, estimate.estimated_ready_date, estimate.lead_business_days) == (
        "infeasible",
        None,
        None,
    )
    assert (estimate.findings, estimate.delivery_feasible) == (("CAPACITY_OUT_OF_HORIZON",), None)
    missing = lead(hours=plenty(**{"WC-BND": reading("WC-BND", {})}))
    assert missing.findings == ("CAPACITY_DATA_MISSING",)


@pytest.mark.parametrize(
    "part,result",
    [
        (replace(BRACKET, tolerance_mm=None), "needs_clarification"),
        (replace(BRACKET, quantity=0), "not_manufacturable"),
    ],
)
def test_blocked_requests_have_no_estimate(part, result):
    estimate = evaluate_lead_time(decide([part]), None, horizon_start=MONDAY)
    assert (estimate.result, estimate.estimated_ready_date, estimate.start_week) == (
        result,
        None,
        None,
    )


def test_capacity_plan_must_match_the_inventory_and_start_week():
    inventory = decide([BRACKET])
    with pytest.raises(ValueError, match="this inventory"):
        evaluate_lead_time(inventory, plan(), horizon_start=MONDAY)
    with pytest.raises(ValueError, match="this inventory"):
        evaluate_lead_time(inventory, None, horizon_start=MONDAY)
    with pytest.raises(ValueError, match="material-ready week"):
        evaluate_lead_time(inventory, plan(inventory=inventory, start_week=2), horizon_start=MONDAY)


def test_snapshot_pins():
    inventory = decide([BRACKET])  # stock rows from SNAP-1; capacity rows from SNAP-CAP
    capacity = plan(inventory=inventory)
    ok = evaluate_lead_time(
        inventory,
        capacity,
        horizon_start=MONDAY,
        inventory_snapshot="SNAP-1",
        capacity_snapshot="SNAP-CAP",
    )
    assert ok.result == "estimated"
    for pins in ({"inventory_snapshot": "SNAP-OLD"}, {"capacity_snapshot": "SNAP-OLD"}):
        with pytest.raises(SnapshotMismatchError):
            evaluate_lead_time(inventory, capacity, horizon_start=MONDAY, **pins)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"horizon_start": date(2026, 11, 10)},
        {"requested_delivery_date": "2026-12-01"},
    ],
)
def test_malformed_inputs(kwargs):
    inventory = decide([BRACKET])
    arguments = {"horizon_start": MONDAY, **kwargs}
    with pytest.raises(ValueError):
        evaluate_lead_time(inventory, plan(inventory=inventory), **arguments)


def test_results_are_immutable_and_reproducible():
    assert lead() == lead()
    with pytest.raises(FrozenInstanceError):
        lead().lead_business_days = 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"horizon_start": date(2026, 11, 11)},
        {"horizon_start": MONDAY, "inventory_snapshot": " "},
        {"horizon_start": MONDAY, "capacity_snapshot": 7},
    ],
)
async def test_arguments_fail_before_sql(kwargs):
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError):
        await estimate_lead_time(
            session, [BRACKET], manufacturing_rules=RULES, pricing_rules=PRICING, **kwargs
        )
    session.scalar.assert_not_awaited()
    session.scalars.assert_not_awaited()
