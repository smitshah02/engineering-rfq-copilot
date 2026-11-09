"""Pricing decisions without a database: margin, rounding, totals, calculation IDs, TD-001."""

import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_capacity_service import ROUTING
from test_costing_service import STAINLESS_430
from test_costing_service import cost as cost_for
from test_inventory_service import PRICING
from test_material_service import RULES, STAINLESS

from rfq_copilot.entities import Part
from rfq_copilot.models import Material
from rfq_copilot.services.costing import evaluate_costs
from rfq_copilot.services.inventory import evaluate_inventory
from rfq_copilot.services.manufacturing import evaluate_manufacturing
from rfq_copilot.services.materials import evaluate_material
from rfq_copilot.services.pricing import (
    calculate_quote,
    calculation_id,
    canonical_part,
    display_quote,
    evaluate_pricing,
)

D = Decimal
QUOTED = dict.fromkeys(["laser", "bend", "weld", "finish", "inspect", "pack"], True)
GT_0001 = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "data/rfq_synthetic_data/evaluation/ground_truth/development/RFQ-0001.json"
    ).read_text()
)
PART_0001 = Part.from_mapping(GT_0001["expected_extraction"][0])


def cost(parts=(PART_0001,), **kwargs):
    return cost_for(parts, **kwargs)


def price(parts=(PART_0001,), estimate=None, quoted=QUOTED, pricing=PRICING):
    estimate = estimate or cost(parts)
    return evaluate_pricing(list(parts), estimate, quoted, pricing)


def test_rfq_0001_matches_ground_truth_including_calculation_id():
    calc = price()
    expected = GT_0001["expected_pricing"]
    shown = display_quote(calc, PRICING)
    assert calc.result == "priced"
    assert shown["calculation_id"] == expected["calculation_id"] == "CALC-72E1F2A14C7EDD41FDF4"
    assert (shown["quote_total_usd"], shown["lines"][0]["line_total_usd"]) == ("2380.74", "2380.74")
    assert shown["lines"][0]["quoted_unit_price_usd"] == "28.0087"
    assert {k: v for k, v in shown.items() if k != "lines"} == {
        k: v for k, v in expected.items() if k != "lines"
    }


def documented_quote():
    """business_rules.md: 10 aluminium panels and 2 powder-coated steel L-brackets."""
    panel = Part.from_mapping(
        {
            "part_id": "PANEL",
            "part_type": "flat_panel",
            "material_id": "AL",
            "finish_id": "none",
            "length_mm": 1000,
            "width_mm": 500,
            "thickness_mm": "2.0",
            "tolerance_mm": "0.5",
            "quantity": 10,
            "hole_count": 0,
        }
    )
    bracket = Part.from_mapping(
        {
            "part_id": "BRACKET",
            "part_type": "l_bracket",
            "material_id": "ST",
            "finish_id": "powder_coat",
            "length_mm": 200,
            "width_mm": 100,
            "height_mm": 50,
            "thickness_mm": "1.0",
            "tolerance_mm": "0.5",
            "quantity": 2,
            "hole_count": 0,
        }
    )
    specs = {
        "AL": replace(
            STAINLESS,
            material_id="AL",
            density_kg_m3=D(2700),
            supported_thickness_mm=(D(2),),
            compatible_finishes=("none",),
        ),
        "ST": replace(
            STAINLESS,
            material_id="ST",
            density_kg_m3=D(7850),
            supported_thickness_mm=(D(1),),
            compatible_finishes=("powder_coat",),
        ),
    }
    parts = [panel, bracket]
    checks = [
        evaluate_manufacturing(
            p,
            RULES,
            evaluate_material(p, RULES, specs[p.material_id], finish_exists=True),
            ROUTING[p.part_type],
        )
        for p in parts
    ]
    from test_inventory_service import stock

    stock_map = {(p.material_id, p.thickness_mm): stock("1000") for p in parts}
    inventory = evaluate_inventory(parts, checks, stock_map, PRICING)
    materials = [
        Material(material_id="AL", cost_usd_per_kg=D(8), pricing_rule_version="price-v1.0"),
        Material(material_id="ST", cost_usd_per_kg=D("2.50"), pricing_rule_version="price-v1.0"),
    ]
    costs = evaluate_costs(
        parts, inventory, materials, manufacturing_rules=RULES, pricing_rules=PRICING
    )
    return parts, costs


def test_dataset_documented_independent_example():
    parts, costs = documented_quote()
    calc = price(parts, costs)
    assert [line.line_total_usd for line in calc.lines] == [D("414.37"), D("107.49")]
    assert calc.quote_total_usd == D("521.86")
    panel = calc.lines[0].cost
    assert (panel.material_cost_per_unit, panel.variable_cost_per_unit) == (
        D("24.192"),
        D("27.917"),
    )
    assert panel.setup_cost_per_line == D("35.75")


def test_gross_margin_is_a_margin_not_a_markup():
    # $100 of cost at a 24% gross margin sells for $131.58, not $124.00.
    line = replace(cost().lines[0], total_manufacturing_cost=D(100), quantity=1)
    estimate = replace(cost(), lines=(line,))
    assert price(estimate=estimate).lines[0].line_total_usd == D("131.58")


def one_line(total, quantity=1, part_id=PART_0001.part_id):
    return replace(
        cost().lines[0], part_id=part_id, total_manufacturing_cost=D(total), quantity=quantity
    )


@pytest.mark.parametrize("mode,shown", [("ROUND_HALF_UP", "1.01"), ("ROUND_HALF_EVEN", "1.00")])
def test_line_total_rounding_uses_the_rule_mode(mode, shown):
    estimate = replace(cost(), lines=(one_line("0.7638"),))  # 0.7638 / 0.76 = 1.005
    pricing = replace(PRICING, document={**PRICING.document, "rounding": mode})
    estimate = replace(estimate, pricing_rule_version=pricing.rule_version)
    assert str(price(estimate=estimate, pricing=pricing).lines[0].line_total_usd) == shown


def test_quote_total_sums_rounded_line_totals():
    first, second = one_line("0.7638", part_id="A"), one_line("0.7638", part_id="B")
    parts = (replace(PART_0001, part_id="A"), replace(PART_0001, part_id="B"))
    base = cost(parts)
    calc = price(parts, replace(base, lines=(first, second)))
    assert [line.line_total_usd for line in calc.lines] == [D("1.01"), D("1.01")]
    assert calc.quote_total_usd == D("2.02")  # not round(1.005 + 1.005) = 2.01


def test_unit_price_is_advisory_and_rounded_separately():
    calc = price(estimate=replace(cost(), lines=(one_line("100", quantity=7),)))
    line = calc.lines[0]
    assert (line.line_total_usd, line.quoted_unit_price_usd) == (D("131.58"), D("18.7971"))
    assert line.quoted_unit_price_usd * 7 == D("131.5797")  # the line total is authoritative


@pytest.mark.parametrize("margin", ["1", "1.2", "-0.01", 0.24, None])
def test_invalid_margins_are_rejected(margin):
    pricing = replace(PRICING, document={**PRICING.document, "gross_margin": margin})
    estimate = replace(cost(), pricing_rule_version=pricing.rule_version)
    with pytest.raises(ValueError):
        price(estimate=estimate, pricing=pricing)


def test_zero_margin_sells_at_cost_rounded():
    pricing = replace(PRICING, document={**PRICING.document, "gross_margin": "0"})
    calc = price(pricing=pricing)
    assert calc.lines[0].line_total_usd == D("1809.36")


@pytest.mark.parametrize(
    "changes",
    [
        {"thickness_mm": D("1.00")},
        {"thickness_mm": D("1.000"), "length_mm": D("450.000"), "tolerance_mm": D("0.50")},
        {"bend_radius_mm": D("1.5")},
    ],
)
def test_calculation_id_does_not_depend_on_number_spelling(changes):
    assert calculation_id([replace(PART_0001, **changes)], "price-v1.0") == calculation_id(
        [PART_0001], "price-v1.0"
    )


def test_calculation_id_changes_with_values_order_and_rule_version():
    base = calculation_id([PART_0001], "price-v1.0")
    other = replace(PART_0001, part_id="X", quantity=86)
    assert calculation_id([replace(PART_0001, quantity=86)], "price-v1.0") != base
    assert calculation_id([PART_0001], "price-v2.0") != base
    assert calculation_id([PART_0001, other], "price-v1.0") != calculation_id(
        [other, PART_0001], "price-v1.0"
    )


def test_canonical_part_spelling():
    assert canonical_part(PART_0001) == GT_0001["expected_extraction"][0]
    odd = replace(PART_0001, length_mm=D("450.25"), thickness_mm=D(2), height_mm=None)
    canonical = canonical_part(odd)
    assert (canonical["length_mm"], canonical["thickness_mm"]) == ("450.25", "2.0")
    assert "height_mm" not in canonical


def test_unquoted_routed_operations_are_refused():
    # TD-001: punch and machine are catalog-only (is_quoted = false).
    with pytest.raises(ValueError, match=r"\['bend'\] are not quoted"):
        price(quoted={**QUOTED, "bend": False})
    with pytest.raises(ValueError, match="not quoted"):
        price(quoted={k: v for k, v in QUOTED.items() if k != "laser"})


@pytest.mark.parametrize(
    "part,result",
    [
        (replace(PART_0001, tolerance_mm=None), "needs_clarification"),
        (replace(PART_0001, quantity=0), "not_manufacturable"),
    ],
)
def test_blocked_requests_are_not_priced(part, result):
    calc = price((part,), cost((part,), materials=()))
    assert (calc.result, calc.calculation_id, calc.lines, calc.quote_total_usd) == (
        result,
        None,
        (),
        None,
    )


def test_mismatches_are_rejected():
    estimate = cost()
    with pytest.raises(ValueError, match="different pricing"):
        price(estimate=estimate, pricing=replace(PRICING, fingerprint="0" * 64))
    with pytest.raises(ValueError, match="exactly these parts"):
        price((PART_0001, PART_0001), estimate)
    with pytest.raises(ValueError, match="do not match"):
        price((replace(PART_0001, part_id="OTHER"),), estimate)
    with pytest.raises(ValueError, match="pricing rule"):
        price(estimate=estimate, pricing=RULES)


def test_rule_flags_and_currency_come_from_the_rule():
    calc = price()
    assert (calc.currency, calc.tax_included, calc.freight_included, calc.shipping_included) == (
        "USD",
        False,
        False,
        False,
    )
    bad = replace(PRICING, document={**PRICING.document, "tax_included": "no"})
    with pytest.raises(ValueError, match="tax_included"):
        price(estimate=replace(cost(), pricing_rule_version=bad.rule_version), pricing=bad)


def test_reproducible_context_independent_and_immutable():
    expected = price()
    with localcontext() as ctx:
        ctx.prec = 4
        assert price() == expected
    with pytest.raises(FrozenInstanceError):
        expected.quote_total_usd = D(0)


def test_material_cost_drives_the_price():
    pricier = Material(
        material_id="MAT-009", cost_usd_per_kg=D("7.4"), pricing_rule_version="price-v1.0"
    )
    assert price(estimate=cost(materials=(pricier,))).quote_total_usd > price().quote_total_usd
    assert STAINLESS_430.cost_usd_per_kg == D("6.4")


@pytest.mark.asyncio
async def test_arguments_fail_before_sql():
    session = AsyncMock(spec=AsyncSession)
    for estimate, pricing in [(None, PRICING), (cost(), RULES)]:
        with pytest.raises(ValueError):
            await calculate_quote(session, [PART_0001], estimate, pricing_rules=pricing)
    session.scalars.assert_not_awaited()
