"""Inventory availability against the guarded, seeded, rolled-back test database.

Parity sources: dev/validation ground truth (expected_inventory and pricing mass fields)
and the reference capacity_engine.check_inventory, loaded as a test oracle only.
"""

import csv
import hashlib
import json
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
from test_day07_materials import MATERIALS, reference_module, rules
from test_day08_manufacturing import base_parts
from test_entities import PARITY_SPLITS

from rfq_copilot.entities import Part
from rfq_copilot.models import MaterialInventory
from rfq_copilot.repositories import get_rule_version
from rfq_copilot.services.inventory import check_inventory

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

PRICING_SHA = hashlib.sha256((SOURCE / "config/pricing_rules.json").read_bytes()).hexdigest()
INVENTORY = list(csv.DictReader((SOURCE / "operations_data/material_inventory.csv").open()))
REF_MATERIALS = {row["material_id"]: row for row in MATERIALS}
D = Decimal


async def pinned(session):
    pricing = await get_rule_version(session, "pricing", "price-v1.0", expected_sha256=PRICING_SHA)
    return {"manufacturing_rules": await rules(session), "pricing_rules": pricing}


def truth_files():
    for split in PARITY_SPLITS:
        for path in sorted((SOURCE / "evaluation/ground_truth" / split).glob("*.json")):
            yield path.stem, json.loads(path.read_text())


def lines(check):
    return [
        (
            line.material_id,
            line.thickness_mm,
            line.required_kg,
            line.available_kg,
            line.status == "sufficient",
        )
        for line in check.lines
    ]


async def test_ground_truth_inventory_outcomes(populated):
    rule_set = await pinned(populated)
    compared, blocked, short = 0, 0, 0
    for rfq_id, truth in truth_files():
        parts = [Part.from_mapping(p) for p in truth.get("expected_extraction") or []]
        if not parts:
            continue
        expected = truth.get("expected_inventory")
        if expected is not None:
            check = await check_inventory(populated, parts, **rule_set)
            assert lines(check) == [
                (
                    row["material_id"],
                    D(row["thickness_mm"]),
                    D(row["required_kg"]),
                    D(row["available_kg"]),
                    row["sufficient"],
                )
                for row in expected
            ], rfq_id
            assert check.result == (
                "sufficient" if all(row["sufficient"] for row in expected) else "insufficient"
            )
            # Lead-time preview: the facts carried here reproduce the labelled procurement days.
            for line, row in zip(check.lines, expected, strict=True):
                leads = [d for d in line.supplier_lead_business_days if d > 0]
                days = 0 if line.status == "sufficient" else max(leads, default=15)
                assert days == row["procurement_business_days"], rfq_id
            compared += 1
            short += check.result == "insufficient"
        elif truth["primary_category"] in ("incomplete", "unsupported"):
            check = await check_inventory(populated, parts, **rule_set)
            assert check.lines == ()
            assert (
                check.result
                == {
                    "incomplete": "needs_clarification",
                    "unsupported": "not_manufacturable",
                }[truth["primary_category"]]
            )
            blocked += 1
    assert (compared, short, blocked) == (100, 48, 40)


async def test_part_masses_match_ground_truth_pricing_lines(populated):
    rule_set = await pinned(populated)
    compared = 0
    for rfq_id, truth in truth_files():
        priced = (truth.get("expected_pricing") or {}).get("lines") or []
        if not priced:
            continue
        parts = [Part.from_mapping(p) for p in truth["expected_extraction"]]
        check = await check_inventory(populated, parts, **rule_set)
        ours = {r.part_id: r for r in check.parts}
        for row in priced:
            r = ours[row["part_id"]]
            assert (r.blank_area_m2, r.net_kg_per_unit, r.allowance_kg_per_unit) == (
                D(row["blank_area_m2"]),
                D(row["net_material_kg_per_unit"]),
                D(row["material_allowance_kg_per_unit"]),
            ), rfq_id
            compared += 1
    assert compared == 120


def variant(base, material_id, quantity, part_id=None):
    supported = json.loads(REF_MATERIALS[material_id]["supported_thickness_mm"])
    thickness = base["thickness_mm"] if base["thickness_mm"] in supported else supported[0]
    source = {
        **base,
        "material_id": material_id,
        "thickness_mm": thickness,
        "finish_id": "none",
        "quantity": quantity,
    }
    if part_id:
        source["part_id"] = part_id
    if "bend_radius_mm" in base:
        source["bend_radius_mm"] = str(D(thickness) * 2)
    return source


async def test_reference_parity_over_types_materials_and_quantities(populated):
    reference = reference_module("capacity_engine")
    rule_set = await pinned(populated)
    cases = []
    bases = list(base_parts().values())
    for base in bases:
        for material_id in REF_MATERIALS:
            for quantity in (1, 37, 1000):
                cases.append([variant(base, material_id, quantity)])
    # Multi-part RFQs that share one material and thickness, so demand is aggregated.
    for first, second in zip(bases, bases[1:], strict=False):
        cases.append(
            [variant(first, "MAT-004", 300), variant(second, "MAT-004", 300, part_id="SECOND")]
        )
    statuses = set()
    for sources in cases:
        check = await check_inventory(
            populated, [Part.from_mapping(s) for s in sources], **rule_set
        )
        assert check.result in ("sufficient", "insufficient"), sources
        ref = reference.check_inventory(sources, REF_MATERIALS, INVENTORY, "2026-11-09")
        assert lines(check) == [
            (
                r["material_id"],
                D(r["thickness_mm"]),
                D(r["required_kg"]),
                D(r["available_kg"]),
                r["sufficient"],
            )
            for r in ref
        ], sources
        statuses |= {line.status for line in check.lines}
    assert len(cases) == 6 * 15 * 3 + 5
    assert statuses == {"sufficient", "insufficient", "unavailable"}


PANEL = {
    "part_id": "PANEL",
    "part_type": "flat_panel",
    "material_id": "MAT-002",
    "finish_id": "none",
    "length_mm": 1000,
    "width_mm": 500,
    "thickness_mm": "2.0",
    "tolerance_mm": "0.5",
    "quantity": 10,
}  # business_rules.md arithmetic: 3.024 kg per unit, 30.24 kg in total


async def stock_row(session, material_id, thickness):
    return (
        (
            await session.execute(
                select(MaterialInventory.__table__).where(
                    MaterialInventory.material_id == material_id,
                    MaterialInventory.thickness_mm == D(thickness),
                )
            )
        )
        .mappings()
        .one()
    )


async def test_exact_stock_boundary_in_the_database(populated):
    rule_set = await pinned(populated)
    row = await stock_row(populated, "MAT-002", "2.0")
    for free, status in [("30.24", "sufficient"), ("30.239999", "insufficient")]:
        await populated.execute(
            update(MaterialInventory)
            .where(MaterialInventory.inventory_id == row["inventory_id"])
            .values(quantity_kg=row["reserved_kg"] + D(free))
        )
        populated.expunge_all()  # the computed available_kg must be re-read
        check = await check_inventory(populated, [Part.from_mapping(PANEL)], **rule_set)
        (line,) = check.lines
        assert (line.required_kg, line.available_kg, line.status) == (D("30.24"), D(free), status)
        assert line.shortage_kg == max(D("30.24") - D(free), D(0))


async def test_multiple_stock_records_are_summed(populated):
    rule_set = await pinned(populated)
    row = dict(await stock_row(populated, "MAT-002", "2.0"))
    del row["available_kg"]  # computed by PostgreSQL
    extra = {**row, "inventory_id": "TEST-DAY9-INV", "warehouse": "WH-Z", "quantity_kg": D("12.5")}
    extra["reserved_kg"] = D("2.5")
    await populated.execute(MaterialInventory.__table__.insert().values(**extra))
    populated.expunge_all()
    (line,) = (await check_inventory(populated, [Part.from_mapping(PANEL)], **rule_set)).lines
    first_free = row["quantity_kg"] - row["reserved_kg"]
    assert line.available_kg == first_free + D(10)
    assert line.inventory_ids == tuple(sorted((row["inventory_id"], "TEST-DAY9-INV")))
    assert len(line.supplier_lead_business_days) == 2


async def test_zero_free_stock_is_unavailable_with_procurement_facts(populated):
    rule_set = await pinned(populated)
    zero = next(r for r in INVENTORY if D(r["available_kg"]) == 0 and r["material_id"] == "MAT-001")
    part = {**PANEL, "material_id": "MAT-001", "thickness_mm": zero["thickness_mm"]}
    check = await check_inventory(populated, [Part.from_mapping(part)], **rule_set)
    (line,) = check.lines
    assert (check.result, line.status, line.available_kg) == ("insufficient", "unavailable", D(0))
    assert line.shortage_kg == line.required_kg
    assert line.availability_class == "procure"
    assert line.inventory_ids == (zero["inventory_id"],)
    assert line.supplier_lead_business_days == (int(zero["supplier_lead_business_days"]),)
    assert line.snapshot_ids and all(line.snapshot_ids)


async def test_wrong_rule_kinds_are_rejected(populated):
    rule_set = await pinned(populated)
    swapped = {
        "manufacturing_rules": rule_set["pricing_rules"],
        "pricing_rules": rule_set["manufacturing_rules"],
    }
    with pytest.raises(ValueError):
        await check_inventory(populated, [Part.from_mapping(PANEL)], **swapped)


async def test_reads_only_bounded_and_reproducible(populated, seed_database):
    rule_set = await pinned(populated)
    table = MaterialInventory.__table__
    before = (await populated.execute(select(table).order_by(table.c.inventory_id))).all()
    second = {**PANEL, "part_id": "PANEL-2", "quantity": 3}
    # +1 for the inventory snapshot selection; the second part shares the first's catalog
    # reads (request-scoped memo) and stock read, so it costs no SELECT (was 7).
    for parts, count in [([PANEL], 10), ([PANEL, second], 10)]:
        populated.expunge_all()
        with statements(seed_database) as captured:
            first = await check_inventory(
                populated, [Part.from_mapping(p) for p in parts], **rule_set
            )
        assert len(captured) == count, captured
        assert all(s.lstrip().upper().startswith("SELECT") for s in captured)
        again = await check_inventory(populated, [Part.from_mapping(p) for p in parts], **rule_set)
        assert again == first
    after = (await populated.execute(select(table).order_by(table.c.inventory_id))).all()
    assert after == before
