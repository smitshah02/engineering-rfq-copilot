"""Manufacturing cost against the guarded, seeded, rolled-back test database.

Parity sources: the cost fields of dev/validation ground truth expected_pricing lines and
the reference pricing_engine.calculate_part, loaded as a test oracle only.
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
from test_day07_materials import reference_module
from test_day08_manufacturing import base_parts
from test_day09_inventory import PANEL, REF_MATERIALS, pinned, truth_files, variant

from rfq_copilot.entities import Part
from rfq_copilot.models import Material
from rfq_copilot.repositories import list_materials
from rfq_copilot.services.costing import display_costs, estimate_costs
from rfq_copilot.services.inventory import check_inventory

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

COST_FIELDS = (
    "part_id",
    "quantity",
    "operations",
    "material_cost_per_unit_usd",
    "labor_cost_per_unit_usd",
    "finish_cost_per_unit_usd",
    "packaging_cost_per_unit_usd",
    "variable_cost_per_unit_usd",
    "setup_cost_per_line_usd",
    "total_manufacturing_cost_usd",
    "pricing_rule_version",
)


async def costs(session, sources, rule_set):
    parts = [Part.from_mapping(s) for s in sources]
    inventory = await check_inventory(session, parts, **rule_set)
    return await estimate_costs(session, parts, inventory, **rule_set)


async def test_ground_truth_cost_fields(populated):
    rule_set = await pinned(populated)
    compared = 0
    for rfq_id, truth in truth_files():
        priced = (truth.get("expected_pricing") or {}).get("lines")
        if not priced:
            continue
        estimate = await costs(populated, truth["expected_extraction"], rule_set)
        shown = [display_costs(line, rule_set["pricing_rules"]) for line in estimate.lines]
        assert shown == [{k: row[k] for k in COST_FIELDS} for row in priced], rfq_id
        compared += len(priced)
    assert compared == 120


async def test_reference_parity_over_types_materials_finishes_quantities(populated):
    reference = reference_module("pricing_engine")
    rule_set = await pinned(populated)
    pricing = reference.load_rules()
    compared = 0
    for base in base_parts().values():
        for material_id, row in REF_MATERIALS.items():
            finishes = json.loads(row["compatible_finishes"])
            for finish in (finishes[0], finishes[-1]):
                for quantity in (1, 250):
                    source = {**variant(base, material_id, quantity), "finish_id": finish}
                    (line,) = (await costs(populated, [source], rule_set)).lines
                    ours = display_costs(line, rule_set["pricing_rules"])
                    ref = reference.calculate_part(source, row, pricing)
                    assert ours == {k: ref[k] for k in COST_FIELDS}, source
                    compared += 1
    assert compared == 6 * 15 * 2 * 2


async def test_list_materials_reads_catalog_rows(populated, seed_database):
    with statements(seed_database) as captured:
        rows = await list_materials(populated, ["MAT-009", "MAT-002", "MAT-002", "MAT-999"])
    assert [r.material_id for r in rows] == ["MAT-002", "MAT-009"]
    assert [r.cost_usd_per_kg for r in rows] == [Decimal("8.1"), Decimal("6.4")]
    assert {r.pricing_rule_version for r in rows} == {"price-v1.0"}
    assert len(captured) == 1
    with statements(seed_database) as captured:
        assert await list_materials(populated, []) == ()
    assert captured == []
    with pytest.raises(ValueError):
        await list_materials(populated, [""])


async def test_live_catalog_cost_is_used(populated):
    rule_set = await pinned(populated)
    await populated.execute(
        update(Material)
        .where(Material.material_id == "MAT-002")
        .values(cost_usd_per_kg=Decimal("10"))
    )
    populated.expunge_all()
    (line,) = (await costs(populated, [PANEL], rule_set)).lines
    assert (line.material_cost_usd_per_kg, line.material_cost_per_unit) == (
        Decimal("10"),
        Decimal("3.024") * 10,
    )


async def test_material_priced_under_another_version_is_rejected(populated):
    rule_set = await pinned(populated)
    await populated.execute(
        update(Material)
        .where(Material.material_id == "MAT-002")
        .values(pricing_rule_version="mfg-v1.0")
    )
    populated.expunge_all()
    with pytest.raises(ValueError, match="priced under mfg-v1.0"):
        await costs(populated, [PANEL], rule_set)


async def test_cost_reads_only_bounded_and_reproducible(populated, seed_database):
    rule_set = await pinned(populated)
    table = Material.__table__
    before = (await populated.execute(select(table).order_by(table.c.material_id))).all()
    parts = [Part.from_mapping(PANEL), Part.from_mapping({**PANEL, "part_id": "P2"})]
    inventory = await check_inventory(populated, parts, **rule_set)
    populated.expunge_all()
    with statements(seed_database) as captured:
        first = await estimate_costs(populated, parts, inventory, **rule_set)
    assert len(captured) == 1 and captured[0].lstrip().upper().startswith("SELECT")
    assert await estimate_costs(populated, parts, inventory, **rule_set) == first
    after = (await populated.execute(select(table).order_by(table.c.material_id))).all()
    assert after == before
