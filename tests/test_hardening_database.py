"""Phase 2.5 hardening against the guarded, seeded, rolled-back test database.

Snapshot selection with two stored snapshots, planning-date checks, rule-document
tampering under an unchanged stored sha256, the release invariant on the seeded policy,
the consistent-read session and catalog-read batching. Every write here happens inside
the populated fixture's transaction and is rolled back.
"""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError
from test_database_queries import populated as populated
from test_database_queries import statements
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day09_inventory import PANEL, pinned, truth_files
from test_day10_capacity import MONDAY
from test_day11_lead_time import snapshots
from test_day15_evaluation import rules as evaluation_rules

from rfq_copilot.database import DatabaseError
from rfq_copilot.entities import Part
from rfq_copilot.integrity import audit_integrity
from rfq_copilot.models import (
    BusinessRuleVersion,
    Customer,
    DataSnapshot,
    MaterialInventory,
    ProductionCapacity,
)
from rfq_copilot.repositories import (
    RuleIntegrityError,
    SnapshotMismatchError,
    get_material_availability,
    get_rule_version,
    get_snapshot,
)
from rfq_copilot.seeding import SeedError
from rfq_copilot.seeding.loader import execute_seed
from rfq_copilot.seeding.rules import register_rule_version
from rfq_copilot.services.evaluation import evaluate_rfq
from rfq_copilot.services.inventory import check_inventory
from rfq_copilot.services.lead_time import estimate_lead_time
from rfq_copilot.services.quotation import quote_parts

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

D = Decimal
INV_B, CAP_B = "inventory-test-snapshot-b", "capacity-test-snapshot-b"


async def register(session, snapshot_id, kind, as_of=date(2026, 11, 2), version="1.1.0"):
    await session.execute(
        insert(DataSnapshot).values(
            snapshot_id=snapshot_id,
            snapshot_kind=kind,
            dataset_version=version,
            as_of_date=as_of,
            sha256=("b" if kind == "inventory" else "c") * 64,
            artifact_ref=f"test://{snapshot_id}",
        )
    )


async def panel_stock_row(session):
    part = Part.from_mapping(PANEL)
    return (
        await session.scalars(
            select(MaterialInventory).where(
                MaterialInventory.material_id == part.material_id,
                MaterialInventory.thickness_mm == part.thickness_mm,
            )
        )
    ).one()


async def add_inventory_snapshot_b(session, quantity="1.000000"):
    """Snapshot B: the same material and thickness as PANEL, with a different quantity."""
    await register(session, INV_B, "inventory")
    original = await panel_stock_row(session)
    values = {
        c.name: getattr(original, c.key)
        for c in MaterialInventory.__table__.columns
        if c.name not in ("available_kg", "row_version")
    }
    values.update(
        inventory_id="INV-TEST-B", snapshot_id=INV_B, quantity_kg=D(quantity), reserved_kg=D(0)
    )
    await session.execute(insert(MaterialInventory).values(**values))
    session.expunge_all()
    return original


# --- Finding 1: snapshot-aware reads ------------------------------------------------


async def test_two_inventory_snapshots_are_never_mixed(populated):
    rule_set, pins = await pinned(populated), await snapshots(populated)
    original = await add_inventory_snapshot_b(populated)
    parts = [Part.from_mapping(PANEL)]

    # Unpinned with two snapshots: refuse instead of guessing or summing.
    with pytest.raises(SnapshotMismatchError, match="pin one explicitly"):
        await check_inventory(populated, parts, **rule_set)
    with pytest.raises(SnapshotMismatchError, match="pin one explicitly"):
        await estimate_lead_time(
            populated,
            parts,
            **rule_set,
            horizon_start=MONDAY,
            capacity_snapshot=pins["capacity_snapshot"],
        )

    a = await estimate_lead_time(populated, parts, **rule_set, horizon_start=MONDAY, **pins)
    (line_a,) = a.inventory.lines
    assert line_a.inventory_ids == (original.inventory_id,)
    assert line_a.available_kg == original.available_kg
    assert a.inventory.snapshot_id == pins["inventory_snapshot"]

    b = await estimate_lead_time(
        populated, parts, **rule_set, horizon_start=MONDAY, **{**pins, "inventory_snapshot": INV_B}
    )
    (line_b,) = b.inventory.lines
    assert (line_b.inventory_ids, line_b.available_kg, line_b.snapshot_ids) == (
        ("INV-TEST-B",),
        D("1.000000"),
        (INV_B,),
    )
    assert line_b.status != line_a.status  # 1 kg cannot cover 30.24 kg
    assert b.snapshots.inventory.snapshot_id == INV_B

    # The catalog view still lists both rows, so the separation above is the query's doing.
    both = await get_material_availability(
        populated, parts[0].material_id, thickness_mm=parts[0].thickness_mm
    )
    assert {r.snapshot_id for r in both.inventory} == {pins["inventory_snapshot"], INV_B}


async def add_capacity_snapshot_b(session, *, version="1.1.0"):
    """Snapshot B: every stored capacity week again (TD-005 allows the overlap), fully booked."""
    await register(session, CAP_B, "capacity", version=version)
    table = ProductionCapacity.__table__
    columns = [c for c in table.columns if c.computed is None]
    rows = (await session.execute(select(*columns))).mappings().all()
    copies = [
        {
            **row,
            "capacity_id": f"B-{row['capacity_id']}",
            "snapshot_id": CAP_B,
            "scheduled_hours": row["nominal_hours"] - row["downtime_hours"],  # 0 free hours
        }
        for row in rows
    ]
    await session.execute(insert(table), copies)
    session.expunge_all()
    return len(copies)


async def test_two_capacity_snapshots_are_never_mixed(populated):
    rule_set, pins = await pinned(populated), await snapshots(populated)
    parts = [Part.from_mapping(PANEL)]
    before = await estimate_lead_time(populated, parts, **rule_set, horizon_start=MONDAY, **pins)
    copied = await add_capacity_snapshot_b(populated)
    assert copied > 0  # same workcentre-weeks, a second snapshot: possible since 20261109_01

    a = await estimate_lead_time(populated, parts, **rule_set, horizon_start=MONDAY, **pins)
    assert (a.result, a.estimated_ready_date) == (before.result, before.estimated_ready_date)
    assert a.capacity.capacity_snapshot_ids == (pins["capacity_snapshot"],)
    assert a.capacity.weeks == before.capacity.weeks  # B's booked hours never leak into A

    b = await estimate_lead_time(
        populated, parts, **rule_set, horizon_start=MONDAY, **{**pins, "capacity_snapshot": CAP_B}
    )
    assert (b.result, b.capacity.snapshot_id) == ("infeasible", CAP_B)
    assert set(b.capacity.capacity_snapshot_ids) <= {CAP_B}

    with pytest.raises(SnapshotMismatchError, match="pin one explicitly"):
        await get_snapshot(populated, "capacity")


async def test_a_week_stays_unique_within_one_snapshot(populated):
    pins = await snapshots(populated)
    table = ProductionCapacity.__table__
    columns = [c for c in table.columns if c.computed is None]
    row = (await populated.execute(select(*columns).limit(1))).mappings().one()
    assert row["snapshot_id"] == pins["capacity_snapshot"]
    with pytest.raises(
        IntegrityError, match="uq_production_capacity_workcentre_id_week_start_snapshot_id"
    ):
        async with populated.begin_nested():
            await populated.execute(insert(table).values(**{**row, "capacity_id": "DUP-1"}))


async def test_single_snapshot_is_selected_without_a_pin(populated):
    rule_set, pins = await pinned(populated), await snapshots(populated)
    parts = [Part.from_mapping(PANEL)]
    unpinned = await estimate_lead_time(populated, parts, **rule_set, horizon_start=MONDAY)
    pinned_result = await estimate_lead_time(
        populated, parts, **rule_set, horizon_start=MONDAY, **pins
    )
    assert unpinned == pinned_result
    assert (unpinned.snapshots.inventory.snapshot_id, unpinned.snapshots.capacity.snapshot_id) == (
        pins["inventory_snapshot"],
        pins["capacity_snapshot"],
    )


@pytest.mark.parametrize(
    "change,message",
    [
        ({"inventory_snapshot": "inventory-missing"}, "No registered snapshot"),
        ({"capacity_snapshot": "capacity-missing"}, "No registered snapshot"),
        ("swap", "not capacity|not inventory"),
    ],
)
async def test_missing_or_wrong_kind_pins_fail(populated, change, message):
    rule_set, pins = await pinned(populated), await snapshots(populated)
    if change == "swap":
        change = {
            "inventory_snapshot": pins["capacity_snapshot"],
            "capacity_snapshot": pins["inventory_snapshot"],
        }
    with pytest.raises(SnapshotMismatchError, match=message):
        await estimate_lead_time(
            populated,
            [Part.from_mapping(PANEL)],
            **rule_set,
            horizon_start=MONDAY,
            **{**pins, **change},
        )


# --- Finding 8: planning dates against the selected snapshots --------------------------


@pytest.mark.parametrize(
    "horizon,message",
    [
        (date(2026, 10, 26), "observed after the horizon"),  # before the observation date
        (date(2027, 1, 18), "no capacity on or after"),  # past the last stored week
    ],
)
async def test_horizons_inconsistent_with_the_snapshots_are_rejected(populated, horizon, message):
    rule_set, pins = await pinned(populated), await snapshots(populated)
    with pytest.raises(SnapshotMismatchError, match=message):
        await estimate_lead_time(
            populated, [Part.from_mapping(PANEL)], **rule_set, horizon_start=horizon, **pins
        )


async def test_last_stored_week_is_a_valid_horizon(populated):
    rule_set, pins = await pinned(populated), await snapshots(populated)
    last = await populated.scalar(
        select(ProductionCapacity.week_start)
        .order_by(ProductionCapacity.week_start.desc())
        .limit(1)
    )
    estimate = await estimate_lead_time(
        populated, [Part.from_mapping(PANEL)], **rule_set, horizon_start=last, **pins
    )
    assert estimate.snapshots.capacity.last_week_start == last


async def test_stock_rows_contradicting_the_snapshot_date_are_rejected(populated):
    rule_set, pins = await pinned(populated), await snapshots(populated)
    original = await panel_stock_row(populated)
    await populated.execute(
        update(MaterialInventory)
        .where(MaterialInventory.inventory_id == original.inventory_id)
        .values(snapshot_date=date(2026, 10, 1))
    )
    populated.expunge_all()
    with pytest.raises(SnapshotMismatchError, match="contradict its as-of date"):
        await estimate_lead_time(
            populated, [Part.from_mapping(PANEL)], **rule_set, horizon_start=MONDAY, **pins
        )


async def test_snapshots_of_different_dataset_versions_are_rejected(populated):
    rule_set, pins = await pinned(populated), await snapshots(populated)
    await add_capacity_snapshot_b(populated, version="9.9.9")
    with pytest.raises(SnapshotMismatchError, match="different dataset versions"):
        await estimate_lead_time(
            populated,
            [Part.from_mapping(PANEL)],
            **rule_set,
            horizon_start=MONDAY,
            **{**pins, "capacity_snapshot": CAP_B},
        )


# --- Finding 3: rule-document tampering under an unchanged stored sha256 ---------------


async def tamper(session, rule_version, path, value):
    """Change one value in the stored JSONB server side; the stored sha256 is untouched."""
    sha = await session.scalar(
        select(BusinessRuleVersion.sha256).where(BusinessRuleVersion.rule_version == rule_version)
    )
    await session.execute(
        text(
            "UPDATE business.business_rule_versions "
            "SET document = jsonb_set(document, CAST(:path AS text[]), CAST(:value AS jsonb)) "
            "WHERE rule_version = :rule_version"
        ),
        {"path": list(path), "value": json.dumps(value), "rule_version": rule_version},
    )
    session.expunge_all()
    after = await session.scalar(
        select(BusinessRuleVersion.sha256).where(BusinessRuleVersion.rule_version == rule_version)
    )
    assert after == sha
    return sha


@pytest.mark.parametrize(
    "kind,rule_version,path,value",
    [
        ("pricing", "price-v1.0", ["gross_margin"], 0.05),
        ("pricing", "price-v1.0", ["workcentre_hourly_rates_usd", "laser"], 1),
        ("manufacturing", "mfg-v1.0", ["limits", "hole_count_max"], 100000),
        ("manufacturing", "mfg-v1.0", ["unsupported_operations"], []),
        ("approval", "approval-v1.1", ["human_release_approval_required"], False),
        ("approval", "approval-v1.1", ["agent_forbidden_actions"], ["override_prices"]),
        ("approval", "approval-v1.1", ["manager_review_threshold_usd"], "99999.00"),
    ],
)
async def test_tampered_rule_content_is_rejected_despite_unchanged_sha(
    populated, kind, rule_version, path, value
):
    sha = await tamper(populated, rule_version, path, value)
    with pytest.raises(RuleIntegrityError, match="differs from its approved version"):
        await get_rule_version(populated, kind, rule_version, expected_sha256=sha)


async def test_rewriting_an_identical_value_is_accepted(populated):
    # Control for the test above: the tamper helper alone does not trip the check.
    sha = await tamper(populated, "price-v1.0", ["version"], "price-v1.0")
    rule = await get_rule_version(populated, "pricing", "price-v1.0", expected_sha256=sha)
    assert rule.document["version"] == "price-v1.0"


async def test_unregistered_rule_version_is_rejected(populated):
    stored = await populated.scalar(
        select(BusinessRuleVersion).where(BusinessRuleVersion.rule_version == "price-v1.0")
    )
    await populated.execute(
        insert(BusinessRuleVersion).values(
            rule_version="price-v9.0",
            rule_kind="pricing",
            sha256="d" * 64,
            document={**stored.document, "version": "price-v9.0", "gross_margin": 0.01},
            source_path="test://unregistered",
        )
    )
    with pytest.raises(RuleIntegrityError, match="not a registered, approved version"):
        await get_rule_version(populated, "pricing", "price-v9.0")


async def test_changed_stored_sha_is_rejected_even_without_a_pin(populated):
    await populated.execute(
        update(BusinessRuleVersion)
        .where(BusinessRuleVersion.rule_version == "price-v1.0")
        .values(sha256="e" * 64)
    )
    with pytest.raises(RuleIntegrityError):
        await get_rule_version(populated, "pricing", "price-v1.0")


async def test_tampered_policy_cannot_reach_an_evaluation(populated):
    await tamper(populated, "approval-v1.1", ["human_release_approval_required"], False)
    with pytest.raises(RuleIntegrityError):
        await evaluation_rules(populated)


# --- Findings 2 and 4 on the seeded data ----------------------------------------------


async def test_seeded_policy_never_authorizes_agent_release_and_fingerprints_are_stable(
    populated,
):
    rule_set, pins = await evaluation_rules(populated), await snapshots(populated)
    parts = [Part.from_mapping(PANEL)]

    async def evaluate(**extra):
        populated.expunge_all()
        return await evaluate_rfq(
            populated,
            parts,
            customer_id="CUST-001",
            **rule_set,
            **pins,
            horizon_start=MONDAY,
            **extra,
        )

    first, again = await evaluate(), await evaluate()
    assert first.quote_release_authorized_for_agent is False
    assert first.evaluation_fingerprint == again.evaluation_fingerprint
    late = await evaluate(requested_delivery_date=date(2026, 11, 10))
    assert late.evaluation_fingerprint != first.evaluation_fingerprint

    await populated.execute(
        update(Customer)
        .where(Customer.customer_id == "CUST-001")
        .values(credit_exposure_usd=Customer.credit_exposure_usd + 1)
    )
    exposed = await evaluate()
    assert exposed.quotation.calculation_id == first.quotation.calculation_id
    assert exposed.evaluation_fingerprint != first.evaluation_fingerprint


# --- Finding 9: read consistency and catalog-read batching ----------------------------


async def test_snapshot_session_is_repeatable_read_and_read_only(seed_database):
    async with seed_database.snapshot_session() as snapshot:
        isolation = await snapshot.scalar(text("SHOW transaction_isolation"))
        read_only = await snapshot.scalar(text("SHOW transaction_read_only"))
        assert (isolation, read_only) == ("repeatable read", "on")
    with pytest.raises(DatabaseError):
        async with seed_database.snapshot_session() as snapshot:
            await snapshot.execute(
                update(Customer).where(Customer.customer_id == "none").values(account_status="x")
            )


async def test_parts_sharing_material_and_type_share_catalog_reads(populated, seed_database):
    rule_set, pins = await pinned(populated), await snapshots(populated)

    async def count(n):
        parts = [Part.from_mapping({**PANEL, "part_id": f"P{i}"}) for i in range(n)]
        populated.expunge_all()
        with statements(seed_database) as captured:
            await quote_parts(populated, parts, **rule_set, **pins, horizon_start=MONDAY)
        assert all(s.lstrip().upper().startswith("SELECT") for s in captured)
        return len(captured)

    # Without the request-scoped memo each extra part re-read its material and routing (7).
    assert await count(3) == await count(1)


# --- TD-004: approval-v1.2 registration and behaviour on the seeded data ----------------


async def test_approval_v1_2_registers_once_and_verifies(populated):
    assert await get_rule_version(populated, "approval", "approval-v1.2") is None
    assert await register_rule_version(populated, "approval-v1.2") == "registered"
    assert await register_rule_version(populated, "approval-v1.2") == "already present"
    rule = await get_rule_version(populated, "approval", "approval-v1.2")
    ratio = rule.document["engineering_review_thresholds"]["borderline_bend_radius"]
    assert ratio["radius_below_minimum_ratio"] == "1.25"


async def test_tampered_approval_v1_2_is_rejected(populated):
    await register_rule_version(populated, "approval-v1.2")
    path = ["engineering_review_thresholds", "borderline_bend_radius", "radius_below_minimum_ratio"]
    await tamper(populated, "approval-v1.2", path, "1.01")
    with pytest.raises(RuleIntegrityError, match="differs from its approved version"):
        await get_rule_version(populated, "approval", "approval-v1.2")


async def test_unapproved_versions_cannot_be_registered(populated):
    with pytest.raises(SeedError, match="not an approved post-baseline"):
        await register_rule_version(populated, "approval-v9.9")


async def test_baseline_checks_accept_only_the_registered_extra(
    populated, seed_database, seed_plan
):
    await register_rule_version(populated, "approval-v1.2")
    verified = await execute_seed(populated, seed_database.config, seed_plan, "verify")
    assert verified["status"] == "PASS"
    audit = await audit_integrity(populated, seed_database.config, seed_plan)
    assert audit["status"] == "PASS"

    stored = await populated.scalar(
        select(BusinessRuleVersion).where(BusinessRuleVersion.rule_version == "price-v1.0")
    )
    await populated.execute(
        insert(BusinessRuleVersion).values(
            rule_version="price-v9.0",
            rule_kind="pricing",
            sha256="d" * 64,
            document={**stored.document, "version": "price-v9.0"},
            source_path="test://unregistered",
        )
    )
    with pytest.raises(SeedError):
        await execute_seed(populated, seed_database.config, seed_plan, "verify")


async def test_approval_v1_2_changes_only_borderline_outcomes(populated):
    """All 160 dev/validation RFQs: v1.2 equals v1.1 except borderline bends, which go to
    engineering review. The labels were generated under v1.1 and are not re-labelled."""
    await register_rule_version(populated, "approval-v1.2")
    rule_set, pins = await evaluation_rules(populated), await snapshots(populated)
    v12 = await get_rule_version(populated, "approval", "approval-v1.2")
    moved, compared = [], 0
    for rfq_id, truth in truth_files():
        requested = truth["expected_delivery_date"]
        kwargs = dict(
            customer_id=truth["expected_customer_id"],
            **pins,
            horizon_start=MONDAY,
            requested_delivery_date=date.fromisoformat(requested) if requested else None,
            contradictions=truth["contradiction_findings"],
        )
        parts = [Part.from_mapping(p) for p in truth["expected_extraction"]]
        old = await evaluate_rfq(populated, parts, **rule_set, **kwargs)
        new = await evaluate_rfq(populated, parts, **{**rule_set, "approval_rules": v12}, **kwargs)
        compared += 1
        assert new.unevaluated_review_conditions == ()
        same = ("outcome", "escalation_reason", "draft_creation_allowed", "manager_review_reasons")
        if not new.borderline_bend_lines:
            assert [getattr(new, f) for f in same] == [getattr(old, f) for f in same], rfq_id
            continue
        moved.append(rfq_id)
        assert new.draft_creation_allowed == old.draft_creation_allowed, rfq_id
        assert old.outcome in ("quote_draft_ready_for_approval", "commercial_review_required")
        assert new.outcome == "engineering_review_required", rfq_id
    assert compared == 160
    print(f"\napproval-v1.2: {len(moved)} of 160 RFQs move to engineering review: {moved}")


async def test_a_borderline_bend_on_seeded_data_needs_engineering_review(populated):
    await register_rule_version(populated, "approval-v1.2")
    rule_set, pins = await evaluation_rules(populated), await snapshots(populated)
    v12 = await get_rule_version(populated, "approval", "approval-v1.2")
    _, truth = next(
        (rfq_id, t)
        for rfq_id, t in truth_files()
        if t["expected_outcome"] == "quote_draft_ready_for_approval"
        and any(p.get("part_type") == "l_bracket" for p in t["expected_extraction"])
    )
    parts = [Part.from_mapping(p) for p in truth["expected_extraction"]]
    kwargs = dict(customer_id=truth["expected_customer_id"], **pins, horizon_start=MONDAY)
    clean = await evaluate_rfq(populated, parts, **{**rule_set, "approval_rules": v12}, **kwargs)
    index = next(
        i for i, line in enumerate(clean.quotation.lines) if line.manufacturing.min_bend_radius_mm
    )
    minimum = clean.quotation.lines[index].manufacturing.min_bend_radius_mm
    parts[index] = replace(parts[index], bend_radius_mm=minimum)  # exactly the minimum
    tight = await evaluate_rfq(populated, parts, **{**rule_set, "approval_rules": v12}, **kwargs)
    assert (tight.outcome, tight.escalation_reason, tight.borderline_bend_lines) == (
        "engineering_review_required",
        "BORDERLINE_BEND_RADIUS",
        (clean.line_numbers[index],),
    )
    assert tight.evaluation_fingerprint != clean.evaluation_fingerprint
    v11 = await evaluate_rfq(populated, parts, **rule_set, **kwargs)
    assert v11.unevaluated_review_conditions == ("borderline_bend_radius",)
