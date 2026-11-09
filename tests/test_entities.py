"""Part entity: exact types, contract shape, and parity with dev/validation ground truth."""

import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.entities import Part
from rfq_copilot.models import RFQLineItem
from rfq_copilot.repositories import get_rule_version

GROUND_TRUTH = (
    Path(__file__).resolve().parents[1] / "data/rfq_synthetic_data/evaluation/ground_truth"
)
# Frozen evaluation stays isolated until the final evaluation phase.
PARITY_SPLITS = ("development", "validation")

FULL = {
    "part_id": "PART-1",
    "part_type": "l_bracket",
    "material_id": "MAT-009",
    "finish_id": "none",
    "length_mm": 450,
    "width_mm": 490,
    "height_mm": 170,
    "thickness_mm": "1.0",
    "tolerance_mm": "0.5",
    "bend_radius_mm": "1.50",
    "weld_length_mm": 0,
    "quantity": 85,
    "hole_count": 11,
}


def ground_truth_parts():
    for split in PARITY_SPLITS:
        for path in sorted((GROUND_TRUTH / split).glob("*.json")):
            for part in json.loads(path.read_text()).get("expected_extraction") or []:
                yield path.stem, part


def test_every_dev_and_validation_extraction_converts_exactly():
    parts = list(ground_truth_parts())
    assert len(parts) == 180
    for rfq_id, source in parts:
        part = Part.from_mapping(source)
        for key, value in source.items():
            converted = getattr(part, key)
            if isinstance(converted, Decimal):
                assert converted == Decimal(str(value)), (rfq_id, key)
            else:
                assert converted == value and type(converted) is type(value), (rfq_id, key)
        assert all(getattr(part, k) is None for k in set(FULL) - set(source))


def test_full_mapping_types():
    part = Part.from_mapping(FULL)
    assert part.length_mm == Decimal("450") and isinstance(part.length_mm, Decimal)
    assert part.bend_radius_mm == Decimal("1.50") and part.quantity == 85
    assert part.weld_length_mm == Decimal(0)


def test_incomplete_request_keeps_missing_fields_as_none():
    part = Part.from_mapping({"part_id": "PART-2", "material_id": None, "quantity": 3})
    assert part.material_id is None and part.tolerance_mm is None and part.quantity == 3


def test_out_of_range_values_pass_through_for_the_validator():
    # Ranges are the manufacturing validator's decision, not the entity's.
    part = Part.from_mapping({**FULL, "thickness_mm": "8.0", "quantity": 0, "part_type": "casting"})
    assert part.thickness_mm == Decimal("8.0") and part.quantity == 0
    assert part.part_type == "casting"


def test_three_decimal_places_is_the_boundary():
    assert Part.from_mapping({**FULL, "tolerance_mm": "0.125"}).tolerance_mm == Decimal("0.125")
    with pytest.raises(ValueError):
        Part.from_mapping({**FULL, "tolerance_mm": "0.1255"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("length_mm", 450.0),
        ("length_mm", True),
        ("length_mm", "NaN"),
        ("length_mm", "Infinity"),
        ("length_mm", "abc"),
        ("length_mm", 10**9),
        ("quantity", 1.0),
        ("quantity", "85"),
        ("quantity", True),
        ("quantity", 2**31),
        ("hole_count", Decimal(3)),
        ("part_id", ""),
        ("part_id", "   "),
        ("part_id", "X" * 65),
        ("part_id", 7),
        ("material_id", ""),
        ("material_id", "MAT\x00"),
    ],
)
def test_rejects_inexact_or_malformed_values(field, value):
    with pytest.raises(ValueError):
        Part.from_mapping({**FULL, field: value})


def test_rejects_missing_part_id_unknown_fields_and_non_mappings():
    with pytest.raises(ValueError):
        Part.from_mapping({k: v for k, v in FULL.items() if k != "part_id"})
    with pytest.raises(ValueError, match="unknown fields"):
        Part.from_mapping({**FULL, "unit_price": "1.00"})
    with pytest.raises(ValueError):
        Part.from_mapping([("part_id", "PART-1")])


def test_part_is_immutable():
    part = Part.from_mapping(FULL)
    with pytest.raises(FrozenInstanceError):
        part.quantity = 1


def test_from_line_copies_stored_columns():
    line = RFQLineItem(
        rfq_id="RFQ-X",
        line_number=1,
        part_id="PART-9",
        part_type="flat_panel",
        thickness_mm=Decimal("2.000"),
        quantity=4,
    )
    part = Part.from_line(line)
    assert part == Part(
        part_id="PART-9", part_type="flat_panel", thickness_mm=Decimal("2.000"), quantity=4
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args,kwargs",
    [
        (("", "mfg-v1.0"), {}),
        (("manufacturing", ""), {}),
        (("manufacturing", "x" * 129), {}),
        (("manufacturing", "mfg-v1.0"), {"expected_sha256": "ABC"}),
        (("manufacturing", "mfg-v1.0"), {"expected_sha256": "A" * 64}),
    ],
)
async def test_rule_version_arguments_fail_before_sql(args, kwargs):
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError):
        await get_rule_version(session, *args, **kwargs)
    session.execute.assert_not_awaited()
