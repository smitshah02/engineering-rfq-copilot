"""Explicit, local-only async Alembic environment reusing the existing database owner."""

import asyncio
from logging.config import fileConfig

from alembic.util import CommandError
from asyncpg import PostgresError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from alembic import context
from rfq_copilot.database import Database
from rfq_copilot.migration_safety import (
    LOCK_KEY,
    MigrationSettings,
    include_name,
    verify_identity,
    verify_version_table,
)
from rfq_copilot.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata


def do_run_migrations(connection, settings):
    verify_identity(connection, settings)
    verify_version_table(connection)
    if not connection.scalar(
        text("SELECT pg_try_advisory_lock(:a,:b)"), {"a": LOCK_KEY[0], "b": LOCK_KEY[1]}
    ):
        raise CommandError("Another RFQ migration command is active on this database.")
    # Preflight SELECTs autobegin in SQLAlchemy 2.x. End that read transaction so
    # Alembic owns (and commits/rolls back) the full transactional-DDL unit.
    connection.rollback()
    try:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=include_name,
            version_table="alembic_version",
            version_table_schema="public",
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if connection.in_transaction():
            connection.rollback()
        connection.execute(
            text("SELECT pg_advisory_unlock(:a,:b)"), {"a": LOCK_KEY[0], "b": LOCK_KEY[1]}
        )
        connection.rollback()


async def run_async_migrations(settings):
    async with Database(settings.database) as database:
        async with database.engine.connect() as connection:
            await connection.run_sync(do_run_migrations, settings)


if context.is_offline_mode():
    raise CommandError(
        "Offline migration execution is disabled: live identity/ownership guards are mandatory."
    )
else:
    settings = MigrationSettings.from_arguments(context.get_x_argument())
    try:
        asyncio.run(run_async_migrations(settings))
    except (SQLAlchemyError, PostgresError, OSError, TimeoutError) as error:
        code = getattr(error, "sqlstate", None) or getattr(
            getattr(error, "orig", None), "sqlstate", None
        )
        suffix = f" (SQLSTATE {code})" if code and str(code).isalnum() else ""
        raise CommandError(
            "Migration database operation failed; transaction rolled back" + suffix
        ) from None
