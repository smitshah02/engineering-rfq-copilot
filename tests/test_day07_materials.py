"""Material catalog against the guarded, seeded, rolled-back test database.

Parity sources: master_data/materials.csv, dev/validation ground truth, and the dataset's
reference validate_part (loaded as a test oracle only; never imported by src/).
"""

import csv
import hashlib
import importlib
import importlib.util
import itertools
import json
import sys
from decimal import Decimal

import pytest
from test_database_queries import populated as populated
from test_database_queries import statements
from test_database_seeding import SOURCE
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_entities import ground_truth_parts

from rfq_copilot.entities import Part
from rfq_copilot.repositories import find_materials, get_rule_version
from rfq_copilot.services.materials import (
    MATERIAL_FIELDS,
    check_part_material,
    evaluate_material,
    get_material_spec,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

MATERIALS = list(csv.DictReader((SOURCE / "master_data/materials.csv").open()))
FINISHES = list(csv.DictReader((SOURCE / "master_data/finishes.csv").open()))
RULE_SHA = hashlib.sha256((SOURCE / "config/manufacturing_rules.json").read_bytes()).hexdigest()
MATERIAL_CODES = {
    "UNSUPPORTED_MATERIAL",
    "UNSUPPORTED_FINISH",
    "MATERIAL_THICKNESS_UNSUPPORTED",
    "MATERIAL_FINISH_INCOMPATIBLE",
} | {f"MISSING_{f.upper()}" for f in MATERIAL_FIELDS}


def reference_business_rules():
    return reference_module("business_rules")


def reference_module(name):
    """Load one reference engine module as a test oracle, without writing bytecode."""
    location = SOURCE / "src"
    spec = importlib.util.spec_from_file_location(
        "rfq_reference", location / "__init__.py", submodule_search_locations=[str(location)]
    )
    package = importlib.util.module_from_spec(spec)
    # The dataset is read-only: importing must not leave __pycache__ inside it.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        sys.modules["rfq_reference"] = package
        spec.loader.exec_module(package)
        return importlib.import_module(f"rfq_reference.{name}")
    finally:
        sys.dont_write_bytecode = previous


async def rules(session):
    return await get_rule_version(session, "manufacturing", "mfg-v1.0", expected_sha256=RULE_SHA)


def expected_result(findings):
    if any(f.startswith("MISSING_") for f in findings):
        return "needs_clarification"
    return "unsupported" if findings else "valid"


async def test_every_spec_matches_master_data(populated):
    for row in MATERIALS:
        spec = await get_material_spec(populated, row["material_id"])
        assert (spec.material_id, spec.category, spec.grade, spec.availability_class) == (
            row["material_id"],
            row["category"],
            row["grade"],
            row["availability_class"],
        )
        assert spec.density_kg_m3 == Decimal(row["density_kg_m3"])
        assert spec.min_bend_radius_factor == Decimal(row["min_bend_radius_factor"])
        assert spec.supported_thickness_mm == tuple(
            sorted(Decimal(t) for t in json.loads(row["supported_thickness_mm"]))
        )
        assert all(type(t) is Decimal for t in spec.supported_thickness_mm)
        assert spec.compatible_finishes == tuple(sorted(json.loads(row["compatible_finishes"])))
        assert spec.supported_operations == tuple(sorted(json.loads(row["supported_operations"])))
        assert not hasattr(spec, "cost_usd_per_kg")


async def test_unknown_material_spec_is_none(populated):
    assert await get_material_spec(populated, "MAT-999") is None
    with pytest.raises(ValueError):
        await get_material_spec(populated, "")


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"grade": "304"}, ("MAT-007",)),
        ({"grade": " 6061-t6 "}, ("MAT-002",)),
        ({"grade": "7075-T6"}, ()),
        ({"category": "copper"}, ("MAT-012", "MAT-013")),
        ({"category": "titanium"}, ()),
        ({"category": "aluminum", "grade": "6061-T6"}, ("MAT-002",)),
        ({"category": "stainless_steel", "grade": "6061-T6"}, ()),
        ({"thickness_mm": Decimal("0.80")}, ("MAT-012", "MAT-013", "MAT-014", "MAT-015")),
        ({"finish_id": "anodized"}, ("MAT-001", "MAT-002", "MAT-003")),
        ({"category": "stainless_steel", "finish_id": "anodized"}, ()),
        ({}, tuple(row["material_id"] for row in MATERIALS)),
    ],
)
async def test_find_materials_by_grade_and_specification(populated, kwargs, expected):
    assert await find_materials(populated, **kwargs) == expected


async def test_find_materials_thickness_and_finish_grid_matches_master_data(populated):
    thicknesses = {t for row in MATERIALS for t in json.loads(row["supported_thickness_mm"])}
    for thickness, finish in itertools.product(
        sorted(thicknesses) + ["2.5"], [f["finish_id"] for f in FINISHES] + ["chrome"]
    ):
        expected = tuple(
            row["material_id"]
            for row in MATERIALS
            if thickness in json.loads(row["supported_thickness_mm"])
            and finish in json.loads(row["compatible_finishes"])
        )
        found = await find_materials(populated, thickness_mm=Decimal(thickness), finish_id=finish)
        assert found == expected, (thickness, finish)


async def test_ground_truth_material_findings(populated):
    pinned = await rules(populated)
    parts = list(ground_truth_parts())
    labels = []
    for split in ("development", "validation"):
        for path in sorted((SOURCE / "evaluation/ground_truth" / split).glob("*.json")):
            labels += json.loads(path.read_text()).get("manufacturing_validation") or []
    assert len(parts) == len(labels) == 180
    for (rfq_id, source), label in zip(parts, labels, strict=True):
        check = await check_part_material(populated, Part.from_mapping(source), pinned)
        expected = tuple(sorted(set(label["findings"]) & MATERIAL_CODES))
        assert (check.result, check.findings) == (expected_result(expected), expected), rfq_id


async def test_reference_parity_over_material_thickness_finish_grid(populated):
    reference = reference_business_rules()
    ref_materials = {
        row["material_id"]: {
            **row,
            "supported_thickness_mm": json.loads(row["supported_thickness_mm"]),
            "compatible_finishes": json.loads(row["compatible_finishes"]),
        }
        for row in MATERIALS
    }
    ref_finishes = {row["finish_id"]: row for row in FINISHES}
    pinned = await rules(populated)
    specs = {m: await get_material_spec(populated, m) for m in ref_materials}
    _, base = next(ground_truth_parts())
    thicknesses = sorted({t for m in ref_materials.values() for t in m["supported_thickness_mm"]})
    compared = 0
    for material, thickness, finish in itertools.product(
        [*ref_materials, "MAT-999"], [*thicknesses, "2.5"], [*ref_finishes, "chrome"]
    ):
        source = {**base, "material_id": material, "thickness_mm": thickness, "finish_id": finish}
        ref = reference.validate_part(source, ref_materials, ref_finishes)
        ours = evaluate_material(
            Part.from_mapping(source),
            pinned,
            specs.get(material),
            finish_exists=finish in ref_finishes,
        )
        expected = tuple(sorted(set(ref["findings"]) & MATERIAL_CODES))
        assert (ours.result, ours.findings) == (expected_result(expected), expected), source
        compared += 1
    assert compared == 16 * 8 * 8


async def test_known_divergence_unpadded_thickness(populated):
    # The reference compares str(thickness) with catalog strings, so "1" != "1.0" there.
    reference = reference_business_rules()
    _, base = next(ground_truth_parts())
    source = {**base, "material_id": "MAT-009", "thickness_mm": "1", "finish_id": "none"}
    row = next(r for r in MATERIALS if r["material_id"] == "MAT-009")
    ref_materials = {
        "MAT-009": {
            **row,
            "supported_thickness_mm": json.loads(row["supported_thickness_mm"]),
            "compatible_finishes": json.loads(row["compatible_finishes"]),
        }
    }
    ref = reference.validate_part(source, ref_materials, {"none": {}})
    assert "MATERIAL_THICKNESS_UNSUPPORTED" in ref["findings"]
    check = await check_part_material(populated, Part.from_mapping(source), await rules(populated))
    assert check.result == "valid"


async def test_check_wires_catalog_facts(populated):
    pinned = await rules(populated)
    part = Part(part_id="P", material_id="MAT-009", thickness_mm=Decimal("1.0"), finish_id="none")
    valid = await check_part_material(populated, part, pinned)
    assert valid.result == "valid" and valid.material.grade == "430"
    assert (valid.rule_version, valid.rule_fingerprint) == ("mfg-v1.0", pinned.fingerprint)
    cases = {
        ("MAT-009", "anodized"): ("MATERIAL_FINISH_INCOMPATIBLE",),
        ("MAT-009", "chrome"): ("MATERIAL_FINISH_INCOMPATIBLE", "UNSUPPORTED_FINISH"),
        ("MAT-999", "none"): ("UNSUPPORTED_MATERIAL",),
        ("MAT-999", "chrome"): ("UNSUPPORTED_FINISH", "UNSUPPORTED_MATERIAL"),
    }
    for (material, finish), findings in cases.items():
        probe = Part(
            part_id="P", material_id=material, thickness_mm=Decimal("1.0"), finish_id=finish
        )
        assert (await check_part_material(populated, probe, pinned)).findings == findings


async def test_wrong_rule_kind_is_rejected(populated):
    pricing = await get_rule_version(populated, "pricing", "price-v1.0")
    part = Part(part_id="P", material_id="MAT-009", thickness_mm=Decimal("1.0"), finish_id="none")
    with pytest.raises(ValueError, match="manufacturing rule"):
        await check_part_material(populated, part, pricing)


async def test_material_reads_are_bounded_selects(populated, seed_database):
    pinned = await rules(populated)
    populated.expunge_all()
    part = Part(part_id="P", material_id="MAT-009", thickness_mm=Decimal("1.0"), finish_id="none")
    expected = {
        part: 6,  # capability read; a compatible finish needs no finish lookup
        Part(
            part_id="P", material_id="MAT-009", thickness_mm=Decimal("1.0"), finish_id="anodized"
        ): 7,
        Part(part_id="P", material_id="MAT-999", thickness_mm=Decimal("1.0"), finish_id="none"): 2,
    }
    for probe, count in expected.items():
        populated.expunge_all()
        with statements(seed_database) as captured:
            await check_part_material(populated, probe, pinned)
        assert len(captured) == count, (probe, captured)
        assert all(s.lstrip().upper().startswith("SELECT") for s in captured)
    with statements(seed_database) as captured:
        await find_materials(populated, grade="304", thickness_mm=Decimal("1.0"), finish_id="none")
    assert len(captured) == 1
