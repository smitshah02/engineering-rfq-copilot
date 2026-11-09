"""Async connection/session ownership, safe errors and a read-only connectivity command."""

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from asyncpg import PostgresError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from rfq_copilot.config import DatabaseConfig, DatabaseConfigurationError

_DRIVER_ERRORS = (SQLAlchemyError, PostgresError, OSError, TimeoutError)


class DatabaseError(RuntimeError):
    """A sanitized database failure, safe to report without the driver exception."""


class DatabaseConnectionError(DatabaseError):
    """Connection verification failed without exposing connection secrets."""


@dataclass(frozen=True)
class ConnectionInfo:
    result: int
    database: str
    username: str
    superuser: bool
    can_create_database: bool


class Database:
    """Own one async engine/factory per application event loop, never a shared session."""

    def __init__(self, config: DatabaseConfig, *, read_only: bool = False) -> None:
        self.config = config
        self._disposed = False
        server_settings = {"application_name": "rfq_copilot", "timezone": "UTC"}
        if read_only:
            server_settings["default_transaction_read_only"] = "on"
        self._engine = create_async_engine(
            config.url,
            echo=False,
            hide_parameters=True,
            pool_pre_ping=True,
            connect_args={
                "timeout": 5,
                "server_settings": server_settings,
            },
        )
        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            close_resets_only=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        """Infrastructure binding; do not log its URL or hand it to an agent."""
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a fresh session; roll back uncommitted work and always close it."""
        if self._disposed:
            raise DatabaseError("Database has been disposed.")
        try:
            async with self._session_factory() as session:
                try:
                    yield session
                finally:
                    # A committed transaction is already complete; rollback then becomes a no-op.
                    # AsyncSession's context manager also closes the session if rollback raises.
                    await session.rollback()
        except _DRIVER_ERRORS:
            raise DatabaseError(
                "Database operation failed; check server availability and access."
            ) from None

    async def verify_connection(self) -> ConnectionInfo:
        """SELECT 1 and verify database/user identity plus restricted role attributes."""
        try:
            async with self.session() as session:
                result = await session.execute(
                    text(
                        "SELECT 1 AS result, current_database() AS database, "
                        "current_user AS username, rolsuper AS superuser, "
                        "rolcreatedb AS can_create_database, rolcreaterole, "
                        "rolreplication, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
                row = result.mappings().one()
                if (
                    row["database"] != self.config.url.database
                    or row["username"] != self.config.url.username
                    or any(
                        row[key]
                        for key in [
                            "superuser",
                            "can_create_database",
                            "rolcreaterole",
                            "rolreplication",
                            "rolbypassrls",
                        ]
                    )
                ):
                    raise DatabaseConnectionError(
                        "Database identity or role privileges are unsafe."
                    )
                return ConnectionInfo(
                    result=row["result"],
                    database=row["database"],
                    username=row["username"],
                    superuser=row["superuser"],
                    can_create_database=row["can_create_database"],
                )
        except DatabaseError:
            raise DatabaseConnectionError("Database connection verification failed.") from None

    async def dispose(self) -> None:
        """Close the pool at shutdown, after owned session contexts have finished."""
        if self._disposed:
            return
        self._disposed = True
        try:
            await self._engine.dispose()
        except _DRIVER_ERRORS:
            self._disposed = False
            raise DatabaseError("Database shutdown failed.") from None

    async def __aenter__(self) -> "Database":
        if self._disposed:
            raise DatabaseError("Database has been disposed.")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.dispose()


async def get_session(database: Database) -> AsyncIterator[AsyncSession]:
    """Yield-style dependency for later application integration; no automatic commit."""
    async with database.session() as session:
        yield session


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify PostgreSQL without printing connection URLs."
    )
    parser.add_argument("--target", choices=["development", "test"], default="development")
    parser.add_argument("--env-file", type=Path, help="Explicit dotenv file; environment wins")
    args = parser.parse_args()

    async def check() -> None:
        config = DatabaseConfig.from_env(args.target, env_file=args.env_file)
        async with Database(config, read_only=True) as database:
            print(json.dumps(asdict(await database.verify_connection()), sort_keys=True))

    try:
        asyncio.run(check())
    except (DatabaseConfigurationError, DatabaseError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
