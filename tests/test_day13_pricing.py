"""Pricing against the guarded, seeded, rolled-back test database.

Parity sources: dev/validation ground truth expected_pricing (every field, including
calculation_id) and the reference pricing_engine.calculate_quote, a test oracle only.
"""

import json
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from test_database_queries import populated as populated
from test_database_queries import statements
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day06_repositories import insert_runtime_rfq
from test_day07_materials import reference_module
from test_day08_manufacturing import base_parts
from test_day09_inventory import PANEL, REF_MATERIALS, pinned, truth_files, variant

from rfq_copilot.entities import Part
from rfq_copilot.models import Operation
from rfq_copilot.repositories import get_rfq_parts, list_operations
from rfq_copilot.services.costing import estimate_costs
from rfq_copilot.services.inventory import check_inventory
from rfq_copilot.services.pricing import calculate_quote, calculation_id, display_quote

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

D = Decimal
# Reference strings keep the scale of their own arithmetic; compare these by value.
MASS_FIELDS = ("blank_area_m2", "net_material_kg_per_unit", "material_allowance_kg_per_unit")


async def quote(session, parts, rule_set):
    inventory = await check_inventory(session, parts, **rule_set)
    costs = await estimate_costs(session, parts, inventory, **rule_set)
    return await calculate_quote(session, parts, costs, pricing_rules=rule_set["pricing_rules"])


def assert_same_quote(ours, expected, context):
    ours_lines, expected_lines = ours.pop("lines"), dict(expected).pop("lines")
    assert ours == {k: v for k, v in expected.items() if k != "lines"}, context
    assert len(ours_lines) == len(expected_lines), context
    for mine, theirs in zip(ours_lines, expected_lines, strict=True):
        assert {k: D(mine[k]) for k in MASS_FIELDS} == {k: D(theirs[k]) for k in MASS_FIELDS}
        assert {k: v for k, v in mine.items() if k not in MASS_FIELDS} == {
            k: v for k, v in theirs.items() if k not in MASS_FIELDS
        }, context


async def test_ground_truth_quotes_field_for_field(populated):
    rule_set = await pinned(populated)
    compared, lines = 0, 0
    for rfq_id, truth in truth_files():
        expected = truth.get("expected_pricing")
        if not expected:
            continue
        parts = [Part.from_mapping(p) for p in truth["expected_extraction"]]
        calc = await quote(populated, parts, rule_set)
        assert_same_quote(display_quote(calc, rule_set["pricing_rules"]), expected, rfq_id)
        compared += 1
        lines += len(calc.lines)
    assert (compared, lines) == (100, 120)


def canonical(source):
    """Spell numbers the way the contract examples do, so reference IDs are comparable."""
    if "bend_radius_mm" in source:
        source = {**source, "bend_radius_mm": f"{D(source['bend_radius_mm']):.2f}"}
    return source


async def test_reference_parity_over_types_materials_and_multi_part_quotes(populated):
    reference = reference_module("pricing_engine")
    rule_set = await pinned(populated)
    bases = list(base_parts().values())
    cases = [[canonical(variant(b, m, q))] for b in bases for m in REF_MATERIALS for q in (1, 77)]
    cases += [
        [canonical(variant(b, m, 25, f"L{i}")) for i, b in enumerate(bases)]
        for m in ("MAT-002", "MAT-004", "MAT-008")
    ]
    for sources in cases:
        calc = await quote(populated, [Part.from_mapping(s) for s in sources], rule_set)
        expected = reference.calculate_quote(sources, REF_MATERIALS)
        assert_same_quote(display_quote(calc, rule_set["pricing_rules"]), expected, sources)
    assert len(cases) == 6 * 15 * 2 + 3


async def test_reference_ids_depend_on_spelling_ours_do_not(populated):
    reference = reference_module("pricing_engine")
    source = base_parts()["l_bracket"]
    respelled = {**source, "thickness_mm": "1.00", "bend_radius_mm": "1.5"}
    ref_a = reference.calculate_quote([source], REF_MATERIALS)["calculation_id"]
    ref_b = reference.calculate_quote([respelled], REF_MATERIALS)["calculation_id"]
    assert ref_a != ref_b  # same part, different reference IDs
    ours = {calculation_id([Part.from_mapping(s)], "price-v1.0") for s in (source, respelled)}
    assert ours == {ref_a}


async def test_parts_read_back_from_the_database_keep_their_calculation_id(populated, seed_plan):
    # Stored Numeric(12,3) columns come back as 450.000, 1.000, 1.500.
    customer = seed_plan.rows["rfqs"][0]["customer_id"]
    await insert_runtime_rfq(populated, customer)
    stored = (await get_rfq_parts(populated, "TEST-DAY6-RFQ", customer_id=customer))[0]
    assert stored.length_mm == D("450") and str(stored.length_mm) != "450"
    mapping = Part.from_mapping(
        {
            "part_id": "PART-A",
            "part_type": "l_bracket",
            "material_id": "MAT-009",
            "finish_id": "none",
            "length_mm": 450,
            "width_mm": 490,
            "height_mm": 170,
            "thickness_mm": "1.0",
            "tolerance_mm": "0.5",
            "bend_radius_mm": "1.50",
            "quantity": 85,
            "hole_count": 11,
        }
    )
    assert calculation_id([stored], "price-v1.0") == calculation_id([mapping], "price-v1.0")


async def test_list_operations_and_quoted_flags(populated, seed_database):
    with statements(seed_database) as captured:
        rows = await list_operations(populated, ["punch", "laser", "machine", "laser", "nope"])
    assert [(r.operation_id, r.is_quoted) for r in rows] == [
        ("laser", True),
        ("machine", False),
        ("punch", False),
    ]
    assert len(captured) == 1
    with statements(seed_database) as captured:
        assert await list_operations(populated, []) == ()
    assert captured == []


async def test_unquoted_operation_in_the_catalog_blocks_pricing(populated):
    # TD-001: an operation the catalog marks as not quoted can never be priced.
    rule_set = await pinned(populated)
    await populated.execute(
        update(Operation).where(Operation.operation_id == "inspect").values(is_quoted=False)
    )
    populated.expunge_all()
    with pytest.raises(ValueError, match=r"\['inspect'\] are not quoted"):
        await quote(populated, [Part.from_mapping(PANEL)], rule_set)


async def test_pricing_reads_only_bounded_and_reproducible(populated, seed_database):
    rule_set = await pinned(populated)
    table = Operation.__table__
    before = (await populated.execute(select(table).order_by(table.c.operation_id))).all()
    parts = [Part.from_mapping(PANEL), Part.from_mapping({**PANEL, "part_id": "P2", "quantity": 4})]
    inventory = await check_inventory(populated, parts, **rule_set)
    costs = await estimate_costs(populated, parts, inventory, **rule_set)
    populated.expunge_all()
    with statements(seed_database) as captured:
        first = await calculate_quote(
            populated, parts, costs, pricing_rules=rule_set["pricing_rules"]
        )
    assert len(captured) == 1 and captured[0].lstrip().upper().startswith("SELECT")
    again = await calculate_quote(populated, parts, costs, pricing_rules=rule_set["pricing_rules"])
    assert again == first and first.result == "priced"
    assert first.quote_total_usd == sum(line.line_total_usd for line in first.lines)
    assert json.dumps(display_quote(first, rule_set["pricing_rules"]))  # JSON-serialisable
    after = (await populated.execute(select(table).order_by(table.c.operation_id))).all()
    assert after == before
