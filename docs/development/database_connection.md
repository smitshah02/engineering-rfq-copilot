# Day 5, Component 2 — Asynchronous database connection layer

This guide describes the Component 2 boundary and its original verification. The completed Day 5 schema, seeding, queries and current test results are summarized in the [final verification report](day_05_final_verification_report.md).

This component implements connections and session ownership only. The approved [Day 4 architecture](../database/database_architecture.md), [table specifications](../database/table_schemas.md), [relationships](../database/relationships_and_constraints.md) and [source mapping](../database/dataset_to_database_mapping.md) remain unchanged. No ORM entities, application tables, migrations, seeding, MCP servers or FastAPI endpoints are introduced.

## Architecture decision

Use SQLAlchemy **2.x asynchronous APIs with asyncpg**, consistently across this layer. This fits the planned asynchronous FastAPI/MCP services and concurrent LangGraph tasks. A `Database` instance owns an `AsyncEngine` and an `async_sessionmaker` on one event loop. Each task/operation obtains a separate `AsyncSession`; sessions are never global or shared between parallel agents. See SQLAlchemy's [async lifecycle and concurrency guidance](https://docs.sqlalchemy.org/en/21/orm/extensions/asyncio.html).

`config.py` loads and validates explicit environment settings. `database.py` owns engine construction, sessions, a yield-style dependency, connection verification, disposal and a safe verification CLI. Importing the package opens no connection. The infrastructure is for backend services; agents receive no engine, session, SQL endpoint or credentials.

## Dependencies and package installation

The existing `uv` project and Python 3.12 virtual environment are retained. The lockfile fixes the actual resolved versions:

| Distribution | Version verified locally | Purpose |
| --- | --- | --- |
| SQLAlchemy | 2.1.8 | Async engine and session factory; declared with the asyncio extra |
| asyncpg | 0.32.0 | PostgreSQL async driver |
| python-dotenv | 1.2.4 | Explicit local environment-file parsing |
| greenlet | 3.5.6 | SQLAlchemy asyncio transitive dependency |
| pytest | 9.2.0 | Existing test runner, now declared in the dev group |
| pytest-asyncio | 1.4.1.post1 | Function-scoped async tests in strict mode |
| ruff | 0.17.4 | Existing lint/format tooling, preserved |

The `uv_build` backend makes the existing `src/rfq_copilot/` package importable through an editable installation. It defines no ORM models. No Alembic, FastAPI, Pydantic or unrelated runtime packages were added.

Reproduce the locked installation while retaining manually installed Day 3 dataset-validation packages:

```bash
uv sync --locked --inexact
```

`--inexact` deliberately preserves extra existing packages; ordinary exact synchronization can remove undeclared Day 3 tools. The original 17 installed distributions were checked for unchanged versions. See [uv synchronization options](https://docs.astral.sh/uv/reference/cli/#uv-sync).

## Environment configuration

Start with the safe placeholders in [`.env.example`](../../.env.example). Actual passwords belong only in local ignored files or a secret-injected process environment. Never put a password in a command argument, shell history, documentation, test output or chat. Do not print the real `.env` or a complete connection URL.

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL` | Development URL using `postgresql+asyncpg://` |
| `TEST_DATABASE_URL` | Separate test URL using the same async driver |
| `TEST_DATABASE_NAME` | Explicit expected database name ending in `_test` |

Enter the existing restricted-role credentials locally, using the secure terminal helper supplied for this setup or your local editor/password manager. Create `.env` only if it is absent; preserve any existing file. Set its permissions to `0600`. The helper percent-encodes username/password characters; when editing manually, encode characters such as `@`, `:`, `/`, `%` and `#` in the URL credential portions. Development and test URLs must identify different databases. The local resources from Component 1 are the development and testing databases; do not use the PostgreSQL administrator for the application connection.

There is no implicit dotenv search. `DatabaseConfig.from_env(..., env_file=Path('.env'))` reads only that explicit file, with interpolation disabled. Process variables override file values without mutating `os.environ`. With no file argument, only process variables are read. Missing configuration fails explicitly. An explicitly requested missing file also fails.

The URL must specify the asyncpg PostgreSQL driver, role, password, host and database. URL query options are rejected in this initial local layer; connection timeout and read-only behavior use typed engine options. The connection timeout is five seconds; standard SQLAlchemy pool limits remain unchanged. Configuration repr/str omit the URL entirely.

For `target='test'`, both URL variables are validated, the test URL must match `TEST_DATABASE_NAME`, that name must end in `_test`, and its database must differ from the development database even if the host differs. Do not bypass those checks for an integration run. `.gitignore` already excludes `.env` and `.env.*`, while allowing the placeholder `.env.example`; it was not changed.

## Read-only connection verification

From the repository root:

```bash
.venv/bin/python -m rfq_copilot.database --env-file .env --target development
.venv/bin/python -m rfq_copilot.database --env-file .env --target test
```

Both commands use `Database(..., read_only=True)`. They execute `SELECT 1` with current database/user and role-attribute checks. The JSON result contains only the integer result, database name, username, superuser flag and CREATEDB flag. It contains no password or URL. A role with superuser/CREATEDB/CREATEROLE/replication/BYPASSRLS privilege fails verification. Nonzero exit status means verification failed; sanitized errors go to stderr.

Connection defaults include UTC, application_name=rfq_copilot, parameter hiding, disabled SQL echo and pool_pre_ping. The latter checks idle connections; this layer does not replay application commands after failure. Production TLS/remote transport configuration is outside this local-only component and must be designed explicitly when needed.

## Session and transaction lifecycle

```python
from pathlib import Path

from sqlalchemy import text

from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database


async def check_backend_connection():
    config = DatabaseConfig.from_env(env_file=Path('.env'))
    async with Database(config) as database:
        await database.verify_connection()  # Check at startup before accepting work.
        async with database.session() as session:
            result = await session.scalar(text('SELECT 1'))
            return result
```

- `database.session()` creates a new session, with autoflush disabled and expire_on_commit=False. SQLAlchemy can begin a transaction automatically on first use, but this helper **never commits**.
- Normal exit rolls back unfinished work. Exceptions also trigger rollback and closure. A business exception propagates; database/driver failures become sanitized `DatabaseError` exceptions.
- Call `await session.commit()` only after the owning business service intentionally completes its transaction. Explicitly entering SQLAlchemy's `session.begin()` context is another caller-owned commit boundary. Already committed work cannot be undone by a later rollback; split commits must not be mistaken for an atomic business operation.
- Closed sessions are permanently closed (`close_resets_only=False`); obtain a new scope instead of reusing one.
- `get_session(database)` provides a yield-style async dependency for a later framework adapter. Framework/manual consumers must close the generator on early exit; the tests exercise this. No FastAPI dependency or endpoint is implemented today.
- Wait for all owned session/task scopes to finish before `await database.dispose()`. `async with Database(...)` does this at outer-scope exit. Disposal is idempotent; the wrapper rejects new sessions after disposal. Do not keep an engine alive across different asyncio event loops. SQLAlchemy disposal does not forcibly reclaim a caller's still-checked-out connection.

`read_only=True` sets PostgreSQL default_transaction_read_only on each new connection. Development verification CLIs and development test connections use this protection. Guarded migration, seeding and constraint fixtures use writable connections only to the isolated test database. Backend application engines default to writable transactions, with explicit caller-owned commits; authentication/authorization and business state transitions still belong to later services, not this generic connection layer.

## Errors and credential handling

`DatabaseConfigurationError` identifies missing/invalid settings without echoing the input. `DatabaseError` wraps database operations and `DatabaseConnectionError` wraps verification failures. Their rendered tracebacks suppress the original driver exception chain. The layer writes no driver diagnostics, connection URLs or SQL parameters to logs; callers must not log `config.url`, engine URLs, local variables or private exception context. Normal application exceptions are not rewritten.

Connection credentials stay only in the local environment and driver memory. Tests use fake unit-test values or a temporary deliberately incorrect password for the negative-authentication check; they never alter the actual PostgreSQL role password. No automated credential generation, role reset or database deletion is implemented.

## Test execution and safety

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m pytest -m 'not integration' -q
.venv/bin/python -m pytest -v
```

The default pytest discovery root is `tests/`; frozen dataset tests/generators are never included. Integration tests are marked `integration` and require real configured PostgreSQL credentials. Missing credentials fail the required run rather than being skipped. A unit-only selection deliberately deselects integration cases and is not sufficient to claim completion.

The integration suite checks development/test identity, UTC and read-only mode; session creation/closure; early dependency cleanup; normal/exception rollback; explicit commit; recovery after invalid SQL; invalid credentials; unavailable server; database and concurrent-session isolation; cancellation cleanup; and actual pool/backend disposal. The development database is used only for read-only connection/identity checks. All other database tests use the explicitly guarded isolated test target.

Transaction tests use PostgreSQL's transactional `application_name` session setting as observable state. They commit/roll back that connection-local setting and restore it where necessary; they create no tables and write no business rows. Negative connection tests reserve a non-listening local port or supply an intentionally incorrect password. Cancellation uses pg_sleep, then checks pool recovery. The disposal test also confirms the previous backend PID has disconnected.

See [Day 5 log](../dev_log/day_05.md) for exact commands, exit codes, each test result and the final observed database/repository checks. These tests validate this component, not future ORM mappings, migrations, business authorization or agent behavior.
