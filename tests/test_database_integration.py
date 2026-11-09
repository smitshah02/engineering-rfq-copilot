"""Authenticated PostgreSQL checks; no tables, schema changes or business-row writes."""

import asyncio
import secrets
import socket
import traceback
from dataclasses import replace

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.database import Database, DatabaseConnectionError, DatabaseError, get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def session_state(session):
    return (
        (
            await session.execute(
                text("SELECT pg_backend_pid() AS pid, current_setting('application_name') AS name")
            )
        )
        .mappings()
        .one()
    )


async def change_session_name(session, name):
    await session.execute(
        text("SELECT set_config('application_name', :name, false)"), {"name": name}
    )


async def test_development_connection_is_read_only_and_identity_matches(development_config):
    async with Database(development_config, read_only=True) as database:
        info = await database.verify_connection()
        assert info.result == 1
        assert info.database == development_config.url.database
        assert info.username == development_config.url.username
        assert not info.superuser and not info.can_create_database
        async with database.session() as session:
            assert await session.scalar(text("SHOW transaction_read_only")) == "on"


async def test_test_connection_and_utc_settings(database, test_config):
    info = await database.verify_connection()
    assert info.result == 1 and info.database == test_config.expected_test_database
    assert info.username == test_config.url.username
    async with database.session() as session:
        assert await session.scalar(text("SHOW timezone")) == "UTC"
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"


async def test_session_creation_and_permanent_cleanup(database):
    async with database.session() as session:
        assert isinstance(session, AsyncSession)
        assert await session.scalar(text("SELECT 1")) == 1
    assert not session.in_transaction()
    assert database.engine.pool.checkedout() == 0
    with pytest.raises(InvalidRequestError, match="permanently closed"):
        await session.execute(text("SELECT 1"))


async def test_dependency_generator_closes_on_early_exit(database):
    dependency = get_session(database)
    session = await anext(dependency)
    assert await session.scalar(text("SELECT 1")) == 1
    await dependency.aclose()
    assert database.engine.pool.checkedout() == 0
    with pytest.raises(InvalidRequestError, match="permanently closed"):
        await session.execute(text("SELECT 1"))


async def test_normal_exit_rolls_back_uncommitted_transaction(database):
    async with database.session() as session:
        before = await session_state(session)
        await change_session_name(session, "uncommitted_component2_probe")
    async with database.session() as session:
        after = await session_state(session)
    assert after["pid"] == before["pid"]
    assert after["name"] == before["name"]


async def test_application_exception_rolls_back_and_propagates(database):
    async with database.session() as session:
        before = await session_state(session)
    with pytest.raises(RuntimeError, match="application exception"):
        async with database.session() as session:
            await change_session_name(session, "exception_component2_probe")
            raise RuntimeError("application exception")
    async with database.session() as session:
        after = await session_state(session)
    assert after["pid"] == before["pid"]
    assert after["name"] == before["name"]
    assert database.engine.pool.checkedout() == 0


async def test_explicit_commit_is_preserved_and_probe_is_restored(database):
    original = "rfq_copilot"
    try:
        async with database.session() as session:
            original = (await session_state(session))["name"]
            await change_session_name(session, "explicit_commit_component2_probe")
            await session.commit()
        async with database.session() as session:
            assert (await session_state(session))["name"] == "explicit_commit_component2_probe"
    finally:
        async with database.session() as session:
            await change_session_name(session, original)
            await session.commit()


async def test_sql_error_rolls_back_and_connection_can_be_reused(database):
    with pytest.raises(DatabaseError, match="Database operation failed"):
        async with database.session() as session:
            await session.execute(text("SELECT 1 / 0"))
    async with database.session() as session:
        assert await session.scalar(text("SELECT 1")) == 1
    assert database.engine.pool.checkedout() == 0


async def test_invalid_credentials_fail_without_leaking_secrets(test_config):
    wrong_password = secrets.token_urlsafe(32)
    config = replace(test_config, url=test_config.url.set(password=wrong_password))
    async with Database(config, read_only=True) as database:
        with pytest.raises(DatabaseConnectionError) as error:
            await database.verify_connection()
        rendered = "".join(traceback.format_exception(error.value))
        if wrong_password in rendered or "postgresql+asyncpg://" in rendered:
            pytest.fail("Connection failure exposed credential information", pytrace=False)


async def test_unavailable_server_returns_sanitized_connection_error(test_config):
    # A bound, non-listening socket prevents another process taking this ephemeral port.
    with socket.socket() as reserved_port:
        reserved_port.bind(("127.0.0.1", 0))
        port = reserved_port.getsockname()[1]
        config = replace(test_config, url=test_config.url.set(host="127.0.0.1", port=port))
        async with Database(config, read_only=True) as database:
            with pytest.raises(DatabaseConnectionError, match="verification failed"):
                await database.verify_connection()


async def test_development_and_test_connections_are_distinct(development_config, test_config):
    async with Database(development_config, read_only=True) as development:
        async with Database(test_config, read_only=True) as testing:
            dev_info, test_info = await asyncio.gather(
                development.verify_connection(), testing.verify_connection()
            )
    assert dev_info.database != test_info.database
    assert dev_info.database == development_config.url.database
    assert test_info.database == test_config.expected_test_database


async def test_concurrent_tasks_have_distinct_sessions_and_connections(database):
    both_ready = asyncio.Event()
    sessions = []
    backend_ids = []

    async def worker():
        async with database.session() as session:
            sessions.append(session)
            backend_ids.append(await session.scalar(text("SELECT pg_backend_pid()")))
            if len(backend_ids) == 2:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=5)

    await asyncio.gather(worker(), worker())
    assert sessions[0] is not sessions[1]
    assert len(set(backend_ids)) == 2
    assert database.engine.pool.checkedout() == 0


async def test_cancelled_query_releases_resources(database):
    query_started = asyncio.Event()

    def before_query(conn, cursor, statement, parameters, context, executemany):
        if "pg_sleep" in statement:
            query_started.set()

    event.listen(database.engine.sync_engine, "before_cursor_execute", before_query)

    async def worker():
        async with database.session() as session:
            await session.execute(text("SELECT pg_sleep(10)"))

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(query_started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert database.engine.pool.checkedout() == 0
        assert (await database.verify_connection()).result == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        event.remove(database.engine.sync_engine, "before_cursor_execute", before_query)


async def test_engine_disposal_closes_pool_and_prevents_reuse(test_config):
    database = Database(test_config, read_only=True)
    async with database.session() as session:
        old_pid = await session.scalar(text("SELECT pg_backend_pid()"))
    old_pool = database.engine.pool
    await database.dispose()
    assert old_pool.checkedout() == 0 and old_pool.checkedin() == 0
    assert database.engine.pool is not old_pool
    await database.dispose()
    with pytest.raises(DatabaseConnectionError):
        await database.verify_connection()
    async with Database(test_config, read_only=True) as other:
        # Client disposal can precede the server's removal of its backend statistics entry.
        # Each read owns a fresh transaction so the statistics snapshot can advance.
        async with asyncio.timeout(2):
            while True:
                async with other.session() as session:
                    remaining = await session.scalar(
                        text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": old_pid},
                    )
                if remaining == 0:
                    break
                await asyncio.sleep(0.02)
