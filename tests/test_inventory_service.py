"""Inventory availability decisions without a database: formulas, scrap, stock statuses."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, Inexact
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_manufacturing_service import ASSEMBLY_ROUTE, BRACKET, PANEL_ROUTE, run
from test_material_service import RULES, STAINLESS

from rfq_copilot.models import Material, MaterialInventory
from rfq_copilot.repositories import MaterialAvailability, RuleVersionDetails
from rfq_copilot.services.inventory import (
    blank_area_m2,
    check_inventory,
    evaluate_inventory,
    part_requirement,
    scrap_fraction,
)

PRICING_FILE = (
    Path(__file__).resolve().parents[1] / "data/rfq_synthetic_data/config/pricing_rules.json"
)
PRICING = RuleVersionDetails(
    rule_version="price-v1.0",
    rule_kind="pricing",
    sha256=hashlib.sha256(PRICING_FILE.read_bytes()).hexdigest(),
    fingerprint="e" * 64,
    document=json.loads(PRICING_FILE.read_text(), parse_float=Decimal),
)
# RFQ-0001 ground truth: 0.3038 m2, 2.3544500 kg net, 2.636984 kg allowance, 224.14364 kg.
BRACKET_REQUIRED = Decimal("224.14364")
D = Decimal


def stock(available, *rows):
    rows = rows or (("INV-1", "SNAP-1", 7, available),)
    inventory = tuple(
        MaterialInventory(
            inventory_id=i, snapshot_id=s, supplier_lead_business_days=lead, available_kg=D(kg)
        )
        for i, s, lead, kg in rows
    )
    return MaterialAvailability(Material(material_id="MAT-009"), inventory, D(available))


def decide(parts, available="1000", stock_map=None, pricing=PRICING, checks=None):
    checks = checks or [run(p, routing=route_for(p)) for p in parts]
    stock_map = stock_map or {(p.material_id, p.thickness_mm): stock(available) for p in parts}
    return evaluate_inventory(parts, checks, stock_map, pricing)


def route_for(part):
    from test_manufacturing_service import BRACKET_ROUTE

    return {"flat_panel": PANEL_ROUTE, "fabricated_assembly": ASSEMBLY_ROUTE}.get(
        part.part_type, BRACKET_ROUTE
    )


@pytest.mark.parametrize(
    "changes,area",
    [
        ({"part_type": "flat_panel", "length_mm": D(1000), "width_mm": D(500)}, "0.5"),
        ({"part_type": "mounting_plate", "length_mm": D(330), "width_mm": D(110)}, "0.0363"),
        ({"part_type": "l_bracket"}, "0.3038"),  # (450 + 170) x 490
        (
            {
                "part_type": "u_bracket",
                "length_mm": D(410),
                "width_mm": D(200),
                "height_mm": D(150),
            },
            "0.205",
        ),
        (
            {"part_type": "enclosure", "length_mm": D(460), "width_mm": D(370), "height_mm": D(80)},
            "0.303",
        ),
        (
            {"part_type": "fabricated_assembly", "length_mm": D(670), "width_mm": D(350)},
            "0.469",
        ),
    ],
)
def test_blank_area_per_part_type(changes, area):
    assert blank_area_m2(replace(BRACKET, **changes)) == D(area)


def test_flat_parts_ignore_height_and_unknown_types_fail():
    panel = replace(BRACKET, part_type="flat_panel", height_mm=D(999))
    assert blank_area_m2(panel) == D("0.2205")
    with pytest.raises(ValueError, match="blank-area"):
        blank_area_m2(replace(BRACKET, part_type="casting"))


def test_dataset_documented_arithmetic():
    # business_rules.md: 10 panels, 1000 x 500 x 2 mm aluminium at 2700 kg/m3.
    panel = replace(
        BRACKET,
        part_type="flat_panel",
        length_mm=D(1000),
        width_mm=D(500),
        thickness_mm=D(2),
        quantity=10,
    )
    r = part_requirement(panel, D(2700), D("0.12"))
    assert (r.net_kg_per_unit, r.allowance_kg_per_unit, r.required_kg) == (
        D("2.7"),
        D("3.024"),
        D("30.24"),
    )


def test_ground_truth_bracket_units_mm_to_m_and_m3():
    r = part_requirement(BRACKET, STAINLESS.density_kg_m3, D("0.12"))
    assert (r.blank_area_m2, r.net_kg_per_unit, r.allowance_kg_per_unit, r.required_kg) == (
        D("0.3038"),
        D("2.35445"),
        D("2.636984"),
        BRACKET_REQUIRED,
    )


@pytest.mark.parametrize(
    "scrap,allowance", [("0", "2.35445"), ("0.12", "2.636984"), ("0.5", "3.531675")]
)
def test_scrap_comes_from_the_pricing_rule(scrap, allowance):
    pricing = replace(PRICING, document={**PRICING.document, "scrap_fraction": scrap})
    check = decide([BRACKET], pricing=pricing)
    assert check.scrap_fraction == D(scrap)
    assert check.parts[0].allowance_kg_per_unit == D(allowance)
    assert (check.pricing_rule_version, check.pricing_rule_fingerprint) == ("price-v1.0", "e" * 64)


@pytest.mark.parametrize("scrap", ["1", "1.5", "-0.01", 0.12, None, "abc", "0.1234567"])
def test_invalid_scrap_is_rejected(scrap):
    with pytest.raises(ValueError):
        scrap_fraction(replace(PRICING, document={**PRICING.document, "scrap_fraction": scrap}))


def test_manufacturing_rule_is_not_a_pricing_rule():
    with pytest.raises(ValueError, match="pricing rule"):
        scrap_fraction(RULES)


def test_sufficient_stock():
    check = decide([BRACKET], available="1000")
    line = check.lines[0]
    assert (check.result, line.status, line.shortage_kg) == ("sufficient", "sufficient", D(0))
    assert (line.required_kg, line.available_kg, line.part_ids) == (
        BRACKET_REQUIRED,
        D(1000),
        ("P1",),
    )


def test_exact_stock_boundary_is_sufficient():
    assert decide([BRACKET], available=str(BRACKET_REQUIRED)).lines[0].status == "sufficient"
    short = decide([BRACKET], available="224.143639").lines[0]
    assert (short.status, short.shortage_kg) == ("insufficient", D("0.000001"))


def test_insufficient_stock_reports_the_shortage():
    check = decide([BRACKET], available="100")
    line = check.lines[0]
    assert (check.result, line.status, line.shortage_kg) == (
        "insufficient",
        "insufficient",
        D("124.14364"),
    )


def test_zero_free_stock_is_unavailable():
    line = decide([BRACKET], available="0").lines[0]
    assert (line.status, line.shortage_kg) == ("unavailable", BRACKET_REQUIRED)


def test_missing_stock_record_is_unavailable():
    empty = MaterialAvailability(Material(material_id="MAT-009"), (), D(0))
    check = decide([BRACKET], stock_map={("MAT-009", D("1.0")): empty})
    line = check.lines[0]
    assert (check.result, line.status, line.inventory_ids, line.supplier_lead_business_days) == (
        "insufficient",
        "unavailable",
        (),
        (),
    )


def test_multiple_stock_records_are_summed_by_the_read_and_reported():
    two = stock("300", ("INV-1", "SNAP-1", 7, "120"), ("INV-2", "SNAP-1", 19, "180"))
    line = decide([BRACKET], stock_map={("MAT-009", D("1.0")): two}).lines[0]
    assert (line.status, line.inventory_ids, line.snapshot_ids) == (
        "sufficient",
        ("INV-1", "INV-2"),
        ("SNAP-1",),
    )
    assert line.supplier_lead_business_days == (7, 19)
    assert line.availability_class == "stocked"


def test_parts_sharing_material_and_thickness_are_aggregated():
    second = replace(BRACKET, part_id="P2", thickness_mm=D("1.000"), quantity=15)
    check = decide([BRACKET, second], available="300")
    (line,) = check.lines
    assert line.part_ids == ("P1", "P2")
    assert line.required_kg == D("263.6984")  # 100 units x 2.636984 kg
    assert (line.status, check.result) == ("sufficient", "sufficient")
    # 250 kg covers either part alone, but not both: demand is shared.
    assert decide([BRACKET, second], available="250").lines[0].status == "insufficient"


def test_different_thicknesses_are_separate_sorted_lines():
    thick = replace(BRACKET, part_id="P2", thickness_mm=D("4.0"), bend_radius_mm=D(4))
    check = decide([thick, BRACKET])
    assert [(line.thickness_mm, line.part_ids) for line in check.lines] == [
        (D("1.0"), ("P1",)),
        (D("4.0"), ("P2",)),
    ]


def test_one_short_line_makes_the_rfq_insufficient():
    thick = replace(BRACKET, part_id="P2", thickness_mm=D("4.0"), bend_radius_mm=D(4))
    stock_map = {("MAT-009", D("1.0")): stock("1000"), ("MAT-009", D("4.0")): stock("0")}
    check = decide([BRACKET, thick], stock_map=stock_map)
    assert check.result == "insufficient"
    assert [line.status for line in check.lines] == ["sufficient", "unavailable"]


def test_parts_needing_clarification_block_the_inventory_check():
    incomplete = replace(BRACKET, part_id="P2", tolerance_mm=None)
    check = decide([BRACKET, incomplete])
    assert (check.result, check.lines, check.parts) == ("needs_clarification", (), ())
    assert [m.result for m in check.manufacturing] == ["valid", "needs_clarification"]


@pytest.mark.parametrize("quantity", [0, -5, 1001])
def test_invalid_quantities_are_not_manufacturable(quantity):
    check = decide([replace(BRACKET, quantity=quantity)])
    assert (check.result, check.lines) == ("not_manufacturable", ())


def test_clarification_takes_precedence_over_unsupported():
    unsupported = replace(BRACKET, part_id="P2", quantity=0)
    incomplete = replace(BRACKET, part_id="P3", tolerance_mm=None)
    assert decide([unsupported, incomplete]).result == "needs_clarification"


def test_inconsistent_inputs_fail():
    with pytest.raises(ValueError, match="No stock read"):
        decide([BRACKET], stock_map={("MAT-001", D("1.0")): stock("1")})
    with pytest.raises(ValueError, match="exactly one"):
        decide([BRACKET], checks=[run(), run()])
    with pytest.raises(ValueError, match="exactly one"):
        evaluate_inventory([], [], {}, PRICING)


def test_inexact_arithmetic_raises_instead_of_rounding():
    huge = replace(STAINLESS, density_kg_m3=D("7." + "3" * 70))
    with pytest.raises(Inexact):
        decide([BRACKET], checks=[run(spec=huge)])


def test_results_are_immutable_and_reproducible():
    first, second = decide([BRACKET]), decide([BRACKET])
    assert first == second
    with pytest.raises(FrozenInstanceError):
        first.lines[0].status = "sufficient"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parts,pricing",
    [([BRACKET], RULES), ([], PRICING), ([{"part_id": "P1"}], PRICING)],
)
async def test_check_arguments_fail_before_sql(parts, pricing):
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError):
        await check_inventory(session, parts, manufacturing_rules=RULES, pricing_rules=pricing)
    session.scalar.assert_not_awaited()
    session.scalars.assert_not_awaited()
