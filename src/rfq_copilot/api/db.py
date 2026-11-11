"""Per-request database sessions for customer-scoped routes.

Reads and writes use separate ``Database`` objects. Read routes keep the read-only one
described below; only attachment uploads use ``get_write_session``.

- The session dependency depends on ``get_principal``, so an unauthenticated request is
  rejected before any database object is created or touched.
- The ``Database`` is either injected into ``create_app`` (caller-owned) or built lazily
  on first use from the process ``DATABASE_URL`` with ``read_only=True`` (PostgreSQL then
  refuses writes), owned by the application and disposed at shutdown.
- Each request gets its own ``AsyncSession`` from ``Database.session()``, which rolls
  back and closes it on success, error or cancellation. Nothing commits.
- Every read transaction starts with ``SET TRANSACTION READ ONLY`` (TD-009), issued from
  the session's ``after_begin`` event, so PostgreSQL refuses writes even when an injected
  ``Database`` was built writable. It runs only when the route queries (a request that
  fails validation still does no I/O); a failure is the same opaque 503.
- Database modules are imported lazily, so the application and its database-free
  endpoints start without them.
"""

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, HTTPException, Request

from rfq_copilot.api.auth import Principal, get_principal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from rfq_copilot.database import Database


def unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="Service unavailable.")


async def get_database(request: Request) -> "Database":
    state = request.app.state
    if state.database is None:
        async with state.database_lock:
            if state.database is None:
                from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
                from rfq_copilot.database import Database

                try:
                    config = DatabaseConfig.from_env("development")
                except DatabaseConfigurationError:
                    raise unavailable() from None
                state.database = Database(config, read_only=True)
                state.owns_database = True
    return state.database


async def get_read_session(
    principal: Annotated[Principal, Depends(get_principal)],
    database: Annotated[Any, Depends(get_database)],
) -> AsyncIterator["AsyncSession"]:
    from sqlalchemy import event

    from rfq_copilot.database import DatabaseError

    try:
        async with database.session() as session:
            event.listen(session.sync_session, "after_begin", _read_only_transaction)
            yield session
    except DatabaseError:
        # Database.session already replaced driver errors with a URL-free message; the
        # client gets neither that nor SQL, parameters or a traceback.
        raise unavailable() from None


def _read_only_transaction(session, transaction, connection) -> None:
    connection.exec_driver_sql("SET TRANSACTION READ ONLY")


async def get_write_database(request: Request) -> "Database":
    """A writable ``Database`` kept apart from the read-only one; built lazily, app-owned."""
    state = request.app.state
    if state.write_database is None:
        async with state.database_lock:
            if state.write_database is None:
                from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError
                from rfq_copilot.database import Database

                try:
                    config = DatabaseConfig.from_env("development")
                except DatabaseConfigurationError:
                    raise unavailable() from None
                state.write_database = Database(config)
                state.owns_write_database = True
    return state.write_database


async def get_write_session(
    principal: Annotated[Principal, Depends(get_principal)],
    database: Annotated[Any, Depends(get_write_database)],
) -> AsyncIterator["AsyncSession"]:
    """Like ``get_read_session`` but writable. The route commits; anything else rolls back."""
    from rfq_copilot.database import DatabaseError

    try:
        async with database.session() as session:
            yield session
    except DatabaseError:
        raise unavailable() from None
