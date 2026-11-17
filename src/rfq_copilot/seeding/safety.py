"""Live local-target, migration and complete managed-schema preflight."""

from pathlib import Path

from alembic.autogenerate import compare_metadata
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
from rfq_copilot.schema_contract import CURRENT, active, contract_guards
from rfq_copilot.seeding import SeedError
from rfq_copilot.seeding.transform import require

ROOT = Path(__file__).resolve().parents[3]
# The active contract (schema_contract.active()) is what verify_schema enforces; these two
# names mirror it for callers and tests that pin them. History of the shape: 20261108_01
# (8ef8e5a9...), TD-005 20261109_01 (6cc43f0e...), TD-012 20261110_01 (3c9f9e44...), Phase
# 3.5 02/03/04/06 (51362661...), K2 20261115_01 (312f1338...), K3 20261116_01 (to be measured
# on the test database).
REVISION = CURRENT.revision
SHAPE_DIGEST = CURRENT.shape_digest
FUNCTION_CONFIG = ["search_path=pg_catalog, pg_temp"]
ROUTINES = (
    "SELECT p.proname, n.nspname, pg_get_userbyid(p.proowner), p.prosrc, p.prosecdef, l.lanname, "
    "p.prorettype::regtype::text, p.pronargs, p.proconfig, "
    "has_function_privilege('public', p.oid, 'EXECUTE') "
    "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
    "JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname IN ('business','orchestration')"
)
TRIGGERS = (
    "SELECT g.tgname, n.nspname, c.relname, p.proname, g.tgtype, g.tgenabled::text, g.tgnargs, "
    "g.tgqual IS NULL, g.tgattr::text, g.tgconstraint = 0, g.tgdeferrable, g.tginitdeferred "
    "FROM pg_trigger g JOIN pg_class c ON c.oid=g.tgrelid "
    "JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=g.tgfoid "
    "WHERE n.nspname IN ('business','orchestration') AND NOT g.tgisinternal"
)
POLICIES = (
    "SELECT count(*) FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid "
    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('business','orchestration')"
)


def trigger_type(events, kind: str = "before") -> int:
    """pg_trigger.tgtype of a FOR EACH ROW trigger on these events: BEFORE (bit 2) for
    ``before``, AFTER (no timing bit) for a ``deferred`` constraint trigger."""
    timing = 2 if kind == "before" else 0
    return 1 | timing | sum({"INSERT": 4, "DELETE": 8, "UPDATE": 16}[event] for event in events)


def expected_trigger(table: str, function: str, events, kind: str) -> tuple:
    """The catalog facts of one allowlisted trigger, in ``TRIGGERS`` column order: always
    enabled, unconditional, no arguments or column list; a plain trigger unless declared
    ``deferred`` (constraint trigger, DEFERRABLE INITIALLY DEFERRED)."""
    deferred = kind == "deferred"
    plain = not deferred
    return ("business", table, function, trigger_type(events, kind), "A", 0, True, "", plain, deferred, deferred)  # fmt: skip


def verify_history_guards(connection, contract, owner: str) -> None:
    """The managed schemas hold exactly the contract's K2 and K3 functions and triggers.

    Names, schema, owner, language, body, SECURITY INVOKER, fixed search_path, no PUBLIC
    EXECUTE; triggers by table, function, timing/events, enabled ALWAYS, unconditional, no
    arguments, no column list, and a constraint trigger (DEFERRABLE INITIALLY DEFERRED) only
    where a revision declares one ``deferred``. Anything missing, altered, disabled or
    additional fails, and row-level security policies are still not allowed. The expected
    definitions come from each revision file only after its own pinned SHA-256 matches.
    """
    try:
        functions, triggers = contract_guards(contract)
    except ValueError:
        raise SeedError("Guard definitions differ from their pinned digest.") from None
    # Each catalog row is (name, *facts); the facts must equal the expected tuple exactly.
    routines = {
        name: ("business", owner, body, False, "plpgsql", "trigger", 0, FUNCTION_CONFIG, False)
        for name, body in functions.items()
    }
    found = connection.execute(text(ROUTINES)).all()
    require(
        len(found) == len(routines) and all(tuple(r[1:]) == routines.get(r[0]) for r in found),
        "schema",
        "unexpected or altered routines",
    )
    wanted = {
        name: expected_trigger(table, function, events, kind)
        for name, table, function, events, kind in triggers
    }
    found = connection.execute(text(TRIGGERS)).all()
    require(
        len(found) == len(wanted) and all(tuple(t[1:]) == wanted.get(t[0]) for t in found),
        "schema",
        "unexpected, altered or disabled triggers",
    )
    require(not connection.scalar(text(POLICIES)), "schema", "unexpected policies")


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
    contract = active()
    metadata = contract.metadata()
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
            == [contract.revision],
            "schema",
            "Alembic revision is not current",
        )
        require(
            ScriptDirectory.from_config(contract.alembic_config()).get_heads()
            == [contract.revision],
            "schema",
            "unsupported Alembic head",
        )
        require(
            shape_digest(owned_shape(connection)) == contract.shape_digest,
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
        require(not compare_metadata(context, metadata), "schema", "Alembic schema drift detected")
        for table in metadata.tables.values():
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
        verify_history_guards(connection, contract, identity["username"])
        return {
            "database": identity["database"],
            "role": identity["username"],
            "postgresql_version_num": identity["version"],
            "alembic_revision": contract.revision,
            "schema_tables": 33,
        }
    except CommandError:
        raise SeedError("Database identity or migration safety verification failed.") from None
    except KeyError:
        raise SeedError("Managed schema definitions are incompatible.") from None
