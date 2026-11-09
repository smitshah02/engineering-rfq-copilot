"""Manufacturing cost decisions without a database: cost model, rounding, TD-003 guards."""

from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal, localcontext
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_inventory_service import PRICING, decide
from test_manufacturing_service import BRACKET
from test_material_service import RULES

from rfq_copilot.models import Material
from rfq_copilot.services.capacity import PLANNING, operation_minutes
from rfq_copilot.services.costing import (
    display_costs,
    estimate_costs,
    evaluate_costs,
)
from rfq_copilot.services.lead_time import plan_start

D = Decimal
STAINLESS_430 = Material(
    material_id="MAT-009", cost_usd_per_kg=D("6.4"), pricing_rule_version="price-v1.0"
)
# RFQ-0001 ground truth pricing line (cost fields only; margin and totals are pricing).
RFQ_0001 = {
    "part_id": "P1",
    "quantity": 85,
    "operations": ["laser", "bend", "inspect", "pack"],
    "material_cost_per_unit_usd": "16.8767",
    "labor_cost_per_unit_usd": "3.1417",
    "finish_cost_per_unit_usd": "0.0000",
    "packaging_cost_per_unit_usd": "0.6500",
    "variable_cost_per_unit_usd": "20.6684",
    "setup_cost_per_line_usd": "52.55",
    "total_manufacturing_cost_usd": "1809.3610",
    "pricing_rule_version": "price-v1.0",
}


def cost(
    parts=(BRACKET,), materials=(STAINLESS_430,), inventory=None, pricing=PRICING, rules=RULES
):
    inventory = inventory or decide(list(parts), pricing=pricing)
    return evaluate_costs(
        list(parts), inventory, list(materials), manufacturing_rules=rules, pricing_rules=pricing
    )


def test_rfq_0001_cost_fields_match_ground_truth():
    estimate = cost()
    assert estimate.result == "costed"
    assert display_costs(estimate.lines[0], PRICING) == RFQ_0001


def test_unrounded_components():
    line = cost().lines[0]
    with localcontext(PLANNING):
        labour = (D("0.976") / 60) * 85 + (D("0.32") / 60) * 72 + (D("1.5") / 60) * 55
    assert line.labour_cost_per_unit == labour
    assert line.material_cost_per_unit == D("2.636984") * D("6.4")
    assert line.setup_cost_per_line == D("52.55")  # (18x85 + 14x72 + 8x55 + 5x35) / 60
    assert [(o.operation_id, o.labour_per_unit_usd == 0) for o in line.operation_costs] == [
        ("laser", False),
        ("bend", False),
        ("inspect", False),
        ("pack", True),  # charged per unit as packaging, not as labour
    ]


def test_finish_is_charged_per_square_metre_with_its_setup():
    finished = replace(BRACKET, finish_id="passivated")
    line = cost((finished,)).lines[0]
    assert line.operations == ("laser", "bend", "finish", "inspect", "pack")
    assert line.finish_cost_per_unit == D("0.3038") * 18
    finish_op = line.operation_costs[2]
    assert (finish_op.labour_per_unit_usd, finish_op.setup_cost_usd) == (0, D(20) * 64 / 60)
    assert line.setup_cost_per_line == D("52.55") + D(20) * 64 / 60


def test_weld_labour_for_assemblies():
    assembly = replace(
        BRACKET,
        part_type="fabricated_assembly",
        bend_radius_mm=None,
        weld_length_mm=D(234),
        quantity=10,
    )
    line = cost((assembly,)).lines[0]
    weld = next(o for o in line.operation_costs if o.operation_id == "weld")
    with localcontext(PLANNING):
        assert weld.run_minutes_per_unit == D(234) / D(65)
        assert weld.labour_per_unit_usd == (D(234) / D(65) / 60) * 90
    assert weld.setup_cost_usd == D(25) * 90 / 60


def test_setup_is_charged_once_per_line():
    small, large = cost((replace(BRACKET, quantity=1),)), cost((replace(BRACKET, quantity=1000),))
    for estimate in (small, large):
        line = estimate.lines[0]
        assert line.total_manufacturing_cost == line.quantity * line.variable_cost_per_unit + D(
            "52.55"
        )


def test_multi_line_total():
    second = replace(BRACKET, part_id="P2", quantity=15)
    estimate = cost((BRACKET, second))
    assert [line.part_id for line in estimate.lines] == ["P1", "P2"]
    assert estimate.total_manufacturing_cost == sum(
        line.total_manufacturing_cost for line in estimate.lines
    )


def test_material_priced_under_another_rule_version_is_rejected():
    stale = Material(
        material_id="MAT-009", cost_usd_per_kg=D("6.4"), pricing_rule_version="price-v0.9"
    )
    with pytest.raises(ValueError, match="priced under price-v0.9"):
        cost(materials=(stale,))
    with pytest.raises(ValueError, match="No catalog row"):
        cost(materials=())


def test_missing_rate_is_rejected():
    rates = dict(PRICING.document["workcentre_hourly_rates_usd"])
    del rates["bend"]
    pricing = replace(PRICING, document={**PRICING.document, "workcentre_hourly_rates_usd": rates})
    with pytest.raises(ValueError, match="workcentre_hourly_rates_usd.bend"):
        cost(pricing=pricing)


@pytest.mark.parametrize("mode,shown", [("ROUND_HALF_UP", "1.0001"), ("ROUND_HALF_EVEN", "1.0000")])
def test_display_uses_the_rule_rounding_mode(mode, shown):
    line = replace(cost().lines[0], material_cost_per_unit=D("1.00005"))
    pricing = replace(PRICING, document={**PRICING.document, "rounding": mode})
    assert display_costs(line, pricing)["material_cost_per_unit_usd"] == shown


def test_unknown_rounding_mode_is_rejected():
    pricing = replace(PRICING, document={**PRICING.document, "rounding": "BANKERS"})
    with pytest.raises(ValueError, match="rounding mode"):
        display_costs(cost().lines[0], pricing)


@pytest.mark.parametrize(
    "part,result",
    [
        (replace(BRACKET, tolerance_mm=None), "needs_clarification"),
        (replace(BRACKET, quantity=0), "not_manufacturable"),
    ],
)
def test_blocked_requests_are_not_costed(part, result):
    estimate = cost((part,), materials=())
    assert (estimate.result, estimate.lines, estimate.total_manufacturing_cost) == (
        result,
        (),
        None,
    )


def test_rule_and_inventory_mismatches_are_rejected():
    inventory = decide([BRACKET])
    with pytest.raises(ValueError, match="different rule"):
        cost(inventory=inventory, pricing=replace(PRICING, fingerprint="0" * 64))
    with pytest.raises(ValueError, match="exactly these parts"):
        cost((BRACKET, BRACKET), inventory=inventory)
    with pytest.raises(ValueError, match="pricing rule"):
        cost(inventory=inventory, pricing=RULES)


def test_results_do_not_depend_on_the_callers_context_and_are_immutable():
    expected = cost()
    with localcontext() as ctx:
        ctx.prec = 6
        assert cost() == expected
        assert display_costs(expected.lines[0], PRICING) == RFQ_0001  # quantize too
    with pytest.raises(FrozenInstanceError):
        expected.lines[0].quantity = 1


def test_td_003_reference_constants_are_refused_for_other_pricing_versions():
    future = replace(PRICING, rule_version="price-v2.0")
    with pytest.raises(ValueError, match="TD-003"):
        operation_minutes(BRACKET, D("0.3038"), future, RULES)
    with pytest.raises(ValueError, match="TD-003"):
        plan_start(decide([BRACKET], pricing=future), date(2026, 11, 9))


@pytest.mark.asyncio
@pytest.mark.parametrize("parts,pricing", [([BRACKET], RULES), ([BRACKET, BRACKET], PRICING)])
async def test_arguments_fail_before_sql(parts, pricing):
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError):
        await estimate_costs(
            session, parts, decide([BRACKET]), manufacturing_rules=RULES, pricing_rules=pricing
        )
    session.scalars.assert_not_awaited()
