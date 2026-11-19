"""Incident-shaped offline regressions; the original model response is unavailable.

Only the model and catalog boundaries are mocked. No database, live MCP or Ollama.
Duplicate candidates deliberately remain separate and require clarification.
"""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_intake import ev, model, reply
from test_intake_validation import MATERIALS, RULES, Catalog
from test_manufacturing_service import BRACKET_ROUTE

from rfq_copilot import intake
from rfq_copilot import workflow_worker as ww
from rfq_copilot.entities import Part
from rfq_copilot.intake_validation import validate_intake
from rfq_copilot.mcp.client import McpToolClient
from rfq_copilot.mcp.contracts import PartCheckInput
from rfq_copilot.services.manufacturing import evaluate_manufacturing
from rfq_copilot.services.materials import MaterialSpec, evaluate_material

PART_ID = "P-TEST-BRACKET-001"
SOURCE = (
    "Part P-TEST-BRACKET-001: quantity 20, part type l bracket, "
    "material aluminum 5052-H32, finish unfinished, length 100 mm, width 60 mm, "
    "height 30 mm, thickness 2 mm, tolerance 0.2 mm, bend radius 4 mm, hole count 0."
)


@pytest.fixture(autouse=True)
def no_live_mcp(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("use the injected offline catalog")

    monkeypatch.setattr(McpToolClient, "call", forbidden)


def stored_line(part_id=PART_ID):
    return SimpleNamespace(
        part_id=part_id, part_type="l_bracket", material_id="MAT-001", finish_id="none",
        length_mm=Decimal("100.000"), width_mm=Decimal("60.000"),
        height_mm=Decimal("30.000"), thickness_mm=Decimal("2.000"),
        bend_radius_mm=Decimal("4.000"), tolerance_mm=Decimal("0.200"),
        quantity=20, hole_count=0, weld_length_mm=None,
    )  # fmt: skip


def source(lines, notes=None):
    return ww.source_text(lines, notes, {"MAT-001": "aluminum 5052-H32"}, {"none": "unfinished"})


def candidate(label=PART_ID, description=True):
    return {
        "label": label,
        "description": ev("l bracket", "part type l bracket") if description else None,
        "quantity": ev(20, "quantity 20"),
        "material": ev("aluminum 5052-H32", "material aluminum 5052-H32"),
        "finish": ev("unfinished", "finish unfinished"),
        "length": ev("100 mm", "length 100 mm"),
        "width": ev("60 mm", "width 60 mm"),
        "height": ev("30 mm", "height 30 mm"),
        "thickness": ev("2 mm", "thickness 2 mm"),
        "tolerance": ev("0.2 mm", "tolerance 0.2 mm"),
        "hole_count": ev(0, "hole count 0"),
        # Intake currently has no structured bend-radius field. Preserve its source
        # evidence as a note; the engineering test below evaluates the stored radius.
        "notes": [ev("bend radius 4 mm")],
    }


async def extract_and_validate(text, candidates, ids=(PART_ID,)):
    graph = intake.build_intake_graph(model(reply({"parts": candidates})))
    extracted = await intake.run_intake(graph, text, source_id="bracket-regression")
    catalog = Catalog()
    validated = await validate_intake(
        extracted, text, rules=RULES, catalog=catalog, trusted_part_ids=ids
    )
    return extracted, validated, catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("label", [PART_ID, f"Part {PART_ID}"])
@pytest.mark.parametrize("description", [True, False], ids=["model_type", "source_type"])
async def test_exact_bracket_single_candidate_ignores_type_word_inside_verified_id(
    label, description
):
    line = stored_line()
    text = source([line])
    assert text == SOURCE
    extracted, validated, _ = await extract_and_validate(text, [candidate(label, description)])
    (part,) = extracted.parts
    assert extracted.part_boundaries == "single_part" and part.label_verified
    assert all(f.status == "verified" for f in part.fields)
    for f in part.fields:
        a, b = f.span
        assert text[a:b] == f.evidence
    assert validated.readiness == "ready_for_manufacturing_evaluation"
    assert validated.clarifications == ()
    (checked,) = validated.parts
    assert checked.part_type == "l_bracket" and checked.label == label
    spec = checked.specification
    for name in (
        "part_id",
        "part_type",
        "material_id",
        "finish_id",
        "length_mm",
        "width_mm",
        "height_mm",
        "thickness_mm",
        "tolerance_mm",
        "quantity",
        "hole_count",
    ):
        assert getattr(spec, name) == getattr(line, name)
    assert part.get("notes").evidence == "bend radius 4 mm"
    execute = AsyncMock(return_value="ok")
    gate = ww.SourceGate(execute, [line])
    await gate("check_manufacturing_feasibility", PartCheckInput(part=spec))
    execute.assert_awaited_once()
    assert not gate.mismatch


@pytest.mark.asyncio
@pytest.mark.parametrize("label", [None, PART_ID])
async def test_one_stored_bracket_two_candidates_reproduce_two_clarification_groups(label):
    text = source([stored_line()])
    # A representative faulty response matching the reported codes, not a recovered
    # incident response: two entries share the numeric/finish evidence and omit material.
    duplicate = candidate(label)
    duplicate["material"] = None
    extracted, validated, catalog = await extract_and_validate(text, [duplicate, dict(duplicate)])
    assert len(extracted.parts) == len(validated.parts) == 2
    assert extracted.part_boundaries == "unverified"
    assert all(not p.label_verified and p.scope is None for p in extracted.parts)
    assert validated.readiness == "needs_clarification"
    assert catalog.calls == []
    expected = {
        ("part_id", "missing"),
        ("part_type", "missing"),
        ("material_id", "missing"),
        *(
            (field, "ownership_unverified")
            for field in (
                "quantity",
                "length",
                "width",
                "height",
                "thickness",
                "tolerance",
                "finish",
                "hole_count",
            )
        ),
    }
    for p in validated.parts:
        assert p.specification is None
        actual = {(c.field, c.kind) for c in validated.clarifications if c.part_index == p.index}
        assert actual == expected
    assert {c.part_index for c in validated.clarifications} == {1, 2}


@pytest.mark.asyncio
async def test_duplicate_candidates_with_different_evidence_are_not_merged():
    text = source([stored_line()], "Width 65 mm.")
    other = candidate()
    other["width"] = ev("65 mm", "Width 65 mm")
    extracted, validated, _ = await extract_and_validate(text, [candidate(), other])
    assert len(extracted.parts) == len(validated.parts) == 2
    assert [p.get("width").value for p in extracted.parts] == ["60 mm", "65 mm"]
    for p in extracted.parts:
        evidence = p.get("width")
        a, b = evidence.span
        assert text[a:b] == evidence.evidence
        assert evidence.status != "verified"
    assert validated.readiness == "needs_clarification"
    assert all(p.specification is None for p in validated.parts)


@pytest.mark.asyncio
async def test_two_distinct_stored_brackets_remain_two_parts():
    other_id = "P-TEST-BRACKET-002"
    lines = [stored_line(), stored_line(other_id)]
    extracted, validated, _ = await extract_and_validate(
        source(lines), [candidate(), candidate(other_id)], (PART_ID, other_id)
    )
    assert extracted.part_boundaries == "labelled"
    assert len(extracted.parts) == len(validated.parts) == 2
    assert validated.readiness == "ready_for_manufacturing_evaluation"
    assert [p.specification.part_id for p in validated.parts] == [PART_ID, other_id]
    for p in extracted.parts:
        assert all(p.scope[0] <= f.span[0] < f.span[1] <= p.scope[1] for f in p.fields)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement,field,kind",
    [
        ("Width 65 mm.", "width", "contradiction"),
        ("Also part type u bracket.", "part_type", "ambiguous"),
    ],
)
async def test_bracket_source_conflicts_outside_identifier_are_preserved(statement, field, kind):
    _, validated, _ = await extract_and_validate(source([stored_line()], statement), [candidate()])
    assert validated.readiness == "needs_clarification"
    assert validated.parts[0].specification is None
    assert any(c.field == field and c.kind == kind for c in validated.clarifications)


def test_stored_bracket_engineering_is_feasible_independently_of_intake():
    row = next(m for m in MATERIALS if m["material_id"] == "MAT-001")
    material = MaterialSpec(
        material_id=row["material_id"], category=row["category"], grade=row["grade"],
        density_kg_m3=Decimal(row["density_kg_m3"]),
        min_bend_radius_factor=Decimal(row["min_bend_radius_factor"]),
        availability_class=row["availability_class"],
        supported_thickness_mm=tuple(map(Decimal, json.loads(row["supported_thickness_mm"]))),
        compatible_finishes=tuple(json.loads(row["compatible_finishes"])),
        supported_operations=tuple(json.loads(row["supported_operations"])),
    )  # fmt: skip
    part = Part.from_mapping(vars(stored_line()))
    checked_material = evaluate_material(part, RULES, material, finish_exists=True)
    checked = evaluate_manufacturing(part, RULES, checked_material, BRACKET_ROUTE)
    assert checked_material.result == checked.result == "valid"
    assert checked.findings == ()
    assert checked.min_bend_radius_mm == Decimal("2")
    assert part.bend_radius_mm == Decimal("4")
    assert checked.operations == ("laser", "bend", "inspect", "pack")
