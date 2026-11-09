"""Live local-target, migration and complete managed-schema preflight."""

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import CheckConstraint, inspect, text

from rfq_copilot.migration_safety import (
    DATABASE_NAMES,
    MigrationSettings,
    include_name,
    owned_shape,
    shape_digest,
    verify_identity,
    verify_version_table,
)
from rfq_copilot.models import Base
from rfq_copilot.seeding import SeedError
from rfq_copilot.seeding.transform import require

ROOT = Path(__file__).resolve().parents[3]
REVISION = "20261109_01"
# Managed-object shape at REVISION: the approved initial revision (20261108_01, digest
# 8ef8e5a9...) plus the approved TD-005 capacity key. Measured on the guarded test database.
SHAPE_DIGEST = "6cc43f0ecb39614e6ee69d50e94c8916e77dc639ad6b1ae1a59c1315944d590d"


def validate_target(config):
    require(config.target in DATABASE_NAMES, "target", "unknown database target")
    require(
        config.url.database == DATABASE_NAMES[config.target],
        "target",
        "database name differs from approved target",
    )
    require(
        config.url.host in {"localhost", "127.0.0.1", "::1"} and config.url.port in {None, 5432},
        "target",
        "only approved local endpoint is allowed",
    )


def canonical(connection, expression, table):
    # EXPLAIN normalizes PostgreSQL casts/IN/ANY without executing the SELECT.
    result = connection.scalar(
        text(
            f"EXPLAIN (VERBOSE, FORMAT JSON, COSTS FALSE) SELECT ({expression}) FROM {table.fullname}"
        )
    )
    return result[0]["Plan"]["Output"][0]


def verify_live_identity(connection, config):
    try:
        return verify_identity(connection, MigrationSettings(config))
    except CommandError:
        raise SeedError("Database identity or migration safety verification failed.") from None


def verify_schema(connection, config):
    try:
        identity = verify_live_identity(connection, config)
        verify_version_table(connection)
        inspector = inspect(connection)
        require(
            inspector.has_table("alembic_version", schema="public"),
            "schema",
            "Alembic migration missing",
        )
        require(
            list(connection.scalars(text("SELECT version_num FROM public.alembic_version")))
            == [REVISION],
            "schema",
            "Alembic revision is not current",
        )
        require(
            ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads()
            == [REVISION],
            "schema",
            "unsupported Alembic head",
        )
        require(
            shape_digest(owned_shape(connection)) == SHAPE_DIGEST,
            "schema",
            "managed object shape differs",
        )
        context = MigrationContext.configure(
            connection,
            opts={
                "include_schemas": True,
                "include_name": include_name,
                "compare_type": True,
                "compare_server_default": True,
            },
        )
        require(
            not compare_metadata(context, Base.metadata), "schema", "Alembic schema drift detected"
        )
        for table in Base.metadata.tables.values():
            actual_checks = {
                c["name"]: c["sqltext"]
                for c in inspector.get_check_constraints(table.name, schema=table.schema)
            }
            for constraint in table.constraints:
                if isinstance(constraint, CheckConstraint):
                    require(
                        canonical(connection, constraint.sqltext, table)
                        == canonical(connection, actual_checks[str(constraint.name)], table),
                        table.fullname,
                        "CHECK definition differs",
                    )
            for column in inspector.get_columns(table.name, schema=table.schema):
                expected = table.c[column["name"]]
                if expected.computed:
                    actual = column.get("computed", {})
                    require(
                        actual.get("persisted") is True
                        and canonical(connection, expected.computed.sqltext, table)
                        == canonical(connection, actual["sqltext"], table),
                        table.fullname,
                        "generated expression differs",
                    )
            indexes = {i["name"]: i for i in inspector.get_indexes(table.name, schema=table.schema)}
            for index in table.indexes:
                expected = index.dialect_options["postgresql"].get("where")
                actual = indexes[str(index.name)].get("dialect_options", {}).get("postgresql_where")
                require(
                    (expected is None) == (actual is None),
                    table.fullname,
                    "index predicate differs",
                )
                if expected is not None:
                    require(
                        canonical(connection, expected, table)
                        == canonical(connection, actual, table),
                        table.fullname,
                        "index predicate differs",
                    )
        require(
            not connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_constraint k JOIN pg_namespace n ON n.oid=k.connamespace "
                    "WHERE n.nspname IN ('business','orchestration') AND (NOT k.convalidated OR NOT k.conenforced))"
                )
            ),
            "schema",
            "unenforced/unvalidated constraint",
        )
        require(
            not connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_index x JOIN pg_class c ON c.oid=x.indrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('business','orchestration') "
                    "AND (NOT x.indisvalid OR NOT x.indisready OR NOT x.indislive))"
                )
            ),
            "schema",
            "invalid index",
        )
        require(
            not connection.scalar(
                text(
                    "SELECT (SELECT count(*) FROM pg_trigger g JOIN pg_class c ON c.oid=g.tgrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('business','orchestration') AND NOT g.tgisinternal) "
                    "+ (SELECT count(*) FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('business','orchestration'))"
                )
            ),
            "schema",
            "unexpected triggers or policies",
        )
        return {
            "database": identity["database"],
            "role": identity["username"],
            "postgresql_version_num": identity["version"],
            "alembic_revision": REVISION,
            "schema_tables": 33,
        }
    except CommandError:
        raise SeedError("Database identity or migration safety verification failed.") from None
    except KeyError:
        raise SeedError("Managed schema definitions are incompatible.") from None
