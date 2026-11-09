"""Real Alembic lifecycle and read-only catalog parity against independent Day 4 rules."""

import asyncio
import io
import json
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import inspect, text
from test_orm_models import (
    APPROVED_TABLES,
    expected_shared_checks,
)
from test_orm_models import (
    approved as approved,
)

from alembic import command
from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database
from rfq_copilot.migration_safety import (
    MigrationSettings,
    cleanup_test_tracker,
    ensure_initial_state,
    ensure_test_downgrade,
    include_name,
    owned_shape,
    verify_identity,
)
from rfq_copilot.models import Base

ROOT = Path(__file__).resolve().parents[1]
REVISION = "20261108_01"
SENTINEL = "rfq_migration_test_unrelated"


def alembic_config(target="test", *, allow=False):
    config = Config(str(ROOT / "alembic.ini"), stdout=io.StringIO())
    arguments = [f"target={target}", f"env_file={ROOT / '.env'}"]
    if allow:
        arguments.append("allow_test_downgrade=true")
    config.cmd_opts = SimpleNamespace(x=arguments)
    return config


def qualified(name):
    assert name in APPROVED_TABLES
    return ("orchestration" if name == "workflow_runs" else "business") + "." + name


def catalog(connection):
    """Read real definitions, including PG18 enforcement flags and supporting indexes."""
    constraints = (
        connection.execute(
            text(
                "SELECT n.nspname AS schema,t.relname AS tab,k.conname AS name,k.contype::text AS kind,"
                "ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY z(num,pos) "
                "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=z.num ORDER BY z.pos) AS cols,"
                "rn.nspname AS remote_schema,r.relname AS remote_table,"
                "ARRAY(SELECT a.attname FROM unnest(k.confkey) WITH ORDINALITY z(num,pos) "
                "JOIN pg_attribute a ON a.attrelid=r.oid AND a.attnum=z.num ORDER BY z.pos) AS remote_cols,"
                "k.confdeltype::text AS ondelete,k.confupdtype::text AS onupdate,"
                "k.confmatchtype::text AS match,k.condeferrable,k.condeferred,k.convalidated,k.conenforced,"
                "pg_get_expr(k.conbin,k.conrelid) AS expression "
                "FROM pg_constraint k JOIN pg_class t ON t.oid=k.conrelid "
                "JOIN pg_namespace n ON n.oid=t.relnamespace LEFT JOIN pg_class r ON r.oid=k.confrelid "
                "LEFT JOIN pg_namespace rn ON rn.oid=r.relnamespace "
                "WHERE n.nspname IN ('business','orchestration') ORDER BY 1,2,3"
            )
        )
        .mappings()
        .all()
    )
    indexes = (
        connection.execute(
            text(
                "SELECT n.nspname AS schema,t.relname AS tab,i.relname AS name,am.amname AS method,"
                "x.indisunique,x.indisvalid,x.indisready,x.indislive,k.conname AS supporting,"
                "ARRAY(SELECT a.attname FROM unnest(x.indkey) WITH ORDINALITY z(num,pos) "
                "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=z.num ORDER BY z.pos) AS cols,"
                "x.indoption::smallint[] AS options,x.indnkeyatts,x.indnatts,"
                "pg_get_expr(x.indpred,x.indrelid) AS predicate "
                "FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid "
                "JOIN pg_class t ON t.oid=x.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace "
                "JOIN pg_am am ON am.oid=i.relam LEFT JOIN pg_constraint k ON k.conindid=i.oid "
                "AND k.contype IN ('p','u') WHERE n.nspname IN ('business','orchestration') ORDER BY 1,2,3"
            )
        )
        .mappings()
        .all()
    )
    inspector = inspect(connection)
    columns = {}
    for table in APPROVED_TABLES:
        schema, name = qualified(table).split(".")
        columns[table] = inspector.get_columns(name, schema=schema)
        for column in columns[table]:
            # Reflected TypeEngine instances compare by identity. Persist their
            # exact PostgreSQL spelling for stable repeated-upgrade snapshots.
            column["type"] = column["type"].compile(dialect=connection.dialect)
    return {
        "shape": owned_shape(connection),
        "constraints": [dict(row) for row in constraints],
        "indexes": [dict(row) for row in indexes],
        "columns": columns,
        "revision": list(
            connection.scalars(text("SELECT version_num FROM public.alembic_version"))
        ),
    }


def canonical(connection, expression, table=None, datatype=None):
    """Ask PostgreSQL's parser/planner to normalize casts, IN/ANY and parentheses.

    EXPLAIN never executes the SELECT, and never changes schema/data. Both sides
    use the same typed table context; literals and boolean precedence are retained.
    """
    if datatype:
        expression = f"CAST(({expression}) AS {datatype})"
    suffix = f" FROM {qualified(table)}" if table else ""
    plan = connection.scalar(
        text(f"EXPLAIN (VERBOSE, FORMAT JSON, COSTS FALSE) SELECT ({expression}) AS value{suffix}")
    )
    if isinstance(plan, str):
        plan = json.loads(plan)
    return plan[0]["Plan"]["Output"][0]


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def probe_values(datatype, strings):
    if datatype == "TEXT":
        return [
            "NULL",
            *map(
                literal, dict.fromkeys(["probe", "", " ", "x" * 64, "x" * 65, "0" * 64, *strings])
            ),
        ]
    if datatype.startswith("NUMERIC"):
        return ["NULL", "1", "0", "-1", "0.0001", "0.9999", "2", "64", "65", "1000", "'NaN'"]
    if datatype in {"INTEGER", "BIGINT"}:
        return ["NULL", "1", "0", "-1", "2", "64", "65", "1000"]
    if datatype == "BOOLEAN":
        return ["NULL", "TRUE", "FALSE"]
    if datatype == "JSONB":
        return ["NULL", "'{}'", "'[]'", "'null'", "'true'", "'1'", "'\"probe\"'"]
    if datatype == "DATE":
        return ["NULL", "'2026-11-02'", "'2026-11-01'", "'2026-11-03'"]
    if datatype == "TIMESTAMPTZ":
        return [
            "NULL",
            "'2026-11-02T00:00:00Z'",
            "'2026-11-01T00:00:00Z'",
            "'2026-11-03T00:00:00Z'",
        ]
    assert datatype == "UUID", datatype
    return ["NULL", "'00000000-0000-4000-8000-000000000001'"]


def check_behavior(connection, expected, actual, columns):
    """Compare TRUE/FALSE/NULL and CHECK acceptance on typed, synthetic SELECT rows.

    No INSERT, seed, temporary table or dataset read is involved. Probe each
    referenced field independently and jointly at null/boundary/type/state cases.
    """
    refs = [col for col in columns if re.search(r"\b" + col["name"] + r"\b", expected)]
    assert refs, expected
    strings = re.findall(r"'((?:[^']|'')*)'", expected)
    values = [probe_values(col["type"], strings) for col in refs]
    baseline = [options[1] for options in values]
    samples = [baseline, ["NULL"] * len(refs)]
    for position, options in enumerate(values):
        for value in options:
            sample = baseline.copy()
            sample[position] = value
            samples.append(sample)
            sparse = ["NULL"] * len(refs)
            sparse[position] = value
            samples.append(sparse)
    for position in range(max(map(len, values))):
        samples.append([options[position % len(options)] for options in values])
    samples = list(dict.fromkeys(tuple(sample) for sample in samples))
    rows = []
    for sample in samples:
        rows.append(
            "("
            + ",".join(
                f"CAST({value} AS {col['type']})" for value, col in zip(sample, refs, strict=True)
            )
            + ")"
        )
    source = (
        "(VALUES " + ",".join(rows) + ") AS probe(" + ",".join(col["name"] for col in refs) + ")"
    )
    result = (
        connection.execute(
            text(
                f"SELECT count(*) AS probes, count(*) FILTER (WHERE ({expected}) IS DISTINCT FROM ({actual})) AS differences, "
                f"count(*) FILTER (WHERE ({actual}) IS FALSE) AS rejected, "
                f"count(*) FILTER (WHERE ({actual}) IS TRUE) AS true_cases, "
                f"count(*) FILTER (WHERE ({actual}) IS NOT FALSE) AS accepted FROM {source}"
            )
        )
        .mappings()
        .one()
    )
    assert result["differences"] == 0, expected
    # Every approved rule must demonstrate TRUE and FALSE, in addition to NULL probes.
    assert result["rejected"] > 0 and result["true_cases"] > 0, (expected, dict(result))
    return result["probes"]


def index_expectations():
    document = (ROOT / "docs/database/relationships_and_constraints.md").read_text()
    section = document.split("### 5.2 Additional indexes", 1)[1].split("## 6.", 1)[0]
    result = {}
    for line in section.splitlines():
        if not line.startswith(("| ix_", "| uq_")):
            continue
        name, table, columns, method, unique, predicate, *_ = [
            s.strip() for s in line.split("|")[1:-1]
        ]
        result[name] = dict(
            table=table,
            columns=columns.split(", "),
            method=method,
            unique=unique == "Yes",
            predicate=None if predicate == "—" else predicate,
        )
    assert len(result) == 14
    return result


def verify_physical(connection, approved, *, expect_empty=True):
    """Compare every approved object/definition with physical PostgreSQL catalogs."""
    result = catalog(connection)
    wanted_tables = sorted([*qualified(name).split("."), "r"] for name in approved)
    assert result["shape"]["tables"] == wanted_tables
    counts = Counter(row["kind"] for row in result["constraints"])
    assert {kind: counts[kind] for kind in "pfuc"} == {"p": 33, "f": 64, "u": 14, "c": 207}
    assert set(counts) <= set("pfucn"), counts
    assert sum(len(cols) for cols in result["columns"].values()) == 261
    assert result["revision"] == [REVISION]
    probes = 0
    for name, expected in approved.items():
        columns = result["columns"][name]
        assert [col["name"] for col in columns] == [col["name"] for col in expected["columns"]], (
            name
        )
        for col, wanted in zip(columns, expected["columns"], strict=True):
            kind = col["type"].replace(" ", "")
            assert kind == wanted["type"].replace(
                "TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE"
            ).replace(" ", ""), (name, col["name"], kind)
            assert col["nullable"] == wanted["nullable"], (name, col["name"])
            default = wanted["default"]
            if default.startswith("generated: "):
                assert col["computed"]["persisted"] is True
                assert canonical(connection, col["computed"]["sqltext"], name) == canonical(
                    connection, default.removeprefix("generated: "), name
                ), (name, col["name"])
            elif default in {"none", "application UUIDv4"}:
                assert col["default"] is None and "computed" not in col, (name, col["name"])
            else:
                assert col["default"] is not None
                assert canonical(connection, col["default"], datatype=wanted["type"]) == canonical(
                    connection, default, datatype=wanted["type"]
                ), (name, col["name"])
        constraints = [row for row in result["constraints"] if row["tab"] == name]
        assert all(row["convalidated"] and row["conenforced"] for row in constraints), name
        assert [tuple(row["cols"]) for row in constraints if row["kind"] == "p"] == [
            expected["pk"]
        ], name
        assert {tuple(row["cols"]) for row in constraints if row["kind"] == "u"} == set(
            expected["unique"]
        ), name
        foreign = [row for row in constraints if row["kind"] == "f"]
        assert {
            (
                tuple(row["cols"]),
                row["remote_table"],
                tuple(row["remote_cols"]),
                row["condeferrable"],
            )
            for row in foreign
        } == {
            (fk["local"], fk["target"], fk["remote"], fk["deferred"]) for fk in expected["fks"]
        }, name
        for fk in foreign:
            assert fk["remote_schema"] == qualified(fk["remote_table"]).split(".")[0]
            assert fk["ondelete"] == fk["onupdate"] == "r" and fk["match"] == "s"
            assert fk["condeferred"] == fk["condeferrable"]
        checks = [row for row in constraints if row["kind"] == "c"]
        found = {canonical(connection, row["expression"], name): row for row in checks}
        wanted_checks = expected["checks"] + expected_shared_checks(expected)
        wanted = {canonical(connection, sql, name): sql for sql in wanted_checks}
        assert len(checks) == len(found) == len(wanted) and found.keys() == wanted.keys(), (
            name,
            found.keys() - wanted.keys(),
            wanted.keys() - found.keys(),
        )
        for key, sql in wanted.items():
            probes += check_behavior(connection, sql, found[key]["expression"], expected["columns"])
        if expect_empty:
            assert not connection.scalar(
                text(f"SELECT EXISTS (SELECT 1 FROM {qualified(name)} LIMIT 1)")
            ), name
    explicit = {index["name"]: index for index in result["indexes"] if index["supporting"] is None}
    expected_indexes = index_expectations()
    assert explicit.keys() == expected_indexes.keys()
    assert len(result["indexes"]) == 61
    assert len([i for i in result["indexes"] if i["supporting"]]) == 47
    for name, expected in expected_indexes.items():
        index = explicit[name]
        assert index["tab"] == expected["table"]
        assert index["method"] == "btree" and expected["method"] == "B-tree"
        assert index["indisunique"] == expected["unique"]
        assert index["cols"] == [c.removesuffix(" DESC") for c in expected["columns"]], name
        assert index["options"] == [3 if c.endswith(" DESC") else 0 for c in expected["columns"]], (
            name
        )
        assert index["indnkeyatts"] == index["indnatts"] == len(index["cols"])
        if expected["predicate"] is None:
            assert index["predicate"] is None, name
        else:
            assert canonical(connection, index["predicate"], expected["table"]) == canonical(
                connection, expected["predicate"], expected["table"]
            ), name
    assert all(
        index["indisvalid"] and index["indisready"] and index["indislive"]
        for index in result["indexes"]
    )
    result["counts"] = dict(counts)
    result["probes"] = probes
    return result


def user_objects(connection):
    return {
        "schemas": [
            list(row)
            for row in connection.execute(
                text(
                    "SELECT nspname,pg_get_userbyid(nspowner),obj_description(oid,'pg_namespace') "
                    "FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname<>'information_schema' ORDER BY 1"
                )
            )
        ],
        "relations": [
            list(row)
            for row in connection.execute(
                text(
                    "SELECT n.nspname,c.relname,c.relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' ORDER BY 1,2,3"
                )
            )
        ],
    }


async def lifecycle(approved):
    settings = MigrationSettings.from_arguments(alembic_config(allow=True).cmd_opts.x)
    evidence = {"operations": []}

    async def run(action, *args, **kwargs):
        config = alembic_config(allow=action == "downgrade")
        await asyncio.to_thread(getattr(command, action), config, *args, **kwargs)
        evidence["operations"].append(action)

    async with Database(settings.database) as database:
        async with database.engine.connect() as connection:
            identity = await connection.run_sync(verify_identity, settings)
            assert identity["database"] == "rfq_copilot_test"
            # Distinct from the per-command lock: one complete lifecycle owns this lease.
            assert await connection.scalar(text("SELECT pg_try_advisory_lock(461441,505)"))
            await connection.rollback()
            original = await connection.run_sync(user_objects)
            assert original["relations"] == [], (
                "Test database contains existing objects; preserved."
            )
            assert [row[0] for row in original["schemas"]] == ["public"], (
                "Test schemas are not empty; preserved."
            )
            await connection.run_sync(ensure_initial_state)
            await connection.rollback()
            await connection.execute(text(f"CREATE SCHEMA {SENTINEL}"))
            await connection.commit()
            try:
                await run("upgrade", "head")
                first = await connection.run_sync(verify_physical, approved)
                await connection.rollback()
                await run("upgrade", "head")
                assert await connection.run_sync(catalog) == {
                    k: v for k, v in first.items() if k not in {"counts", "probes"}
                }
                await connection.rollback()
                await run("current", check_heads=True)
                await run("check")
                # Extra managed objects must block removal before any destructive DDL.
                await connection.execute(
                    text(
                        "ALTER TABLE business.customers ADD CONSTRAINT rfq_test_extra CHECK (TRUE)"
                    )
                )
                await connection.commit()
                with pytest.raises(CommandError, match="Unexpected managed objects"):
                    await asyncio.to_thread(command.downgrade, alembic_config(allow=True), "base")
                assert (
                    await connection.scalar(text("SELECT version_num FROM public.alembic_version"))
                    == REVISION
                )
                await connection.execute(
                    text("ALTER TABLE business.customers DROP CONSTRAINT rfq_test_extra")
                )
                await connection.commit()
                # A dependency outside managed schemas must cause a transactional rollback.
                await connection.execute(
                    text(
                        "CREATE VIEW public.rfq_test_dependency AS SELECT customer_id FROM business.customers"
                    )
                )
                await connection.commit()
                with pytest.raises(CommandError, match="transaction rolled back"):
                    await asyncio.to_thread(command.downgrade, alembic_config(allow=True), "base")
                assert (
                    await connection.scalar(text("SELECT version_num FROM public.alembic_version"))
                    == REVISION
                )
                assert await connection.run_sync(owned_shape) == first["shape"]
                await connection.execute(text("DROP VIEW public.rfq_test_dependency RESTRICT"))
                await connection.commit()
                await run("downgrade", "base")
                base = await connection.run_sync(user_objects)
                assert {row[0] for row in base["schemas"]} == {"public", SENTINEL}
                assert {tuple(row) for row in base["relations"]} == {
                    ("public", "alembic_version", "r"),
                    ("public", "alembic_version_pkc", "i"),
                }
                assert (
                    await connection.scalar(text("SELECT count(*) FROM public.alembic_version"))
                    == 0
                )
                await connection.rollback()
                await run("upgrade", "head")
                second = await connection.run_sync(verify_physical, approved)
                assert first == second
                await connection.rollback()
                await run("check")
                evidence.update(first=first, second=second, base=base, identity=identity)
            finally:
                await connection.rollback()
                # Never force cleanup. The revision must pass all live ownership/shape/data guards.
                if await connection.scalar(
                    text("SELECT to_regclass('business.customers') IS NOT NULL")
                ):
                    await connection.rollback()
                    await run("downgrade", "base")
                await connection.run_sync(cleanup_test_tracker)
                await connection.execute(text(f"DROP SCHEMA {SENTINEL} RESTRICT"))
                await connection.commit()
                restored = await connection.run_sync(user_objects)
                assert restored == original
                evidence.update(original=original, restored=restored)
                await connection.execute(text("SELECT pg_advisory_unlock(461441,505)"))
                await connection.rollback()
    return evidence


@pytest.fixture(scope="module")
def migration_lifecycle(approved):
    return asyncio.run(lifecycle(approved))


def test_configuration_metadata_and_revision():
    config = alembic_config()
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [REVISION]
    revisions = list(script.walk_revisions())
    assert len(revisions) == 1
    revision = revisions[0]
    assert revision.down_revision is None and revision.dependencies is None
    assert revision.doc == "initial_rfq_database_schema"
    assert callable(revision.module.upgrade) and callable(revision.module.downgrade)
    assert len(Base.metadata.tables) == 33
    assert config.get_main_option("sqlalchemy.url") is None


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["target=production"],
        ["target=test", "target=development"],
        ["target=test", "unknown=value"],
        ["target=test", "allow_test_downgrade=yes"],
        ["target=development", "allow_test_downgrade=true"],
        ["test"],
    ],
)
def test_reject_ambiguous_or_unsafe_targets(arguments):
    with pytest.raises(CommandError):
        MigrationSettings.from_arguments(arguments)


@pytest.mark.parametrize(
    "target,host,database",
    [
        ("test", "127.0.0.1", "rfq_copilot_dev"),
        ("development", "127.0.0.1", "production"),
        ("test", "remote.example.invalid", "rfq_copilot_test"),
        ("test", "127.0.0.1:5433", "rfq_copilot_test"),
    ],
)
def test_configuration_never_falls_back_and_errors_hide_password(
    monkeypatch, target, host, database
):
    password = "do-not-display-this-test-secret"
    monkeypatch.setenv("DATABASE_URL", f"postgresql+asyncpg://tester:{password}@{host}/{database}")
    monkeypatch.setenv(
        "TEST_DATABASE_URL", f"postgresql+asyncpg://tester:{password}@{host}/{database}"
    )
    monkeypatch.setenv("TEST_DATABASE_NAME", "rfq_copilot_test")
    with pytest.raises(CommandError) as error:
        MigrationSettings.from_arguments([f"target={target}"])
    assert password not in str(error.value)


@pytest.mark.parametrize(
    "schema,allowed",
    [
        (None, False),
        ("public", False),
        ("external", False),
        ("business", True),
        ("orchestration", True),
    ],
)
def test_schema_filter(schema, allowed):
    assert include_name(schema, "schema", {}) is allowed


def test_unknown_tables_are_preserved_by_autogeneration():
    assert include_name("customers", "table", {"schema_name": "business"})
    assert not include_name("unexpected", "table", {"schema_name": "business"})
    assert not include_name("customers", "table", {"schema_name": "public"})


def test_direct_revision_execution_refused():
    with pytest.raises(CommandError, match="live verified"):
        ensure_initial_state(SimpleNamespace(info={}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("database", "rfq_copilot_dev"),
        ("username", "other"),
        ("owner", "other"),
        ("encoding", "LATIN1"),
        ("address", "192.0.2.1"),
        ("port", 5433),
        ("version", 170000),
        ("rolsuper", True),
        ("rolcreatedb", True),
        ("rolcreaterole", True),
        ("rolreplication", True),
        ("rolbypassrls", True),
    ],
)
def test_live_identity_must_match_endpoint_owner_and_restricted_role(field, value):
    from sqlalchemy.engine import make_url

    config = DatabaseConfig(
        make_url("postgresql+asyncpg://tester:fake@127.0.0.1/rfq_copilot_test"),
        "test",
        "rfq_copilot_test",
        "rfq_copilot_dev",
    )
    row = dict(
        database="rfq_copilot_test",
        username="tester",
        owner="tester",
        encoding="UTF8",
        address="127.0.0.1",
        port=5432,
        version=180006,
        oid=1,
        rolsuper=False,
        rolcreatedb=False,
        rolcreaterole=False,
        rolreplication=False,
        rolbypassrls=False,
    )
    row[field] = value
    connection = Mock(info={})
    connection.execute.return_value.mappings.return_value.one.return_value = row
    with pytest.raises(CommandError, match="identity, ownership, endpoint or role"):
        verify_identity(connection, MigrationSettings(config))
    assert connection.info == {}


@pytest.mark.integration
def test_authentication_failure_does_not_expose_credentials(monkeypatch, caplog):
    password = "intentional-invalid-secret-for-migration-test"
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+asyncpg://rfq_migration_no_such_role:{password}@127.0.0.1/rfq_copilot_dev",
    )
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        f"postgresql+asyncpg://rfq_migration_no_such_role:{password}@127.0.0.1/rfq_copilot_test",
    )
    monkeypatch.setenv("TEST_DATABASE_NAME", "rfq_copilot_test")
    with pytest.raises(CommandError, match="Migration database operation failed") as error:
        command.current(alembic_config())
    assert password not in str(error.value) + caplog.text


@pytest.mark.parametrize(
    "target,opt_in", [("development", False), ("development", True), ("test", False)]
)
def test_downgrade_denied_before_database_operations(target, opt_in):
    config = DatabaseConfig.from_env(target, env_file=ROOT / ".env")
    connection = Mock(
        info={
            "rfq_migration_settings": MigrationSettings(config, opt_in),
            "rfq_migration_identity": {"database": config.url.database},
        }
    )
    with pytest.raises(CommandError, match="verified isolated test"):
        ensure_test_downgrade(connection, REVISION, "unused")
    connection.execute.assert_not_called()
    connection.scalar.assert_not_called()


@pytest.mark.integration
def test_real_upgrade_downgrade_reupgrade_and_cleanup(migration_lifecycle):
    result = migration_lifecycle
    assert result["first"] == result["second"]
    assert result["original"] == result["restored"]
    assert result["operations"] == [
        "upgrade",
        "upgrade",
        "current",
        "check",
        "downgrade",
        "upgrade",
        "check",
        "downgrade",
    ]


@pytest.mark.integration
@pytest.mark.parametrize("name", APPROVED_TABLES)
def test_each_physical_table_matches_day4(migration_lifecycle, approved, name):
    assert len(migration_lifecycle["first"]["columns"][name]) == len(approved[name]["columns"])


@pytest.mark.integration
def test_independent_checks_indexes_and_empty_data(migration_lifecycle):
    result = migration_lifecycle["first"]
    assert result["counts"]["c"] == 207
    assert result["probes"] > 2000
    assert sum(i["predicate"] is not None for i in result["indexes"]) == 7
    assert sum(i["predicate"] is not None and i["indisunique"] for i in result["indexes"]) == 3


@pytest.mark.integration
def test_development_physical_schema_read_only(approved):
    def data_fingerprint(connection):
        return {
            name: connection.scalar(
                text(
                    "SELECT md5(COALESCE(jsonb_agg(row_data ORDER BY row_data::text)::text,'[]')) "
                    f"FROM (SELECT to_jsonb(t) AS row_data FROM {qualified(name)} t) source"
                )
            )
            for name in APPROVED_TABLES
        }

    async def verify():
        settings = MigrationSettings.from_arguments(alembic_config("development").cmd_opts.x)
        async with Database(settings.database, read_only=True) as database:
            async with database.engine.connect() as connection:
                await connection.run_sync(verify_identity, settings)
                assert await connection.scalar(text("SHOW transaction_read_only")) == "on"
                before = await connection.run_sync(data_fingerprint)
                result = await connection.run_sync(
                    lambda sync: verify_physical(sync, approved, expect_empty=False)
                )
                assert await connection.run_sync(data_fingerprint) == before
                return result

    result = asyncio.run(verify())
    assert result["revision"] == [REVISION]
