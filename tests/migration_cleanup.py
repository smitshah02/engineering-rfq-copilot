"""Test-only removal of Alembic's empty version table after a guarded downgrade to base.

Alembic never drops ``public.alembic_version``; a disposable test database is "empty" only
when it is gone. ``drop_empty_version_table`` is called by the lifecycle fixtures only after
``command.downgrade(..., "base")`` returned, and it refuses (raising, changing nothing)
unless every condition holds:

- the connection carries the verified migration identity and test scope with explicit
  downgrade opt-in, on the guarded ``rfq_copilot_test`` database itself;
- the managed ``business``/``orchestration`` schemas are gone, so none of their relations,
  triggers or functions remain, and the version table holds zero rows
  (``migration_safety.ensure_initial_state``);
- the version table has Alembic's exact shape and the verified owner
  (``migration_safety.verify_version_table``);
- ``public`` holds nothing but that table and its primary-key index, and no trigger.

Only then is it dropped, ``RESTRICT`` (never CASCADE), by ``cleanup_test_tracker``, and
``public`` is verified empty. A failed downgrade never reaches this helper, so the database
is preserved for inspection. ``src/`` never imports this module.

Manual one-off (only after a successful guarded CLI ``downgrade base`` on the test DB):
    .venv/bin/python tests/migration_cleanup.py --confirm-database rfq_copilot_test
"""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import text

from rfq_copilot.migration_safety import cleanup_test_tracker

TEST_DATABASE = "rfq_copilot_test"
TRACKER = {("alembic_version", "r"), ("alembic_version_pkc", "i")}
LOCK = (461441, 505)  # the lifecycle lease lock: never concurrent with a lifecycle


def public_relations(connection) -> set[tuple[str, str]]:
    rows = connection.execute(
        text(
            "SELECT c.relname, c.relkind::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'"
        )
    )
    return {tuple(row) for row in rows}


def drop_empty_version_table(connection) -> None:
    """Drop the empty version table of a fully downgraded test database, or refuse."""
    identity = connection.info.get("rfq_migration_identity") or {}
    if identity.get("database") != TEST_DATABASE:
        raise AssertionError("Tracker cleanup is limited to the verified test database.")
    if connection.scalar(text("SELECT current_database()")) != TEST_DATABASE:
        raise AssertionError("Tracker cleanup is limited to the verified test database.")
    unexpected = public_relations(connection) - TRACKER
    if unexpected:
        raise AssertionError(f"Unexpected public relations; nothing removed: {sorted(unexpected)}")
    triggers = connection.scalar(
        text(
            "SELECT count(*) FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'"
        )
    )
    if triggers:
        raise AssertionError("Unexpected public triggers; nothing removed.")
    # Scope + opt-in, no managed schemas, zero version rows, exact shape and owner, RESTRICT.
    cleanup_test_tracker(connection)
    if public_relations(connection):
        raise AssertionError("Public objects remain after tracker cleanup.")


async def _main(argv=None) -> None:
    from rfq_copilot.config import DatabaseConfig
    from rfq_copilot.database import Database
    from rfq_copilot.migration_safety import MigrationSettings, verify_identity

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm-database", required=True)
    if parser.parse_args(argv).confirm_database != TEST_DATABASE:
        raise SystemExit(f"Refused: --confirm-database must be {TEST_DATABASE}.")
    root = Path(__file__).resolve().parents[1]
    config = DatabaseConfig.from_env("test", env_file=root / ".env")
    settings = MigrationSettings(config, allow_test_downgrade=True)
    async with Database(config) as database:
        async with database.engine.connect() as connection:
            await connection.run_sync(verify_identity, settings)
            if not await connection.scalar(text("SELECT pg_try_advisory_lock(:a, :b)"), dict(zip("ab", LOCK))):  # fmt: skip
                raise SystemExit("Refused: a migration lifecycle holds the test database.")
            try:
                await connection.run_sync(drop_empty_version_table)
                await connection.commit()
            finally:
                await connection.rollback()
                await connection.execute(text("SELECT pg_advisory_unlock(:a, :b)"), dict(zip("ab", LOCK)))  # fmt: skip
                await connection.rollback()
    print("Removed the empty public.alembic_version of rfq_copilot_test.")


if __name__ == "__main__":
    asyncio.run(_main())
