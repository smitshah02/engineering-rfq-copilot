"""Independent SELECT-only PostgreSQL integrity evidence for the approved seed baseline."""

import argparse
import asyncio
import hashlib
import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    CheckConstraint,
    Numeric,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
from rfq_copilot.database import Database, DatabaseError
from rfq_copilot.models import Base
from rfq_copilot.seeding import SeedError
from rfq_copilot.seeding.loader import snapshot_artifacts
from rfq_copilot.seeding.mapping import EMPTY_TABLES, TABLES, SeedPlan, build_plan
from rfq_copilot.seeding.safety import validate_target, verify_schema
from rfq_copilot.seeding.sources import load_sources
from rfq_copilot.seeding.transform import json_equal, json_value


class IntegrityVerificationError(ValueError):
    """Safe table/column diagnostics, without source values or connection details."""


def check(condition, location, rule):
    if not condition:
        raise IntegrityVerificationError(f"{location}: {rule}.")


async def fingerprint(session):
    """Hash every complete row on the server, including registration timestamps.

    SQL identifiers come exclusively from the registered approved metadata, never
    caller strings. Sorting canonical JSONB rows makes heap/insert order irrelevant.
    No row payload is returned to a log or report.
    """
    tables = {}
    for table in sorted(Base.metadata.tables.values(), key=lambda t: t.fullname):
        row = (
            (
                await session.execute(
                    text(
                        "SELECT count(*) AS rows, encode(sha256(convert_to("
                        "COALESCE(jsonb_agg(j ORDER BY j::text)::text,'[]'),'UTF8')),'hex') AS sha256 "
                        f"FROM (SELECT to_jsonb(t) AS j FROM {table.fullname} t) source"
                    )
                )
            )
            .mappings()
            .one()
        )
        tables[table.fullname] = dict(row)
    encoded = json.dumps(tables, sort_keys=True, separators=(",", ":")).encode()
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "tables": tables}


def verify_definitions(connection):
    """Compare actual PK, UNIQUE and every FK tuple/action/timing independently."""
    definitions = (
        connection.execute(
            text(
                "SELECT n.nspname||'.'||t.relname AS tab,k.conname AS name,k.contype::text AS kind,"
                "ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY z(num,pos) "
                "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=z.num ORDER BY z.pos) AS cols,"
                "rn.nspname||'.'||r.relname AS remote_table,"
                "ARRAY(SELECT a.attname FROM unnest(k.confkey) WITH ORDINALITY z(num,pos) "
                "JOIN pg_attribute a ON a.attrelid=r.oid AND a.attnum=z.num ORDER BY z.pos) AS remote_cols,"
                "k.confdeltype::text AS ondelete,k.confupdtype::text AS onupdate,k.confmatchtype::text AS match,"
                "k.condeferrable,k.condeferred,k.convalidated,k.conenforced "
                "FROM pg_constraint k JOIN pg_class t ON t.oid=k.conrelid "
                "JOIN pg_namespace n ON n.oid=t.relnamespace LEFT JOIN pg_class r ON r.oid=k.confrelid "
                "LEFT JOIN pg_namespace rn ON rn.oid=r.relnamespace "
                "WHERE n.nspname IN ('business','orchestration')"
            )
        )
        .mappings()
        .all()
    )
    counts = {kind: sum(d["kind"] == kind for d in definitions) for kind in ["p", "f", "u", "c"]}
    check(counts == {"p": 33, "f": 64, "u": 14, "c": 207}, "catalog", "constraint counts differ")
    for table in Base.metadata.tables.values():
        found = [d for d in definitions if d["tab"] == table.fullname]
        check(
            all(d["convalidated"] and d["conenforced"] for d in found),
            table.fullname,
            "unenforced constraint",
        )
        check(
            [tuple(d["cols"]) for d in found if d["kind"] == "p"]
            == [tuple(c.name for c in table.primary_key)],
            table.fullname,
            "primary key differs",
        )
        check(
            {tuple(d["cols"]) for d in found if d["kind"] == "u"}
            == {
                tuple(c.name for c in k.columns)
                for k in table.constraints
                if isinstance(k, UniqueConstraint)
            },
            table.fullname,
            "unique keys differ",
        )
        wanted = {
            (
                connection.dialect.identifier_preparer.format_constraint(fk),
                tuple(e.parent.name for e in fk.elements),
                fk.referred_table.fullname,
                tuple(e.column.name for e in fk.elements),
                bool(fk.deferrable),
                fk.initially == "DEFERRED",
            )
            for fk in table.foreign_key_constraints
        }
        actual = {
            (
                d["name"],
                tuple(d["cols"]),
                d["remote_table"],
                tuple(d["remote_cols"]),
                d["condeferrable"],
                d["condeferred"],
            )
            for d in found
            if d["kind"] == "f"
        }
        check(actual == wanted, table.fullname, "foreign-key definitions differ")
        check(
            all(
                d["ondelete"] == d["onupdate"] == "r" and d["match"] == "s"
                for d in found
                if d["kind"] == "f"
            ),
            table.fullname,
            "referential actions differ",
        )
    return {
        "tables": len(Base.metadata.tables),
        "columns": sum(len(t.c) for t in TABLES.values()),
        "primary_keys": counts["p"],
        "foreign_keys": counts["f"],
        "additional_unique_constraints": counts["u"],
        "check_constraints": counts["c"],
        "explicit_indexes": sum(len(t.indexes) for t in TABLES.values()),
        "unique_partial_indexes": sum(
            bool(i.unique) and i.dialect_options["postgresql"].get("where") is not None
            for t in TABLES.values()
            for i in t.indexes
        ),
    }


async def audit_integrity(session, config: DatabaseConfig, plan: SeedPlan):
    """Inspect a caller-owned session without commits, DML or importer counters."""
    validate_target(config)
    connection = await session.connection()
    identity = await connection.run_sync(verify_schema, config)
    definitions = await connection.run_sync(verify_definitions)
    before = await fingerprint(session)
    results = {}
    compared = numeric_values = fk_checks = check_checks = unique_checks = 0
    for name, table in sorted(TABLES.items()):
        count = before["tables"][table.fullname]["rows"]
        distinct = await session.scalar(
            select(func.count(func.distinct(func.row(*table.primary_key)))).select_from(table)
        )
        check(count == distinct, table.fullname, "duplicate primary key")
        missing = or_(*(c.is_(None) for c in table.c if not c.nullable))
        nulls = await session.scalar(select(func.count()).select_from(table).where(missing))
        check(nulls == 0, table.fullname, "required field is NULL")
        for fk in table.foreign_key_constraints:
            child = table.alias("child")
            parent = fk.referred_table.alias("parent")
            present = and_(*(child.c[e.parent.name].is_not(None) for e in fk.elements))
            matches = (
                select(1)
                .select_from(parent)
                .where(
                    and_(*(parent.c[e.column.name] == child.c[e.parent.name] for e in fk.elements))
                )
                .correlate(child)
                .exists()
            )
            bad = await session.scalar(
                select(func.count()).select_from(child).where(present, ~matches)
            )
            check(bad == 0, table.fullname, "unresolved foreign-key tuple")
            fk_checks += 1
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint):
                bad = await session.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(text(f"({constraint.sqltext}) IS FALSE"))
                )
                check(bad == 0, table.fullname, "CHECK violation")
                check_checks += 1
        unique_sets = [
            (list(k.columns), None) for k in table.constraints if isinstance(k, UniqueConstraint)
        ]
        unique_sets += [
            (list(i.columns), i.dialect_options["postgresql"].get("where"))
            for i in table.indexes
            if i.unique
        ]
        for columns, predicate in unique_sets:
            query = select(*columns).where(and_(*(c.is_not(None) for c in columns)))
            if predicate is not None:
                query = query.where(predicate)
            duplicates = query.group_by(*columns).having(func.count() > 1).subquery()
            check(
                await session.scalar(select(func.count()).select_from(duplicates)) == 0,
                table.fullname,
                "unique tuple violation",
            )
            unique_checks += 1

        # Expected values use the audited, checksum-validated Component 5 mapping.
        # Actual values use independent SQL, exact JSON text and per-field comparisons.
        columns = [c.cast(Text).label(c.name) if isinstance(c.type, JSONB) else c for c in table.c]
        stored = [dict(r) for r in (await session.execute(select(*columns))).mappings()]
        for record in stored:
            for col in table.c:
                if isinstance(col.type, JSONB) and record[col.name] is not None:
                    record[col.name] = json_value(record[col.name], "stored JSON")
        key_columns = [c.name for c in table.primary_key]
        lookup = {tuple(r[c] for c in key_columns): r for r in stored}
        check(count == len(plan.rows[name]), table.fullname, "source count differs")
        for expected in plan.rows[name]:
            key = tuple(expected[c] for c in key_columns)
            check(key in lookup, table.fullname, "source identifier missing")
            record = lookup[key]
            for column, value in expected.items():
                check(
                    json_equal(record[column], value)
                    if isinstance(table.c[column].type, JSONB)
                    else record[column] == value,
                    f"{table.fullname}.{column}",
                    "source value differs",
                )
                compared += 1
                if isinstance(table.c[column].type, Numeric) and value is not None:
                    check(
                        isinstance(record[column], Decimal),
                        table.fullname,
                        "numeric is not Decimal",
                    )
                    numeric_values += 1
            if "registered_at" in record:
                check(
                    isinstance(record["registered_at"], datetime)
                    and record["registered_at"].tzinfo is not None,
                    table.fullname,
                    "invalid registration timestamp",
                )
        results[table.fullname] = {
            "expected": len(plan.rows[name]),
            "actual": count,
            "distinct_primary_keys": distinct,
            "null_violations": nulls,
            "status": "PASS",
        }
    snapshot_artifacts(plan)
    after = await fingerprint(session)
    check(before == after, "database", "verification changed the snapshot")
    return {
        "status": "PASS",
        **identity,
        "definitions": definitions,
        "records": sum(r["actual"] for r in results.values()),
        "populated_tables": sum(r["actual"] > 0 for r in results.values()),
        "empty_runtime_tables": [TABLES[n].fullname for n in sorted(EMPTY_TABLES)],
        "source_values_checked": compared,
        "decimal_values_checked": numeric_values,
        "foreign_key_data_checks": fk_checks,
        "check_data_checks": check_checks,
        "unique_data_checks": unique_checks,
        "violations": 0,
        "tables": results,
        "fingerprint": before,
        "preserved": before == after,
        "source_integrity": plan.bundle.integrity,
    }


async def run_integrity(config, plan):
    started = time.monotonic()
    validate_target(config)
    async with Database(config, read_only=True) as database:
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                result = await audit_integrity(session, config, plan)
    result["duration_seconds"] = round(time.monotonic() - started, 4)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify seeded PostgreSQL using SELECT-only checks."
    )
    parser.add_argument("--target", required=True, choices=["development", "test"])
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--snapshot-dir", required=True, type=Path)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    try:
        config = DatabaseConfig.from_env(args.target, env_file=args.env_file)
        plan = build_plan(load_sources(args.source, args.archive), args.snapshot_dir)
        print(json.dumps(asyncio.run(run_integrity(config, plan)), sort_keys=True))
        return 0
    except (
        IntegrityVerificationError,
        SeedError,
        DatabaseError,
        DatabaseConfigurationError,
    ) as error:
        # Do not render driver/local-variable/source payloads at the CLI boundary.
        print(json.dumps({"status": "FAIL", "error": str(error)}))
        return 1
    except OSError:
        print(
            json.dumps({"status": "FAIL", "error": "Required source/snapshot could not be read."})
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
