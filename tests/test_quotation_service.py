"""Multi-part quotation assembly without a database: lines, blocking, order, integrity."""

from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_capacity_service import MONDAY, plan, plenty, reading
from test_costing_service import STAINLESS_430
from test_inventory_service import PRICING, decide
from test_material_service import RULES
from test_pricing_service import PART_0001, QUOTED

from rfq_copilot.services.costing import evaluate_costs
from rfq_copilot.services.lead_time import evaluate_lead_time, plan_start
from rfq_copilot.services.pricing import evaluate_pricing
from rfq_copilot.services.quotation import assemble_quotation, quote_parts

D = Decimal
FIRST = PART_0001
SECOND = replace(PART_0001, part_id="PART-2", quantity=15, finish_id="passivated")
THICK = replace(PART_0001, part_id="PART-3", thickness_mm=D("4.0"), bend_radius_mm=D(4))


def services(parts, hours=None, requested=None):
    parts = list(parts)
    inventory = decide(parts)
    capacity = None
    if inventory.result in ("sufficient", "insufficient"):
        week = plan_start(inventory, MONDAY).start_week
        capacity = plan(parts, inventory=inventory, start_week=week, capacity=hours)
    lead = evaluate_lead_time(
        inventory, capacity, horizon_start=MONDAY, requested_delivery_date=requested
    )
    costs = evaluate_costs(
        parts, inventory, [STAINLESS_430], manufacturing_rules=RULES, pricing_rules=PRICING
    )
    return lead, costs, evaluate_pricing(parts, costs, QUOTED, PRICING)


def quote(parts=(FIRST, SECOND), numbers=None, **kwargs):
    numbers = numbers or range(1, len(parts) + 1)
    return assemble_quotation(list(parts), list(numbers), *services(parts, **kwargs))


def test_two_line_quotation():
    q = quote()
    assert (q.result, q.blocking_lines, q.currency) == ("quoted", (), "USD")
    assert [o.line_number for o in q.lines] == [1, 2]
    assert q.quote_total_usd == sum(o.priced.line_total_usd for o in q.lines)
    assert q.calculation_id == q.pricing.calculation_id and q.calculation_id.startswith("CALC-")
    assert [o.priced.cost.part_id for o in q.lines] == ["PART-000010", "PART-2"]
    assert q.lines[1].priced.cost.operations == ("laser", "bend", "finish", "inspect", "pack")
    assert (q.estimated_ready_date, q.lead_business_days) == (date(2026, 11, 13), 4)


def test_lines_sharing_stock_point_at_the_same_stock_line():
    q = quote()
    first, second = q.lines
    assert first.material_key == second.material_key == ("MAT-009", D("1.0"))
    shared = q.lead_time.inventory.lines[0]
    assert shared.part_ids == ("PART-000010", "PART-2")
    assert (first.stock_status, first.procurement_business_days) == ("sufficient", 0)


def test_each_line_carries_its_own_operations_and_week():
    q = quote()
    assert [op.operation_id for op in q.lines[0].operations] == ["laser", "bend", "inspect", "pack"]
    assert all(op.part_id == "PART-2" for op in q.lines[1].operations)
    assert q.lines[0].last_week_start == MONDAY and q.lines[0].unscheduled is None


def test_a_line_needing_clarification_blocks_the_quotation():
    q = quote((FIRST, replace(SECOND, tolerance_mm=None)))
    assert (q.result, q.blocking_lines, q.calculation_id, q.quote_total_usd) == (
        "needs_clarification",
        (2,),
        None,
        None,
    )
    assert q.lines[0].manufacturing.result == "valid" and q.lines[0].priced is None
    assert q.lines[1].manufacturing.findings == ("MISSING_TOLERANCE_MM",)


def test_unsupported_lines_and_clarification_precedence():
    unsupported = replace(SECOND, quantity=0)
    q = quote((FIRST, unsupported))
    assert (q.result, q.blocking_lines) == ("not_manufacturable", (2,))
    incomplete = replace(THICK, tolerance_mm=None)
    q = quote((unsupported, FIRST, incomplete))
    assert (q.result, q.blocking_lines) == ("needs_clarification", (3,))


def test_capacity_infeasible_quotation_is_still_priced_with_findings():
    hours = plenty(**{"WC-BND": reading("WC-BND", dict.fromkeys(range(10), "0.5"))})
    q = quote((FIRST,), hours=hours)
    assert (q.result, q.estimated_ready_date, q.findings) == (
        "quoted",
        None,
        ("CAPACITY_OUT_OF_HORIZON",),
    )
    assert q.lines[0].unscheduled.operation_id == "bend"
    assert q.quote_total_usd == D("2380.74")


def test_late_delivery_is_a_finding_not_a_block():
    q = quote(requested=date(2026, 11, 12))
    assert (q.result, q.findings) == ("quoted", ("DELIVERY_DATE_INFEASIBLE",))


def test_line_order_matters_for_the_plan_and_id_but_not_the_prices():
    tight = plenty(**{"WC-LAS": reading("WC-LAS", {0: "1.7", 1: "500"})})
    forward = quote((FIRST, THICK), hours=tight)
    backward = quote((THICK, FIRST), hours=tight)
    prices = lambda q: {o.part_id: o.priced.line_total_usd for o in q.lines}  # noqa: E731
    assert prices(forward) == prices(backward)
    assert forward.quote_total_usd == backward.quote_total_usd
    assert forward.calculation_id != backward.calculation_id
    weeks = lambda q: {o.part_id: o.last_week_start for o in q.lines}  # noqa: E731
    assert weeks(forward) != weeks(backward)  # whoever comes first takes week 0's laser hours


def test_explicit_line_numbers_are_kept():
    q = quote(numbers=(10, 20))
    assert [o.line_number for o in q.lines] == [10, 20]
    blocked = quote((FIRST, replace(SECOND, tolerance_mm=None)), numbers=(10, 20))
    assert blocked.blocking_lines == (20,)


@pytest.mark.parametrize(
    "parts,numbers,message",
    [
        ((), (), "one or more"),
        ((FIRST, replace(SECOND, part_id=FIRST.part_id)), (1, 2), "unique within"),
        ((FIRST, SECOND), (1,), "line number"),
        ((FIRST, SECOND), (0, 1), "line number"),
        ((FIRST, SECOND), (True, 2), "line number"),
        ((FIRST, SECOND), (2, 1), "ascending"),
        ((FIRST, SECOND), (1, 1), "ascending"),
    ],
)
def test_invalid_line_sets_are_rejected(parts, numbers, message):
    lead, costs, pricing = services((FIRST, SECOND))
    with pytest.raises(ValueError, match=message):
        assemble_quotation(list(parts), list(numbers), lead, costs, pricing)


def test_results_from_other_inputs_are_rejected():
    lead, costs, pricing = services((FIRST, SECOND))
    other_lead, other_costs, other_pricing = services((FIRST, SECOND))
    with pytest.raises(ValueError, match="this cost estimate"):
        assemble_quotation([FIRST, SECOND], [1, 2], lead, costs, other_pricing)
    with pytest.raises(ValueError, match="stock check"):
        assemble_quotation([FIRST, SECOND], [1, 2], lead, other_costs, other_pricing)
    single, _, _ = services((FIRST,))
    with pytest.raises(ValueError, match="exactly these lines"):
        assemble_quotation([FIRST, SECOND], [1, 2], single, costs, pricing)
    with pytest.raises(ValueError, match="must be priced"):
        assemble_quotation([FIRST, SECOND], [1, 2], lead, costs, None)


def test_quotation_is_immutable_and_reproducible():
    assert quote() == quote()
    with pytest.raises(FrozenInstanceError):
        quote().result = "quoted"


@pytest.mark.asyncio
async def test_line_validation_happens_before_sql():
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError, match="unique"):
        await quote_parts(
            session,
            [FIRST, FIRST],
            manufacturing_rules=RULES,
            pricing_rules=PRICING,
            horizon_start=MONDAY,
        )
    session.scalar.assert_not_awaited()
    session.scalars.assert_not_awaited()
