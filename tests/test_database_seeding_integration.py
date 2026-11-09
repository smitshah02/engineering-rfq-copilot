"""Real PostgreSQL imports in rolled-back test transactions; guarded schema restoration."""

import asyncio
import copy
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_database_migrations import alembic_config, user_objects
from test_database_seeding import ARCHIVE, SOURCE

from alembic import command
from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database, DatabaseError
from rfq_copilot.migration_safety import (
    MigrationSettings,
    cleanup_test_tracker,
    verify_identity,
)
from rfq_copilot.seeding import SeedError, loader
from rfq_copilot.seeding.loader import execute_seed, read_rows, run_seed
from rfq_copilot.seeding.mapping import EMPTY_TABLES, EXPECTED_COUNTS, TABLES, build_plan
from rfq_copilot.seeding.sources import load_sources

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]
ROOT = Path(__file__).resolve().parents[1]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def seed_database():
    config = DatabaseConfig.from_env("test", env_file=ROOT / ".env")
    settings = MigrationSettings(config, allow_test_downgrade=True)
    async with Database(config) as database:
        async with database.engine.connect() as lease:
            identity = await lease.run_sync(verify_identity, settings)
            assert identity["database"] == "rfq_copilot_test"
            assert await lease.scalar(text("SELECT pg_try_advisory_lock(461441,505)"))
            await lease.rollback()
            original = await lease.run_sync(user_objects)
            assert original["relations"] == [], "Existing test objects are preserved."
            assert [r[0] for r in original["schemas"]] == ["public"]
            await lease.rollback()
            try:
                await asyncio.to_thread(command.upgrade, alembic_config(), "head")
                yield database
            finally:
                await lease.rollback()
                if await lease.scalar(text("SELECT to_regclass('business.customers') IS NOT NULL")):
                    for table in TABLES.values():
                        assert await lease.scalar(select(func.count()).select_from(table)) == 0
                    await lease.rollback()
                    await asyncio.to_thread(command.downgrade, alembic_config(allow=True), "base")
                await lease.run_sync(cleanup_test_tracker)
                await lease.commit()
                assert await lease.run_sync(user_objects) == original
                await lease.execute(text("SELECT pg_advisory_unlock(461441,505)"))
                await lease.rollback()


@pytest.fixture(scope="module")
def seed_bundle():
    return load_sources(SOURCE, ARCHIVE)


@pytest.fixture
def seed_plan(seed_bundle, tmp_path):
    return build_plan(seed_bundle, tmp_path / "snapshots")


@pytest_asyncio.fixture(loop_scope="module")
async def session(seed_database):
    async with seed_database.session() as session:
        transaction = await session.begin()
        try:
            yield session
        finally:
            if transaction.is_active:
                await transaction.rollback()
            await session.rollback()


async def apply(session, database, plan):
    return await execute_seed(
        session, database.config, plan, "apply", confirm_database="rfq_copilot_test"
    )


async def assert_empty(database):
    async with database.session() as observer:
        for table in TABLES.values():
            assert await observer.scalar(select(func.count()).select_from(table)) == 0


async def test_real_dry_run_is_read_only_and_creates_no_artifacts(seed_database, seed_plan):
    result = await run_seed(seed_database.config, seed_plan, "dry-run")
    assert result["inserted"] == 0 and result["transaction"] == "read_only"
    assert result["before_digest"] == result["after_digest"]
    assert not seed_plan.snapshot_dir.exists()
    await assert_empty(seed_database)


async def test_full_transactional_import_counts_and_representative_values(
    session, seed_database, seed_plan
):
    result = await apply(session, seed_database, seed_plan)
    assert result["inserted"] == 1089 and result["final_state"] == "identical"
    for name, count in EXPECTED_COUNTS.items():
        assert await session.scalar(select(func.count()).select_from(TABLES[name])) == count
    for name in EMPTY_TABLES:
        assert await read_rows(session, name) == []
    for name in [
        "customers",
        "users",
        "contacts",
        "materials",
        "finishes",
        "workcentres",
        "material_inventory",
        "production_capacity",
        "procurement_records",
        "rfqs",
        "quotes",
        "quote_versions",
        "quote_line_items",
        "approval_history",
    ]:
        actual = await read_rows(session, name)
        for record in seed_plan.rows[name]:
            assert record in actual
    await session.rollback()
    await assert_empty(seed_database)


async def test_second_import_is_identical_without_insert_update_or_delete(
    session, seed_database, seed_plan
):
    first = await apply(session, seed_database, seed_plan)
    writes = []

    def trace(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    event.listen(seed_database.engine.sync_engine, "before_cursor_execute", trace)
    try:
        second = await apply(session, seed_database, seed_plan)
    finally:
        event.remove(seed_database.engine.sync_engine, "before_cursor_execute", trace)
    assert writes == []
    assert second["inserted"] == 0 and second["already_existing"] == 1089
    assert first["after_digest"] == second["before_digest"] == second["after_digest"]


async def test_verification_only_after_import(session, seed_database, seed_plan):
    await apply(session, seed_database, seed_plan)
    result = await execute_seed(session, seed_database.config, seed_plan, "verify")
    assert result["status"] == "PASS" and result["inserted"] == 0


async def test_verify_empty_baseline_is_failure(session, seed_database, seed_plan):
    with pytest.raises(SeedError, match="not imported"):
        await execute_seed(session, seed_database.config, seed_plan, "verify")


async def test_apply_confirmation_is_mandatory(session, seed_database, seed_plan):
    with pytest.raises(SeedError, match="confirmation"):
        await execute_seed(session, seed_database.config, seed_plan, "apply")
    await assert_empty(seed_database)


async def test_wrong_live_database_identity_refused(session, seed_plan):
    development = DatabaseConfig.from_env("development", env_file=ROOT / ".env")
    with pytest.raises(SeedError, match="identity"):
        await execute_seed(session, development, seed_plan, "dry-run")


@pytest.mark.parametrize("fault", ["revision", "missing_table", "check_definition"])
async def test_missing_or_drifted_schema_is_rejected(session, seed_database, seed_plan, fault):
    if fault == "revision":
        await session.execute(text("DELETE FROM public.alembic_version"))
    elif fault == "missing_table":
        await session.execute(
            text("ALTER TABLE business.customers RENAME TO test_missing_customers")
        )
    else:
        await session.execute(
            text("ALTER TABLE business.customers DROP CONSTRAINT ck_customers_rule_01")
        )
        await session.execute(
            text("ALTER TABLE business.customers ADD CONSTRAINT ck_customers_rule_01 CHECK (TRUE)")
        )
    with pytest.raises(SeedError, match="revision|shape|CHECK"):
        await execute_seed(session, seed_database.config, seed_plan, "dry-run")


@pytest.mark.parametrize(
    "column,value",
    [("company_name", "operational edit"), ("credit_limit_usd", 123), ("row_version", 2)],
)
async def test_stored_source_conflict_never_overwrites(
    session, seed_database, seed_plan, column, value
):
    await apply(session, seed_database, seed_plan)
    name = "material_inventory" if column == "row_version" else "customers"
    table = TABLES[name]
    primary = list(table.primary_key)[0]
    identifier = seed_plan.rows[name][0][primary.name]
    await session.execute(update(table).where(primary == identifier).values({column: value}))
    before = await read_rows(session, name)
    with pytest.raises(SeedError, match="stored/source conflict"):
        await apply(session, seed_database, seed_plan)
    assert await read_rows(session, name) == before


async def test_partial_state_is_preserved_and_not_merged(session, seed_database, seed_plan):
    customer = seed_plan.rows["customers"][0]
    await session.execute(insert(TABLES["customers"]).values(customer))
    with pytest.raises(SeedError, match="partial/unexpected"):
        await apply(session, seed_database, seed_plan)
    assert await read_rows(session, "customers") == [customer]
    assert not seed_plan.snapshot_dir.exists()


async def test_invalid_source_rolls_back_without_business_writes(seed_database, seed_plan):
    seed_plan.rows["customers"][0]["credit_limit_usd"] = Decimal(-1)
    with pytest.raises(SeedError, match="CHECK"):
        await run_seed(
            seed_database.config, seed_plan, "apply", confirm_database="rfq_copilot_test"
        )
    await assert_empty(seed_database)


@pytest.mark.parametrize(
    "fault", ["check", "foreign_key", "unique", "deferred_pointer", "application_failure"]
)
async def test_late_failure_rolls_back_all_inserted_parents(
    monkeypatch, seed_database, seed_plan, fault
):
    original = loader.insert_table
    visited = []

    async def injected(session, name, records):
        visited.append(name)
        if fault == "deferred_pointer" and name == "quotes":
            records = copy.deepcopy(records)
            records[0]["current_version"] = 999
        if name == "quote_line_items":
            assert "customers" in visited and "quote_versions" in visited
            if fault == "application_failure":
                raise RuntimeError("injected application failure")
            records = copy.deepcopy(records)
            if fault == "check":
                records[0]["quantity"] = -1
            elif fault == "foreign_key":
                records[0]["quote_version"] = 999
            elif fault == "unique":
                records.append(copy.deepcopy(records[0]))
        await original(session, name, records)

    monkeypatch.setattr(loader, "insert_table", injected)
    expected = RuntimeError if fault == "application_failure" else DatabaseError
    with pytest.raises(expected):
        await run_seed(
            seed_database.config, seed_plan, "apply", confirm_database="rfq_copilot_test"
        )
    await assert_empty(seed_database)


async def test_cancelled_import_rolls_back_and_releases_pool(monkeypatch, seed_database, seed_plan):
    ready = asyncio.Event()
    original = loader.insert_table

    async def pause(session, name, records):
        await original(session, name, records)
        if name == "customers":
            ready.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(loader, "insert_table", pause)
    worker = asyncio.create_task(
        run_seed(seed_database.config, seed_plan, "apply", confirm_database="rfq_copilot_test")
    )
    try:
        await asyncio.wait_for(ready.wait(), 5)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        await assert_empty(seed_database)
        assert seed_database.engine.pool.checkedout() == 1  # Module's lifecycle lease only.
    finally:
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_concurrent_import_attempt_is_rejected(session, seed_database, seed_plan):
    await apply(session, seed_database, seed_plan)
    with pytest.raises(SeedError, match="another migration/import"):
        await run_seed(
            seed_database.config, seed_plan, "apply", confirm_database="rfq_copilot_test"
        )
    assert await session.scalar(select(func.count()).select_from(TABLES["customers"])) == 40


async def test_corrupt_archived_artifact_blocks_replay(session, seed_database, seed_plan):
    await apply(session, seed_database, seed_plan)
    path = Path(seed_plan.rows["data_snapshots"][0]["artifact_ref"])
    path.chmod(0o600)
    path.write_bytes(b"corrupt isolated test artifact")
    with pytest.raises(SeedError, match="artifact checksum"):
        await apply(session, seed_database, seed_plan)


async def test_json_type_conflict_is_rejected_without_overwrite(session, seed_database, seed_plan):
    from sqlalchemy import literal
    from sqlalchemy.dialects.postgresql import JSONB

    from rfq_copilot.seeding.transform import json_text

    await apply(session, seed_database, seed_plan)
    rule = next(
        r
        for r in seed_plan.rows["business_rule_versions"]
        if r["rule_kind"] == "business_constraints"
    )
    document = {**rule["document"], "agent_can_finalize_approval": 0}
    table = TABLES["business_rule_versions"]
    await session.execute(
        update(table)
        .where(table.c.rule_version == rule["rule_version"])
        .values(document=literal(json_text(document)).cast(JSONB))
    )
    before = await read_rows(session, "business_rule_versions")
    with pytest.raises(SeedError, match="stored/source conflict"):
        await apply(session, seed_database, seed_plan)
    assert await read_rows(session, "business_rule_versions") == before
    assert await session.scalar(select(func.count()).select_from(TABLES["customers"])) == 40


async def test_owned_run_seed_commits_before_reporting_success(
    monkeypatch, seed_database, seed_plan
):
    # Commit a real savepoint inside an outer rollback-only test transaction.
    # This exercises run_seed's commit path without leaving committed test records.
    async with seed_database.engine.connect() as connection:
        outer = await connection.begin()

        class BoundDatabase:
            def __init__(self, config, *, read_only=False):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            @asynccontextmanager
            async def session(self):
                async with AsyncSession(
                    bind=connection, join_transaction_mode="create_savepoint"
                ) as session:
                    yield session

        monkeypatch.setattr(loader, "Database", BoundDatabase)
        try:
            result = await run_seed(
                seed_database.config, seed_plan, "apply", confirm_database="rfq_copilot_test"
            )
            assert result["transaction"] == "committed" and result["inserted"] == 1089
            assert (
                await connection.scalar(select(func.count()).select_from(TABLES["customers"])) == 40
            )
        finally:
            await outer.rollback()
    await assert_empty(seed_database)


async def test_postinsert_verification_failure_rolls_back(monkeypatch, seed_database, seed_plan):
    original = loader.database_state
    calls = 0

    async def fail_after_insert(session, plan):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SeedError("injected verification failure")
        return await original(session, plan)

    monkeypatch.setattr(loader, "database_state", fail_after_insert)
    with pytest.raises(SeedError, match="verification failure"):
        await run_seed(
            seed_database.config, seed_plan, "apply", confirm_database="rfq_copilot_test"
        )
    await assert_empty(seed_database)
