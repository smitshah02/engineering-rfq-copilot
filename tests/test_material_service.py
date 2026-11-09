"""Material catalog decisions without a database: findings, boundaries and rule handling."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.entities import Part
from rfq_copilot.repositories import RuleVersionDetails, find_materials
from rfq_copilot.services.materials import (
    MaterialSpec,
    check_part_material,
    evaluate_material,
)

RULE_FILE = (
    Path(__file__).resolve().parents[1] / "data/rfq_synthetic_data/config/manufacturing_rules.json"
)
RULES = RuleVersionDetails(
    rule_version="mfg-v1.0",
    rule_kind="manufacturing",
    sha256=hashlib.sha256(RULE_FILE.read_bytes()).hexdigest(),
    fingerprint="f" * 64,
    document=json.loads(RULE_FILE.read_text(), parse_float=Decimal),
)
STEEL_THICKNESS = tuple(Decimal(t) for t in ("1.0", "1.5", "2.0", "3.0", "4.0", "6.0"))
STAINLESS = MaterialSpec(
    material_id="MAT-009",
    category="stainless_steel",
    grade="430",
    density_kg_m3=Decimal("7750"),
    min_bend_radius_factor=Decimal("1.0"),
    availability_class="stocked",
    supported_thickness_mm=STEEL_THICKNESS,
    compatible_finishes=("brushed", "none", "passivated"),
    supported_operations=("bend", "finish", "inspect", "laser", "pack", "weld"),
)
COPPER = replace(
    STAINLESS,
    material_id="MAT-012",
    category="copper",
    grade="C110",
    supported_thickness_mm=tuple(Decimal(t) for t in ("0.8", "1.0", "1.5", "2.0", "3.0", "4.0")),
    compatible_finishes=("brushed", "none"),
)
PART = Part(part_id="P1", material_id="MAT-009", thickness_mm=Decimal("1.0"), finish_id="none")


def run(part=PART, spec=STAINLESS, finish_exists=True, rules=RULES):
    return evaluate_material(part, rules, spec, finish_exists=finish_exists)


def test_valid_material_carries_spec_and_rule_provenance():
    check = run()
    assert (check.result, check.findings, check.material) == ("valid", (), STAINLESS)
    assert (check.rule_version, check.rule_fingerprint) == ("mfg-v1.0", "f" * 64)


def test_unknown_material():
    check = run(replace(PART, material_id="MAT-999"), spec=None)
    assert (check.result, check.findings, check.material) == (
        "unsupported",
        ("UNSUPPORTED_MATERIAL",),
        None,
    )


def test_unknown_material_and_unknown_finish():
    check = run(replace(PART, finish_id="chrome"), spec=None, finish_exists=False)
    assert check.findings == ("UNSUPPORTED_FINISH", "UNSUPPORTED_MATERIAL")


@pytest.mark.parametrize("thickness", ["1.0", "1", "1.00", "6.0", "6.000"])
def test_supported_thickness_compares_as_decimal(thickness):
    assert run(replace(PART, thickness_mm=Decimal(thickness))).result == "valid"


@pytest.mark.parametrize("thickness", ["0.8", "0.999", "1.001", "2.5", "5.999", "6.001", "8.0"])
def test_unsupported_thickness(thickness):
    check = run(replace(PART, thickness_mm=Decimal(thickness)))
    assert (check.result, check.findings) == ("unsupported", ("MATERIAL_THICKNESS_UNSUPPORTED",))


def test_thickness_support_is_per_material():
    thin = replace(PART, thickness_mm=Decimal("0.8"))
    assert run(replace(thin, material_id="MAT-012"), spec=COPPER).result == "valid"
    assert run(thin).findings == ("MATERIAL_THICKNESS_UNSUPPORTED",)
    assert run(
        replace(PART, material_id="MAT-012", thickness_mm=Decimal("6.0")), spec=COPPER
    ).findings == ("MATERIAL_THICKNESS_UNSUPPORTED",)


def test_incompatible_finish_that_exists_in_catalog():
    check = run(replace(PART, finish_id="anodized"))
    assert check.findings == ("MATERIAL_FINISH_INCOMPATIBLE",)


def test_finish_unknown_to_the_catalog():
    check = run(replace(PART, finish_id="chrome"), finish_exists=False)
    assert check.findings == ("MATERIAL_FINISH_INCOMPATIBLE", "UNSUPPORTED_FINISH")


def test_all_material_findings_together():
    part = replace(PART, thickness_mm=Decimal("2.5"), finish_id="chrome")
    assert run(part, finish_exists=False).findings == (
        "MATERIAL_FINISH_INCOMPATIBLE",
        "MATERIAL_THICKNESS_UNSUPPORTED",
        "UNSUPPORTED_FINISH",
    )


@pytest.mark.parametrize("field", ["material_id", "thickness_mm", "finish_id"])
def test_missing_specification_needs_clarification(field):
    check = run(replace(PART, **{field: None}))
    assert (check.result, check.findings) == ("needs_clarification", (f"MISSING_{field.upper()}",))


def test_missing_fields_stop_further_material_checks():
    part = Part(part_id="P2", finish_id="chrome")
    check = run(part, spec=None, finish_exists=False)
    assert check.findings == ("MISSING_MATERIAL_ID", "MISSING_THICKNESS_MM")


def test_mandatory_fields_come_from_the_rule_version():
    relaxed = replace(
        RULES,
        document={
            **RULES.document,
            "mandatory_fields": [f for f in RULES.document["mandatory_fields"] if f != "finish_id"],
        },
    )
    assert run(replace(PART, finish_id=None), rules=relaxed).result == "valid"
    assert run(replace(PART, finish_id=None)).result == "needs_clarification"


def test_rejects_non_manufacturing_rules():
    with pytest.raises(ValueError, match="manufacturing rule"):
        run(rules=replace(RULES, rule_kind="pricing", rule_version="price-v1.0"))


def test_result_is_immutable():
    with pytest.raises(FrozenInstanceError):
        run().result = "valid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "part,rules",
    [
        ({"part_id": "P1"}, RULES),
        (PART, replace(RULES, rule_kind="pricing")),
        (PART, RULES.document),
    ],
)
async def test_check_arguments_fail_before_sql(part, rules):
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError):
        await check_part_material(session, part, rules)
    session.execute.assert_not_awaited()
    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"grade": ""},
        {"grade": "X" * 65},
        {"category": "   "},
        {"finish_id": "none\x00"},
        {"thickness_mm": 1.0},
        {"thickness_mm": Decimal("0")},
        {"thickness_mm": Decimal("1.0001")},
    ],
)
async def test_find_materials_arguments_fail_before_sql(kwargs):
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError):
        await find_materials(session, **kwargs)
    session.scalars.assert_not_awaited()
