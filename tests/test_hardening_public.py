"""Phase 2.5 hardening checks that need neither PostgreSQL nor the private dataset.

Part of the public smoke suite (see README "Testing"): canonical JSON for fingerprints,
the planning-date policy, the Intake boundary for requested operations and the
request-scoped catalog memo. Full verification still needs the complete suite.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from rfq_copilot.canonical import canonical_json, fingerprint
from rfq_copilot.entities import Part
from rfq_copilot.repositories import SnapshotDetails, SnapshotMismatchError
from rfq_copilot.services.lead_time import SnapshotSelection, validate_planning_dates
from rfq_copilot.services.materials import memo

D = Decimal
MONDAY = date(2026, 11, 9)
INV = SnapshotDetails("INV-A", "inventory", "1.1.0", date(2026, 11, 2), None)
CAP = SnapshotDetails("CAP-A", "capacity", "1.1.0", date(2026, 11, 2), date(2027, 1, 11))


# --- Finding 4: canonical serialization behind the evaluation fingerprint --------------


def test_canonical_json_is_order_and_spelling_stable():
    assert canonical_json({"b": D("1.50"), "a": [date(2026, 11, 9), None, True]}) == (
        '{"a":["2026-11-09",null,true],"b":1.50}'
    )
    assert fingerprint({"x": 1, "y": 2}) == fingerprint({"y": 2, "x": 1})
    assert canonical_json(D("1.5")) != canonical_json("1.5")  # number vs text


@pytest.mark.parametrize("value", [1.5, D("NaN"), D("Infinity"), object(), {1: "a"}])
def test_canonical_json_refuses_inexact_or_unknown_values(value):
    with pytest.raises(ValueError):
        canonical_json(value)


# --- Finding 8: planning-date policy --------------------------------------------------


@pytest.mark.parametrize(
    "inventory,capacity,horizon",
    [
        (INV, CAP, MONDAY),  # the dataset: observed 2026-11-02, planned from 2026-11-09
        (INV, CAP, date(2026, 11, 2)),  # boundary: planning on the observation Monday
        (INV, CAP, date(2027, 1, 11)),  # boundary: the last stored capacity week
    ],
)
def test_consistent_snapshot_dates_are_accepted(inventory, capacity, horizon):
    validate_planning_dates(SnapshotSelection(inventory, capacity), horizon)


@pytest.mark.parametrize(
    "inventory,capacity,horizon,message",
    [
        (INV, CAP, date(2026, 10, 26), "observed after the horizon"),
        (replace(INV, as_of_date=date(2026, 11, 16)), CAP, MONDAY, "observed after"),
        (INV, replace(CAP, as_of_date=date(2026, 11, 10)), MONDAY, "observed after"),
        (INV, CAP, date(2027, 1, 18), "no capacity on or after"),  # stale snapshot
        (INV, replace(CAP, last_week_start=None), MONDAY, "no capacity"),
        (replace(INV, dataset_version="1.2.0"), CAP, MONDAY, "different dataset versions"),
        (CAP, INV, MONDAY, "one inventory and one capacity"),
    ],
)
def test_inconsistent_snapshot_dates_are_rejected(inventory, capacity, horizon, message):
    with pytest.raises(SnapshotMismatchError, match=message):
        validate_planning_dates(SnapshotSelection(inventory, capacity), horizon)


@pytest.mark.parametrize("horizon", [date(2026, 11, 10), "2026-11-09"])
def test_invalid_horizons_are_rejected(horizon):
    with pytest.raises(ValueError, match="Monday"):
        validate_planning_dates(SnapshotSelection(INV, CAP), horizon)


# --- Finding 6: requested operations are refused, never dropped ----------------------


@pytest.mark.parametrize("operations", [["machine"], ["anodize_steel", "laser"], []])
def test_requested_operations_are_refused_at_the_part_boundary(operations):
    source = {"part_id": "P1", "part_type": "flat_panel", "requested_operations": operations}
    with pytest.raises(ValueError, match="TD-001"):
        Part.from_mapping(source)


def test_other_unknown_part_fields_still_fail():
    with pytest.raises(ValueError, match="unknown fields"):
        Part.from_mapping({"part_id": "P1", "surface_treatment": "x"})


# --- Finding 9: request-scoped catalog memo -------------------------------------------


@pytest.mark.asyncio
async def test_memo_loads_once_per_calculation_and_never_without_a_scope():
    calls = []

    async def load():
        calls.append(1)
        return "spec"

    reads = {}
    assert [await memo(reads, ("material_spec", "M"), load) for _ in range(3)] == ["spec"] * 3
    assert len(calls) == 1
    await memo(None, ("material_spec", "M"), load)
    await memo(None, ("material_spec", "M"), load)
    assert len(calls) == 3  # no scope, no caching: nothing outlives one calculation
