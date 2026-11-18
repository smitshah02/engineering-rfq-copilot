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


class PrivilegedRuntimeRole(DatabaseError):
    """The session's role could rewrite history; binding work must not run on it."""


# The only columns the runtime role may UPDATE (K2/K4/K5, docs/database/k2_runtime_roles.sql):
# the Day 52 revision pointer, the superseded-version flag, submission (K4) and the K5
# clearing of a rejection on revision. Decisions are not here (K5: DECISION_UPDATABLE).
RUNTIME_UPDATABLE = frozenset(
    {
        "quotes.current_version",
        "quotes.status",
        "quotes.row_version",
        "quote_versions.is_current",
        "quotes.submitted_by",
        "quotes.rejection_reason",
    }
)
# K5 (Day 55): the NOLOGIN group whose members alone may record human decisions
# (docs/database/k5_decision_roles.sql), and what it may write. Bound by the k5_* triggers.
DECISION_GROUP = "rfq_quote_decision"
DECISION_UPDATABLE = frozenset(
    {
        "quotes.status",
        "quotes.row_version",
        "quotes.approved_version",
        "quotes.approver_id",
        "quotes.approval_timestamp",
        "quotes.rejection_reason",
        "quotes.released_by",
        "quotes.released_at",
        "quote_versions.approval_valid",
        "approval_requests.status",
        "approval_requests.resolved_by",
        "approval_requests.resolved_at",
    }
)
DECISION_INSERTABLE = frozenset({"approval_history", "audit_events", "idempotency_records"})
# The only tables the runtime role may INSERT into: the six K2 tables and, since K3, the
# calculation records (docs/database/k2_runtime_roles.sql). Since Milestone 2 Step 2 also
# customer RFQs and their lines (INSERT only: k2_*_immutable refuses UPDATE and DELETE).
# Rule versions and everything else are owner-written; an INSERT grant there is a violation.
RUNTIME_INSERTABLE = frozenset(
    {
        "rfq_attachments",
        "idempotency_records",
        "audit_events",
        "quotes",
        "quote_versions",
        "quote_line_items",
        "pricing_calculations",
        # K4 (Day 54D): one pending request per version and its append-only history.
        "approval_requests",
        "approval_history",
        # Milestone 2 Step 2: POST /api/v1/rfqs (rfq_submission).
        "rfqs",
        "rfq_line_items",
        # Checkpoint A1: the RFQ's queued processing request, in the same transaction.
        "workflow_runs",
    }
)
# Checkpoint A1/A3: the dedicated workflow worker role (docs/database/a1_workflow_role.sql).
# It claims, heartbeats and settles runs, and writes their audit events; nothing else. It
# cannot create RFQs, enqueue, or touch a quotation (no INSERT/UPDATE there at all).
WORKFLOW_UPDATABLE = frozenset(
    {
        "workflow_runs.status",
        "workflow_runs.lease_owner",
        "workflow_runs.lease_expires_at",
        "workflow_runs.attempt_count",
        "workflow_runs.claim_generation",
        "workflow_runs.disposition",
        "workflow_runs.last_error",
        "workflow_runs.external_workflow_id",
        "workflow_runs.updated_at",
    }
)
WORKFLOW_INSERTABLE = frozenset({"audit_events"})
# Checkpoint A4: the quotation-drafter role (docs/database/a4_drafter_role.sql). It inserts one
# new draft through quote_drafts.create_quote_draft (the quote, version 1, its lines, the K3
# calculation record, the receipt and the audit event) and settles the drafting phase of its
# claimed run. No UPDATE of any quotation, RFQ, approval or identity row; no decision group.
DRAFTER_UPDATABLE = frozenset(
    {
        "workflow_runs.status",
        "workflow_runs.lease_owner",
        "workflow_runs.lease_expires_at",
        "workflow_runs.claim_generation",
        "workflow_runs.draft_attempt_count",
        "workflow_runs.disposition",
        "workflow_runs.last_error",
        "workflow_runs.quote_id",
        "workflow_runs.updated_at",
    }
)
DRAFTER_INSERTABLE = frozenset(
    {
        "quotes",
        "quote_versions",
        "quote_line_items",
        "pricing_calculations",
        "idempotency_records",
        "audit_events",
    }
)
_RUNTIME_ROLE_CHECKS = text(
    "WITH me AS (SELECT r.* FROM pg_roles r WHERE r.rolname = current_user), "
    "managed AS (SELECT c.oid, c.relname, c.relowner FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname IN ('business','orchestration') AND c.relkind IN ('r','p')) "
    "SELECT ARRAY_REMOVE(ARRAY["
    "CASE WHEN me.rolsuper OR me.rolcreaterole OR me.rolcreatedb OR me.rolreplication "
    "OR me.rolbypassrls THEN 'role_attributes' END, "
    "CASE WHEN pg_has_role(current_user, d.datdba, 'MEMBER') THEN 'database_owner' END, "
    "CASE WHEN EXISTS (SELECT 1 FROM managed m WHERE pg_has_role(current_user, m.relowner, 'MEMBER')) "
    "THEN 'table_owner' END, "
    "CASE WHEN has_database_privilege(current_user, d.oid, 'CREATE') "
    "OR has_schema_privilege(current_user, 'business', 'CREATE') "
    "OR has_schema_privilege(current_user, 'orchestration', 'CREATE') "
    "OR has_schema_privilege(current_user, 'public', 'CREATE') THEN 'create' END, "
    "CASE WHEN EXISTS (SELECT 1 FROM managed m WHERE "
    "has_table_privilege(current_user, m.oid, 'DELETE') "
    "OR has_table_privilege(current_user, m.oid, 'TRUNCATE') "
    "OR has_table_privilege(current_user, m.oid, 'TRIGGER') "
    "OR has_table_privilege(current_user, m.oid, 'REFERENCES')) THEN 'table_privileges' END, "
    "CASE WHEN EXISTS (SELECT 1 FROM managed m JOIN pg_attribute a ON a.attrelid = m.oid "
    "WHERE a.attnum > 0 AND NOT a.attisdropped "
    "AND has_column_privilege(current_user, m.oid, a.attnum, 'UPDATE') "
    "AND m.relname || '.' || a.attname <> ALL (CAST(:updatable AS text[]))) "
    "THEN 'update_privileges' END, "
    "CASE WHEN EXISTS (SELECT 1 FROM managed m WHERE "
    "has_table_privilege(current_user, m.oid, 'INSERT') "
    "AND m.relname <> ALL (CAST(:insertable AS text[]))) THEN 'insert_privileges' END, "
    "CASE WHEN EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
    "WHERE n.nspname IN ('business','orchestration') "
    "AND has_function_privilege(current_user, p.oid, 'EXECUTE')) THEN 'execute' END, "
    "CASE WHEN has_parameter_privilege(current_user, 'session_replication_role', 'SET') "
    "THEN 'parameter_privileges' END, "
    # K5: decision sessions must be members of the decision group, all others must not.
    "CASE WHEN COALESCE(pg_has_role(current_user, to_regrole('rfq_quote_decision')::oid, "
    "'MEMBER'), false) <> CAST(:decision AS boolean) THEN 'decision_membership' END"
    "], NULL) FROM me, pg_database d WHERE d.datname = current_database()"
)


async def runtime_role_violations(
    session: AsyncSession, *, decision: bool = False, workflow: bool = False, drafter: bool = False
) -> tuple[str, ...]:
    """What makes the session's current role unfit to run binding work; () if restricted.

    Reads only catalogs and privilege functions of the live connection: role attributes,
    ownership or membership of the database or any managed table, CREATE anywhere it
    matters, table DELETE/TRUNCATE/TRIGGER/REFERENCES, UPDATE on any column outside
    ``RUNTIME_UPDATABLE``, INSERT on any table outside ``RUNTIME_INSERTABLE``, EXECUTE on
    the K2/K3 trigger functions, and SET on ``session_replication_role`` (which would
    silence non-ALWAYS triggers). K5: ``decision=True`` checks the decision role instead
    (``DECISION_UPDATABLE``/``DECISION_INSERTABLE``, member of ``DECISION_GROUP``); the
    ordinary runtime role must not be a member (``decision_membership``). A1:
    ``workflow=True`` checks the worker role (``WORKFLOW_UPDATABLE``/``WORKFLOW_INSERTABLE``,
    never a decision-group member).
    """
    if decision + workflow + drafter > 1:
        raise ValueError("Choose one restricted-role check.")
    updatable, insertable = (
        (DECISION_UPDATABLE, DECISION_INSERTABLE)
        if decision
        else (WORKFLOW_UPDATABLE, WORKFLOW_INSERTABLE)
        if workflow
        else (DRAFTER_UPDATABLE, DRAFTER_INSERTABLE)
        if drafter
        else (RUNTIME_UPDATABLE, RUNTIME_INSERTABLE)
    )
    found = await session.scalar(
        _RUNTIME_ROLE_CHECKS,
        {"updatable": sorted(updatable), "insertable": sorted(insertable), "decision": decision},
    )
    return tuple(found)


async def require_restricted_runtime(session: AsyncSession) -> None:
    """Fail closed before binding work (approval, rejection, release) on a privileged role.

    K2 triggers do not bind the table owner, who can disable them, so a binding workflow
    must call this first, inside its own transaction. Only a reason code leaves here.
    """
    violations = await runtime_role_violations(session)
    if violations:
        raise PrivilegedRuntimeRole(
            "Binding work refused: the database role is not the restricted runtime role ("
            + ", ".join(violations)
            + ")."
        )


async def require_decision_runtime(session: AsyncSession) -> None:
    """K5: fail closed before a human decision unless the session is the restricted decision
    role (member of ``DECISION_GROUP``, decision privileges only). Only codes leave here."""
    violations = await runtime_role_violations(session, decision=True)
    if violations:
        raise PrivilegedRuntimeRole(
            "Decision refused: the database role is not the restricted decision role ("
            + ", ".join(violations)
            + ")."
        )


async def require_workflow_runtime(session: AsyncSession) -> None:
    """A1: fail closed unless the session is the restricted workflow worker role."""
    violations = await runtime_role_violations(session, workflow=True)
    if violations:
        raise PrivilegedRuntimeRole(
            "Workflow refused: the database role is not the restricted workflow role ("
            + ", ".join(violations)
            + ")."
        )


async def require_drafter_runtime(session: AsyncSession) -> None:
    """A4: fail closed unless the session is the restricted quotation-drafter role (not the
    owner, the API role, the workflow worker or a decision-group member)."""
    violations = await runtime_role_violations(session, drafter=True)
    if violations:
        raise PrivilegedRuntimeRole(
            "Drafting refused: the database role is not the restricted drafter role ("
            + ", ".join(violations)
            + ")."
        )


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
                    # A URL/flag cannot prove restricted authority. Check before identity
                    # reads or route work, on every session, without leaving an implicit
                    # transaction that would break caller-owned begin()/snapshot setup.
                    config = self.config
                    if config.runtime or config.decision or config.workflow or config.drafter:
                        if config.decision:
                            await require_decision_runtime(session)
                        elif config.workflow:
                            await require_workflow_runtime(session)
                        elif config.drafter:
                            await require_drafter_runtime(session)
                        else:
                            await require_restricted_runtime(session)
                        await session.rollback()
                    yield session
                finally:
                    # A committed transaction is already complete; rollback then becomes a no-op.
                    # AsyncSession's context manager also closes the session if rollback raises.
                    await session.rollback()
        except _DRIVER_ERRORS:
            raise DatabaseError(
                "Database operation failed; check server availability and access."
            ) from None

    @asynccontextmanager
    async def snapshot_session(self) -> AsyncIterator[AsyncSession]:
        """A session whose one transaction is REPEATABLE READ and READ ONLY.

        Every statement of an evaluation then sees the same committed database state, and
        nothing can be written. Commit or roll back ends the snapshot; use one session per
        evaluation.
        """
        async with self.session() as session:
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            yield session

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
        config = DatabaseConfig.from_env(args.target, env_file=Path(args.env_file))
        async with Database(config, read_only=True) as database:
            print(json.dumps(asdict(await database.verify_connection()), sort_keys=True))

    try:
        asyncio.run(check())
    except (DatabaseConfigurationError, DatabaseError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
