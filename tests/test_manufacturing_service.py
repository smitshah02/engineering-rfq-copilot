"""Manufacturing validator decisions without a database: limits, MFG-001, bends, routes."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_material_service import RULES, STAINLESS

from rfq_copilot.entities import Part
from rfq_copilot.models import OperationRouting
from rfq_copilot.services.manufacturing import (
    check_manufacturing_capability,
    evaluate_manufacturing,
    route_operations,
)
from rfq_copilot.services.materials import evaluate_material

BRACKET = Part(
    part_id="P1",
    part_type="l_bracket",
    material_id="MAT-009",
    finish_id="none",
    length_mm=Decimal("450"),
    width_mm=Decimal("490"),
    height_mm=Decimal("170"),
    thickness_mm=Decimal("1.0"),
    tolerance_mm=Decimal("0.5"),
    bend_radius_mm=Decimal("1.50"),
    quantity=85,
    hole_count=11,
)


def steps(*rows):
    return tuple(
        OperationRouting(operation_id=op, required_when=when, sequence=i + 1)
        for i, (op, when) in enumerate(rows)
    )


BRACKET_ROUTE = steps(
    ("laser", "always"),
    ("bend", "always"),
    ("finish", "finish_not_none"),
    ("inspect", "always"),
    ("pack", "always"),
)
PANEL_ROUTE = steps(
    ("laser", "always"), ("finish", "finish_not_none"), ("inspect", "always"), ("pack", "always")
)
ASSEMBLY_ROUTE = steps(
    ("laser", "always"),
    ("weld", "always"),
    ("finish", "finish_not_none"),
    ("inspect", "always"),
    ("pack", "always"),
)


def rule(**changes):
    document = dict(RULES.document)
    limits = dict(document["limits"])
    for key, value in changes.items():
        if key in limits or key.startswith("limit_"):
            limits[key.removeprefix("limit_")] = value
        else:
            document[key] = value
    return replace(RULES, document={**document, "limits": limits})


def run(part=BRACKET, *, spec=STAINLESS, routing=BRACKET_ROUTE, rules=RULES, finish_exists=True):
    material = evaluate_material(part, rules, spec, finish_exists=finish_exists)
    return evaluate_manufacturing(part, rules, material, routing)


def findings(**changes):
    return run(replace(BRACKET, **changes)).findings


def test_valid_bracket_with_route_and_provenance():
    check = run()
    assert (check.result, check.findings) == ("valid", ())
    assert check.operations == ("laser", "bend", "inspect", "pack")
    assert check.material.result == "valid" and check.rule_version == "mfg-v1.0"


def test_finish_adds_the_finish_operation():
    check = run(replace(BRACKET, finish_id="passivated"))
    assert check.operations == ("laser", "bend", "finish", "inspect", "pack")


@pytest.mark.parametrize(
    "field,ok,bad",
    [
        ("length_mm", ["20", "1200"], ["19.999", "1200.001"]),
        ("width_mm", ["20", "1000"], ["19.999", "1000.001"]),
        ("height_mm", ["10", "450"], ["9.999", "450.001"]),
    ],
)
def test_dimension_ranges_are_inclusive(field, ok, bad):
    for value in ok:
        assert findings(**{field: Decimal(value)}) == ()
    for value in bad:
        assert findings(**{field: Decimal(value)}) == (f"OUT_OF_RANGE_{field.upper()}",)


@pytest.mark.parametrize(
    "quantity,expected",
    [(1, ()), (1000, ()), (0, ("OUT_OF_RANGE_QUANTITY",)), (1001, ("OUT_OF_RANGE_QUANTITY",))],
)
def test_quantity_range(quantity, expected):
    assert findings(quantity=quantity) == expected


def test_thickness_envelope_and_catalog_are_separate_findings():
    assert findings(thickness_mm=Decimal("6.001"), bend_radius_mm=Decimal("7")) == (
        "MATERIAL_THICKNESS_UNSUPPORTED",
        "OUT_OF_RANGE_THICKNESS_MM",
    )


@pytest.mark.parametrize(
    "tolerance,expected", [("0.2", ()), ("0.200", ()), ("0.199", ("TOLERANCE_UNSUPPORTED",))]
)
def test_minimum_tolerance(tolerance, expected):
    assert findings(tolerance_mm=Decimal(tolerance)) == expected


# MFG-001 regression: the configured hole_count_max=300 is enforced.
@pytest.mark.parametrize(
    "holes,expected",
    [
        (0, ()),
        (300, ()),
        (None, ()),  # unstated: rule default 0
        (301, ("OUT_OF_RANGE_HOLE_COUNT",)),
        (10_000, ("OUT_OF_RANGE_HOLE_COUNT",)),
        (-1, ("OUT_OF_RANGE_HOLE_COUNT",)),
    ],
)
def test_mfg_001_hole_count_maximum(holes, expected):
    check = run(replace(BRACKET, hole_count=holes))
    assert check.findings == expected
    assert check.result == ("unsupported" if expected else "valid")


@pytest.mark.parametrize("holes", [300.0, 300.5, "300", True, Decimal(300)])
def test_mfg_001_malformed_hole_counts_never_reach_the_validator(holes):
    source = {"part_id": "P1", "hole_count": holes}
    with pytest.raises(ValueError):
        Part.from_mapping(source)


def test_hole_maximum_comes_from_the_rule():
    assert run(replace(BRACKET, hole_count=11), rules=rule(hole_count_max=10)).findings == (
        "OUT_OF_RANGE_HOLE_COUNT",
    )


def test_bend_count_maximum_comes_from_the_rule():
    assert run(rules=rule(bends_max=0)).findings == ("OUT_OF_RANGE_BEND_COUNT",)
    assert (
        run(
            replace(BRACKET, part_type="flat_panel"), routing=PANEL_ROUTE, rules=rule(bends_max=0)
        ).findings
        == ()
    )


@pytest.mark.parametrize(
    "thickness,radius,expected",
    [
        ("1.0", "1.0", ()),
        ("1.0", "0.999", ("BEND_RADIUS_TOO_SMALL",)),
        ("4.0", "4.0", ()),
        ("4.0", "3.999", ("BEND_RADIUS_TOO_SMALL",)),
    ],
)
def test_bend_radius_at_least_thickness_times_factor(thickness, radius, expected):
    assert findings(thickness_mm=Decimal(thickness), bend_radius_mm=Decimal(radius)) == expected


def test_bend_radius_ignored_for_parts_without_bends_and_when_unstated():
    panel = replace(BRACKET, part_type="flat_panel", bend_radius_mm=Decimal("0.1"))
    assert run(panel, routing=PANEL_ROUTE).findings == ()
    assert findings(bend_radius_mm=None) == ()


def test_material_bend_factor_can_only_tighten_the_rule():
    strict = replace(STAINLESS, min_bend_radius_factor=Decimal("2.0"))
    assert run(spec=strict).findings == ("BEND_RADIUS_TOO_SMALL",)  # 1.5 < 1.0 x 2.0
    loose = replace(STAINLESS, min_bend_radius_factor=Decimal("0.5"))
    assert run(replace(BRACKET, bend_radius_mm=Decimal("0.9")), spec=loose).findings == (
        "BEND_RADIUS_TOO_SMALL",
    )


def test_missing_non_material_field_wins_over_material_findings():
    part = replace(BRACKET, tolerance_mm=None, finish_id="chrome")
    check = run(part, finish_exists=False)
    assert (check.result, check.findings, check.operations) == (
        "needs_clarification",
        ("MISSING_TOLERANCE_MM",),
        (),
    )
    assert check.material.result == "unsupported"  # material detail is kept for the caller


def test_missing_material_and_part_fields_are_reported_together():
    check = run(replace(BRACKET, material_id=None, quantity=None), spec=None)
    assert check.findings == ("MISSING_MATERIAL_ID", "MISSING_QUANTITY")


def test_conditional_required_fields():
    assert findings(height_mm=None) == ("MISSING_HEIGHT_MM",)
    panel = replace(BRACKET, part_type="flat_panel", height_mm=None)
    assert run(panel, routing=PANEL_ROUTE).result == "valid"
    assembly = replace(BRACKET, part_type="fabricated_assembly", bend_radius_mm=None)
    assert run(assembly, routing=ASSEMBLY_ROUTE).findings == ("MISSING_WELD_LENGTH_MM",)
    welded = replace(assembly, weld_length_mm=Decimal("234"))
    assert run(welded, routing=ASSEMBLY_ROUTE).operations == ("laser", "weld", "inspect", "pack")


def test_unsupported_part_type_has_no_route():
    check = run(replace(BRACKET, part_type="casting"), routing=())
    assert (check.findings, check.operations) == (("UNSUPPORTED_PART_TYPE",), ())


def test_route_operation_not_supported_by_material_or_blocked_by_rule():
    no_bend = replace(STAINLESS, supported_operations=("inspect", "laser", "pack"))
    assert run(spec=no_bend).findings == ("UNSUPPORTED_OPERATION",)
    assert run(rules=rule(unsupported_operations=["bend"])).findings == ("UNSUPPORTED_OPERATION",)


def test_unknown_limit_fails_closed():
    # The MFG-001 failure class: a configured limit must never be silently skipped.
    with pytest.raises(ValueError, match="does not enforce.*'welds_max'"):
        run(rules=rule(limit_welds_max=3))
    with pytest.raises(ValueError, match="does not enforce.*'area_m2'"):
        run(rules=rule(limit_area_m2=[0, 2]))


def test_material_check_under_another_rule_is_rejected():
    material = evaluate_material(BRACKET, RULES, STAINLESS, finish_exists=True)
    with pytest.raises(ValueError, match="different rule"):
        evaluate_manufacturing(
            BRACKET, replace(RULES, fingerprint="0" * 64), material, BRACKET_ROUTE
        )


def test_routing_integrity_errors():
    with pytest.raises(ValueError, match="routing condition"):
        route_operations(BRACKET, steps(("laser", "sometimes")))
    with pytest.raises(ValueError, match="No routing"):
        run(routing=())


def test_result_is_immutable():
    with pytest.raises(FrozenInstanceError):
        run().operations = ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "part,rules",
    [({"part_id": "P1"}, RULES), (BRACKET, replace(RULES, rule_kind="pricing"))],
)
async def test_check_arguments_fail_before_sql(part, rules):
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError):
        await check_manufacturing_capability(session, part, rules)
    session.scalar.assert_not_awaited()
    session.scalars.assert_not_awaited()
