"""Manufacturing validator against the guarded, seeded, rolled-back test database.

Parity sources: dev/validation ground truth (findings and priced operation routes),
operation_routing.csv, and the reference validate_part as a test oracle only.
"""

import csv
import json
from decimal import Decimal

import pytest
from test_database_queries import populated as populated
from test_database_queries import statements
from test_database_seeding import SOURCE
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day07_materials import FINISHES, MATERIALS, reference_business_rules, rules
from test_entities import PARITY_SPLITS

from rfq_copilot.entities import Part
from rfq_copilot.repositories import list_part_routing
from rfq_copilot.services.manufacturing import (
    check_manufacturing_capability,
    evaluate_manufacturing,
)
from rfq_copilot.services.materials import evaluate_material, get_material_spec

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

ROUTING = list(csv.DictReader((SOURCE / "operations_data/operation_routing.csv").open()))


def labelled_cases():
    """(rfq_id, part source, manufacturing label, priced operations or None) per part."""
    for split in PARITY_SPLITS:
        for path in sorted((SOURCE / "evaluation/ground_truth" / split).glob("*.json")):
            truth = json.loads(path.read_text())
            priced = {
                line["part_id"]: line["operations"]
                for line in (truth.get("expected_pricing") or {}).get("lines", [])
            }
            parts = truth.get("expected_extraction") or []
            labels = truth.get("manufacturing_validation") or []
            for part, label in zip(parts, labels, strict=True):
                yield path.stem, part, label, priced.get(part["part_id"])


def base_parts():
    """One valid ground-truth part per part type."""
    found = {}
    for _, part, label, _ in labelled_cases():
        if label["result"] == "valid":
            found.setdefault(part["part_type"], part)
    assert len(found) == 6
    return found


async def test_ground_truth_findings_and_routes(populated):
    pinned = await rules(populated)
    cases, routes = 0, 0
    for rfq_id, source, label, priced in labelled_cases():
        check = await check_manufacturing_capability(populated, Part.from_mapping(source), pinned)
        assert (check.result, list(check.findings)) == (label["result"], label["findings"]), rfq_id
        if priced is not None:
            assert list(check.operations) == priced, rfq_id
            routes += 1
        cases += 1
    assert (cases, routes) == (180, 120)


async def test_routing_matches_operation_routing_csv(populated):
    for part_type in {row["part_type"] for row in ROUTING}:
        stored = await list_part_routing(populated, part_type)
        expected = sorted(
            (r for r in ROUTING if r["part_type"] == part_type), key=lambda r: int(r["sequence"])
        )
        assert [(s.operation_id, s.sequence, s.required_when) for s in stored] == [
            (r["operation_id"], int(r["sequence"]), r["required_when"]) for r in expected
        ]
    assert await list_part_routing(populated, "casting") == ()


def perturbations(base):
    thickness = Decimal(base["thickness_mm"])
    yield {}
    for field, values in {
        "length_mm": ["19.999", 20, 1200, "1200.001"],
        "width_mm": ["19.999", 20, 1000, "1000.001"],
        "height_mm": ["9.999", 10, 450, "450.001"],
        "thickness_mm": ["0.799", "0.8", "6.0", "6.001", "2.5"],
        "tolerance_mm": ["0.199", "0.2", "0.05"],
        "quantity": [0, 1, 1000, 1001],
        "hole_count": [0, 300],
        "part_type": ["casting", "flat_panel", "enclosure"],
        "material_id": ["MAT-999", "MAT-012"],
        "finish_id": ["anodized", "chrome", "none"],
    }.items():
        for value in values:
            yield {field: value}
    if "bend_radius_mm" in base:
        for radius in (thickness - Decimal("0.001"), thickness, thickness * 2):
            yield {"bend_radius_mm": str(radius)}
    for field in ("tolerance_mm", "material_id", "height_mm", "quantity"):
        yield {field: None}


async def test_reference_parity_over_single_field_changes(populated):
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
    routing = {
        t: await list_part_routing(populated, t) for t in pinned.document["allowed_part_types"]
    }
    compared = 0
    for base in base_parts().values():
        for change in perturbations(base):
            source = {k: v for k, v in {**base, **change}.items() if v is not None}
            ref = reference.validate_part(source, ref_materials, ref_finishes)
            part = Part.from_mapping(source)
            material = evaluate_material(
                part,
                pinned,
                specs.get(part.material_id),
                finish_exists=part.finish_id in ref_finishes,
            )
            ours = evaluate_manufacturing(part, pinned, material, routing.get(part.part_type, ()))
            assert (ours.result, list(ours.findings)) == (ref["result"], ref["findings"]), source
            compared += 1
    # 6 part types x 39 changes, plus 3 bend-radius changes for each of the 3 bent types.
    assert compared == 6 * 39 + 3 * 3


async def test_mfg_001_regression_against_the_reference(populated):
    reference = reference_business_rules()
    pinned = await rules(populated)
    base = base_parts()["flat_panel"]
    row = next(r for r in MATERIALS if r["material_id"] == base["material_id"])
    ref_materials = {
        row["material_id"]: {
            **row,
            "supported_thickness_mm": json.loads(row["supported_thickness_mm"]),
            "compatible_finishes": json.loads(row["compatible_finishes"]),
        }
    }
    ref_finishes = {row["finish_id"]: row for row in FINISHES}
    for holes, expected in [
        (300, ("valid", ())),
        (301, ("unsupported", ("OUT_OF_RANGE_HOLE_COUNT",))),
    ]:
        source = {**base, "hole_count": holes}
        check = await check_manufacturing_capability(populated, Part.from_mapping(source), pinned)
        assert (check.result, check.findings) == expected
        # The frozen reference still accepts 301: the defect is fixed here, not there.
        assert reference.validate_part(source, ref_materials, ref_finishes)["result"] == "valid"


async def test_validation_reads_are_bounded_selects(populated, seed_database):
    pinned = await rules(populated)
    source = base_parts()["l_bracket"]
    for change, count in [({}, 7), ({"part_type": "casting"}, 6), ({"material_id": "MAT-999"}, 3)]:
        populated.expunge_all()
        with statements(seed_database) as captured:
            await check_manufacturing_capability(
                populated, Part.from_mapping({**source, **change}), pinned
            )
        assert len(captured) == count, (change, captured)
        assert all(s.lstrip().upper().startswith("SELECT") for s in captured)
