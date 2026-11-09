"""Lead time against the guarded, seeded, rolled-back test database.

Parity sources: dev/validation ground truth (expected_schedule dates, procurement days,
delivery escalations, draft_creation_allowed) and the reference capacity_engine
(check_inventory + estimate_schedule), loaded as a test oracle only.
"""

from datetime import date

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
from test_day09_inventory import INVENTORY, PANEL, REF_MATERIALS, pinned, truth_files, variant
from test_day10_capacity import CAPACITY, MONDAY

from rfq_copilot.entities import Part
from rfq_copilot.models import DataSnapshot, MaterialInventory, ProductionCapacity
from rfq_copilot.services.lead_time import SnapshotMismatchError, estimate_lead_time

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


async def snapshots(session):
    rows = (
        await session.execute(select(DataSnapshot.snapshot_kind, DataSnapshot.snapshot_id))
    ).all()
    pins = dict(rows)
    assert set(pins) == {"inventory", "capacity"} and len(rows) == 2
    return {"inventory_snapshot": pins["inventory"], "capacity_snapshot": pins["capacity"]}


async def estimate(session, sources, rule_set, **kwargs):
    parts = [Part.from_mapping(s) for s in sources]
    return await estimate_lead_time(session, parts, **rule_set, horizon_start=MONDAY, **kwargs)


async def test_ground_truth_lead_times_and_delivery_findings(populated):
    rule_set = await pinned(populated)
    pins = await snapshots(populated)
    compared, late, blocked = 0, 0, 0
    for rfq_id, truth in truth_files():
        sources = truth.get("expected_extraction") or []
        if not sources:
            continue
        requested = truth["expected_delivery_date"]
        requested = date.fromisoformat(requested) if requested else None
        expected = truth.get("expected_schedule")
        result = await estimate(
            populated, sources, rule_set, requested_delivery_date=requested, **pins
        )
        if expected is None:
            if truth["primary_category"] in ("incomplete", "unsupported"):
                assert result.result in ("needs_clarification", "not_manufacturable")
                assert result.estimated_ready_date is None
                blocked += 1
            continue
        ready = expected["estimated_ready_date"]
        assert (
            result.estimated_ready_date,
            result.lead_business_days,
            result.procurement_business_days,
        ) == (
            date.fromisoformat(ready) if ready else None,
            expected["lead_business_days"],
            expected["procurement_business_days"],
        ), rfq_id
        assert (result.result == "estimated") is expected["feasible"]
        # Lead-time findings are exactly what blocks draft creation in the labels.
        assert (result.findings == ()) is truth["draft_creation_allowed"], rfq_id
        if truth["escalation_reason"] in ("DELIVERY_DATE_INFEASIBLE", "CAPACITY_OUT_OF_HORIZON"):
            assert result.findings == (truth["escalation_reason"],), rfq_id
        late += "DELIVERY_DATE_INFEASIBLE" in result.findings
        compared += 1
    assert (compared, late, blocked) == (100, 2, 40)


async def test_reference_parity_across_materials_quantities_and_mixes(populated):
    reference = reference_module("capacity_engine")
    rule_set = await pinned(populated)
    bases = list(base_parts().values())
    cases = [[variant(b, m, q)] for b in bases for m in REF_MATERIALS for q in (40, 400)]
    cases.append([variant(b, "MAT-004", 300, f"MIX-{i}") for i, b in enumerate(bases)])
    cases.append([variant(b, f"MAT-0{10 + i}", 900, f"BIG-{i}") for i, b in enumerate(bases)])
    outcomes, delays = set(), set()
    for sources in cases:
        ours = await estimate(populated, sources, rule_set)
        stock = reference.check_inventory(sources, REF_MATERIALS, INVENTORY, MONDAY.isoformat())
        ref = reference.estimate_schedule(sources, CAPACITY, MONDAY.isoformat(), stock)
        ready = ref["estimated_ready_date"]
        assert (
            ours.estimated_ready_date,
            ours.lead_business_days,
            ours.procurement_business_days,
            ours.result == "estimated",
        ) == (
            date.fromisoformat(ready) if ready else None,
            ref["lead_business_days"],
            ref["procurement_business_days"],
            ref["feasible"],
        ), sources
        assert [d.procurement_business_days for d in ours.material_delays] == [
            r["procurement_business_days"] for r in stock
        ]
        outcomes.add(ours.result)
        delays.add(ours.procurement_business_days)
    assert len(cases) == 6 * 15 * 2 + 2
    assert "estimated" in outcomes and {0, 7, 11, 15, 19} <= delays


async def test_zero_lead_short_stock_uses_the_default_delay(populated):
    # TD-002 regression: the reference code's rule, not the documentation's wording.
    rule_set = await pinned(populated)
    big = {**PANEL, "quantity": 1000}  # 3,024 kg of MAT-002 2.0 mm: always short
    await populated.execute(
        update(MaterialInventory)
        .where(MaterialInventory.material_id == "MAT-002", MaterialInventory.thickness_mm == 2)
        .values(supplier_lead_business_days=0)
    )
    populated.expunge_all()
    result = await estimate(populated, [big], rule_set)
    (delay,) = result.material_delays
    assert (delay.status, delay.procurement_business_days) == ("insufficient", 15)
    assert result.earliest_start_date == date(2026, 11, 30)


async def test_wrong_snapshot_pin_is_rejected(populated):
    rule_set = await pinned(populated)
    pins = await snapshots(populated)
    for wrong in ({"inventory_snapshot": "SNAP-OTHER"}, {"capacity_snapshot": "SNAP-OTHER"}):
        with pytest.raises(SnapshotMismatchError):
            await estimate(populated, [PANEL], rule_set, **{**pins, **wrong})


async def test_reads_only_bounded_and_reproducible(populated, seed_database):
    rule_set = await pinned(populated)
    tables = (MaterialInventory.__table__, ProductionCapacity.__table__)

    async def rows():
        return [(await populated.execute(select(t).order_by(*t.primary_key))).all() for t in tables]

    before = await rows()
    populated.expunge_all()
    with statements(seed_database) as captured:
        first = await estimate(populated, [PANEL], rule_set)
    # Manufacturing (7) + stock (2) + capacity routing (1) + 3 workcentres x 2.
    assert len(captured) == 16
    assert all(s.lstrip().upper().startswith("SELECT") for s in captured)
    assert await estimate(populated, [PANEL], rule_set) == first
    assert (first.result, first.estimated_ready_date, first.lead_business_days) == (
        "estimated",
        date(2026, 11, 13),
        4,
    )
    assert await rows() == before
