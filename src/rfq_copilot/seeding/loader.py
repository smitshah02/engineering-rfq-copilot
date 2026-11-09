"""One locked transaction, exact baseline comparison, bounded inserts and verification."""

import hashlib
import os
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import CheckConstraint, Text, bindparam, insert, select, text
from sqlalchemy.dialects.postgresql import JSONB

from rfq_copilot.database import Database
from rfq_copilot.seeding.mapping import TABLES, SeedPlan, key_for, validate_plan
from rfq_copilot.seeding.rules import is_registered_extra
from rfq_copilot.seeding.safety import validate_target, verify_live_identity, verify_schema
from rfq_copilot.seeding.sources import sha256_file
from rfq_copilot.seeding.transform import json_equal, json_text, json_value, require


def json_ready(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    return value


async def read_rows(session, name):
    table = TABLES[name]
    columns = [c.cast(Text).label(c.name) if isinstance(c.type, JSONB) else c for c in table.c]
    result = [
        dict(r)
        for r in (await session.execute(select(*columns).order_by(*table.primary_key))).mappings()
    ]
    for record in result:
        for column in table.c:
            if isinstance(column.type, JSONB) and record[column.name] is not None:
                record[column.name] = json_value(record[column.name], "stored JSON")
    return result


async def database_state(session, plan: SeedPlan):
    """Reject extra/partial/live-mutated data; never merge a partial baseline."""
    stored = {name: await read_rows(session, name) for name in plan.order}
    counts = {TABLES[n].fullname: len(v) for n, v in stored.items()}
    digest = hashlib.sha256(json_text(json_ready(stored)).encode("utf-8")).hexdigest()
    if not any(counts.values()):
        return "empty", counts, digest
    for name in plan.order:
        # Approved post-baseline rule versions (seeding/rules.py) are the only extra rows.
        baseline = {r.get("rule_version") for r in plan.rows[name]}
        stored[name] = [r for r in stored[name] if not is_registered_extra(name, r, baseline)]
        require(
            len(stored[name]) == len(plan.rows[name]),
            TABLES[name].fullname,
            "partial/unexpected database state; import refused",
        )
        expected = {key_for(r, TABLES[name].primary_key): r for r in plan.rows[name]}
        for ordinal, record in enumerate(stored[name], 1):
            key = key_for(record, TABLES[name].primary_key)
            require(
                key in expected, TABLES[name].fullname, "unexpected source identity; import refused"
            )
            for column, value in expected[key].items():
                require(
                    json_equal(record[column], value)
                    if isinstance(TABLES[name].c[column].type, JSONB)
                    else record[column] == value,
                    f"{TABLES[name].fullname}:{ordinal}:{column}",
                    "stored/source conflict; import refused",
                )
            if "registered_at" in record:
                require(
                    isinstance(record["registered_at"], datetime)
                    and record["registered_at"].tzinfo is not None,
                    name,
                    "invalid registration timestamp",
                )
    return "identical", counts, digest


async def validate_sql_rows(session, plan):
    """Evaluate every approved CHECK in SELECTs; dry-run uses no INSERT or UPDATE."""
    for name in plan.order:
        records, table = plan.rows[name], TABLES[name]
        if not records:
            continue
        checks = [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]
        if not checks:
            continue
        fields = ",".join(
            f'"{c.name}" {c.type.compile(dialect=session.bind.dialect)}' for c in table.c
        )
        predicate = " OR ".join(f"({check}) IS FALSE" for check in checks)
        sql = text(
            f"SELECT count(*) FROM jsonb_to_recordset(CAST(:records AS jsonb)) AS seed({fields}) WHERE {predicate}"
        )
        invalid = await session.scalar(sql, {"records": json_text(json_ready(records))})
        require(invalid == 0, table.fullname, "source violates database CHECK")


def snapshot_artifacts(plan, *, create=False):
    directory = plan.snapshot_dir
    require(not directory.is_symlink(), "snapshots", "symlink directory forbidden")
    if create:
        require(
            all(not p.is_symlink() for p in directory.absolute().parents),
            "snapshots",
            "symlink parent forbidden",
        )
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(
            directory.stat().st_mode & 0o077 == 0, "snapshots", "directory must be private (0700)"
        )
    for snapshot in plan.rows["data_snapshots"]:
        path = Path(snapshot["artifact_ref"])
        if create and not path.exists():
            source = "operations_data/" + (
                "material_inventory.csv"
                if snapshot["snapshot_kind"] == "inventory"
                else "production_capacity.csv"
            )
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=directory, prefix=".seed-", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(plan.bundle.raw[source])
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.fchmod(stream.fileno(), 0o444)
                try:
                    os.link(
                        temporary, path
                    )  # Atomic publication; never replace an existing artifact.
                except FileExistsError:
                    pass
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        require(
            path.is_file() and not path.is_symlink(),
            "snapshots",
            "required immutable artifact missing",
        )
        require(sha256_file(path) == snapshot["sha256"], "snapshots", "artifact checksum differs")


async def insert_table(session, name, records):
    table = TABLES[name]
    columns = [c for c in table.c if not c.computed and c.name != "registered_at"]
    statement = insert(table).values(
        {
            c.name: bindparam(c.name, type_=Text).cast(JSONB)
            if isinstance(c.type, JSONB)
            else bindparam(c.name, type_=c.type)
            for c in columns
        }
    )
    for start in range(0, len(records), 100):
        batch = [
            {
                c.name: json_text(r[c.name])
                if isinstance(c.type, JSONB) and r[c.name] is not None
                else r[c.name]
                for c in columns
            }
            for r in records[start : start + 100]
        ]
        if batch:
            await session.execute(statement, batch)


async def execute_seed(session, config, plan, mode, *, confirm_database=None):
    """Caller owns the transaction. This function never commits or closes its session."""
    validate_target(config)
    require(mode in {"dry-run", "apply", "verify"}, "mode", "unknown action")
    require(session.in_transaction(), "transaction", "explicit transaction required")
    if mode == "apply":
        require(
            confirm_database == config.url.database,
            "apply",
            "explicit target database confirmation required",
        )
    validate_plan(plan)
    await session.execute(text("SET LOCAL lock_timeout = '5s'"))
    await session.execute(text("SET LOCAL statement_timeout = '30s'"))
    for key in [504, 506]:  # Coordinate with migrations and concurrent seeders.
        require(
            await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(461441,:key)"), {"key": key}
            ),
            "database",
            "another migration/import is active",
        )
    connection = await session.connection()
    await connection.run_sync(verify_live_identity, config)
    if mode == "apply":
        tables = ",".join(sorted(t.fullname for t in TABLES.values()))
        await session.execute(text(f"LOCK TABLE {tables} IN SHARE MODE"))
    # Verify after table locking so DDL cannot change between the check and writes.
    identity = await connection.run_sync(verify_schema, config)
    state, counts, before_digest = await database_state(session, plan)
    await validate_sql_rows(session, plan)
    if mode == "verify":
        require(state == "identical", "verify", "baseline is not imported")
    if state == "identical":
        snapshot_artifacts(plan)
    inserted = 0
    if mode == "apply" and state == "empty":
        snapshot_artifacts(plan, create=True)
        for name in plan.order:
            await insert_table(session, name, plan.rows[name])
            inserted += len(plan.rows[name])
        await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        after_state, counts, _ = await database_state(session, plan)
        require(after_state == "identical", "import", "post-insert verification failed")
    after_state, after_counts, after_digest = await database_state(session, plan)
    if mode != "apply" or state == "identical":
        require(before_digest == after_digest, "database", "read-only/idempotent state changed")
    return {
        **plan.summary(),
        **identity,
        "mode": mode,
        "status": "PASS",
        "initial_state": state,
        "final_state": after_state,
        "inserted": inserted,
        "already_existing": sum(map(len, plan.rows.values())) if state == "identical" else 0,
        "conflicted": 0,
        "invalid": 0,
        "counts": after_counts,
        "before_digest": before_digest,
        "after_digest": after_digest,
    }


async def run_seed(config, plan, mode, *, confirm_database=None):
    """Use the existing engine/session owner; success is reported only after commit."""
    started = time.monotonic()
    validate_target(config)
    async with Database(config, read_only=mode != "apply") as database:
        async with database.session() as session:
            async with session.begin():
                if mode != "apply":
                    await session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    )
                result = await execute_seed(
                    session, config, plan, mode, confirm_database=confirm_database
                )
    result.update(
        transaction="committed" if mode == "apply" else "read_only",
        duration_seconds=round(time.monotonic() - started, 4),
    )
    return result
