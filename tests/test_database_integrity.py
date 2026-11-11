"""Real constraint/transaction enforcement, reusing Component 5's guarded fixtures."""

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from test_database_seeding_integration import (
    seed_bundle as seed_bundle,
)
from test_database_seeding_integration import (
    seed_database as seed_database,
)
from test_database_seeding_integration import (
    seed_plan as seed_plan,
)
from test_database_seeding_integration import (
    session as session,
)

from rfq_copilot.config import DatabaseConfig
from rfq_copilot.integrity import (
    IntegrityVerificationError,
    audit_integrity,
    fingerprint,
    run_integrity,
)
from rfq_copilot.seeding.loader import execute_seed, insert_table
from rfq_copilot.seeding.mapping import EXPECTED_COUNTS, TABLES, build_plan
from rfq_copilot.seeding.rules import REGISTERED
from rfq_copilot.seeding.transform import json_text


def expected_records(overlay_state):
    """Baseline + registered rules + the rows of the reported overlay (pinned file)."""
    from rfq_copilot.schema_contract import active
    from rfq_copilot.seeding.policy import policy_rows

    base = 1089 + len(REGISTERED)
    if overlay_state == "absent":
        return base
    assert overlay_state is not None and overlay_state.startswith("applied:")
    version = overlay_state.removeprefix("applied:")
    assert version in active().policy_overlays
    return base + sum(len(rows) for rows in policy_rows(version).values())


pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]
ROOT = Path(__file__).resolve().parents[1]
POPULATED = sorted(n for n, count in EXPECTED_COUNTS.items() if count)


@pytest_asyncio.fixture(loop_scope="module")
async def populated(session, seed_database, seed_plan):
    await execute_seed(
        session, seed_database.config, seed_plan, "apply", confirm_database="rfq_copilot_test"
    )
    return session


async def reject(session, name, operation, sqlstate):
    table = TABLES[name]
    before = await session.scalar(select(func.count()).select_from(table))
    with pytest.raises(DBAPIError) as error:
        async with session.begin_nested():
            await operation()
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert error.value.orig.sqlstate == sqlstate
    assert await session.scalar(text("SELECT 1")) == 1
    assert await session.scalar(select(func.count()).select_from(table)) == before


async def test_current_development_integrity_and_preservation(seed_bundle):
    config = DatabaseConfig.from_env("development", env_file=ROOT / ".env")
    plan = build_plan(seed_bundle, ROOT / "data/rfq_seed_snapshots")
    result = await run_integrity(config, plan)
    assert result["database"] == "rfq_copilot_dev" and result["role"] == "rfq_copilot_dev"
    # Head 20261111_06 (TD-012 + Phase 3.5); values measured on the seeded test DB.
    assert result["alembic_revision"] == "20261111_06"
    assert result["definitions"] == {
        "tables": 36,
        "columns": 278,
        "primary_keys": 36,
        "foreign_keys": 71,
        "additional_unique_constraints": 14,
        "check_constraints": 218,
        "explicit_indexes": 19,
        "unique_partial_indexes": 5,
    }
    # The 1089-record baseline, each approved post-baseline rule (approval-v1.2) and, if
    # applied, exactly one overlay the active contract accepts (complete; partial fails).
    assert result["records"] == expected_records(result["policy_overlay"])
    assert result["source_values_checked"] == 9587
    assert result["decimal_values_checked"] == 1353
    assert result["foreign_key_data_checks"] == 71 and result["check_data_checks"] == 218
    assert result["unique_data_checks"] == 19 and result["violations"] == 0
    assert result["populated_tables"] == 26 and len(result["empty_runtime_tables"]) == 10
    assert result["preserved"]


@pytest.mark.parametrize("name", POPULATED)
async def test_duplicate_source_primary_key_rejected(populated, seed_plan, name):
    await reject(
        populated, name, lambda: insert_table(populated, name, [seed_plan.rows[name][0]]), "23505"
    )


@pytest.mark.parametrize(
    "name,id_column",
    [
        ("business_rule_versions", "rule_version"),
        ("data_snapshots", "snapshot_id"),
        ("users", "user_id"),
        ("operation_routing", "routing_id"),
        ("production_capacity", "capacity_id"),
        ("quotes", "quote_id"),
        ("quote_versions", "quote_version_id"),
        ("quote_line_items", "quote_line_id"),
    ],
)
async def test_distinct_pk_cannot_duplicate_unique_key(populated, seed_plan, name, id_column):
    source = seed_plan.rows[name][0]
    if name == "quote_versions":
        source = next(r for r in seed_plan.rows[name] if not r["is_current"])
    record = {**source, id_column: "TEST-UNIQUE"}
    await reject(populated, name, lambda: insert_table(populated, name, [record]), "23505")


@pytest.mark.parametrize(
    "name,column,value,state",
    [
        ("contacts", "customer_id", "MISSING-CUSTOMER", "23503"),
        ("material_inventory", "thickness_mm", Decimal("7.5"), "23503"),
        ("operation_routing", "operation_id", "MISSING-OPERATION", "23503"),
        ("approval_history", "quote_version", 999, "23503"),
        ("quotes", "current_version", 999, "23503"),
        ("users", "display_name", None, "23502"),
        ("material_inventory", "quantity_kg", Decimal(-1), "23514"),
        ("material_inventory", "reserved_kg", Decimal(-1), "23514"),
        ("material_inventory", "reserved_kg", Decimal("999999"), "23514"),
        ("production_capacity", "scheduled_hours", Decimal("9999"), "23514"),
        ("production_capacity", "downtime_hours", Decimal("9999"), "23514"),
        ("production_capacity", "week_start", date(2026, 11, 10), "23514"),
        ("customers", "credit_limit_usd", Decimal(-1), "23514"),
        ("customers", "credit_limit_usd", Decimal("NaN"), "23514"),
        ("customers", "credit_limit_usd", Decimal("1e30"), "22003"),
        ("customers", "account_status", "unknown_status", "23514"),
        ("quote_versions", "version", 0, "23514"),
        ("quote_versions", "total_usd", Decimal(-1), "23514"),
    ],
)
async def test_invalid_updates_rejected(populated, seed_plan, name, column, value, state):
    table = TABLES[name]
    record = seed_plan.rows[name][0]
    if name == "quotes":
        record = next(r for r in seed_plan.rows[name] if r["approved_version"] is None)
    where = [c == record[c.name] for c in table.primary_key]

    async def invalid():
        await populated.execute(update(table).where(*where).values({column: value}))

    await reject(populated, name, invalid, state)


async def test_valid_operation_and_workcentre_with_wrong_composite_pair_rejected(
    populated, seed_plan
):
    record = seed_plan.rows["operation_routing"][0]
    wrong = next(
        r["operation_id"]
        for r in seed_plan.rows["operations"]
        if r["operation_id"] != record["operation_id"]
    )

    async def invalid():
        await populated.execute(
            update(TABLES["operation_routing"])
            .where(TABLES["operation_routing"].c.routing_id == record["routing_id"])
            .values(operation_id=wrong)
        )

    await reject(populated, "operation_routing", invalid, "23503")


@pytest.mark.parametrize(
    "fault,state", [("required", "23502"), ("money", "23514"), ("foreign", "23503")]
)
async def test_invalid_inserts_rejected(populated, seed_plan, fault, state):
    name = "contacts" if fault == "foreign" else "customers"
    record = {**seed_plan.rows[name][0]}
    record[list(TABLES[name].primary_key)[0].name] = "TEST-INVALID"
    record.update(
        {"company_name": None}
        if fault == "required"
        else {"credit_limit_usd": Decimal(-1)}
        if fault == "money"
        else {"customer_id": "MISSING-CUSTOMER"}
    )
    await reject(populated, name, lambda: insert_table(populated, name, [record]), state)


@pytest.mark.parametrize(
    "name",
    [
        "customers",
        "users",
        "materials",
        "material_thickness_options",
        "workcentres",
        "business_rule_versions",
        "quotes",
        "quote_versions",
    ],
)
async def test_restrict_delete_preserves_referenced_parent(populated, seed_plan, name):
    table = TABLES[name]
    record = seed_plan.rows[name][0]
    if name == "business_rule_versions":
        record = next(r for r in seed_plan.rows[name] if r["rule_kind"] == "pricing")
    if name == "users":
        referenced = seed_plan.rows["quotes"][0]["created_by"]
        record = next(r for r in seed_plan.rows[name] if r["user_id"] == referenced)
    if name == "quote_versions":
        record = next(r for r in seed_plan.rows[name] if r["is_current"])

    async def invalid():
        await populated.execute(
            delete(table).where(*(c == record[c.name] for c in table.primary_key))
        )

    await reject(populated, name, invalid, "23001")


async def test_cross_schema_foreign_key_enforced(populated, seed_plan):
    record = {
        "workflow_run_id": uuid4(),
        "customer_id": "MISSING-CUSTOMER",
        "initiated_by": seed_plan.rows["users"][0]["user_id"],
        "correlation_id": "integrity-test",
        "graph_version": "isolated-test",
        "rule_versions": {},
        "source_snapshot_versions": {},
    }

    async def invalid():
        await populated.execute(insert(TABLES["workflow_runs"]).values(record))

    await reject(populated, "workflow_runs", invalid, "23503")


async def test_primary_contact_partial_unique_index(populated, seed_plan):
    contact = next(r for r in seed_plan.rows["contacts"] if r["is_primary"])
    duplicate = {**contact, "contact_id": "TEST-SECOND-PRIMARY"}
    await reject(
        populated, "contacts", lambda: insert_table(populated, "contacts", [duplicate]), "23505"
    )
    # Same customer with a non-primary contact is allowed by the predicate.
    await insert_table(populated, "contacts", [{**duplicate, "is_primary": False}])


async def test_current_version_partial_unique_index(populated, seed_plan):
    version = next(r for r in seed_plan.rows["quote_versions"] if r["is_current"])
    duplicate = {**version, "quote_version_id": "TEST-SECOND-CURRENT", "version": 999}
    await reject(
        populated,
        "quote_versions",
        lambda: insert_table(populated, "quote_versions", [duplicate]),
        "23505",
    )
    await insert_table(
        populated, "quote_versions", [{**duplicate, "is_current": False, "approval_valid": False}]
    )


async def test_pending_approval_partial_unique_index(populated, seed_plan):
    quote = next(q for q in seed_plan.rows["quotes"] if q["status"] == "pending_approval")
    record = {
        "approval_request_id": "TEST-REQUEST-1",
        "quote_id": quote["quote_id"],
        "quote_version": quote["current_version"],
        "requested_by": quote["created_by"],
        "approval_rule_version": "approval-v1.1",
    }
    await populated.execute(insert(TABLES["approval_requests"]).values(record))

    async def duplicate():
        await populated.execute(
            insert(TABLES["approval_requests"]).values(
                {**record, "approval_request_id": "TEST-REQUEST-2"}
            )
        )

    await reject(populated, "approval_requests", duplicate, "23505")


async def test_approval_request_version_triple_foreign_key(populated, seed_plan):
    quote = next(q for q in seed_plan.rows["quotes"] if q["status"] == "pending_approval")
    await populated.execute(
        insert(TABLES["approval_requests"]).values(
            approval_request_id="TEST-APPROVAL-TRIPLE",
            quote_id=quote["quote_id"],
            quote_version=quote["current_version"],
            requested_by=quote["created_by"],
            approval_rule_version="approval-v1.1",
        )
    )
    event = next(
        e for e in seed_plan.rows["approval_history"] if e["quote_id"] != quote["quote_id"]
    )

    async def wrong_request():
        await populated.execute(
            update(TABLES["approval_history"])
            .where(TABLES["approval_history"].c.approval_id == event["approval_id"])
            .values(approval_request_id="TEST-APPROVAL-TRIPLE")
        )

    await reject(populated, "approval_history", wrong_request, "23503")


async def test_runtime_dimensional_and_composite_uniqueness(populated, seed_plan):
    record = {
        "rfq_line_id": uuid4(),
        "rfq_id": seed_plan.rows["rfqs"][0]["rfq_id"],
        "line_number": 1,
        "part_id": "TEST-PART",
        "length_mm": Decimal(-1),
    }

    async def bad_dimension():
        await populated.execute(insert(TABLES["rfq_line_items"]).values(record))

    await reject(populated, "rfq_line_items", bad_dimension, "23514")
    record["length_mm"] = Decimal("100")
    await populated.execute(insert(TABLES["rfq_line_items"]).values(record))

    async def duplicate():
        await populated.execute(
            insert(TABLES["rfq_line_items"]).values({**record, "rfq_line_id": uuid4()})
        )

    await reject(populated, "rfq_line_items", duplicate, "23505")


async def test_valid_fk_but_changed_source_association_detected(
    populated, seed_database, seed_plan
):
    contact = seed_plan.rows["contacts"][0]
    other = next(
        c["customer_id"]
        for c in seed_plan.rows["customers"]
        if c["customer_id"] != contact["customer_id"]
    )
    await populated.execute(
        update(TABLES["contacts"])
        .where(TABLES["contacts"].c.contact_id == contact["contact_id"])
        .values(customer_id=other, is_primary=False)
    )
    with pytest.raises(IntegrityVerificationError, match="source value differs"):
        await audit_integrity(populated, seed_database.config, seed_plan)


async def test_json_boolean_cannot_masquerade_as_numeric_zero(populated, seed_database, seed_plan):
    from sqlalchemy import literal

    rule = next(
        r
        for r in seed_plan.rows["business_rule_versions"]
        if r["rule_kind"] == "business_constraints"
    )
    document = {**rule["document"], "agent_can_finalize_approval": 0}
    table = TABLES["business_rule_versions"]
    await populated.execute(
        update(table)
        .where(table.c.rule_version == rule["rule_version"])
        .values(document=literal(json_text(document)).cast(JSONB))
    )
    with pytest.raises(IntegrityVerificationError, match="source value differs"):
        await audit_integrity(populated, seed_database.config, seed_plan)


@pytest.mark.parametrize("fault", ["check", "foreign_key", "unique"])
async def test_multirecord_constraint_failure_is_atomic(populated, seed_plan, fault):
    name = "contacts" if fault == "foreign_key" else "customers"
    key = list(TABLES[name].primary_key)[0].name
    first = {**seed_plan.rows[name][0], key: "TEST-ATOMIC-1"}
    if name == "contacts":
        first["is_primary"] = False
    second = {**first, key: "TEST-ATOMIC-2"}
    if fault == "check":
        second["credit_limit_usd"] = Decimal(-1)
    elif fault == "foreign_key":
        second["customer_id"] = "MISSING-CUSTOMER"
    else:
        second[key] = first[key]
    before = await fingerprint(populated)
    await reject(
        populated,
        name,
        lambda: insert_table(populated, name, [first, second]),
        {"check": "23514", "foreign_key": "23503", "unique": "23505"}[fault],
    )
    assert await fingerprint(populated) == before


@pytest.mark.parametrize("mode", ["commit", "rollback", "exception"])
async def test_owned_session_transaction_boundaries(seed_database, seed_plan, mode):
    table = TABLES["customers"]
    record = seed_plan.rows["customers"][0]
    try:
        if mode == "exception":
            with pytest.raises(RuntimeError, match="test rollback"):
                async with seed_database.session() as owned:
                    async with owned.begin():
                        await owned.execute(insert(table).values(record))
                        raise RuntimeError("test rollback")
        else:
            async with seed_database.session() as owned:
                await owned.execute(insert(table).values(record))
                if mode == "commit":
                    await owned.commit()
                else:
                    await owned.rollback()
        async with seed_database.session() as observer:
            assert await observer.scalar(select(func.count()).select_from(table)) == int(
                mode == "commit"
            )
    finally:
        # Remove only this fixture's committed source row, never reset a database.
        if mode == "commit":
            assert seed_database.config.target == "test"
            async with seed_database.session() as cleanup:
                await cleanup.execute(
                    delete(table).where(table.c.customer_id == record["customer_id"])
                )
                await cleanup.commit()
    assert seed_database.engine.pool.checkedout() == 1  # Lifecycle lease only.


async def test_async_cancellation_rolls_back_written_row_and_closes_session(
    seed_database, seed_plan
):
    ready = asyncio.Event()

    async def worker():
        async with seed_database.session() as owned:
            async with owned.begin():
                await owned.execute(
                    insert(TABLES["customers"]).values(seed_plan.rows["customers"][0])
                )
                ready.set()
                await owned.execute(text("SELECT pg_sleep(10)"))

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(ready.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with seed_database.session() as observer:
            assert await observer.scalar(select(func.count()).select_from(TABLES["customers"])) == 0
            assert await observer.scalar(text("SELECT 1")) == 1
        assert seed_database.engine.pool.checkedout() == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
