"""Local migration target/ownership guards; no business operations or credential logging."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from alembic.util import CommandError
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError

MANAGED_SCHEMAS = frozenset({"business", "orchestration"})
VERSION_SCHEMA = "public"
VERSION_TABLE = "alembic_version"
DATABASE_NAMES = {"development": "rfq_copilot_dev", "test": "rfq_copilot_test"}
LOCK_KEY = (461441, 504)


@dataclass(frozen=True)
class MigrationSettings:
    database: DatabaseConfig
    allow_test_downgrade: bool = False

    @classmethod
    def from_arguments(cls, arguments: list[str]) -> "MigrationSettings":
        values = {}
        for argument in arguments:
            key, sep, value = argument.partition("=")
            if (
                not sep
                or key not in {"target", "env_file", "allow_test_downgrade"}
                or key in values
            ):
                raise CommandError("Use unique target/env_file/allow_test_downgrade arguments.")
            values[key] = value
        target = values.get("target")
        if target not in DATABASE_NAMES:
            raise CommandError("An explicit target=development or target=test is required.")
        flag = values.get("allow_test_downgrade", "false")
        if flag not in {"true", "false"}:
            raise CommandError("allow_test_downgrade must be true or false.")
        if flag == "true" and target != "test":
            raise CommandError("Development downgrades are forbidden.")
        try:
            database = DatabaseConfig.from_env(
                target, env_file=Path(values["env_file"]) if "env_file" in values else None
            )
        except DatabaseConfigurationError as error:
            raise CommandError(str(error)) from None
        if database.url.database != DATABASE_NAMES[target]:
            raise CommandError(
                "Configured database is outside the approved local migration targets."
            )
        if database.url.host not in {"localhost", "127.0.0.1", "::1"} or database.url.port not in {
            None,
            5432,
        }:
            raise CommandError(
                "Migrations are restricted to the approved local PostgreSQL endpoint."
            )
        return cls(database, flag == "true")


def include_name(name, kind, parent_names):
    """Never autogenerate operations for unrelated schemas/tables; audits detect extras."""
    if kind == "schema":
        return name in MANAGED_SCHEMAS
    if kind == "table":
        from rfq_copilot.models import Base

        return f"{parent_names.get('schema_name')}.{name}" in Base.metadata.tables
    return True


def verify_identity(connection: Connection, settings: MigrationSettings) -> dict:
    row = (
        connection.execute(
            text(
                "SELECT current_database() AS database, current_user AS username, d.oid::bigint AS oid, "
                "pg_get_userbyid(d.datdba) AS owner, pg_encoding_to_char(d.encoding) AS encoding, "
                "host(inet_server_addr()) AS address, inet_server_port() AS port, "
                "current_setting('server_version_num')::int AS version, "
                "r.rolsuper,r.rolcreatedb,r.rolcreaterole,r.rolreplication,r.rolbypassrls "
                "FROM pg_database d JOIN pg_roles r ON r.rolname=current_user "
                "WHERE d.datname=current_database()"
            )
        )
        .mappings()
        .one()
    )
    if (
        row["database"] != DATABASE_NAMES[settings.database.target]
        or row["database"] != settings.database.url.database
        or row["username"] != settings.database.url.username
        or row["owner"] != row["username"]
        or row["encoding"] != "UTF8"
        or row["address"] not in {"127.0.0.1", "::1"}
        or row["port"] != 5432
        or row["version"] // 10000 != 18
        or any(
            row[key]
            for key in [
                "rolsuper",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
            ]
        )
    ):
        raise CommandError("Migration database identity, ownership, endpoint or role is unsafe.")
    connection.info["rfq_migration_settings"] = settings
    connection.info["rfq_migration_identity"] = dict(row)
    return dict(row)


def verify_version_table(connection: Connection) -> None:
    inspector = inspect(connection)
    if not inspector.has_table(VERSION_TABLE, schema=VERSION_SCHEMA):
        return
    cols = inspector.get_columns(VERSION_TABLE, schema=VERSION_SCHEMA)
    pk = inspector.get_pk_constraint(VERSION_TABLE, schema=VERSION_SCHEMA)
    if (
        len(cols) != 1
        or cols[0]["name"] != "version_num"
        or cols[0]["nullable"]
        or str(cols[0]["type"]) != "VARCHAR(32)"
        or pk["constrained_columns"] != ["version_num"]
    ):
        raise CommandError(
            "Unexpected Alembic version table shape; preserved without modification."
        )
    owner = connection.scalar(
        text(
            "SELECT pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relname='alembic_version'"
        )
    )
    if owner != connection.info["rfq_migration_identity"]["username"]:
        raise CommandError("Alembic version table has an unexpected owner.")


def _scope(connection: Connection) -> MigrationSettings:
    settings = connection.info.get("rfq_migration_settings")
    identity = connection.info.get("rfq_migration_identity")
    if not isinstance(settings, MigrationSettings) or not identity:
        raise CommandError(
            "Migration requires a live verified environment; direct execution refused."
        )
    return settings


def ensure_initial_state(connection: Connection) -> None:
    _scope(connection)
    existing = set(
        connection.scalars(
            text("SELECT nspname FROM pg_namespace WHERE nspname IN ('business','orchestration')")
        )
    )
    if existing:
        raise CommandError(
            "Managed schemas already exist; initial migration refused without changes."
        )
    if inspect(connection).has_table(VERSION_TABLE, schema=VERSION_SCHEMA):
        if connection.scalar(text("SELECT count(*) FROM public.alembic_version")):
            raise CommandError("Conflicting migration history; initial migration refused.")


def owned_shape(connection: Connection) -> dict:
    """Names/type/nullability fingerprint, excluding PostgreSQL 18 native NOT NULL records."""
    tables = connection.execute(
        text(
            "SELECT n.nspname,c.relname,c.relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname IN ('business','orchestration') AND c.relkind NOT IN ('i') ORDER BY 1,2,3"
        )
    ).all()
    columns = connection.execute(
        text(
            "SELECT n.nspname,c.relname,a.attname,a.attnotnull FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('business','orchestration') "
            "AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1,2,a.attnum"
        )
    ).all()
    constraints = connection.execute(
        text(
            "SELECT n.nspname,c.relname,k.conname,k.contype::text FROM pg_constraint k "
            "JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname IN ('business','orchestration') AND k.contype<>'n' ORDER BY 1,2,3"
        )
    ).all()
    indexes = connection.execute(
        text(
            "SELECT n.nspname,t.relname,i.relname FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid "
            "JOIN pg_class t ON t.oid=x.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace "
            "WHERE n.nspname IN ('business','orchestration') ORDER BY 1,2,3"
        )
    ).all()
    return {
        key: [list(row) for row in value]
        for key, value in [
            ("tables", tables),
            ("columns", columns),
            ("constraints", constraints),
            ("indexes", indexes),
        ]
    }


def shape_digest(shape: dict) -> str:
    return hashlib.sha256(
        json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def ensure_test_downgrade(connection: Connection, revision: str, fingerprint: str) -> None:
    settings = _scope(connection)
    identity = connection.info["rfq_migration_identity"]
    if (
        settings.database.target != "test"
        or not settings.allow_test_downgrade
        or identity["database"] != DATABASE_NAMES["test"]
    ):
        raise CommandError(
            "Downgrade requires the verified isolated test target and explicit opt-in."
        )
    # Bind the destructive operation to this actual database OID, not only a name.
    if (
        connection.scalar(
            text("SELECT oid::bigint FROM pg_database WHERE datname=current_database()")
        )
        != identity["oid"]
    ):
        raise CommandError("Database identity changed; downgrade refused.")
    if list(connection.scalars(text("SELECT version_num FROM public.alembic_version"))) != [
        revision
    ]:
        raise CommandError("Unexpected revision history; downgrade refused.")
    schemas = connection.execute(
        text(
            "SELECT nspname,pg_get_userbyid(nspowner),obj_description(oid,'pg_namespace') FROM pg_namespace "
            "WHERE nspname IN ('business','orchestration') ORDER BY nspname"
        )
    ).all()
    if len(schemas) != 2 or any(
        owner != identity["username"] or comment != f"rfq-copilot:{revision}"
        for _, owner, comment in schemas
    ):
        raise CommandError("Schema ownership/provenance does not match this migration.")
    if connection.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname IN ('business','orchestration') "
            "AND c.relowner<>(SELECT oid FROM pg_roles WHERE rolname=current_user))"
        )
    ):
        raise CommandError("Managed relations have an unexpected owner; downgrade refused.")
    if shape_digest(owned_shape(connection)) != fingerprint:
        raise CommandError(
            "Unexpected managed objects or shape; downgrade refused without removal."
        )
    extras = connection.scalar(
        text(
            "SELECT (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname IN ('business','orchestration')) + "
            "(SELECT count(*) FROM pg_trigger g JOIN pg_class c ON c.oid=g.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname IN ('business','orchestration') AND NOT g.tgisinternal) + "
            "(SELECT count(*) FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname IN ('business','orchestration')) + "
            "(SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
            "WHERE n.nspname IN ('business','orchestration') AND t.typtype IN ('d','e'))"
        )
    )
    if extras:
        raise CommandError("Unexpected routines/triggers/policies/types; downgrade refused.")
    for schema, table, _ in owned_shape(connection)["tables"]:
        qualified = (
            connection.dialect.identifier_preparer.quote_schema(schema)
            + "."
            + connection.dialect.identifier_preparer.quote(table)
        )
        if connection.scalar(text("SELECT EXISTS (SELECT 1 FROM " + qualified + " LIMIT 1)")):
            raise CommandError("Application records exist; empty-schema downgrade refused.")


def cleanup_test_tracker(connection: Connection) -> None:
    settings = _scope(connection)
    if settings.database.target != "test" or not settings.allow_test_downgrade:
        raise CommandError("Test tracker cleanup requires verified test scope and explicit opt-in.")
    ensure_initial_state(connection)
    verify_version_table(connection)
    if inspect(connection).has_table(VERSION_TABLE, schema=VERSION_SCHEMA):
        connection.execute(text("DROP TABLE public.alembic_version RESTRICT"))
