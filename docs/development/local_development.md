# Local development, database administration and integrity

Start with the [README](../../README.md#local-setup) and placeholder-only
[environment template](../../.env.example). Use Python 3.12, uv, PostgreSQL 18 on loopback,
a supported Node.js version, private dataset v1.1.0 and an installed local Ollama model.
An existing demonstrated environment should reuse its approved identities, keys and state.

## Provisioning and startup sequence

Dependency installation does not provision a database. A fresh environment needs reviewed
active migrations through `20261118_01`, baseline import, pinned rule/permission registration,
restricted app/decision/worker/drafter roles, approved customer/staff/service bindings and
explicit queue activation. Follow the existing guarded operator CLIs; no startup command
creates those authorities. API policy v3 uses the registered RBAC v4 superset for customer
and workflow identities. Deferred multi-customer migrations remain outside active startup.

Use `DATABASE_URL` only for administration, `APP_DATABASE_URL` for ordinary API/MCP,
`DECISION_DATABASE_URL` for human decisions, `WORKFLOW_DATABASE_URL` for evaluation and
`DRAFTER_DATABASE_URL` for drafting. The corresponding `TEST_*` values are isolated test
credentials. Never export a shared owner/decision environment to every process.

For process-specific loading, all service commands, local model trust, frontend proxying
and safe shutdown, use [background processing](background_processing.md). Authentication
and decision rules are in [authentication](authentication.md); routes and input contracts
are in [REST APIs](database_rest_apis.md). Verification scope is in [testing](testing.md).

The following sections retain detailed connection, migration, import and integrity operating
contracts. Baseline table/count discussions describe frozen import state, not current runtime
records. Ordinary serving selects only its restricted credential and target metadata.



## Database connections and session ownership

This guide explains the asynchronous connection foundation. For the completed backend and current setup, see the [project overview](../../README.md#local-setup). Database-owner, restricted-runtime and human-decision credentials have distinct authority; see the [K2](../database/k2_runtime_roles.sql) and [K5](../database/k5_decision_roles.sql) provisioning artifacts. Foundation-specific counts and examples below describe their original scope, not the current schema or a new verification run.

This document covers connections and session ownership. The approved [database design architecture](../database/database_architecture.md), [table specifications](../database/table_schemas.md), [relationships](../database/relationships_and_constraints.md) and [source mapping](../database/dataset_to_database_mapping.md) provide complementary schema references. The sections below cover ORM migration, seeding and serving boundaries.

<a id="database-connection-architecture-decision"></a>
### Architecture decision

Use SQLAlchemy **2.x asynchronous APIs with asyncpg**, consistently across this layer. This fits the implemented asynchronous FastAPI/MCP services and concurrent LangGraph tasks. A `Database` instance owns an `AsyncEngine` and an `async_sessionmaker` on one event loop. Each task/operation obtains a separate `AsyncSession`; sessions are never global or shared between parallel agents. See SQLAlchemy's [async lifecycle and concurrency guidance](https://docs.sqlalchemy.org/en/21/orm/extensions/asyncio.html).

`config.py` loads and validates explicit environment settings. `database.py` owns engine construction, sessions, a yield-style dependency, connection verification, disposal and a safe verification CLI. Importing the package opens no connection. The infrastructure is for backend services; agents receive no engine, session, SQL endpoint or credentials.

<a id="database-connection-dependencies-and-package-installation"></a>
### Dependencies and package installation

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

Reproduce the locked installation while retaining manually installed dataset validation dataset-validation packages:

```bash
uv sync --locked --inexact
```

`--inexact` deliberately preserves extra existing packages; ordinary exact synchronization can remove undeclared dataset validation tools. See [uv synchronization options](https://docs.astral.sh/uv/reference/cli/#uv-sync).

<a id="database-connection-environment-configuration"></a>
### Environment configuration

Start with the safe placeholders in [`.env.example`](../../.env.example). Actual passwords belong only in local ignored files or a secret-injected process environment. Never put a password in a command argument, shell history, documentation, test output or chat. Do not print the real `.env` or a complete connection URL.

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL` | Development owner URL using `postgresql+asyncpg://` |
| `TEST_DATABASE_URL` | Separate test URL using the same async driver |
| `TEST_DATABASE_NAME` | Explicit expected database name ending in `_test` |
| `APP_DATABASE_URL`, `TEST_APP_DATABASE_URL` | Restricted runtime role on the matching database |
| `DECISION_DATABASE_URL`, `TEST_DECISION_DATABASE_URL` | Separate K5 role, supplied only to human-decision execution |

Enter the intended role credentials privately using your editor or password manager; do not confuse owner credentials with the restricted application or decision roles. Create `.env` only if it is absent; preserve any existing file. Set its permissions to `0600`. The helper percent-encodes username/password characters; when editing manually, encode characters such as `@`, `:`, `/`, `%` and `#` in the URL credential portions. Development and test URLs must identify different databases. The local resources from Component 1 are the development and testing databases; do not use the PostgreSQL administrator for the application connection.

There is no implicit dotenv search. `DatabaseConfig.from_env(..., env_file=Path('.env'))` reads only that explicit file, with interpolation disabled. Process variables override file values without mutating `os.environ`. With no file argument, only process variables are read. Missing configuration fails explicitly. An explicitly requested missing file also fails.

The URL must specify the asyncpg PostgreSQL driver, role, password, host and database. URL query options are rejected in this initial local layer; connection timeout and read-only behavior use typed engine options. The connection timeout is five seconds; standard SQLAlchemy pool limits remain unchanged. Configuration repr/str omit the URL entirely.

For `target='test'`, both URL variables are validated, the test URL must match `TEST_DATABASE_NAME`, that name must end in `_test`, and its database must differ from the development database even if the host differs. Do not bypass those checks for an integration run. `.gitignore` already excludes `.env` and `.env.*`, while allowing the placeholder `.env.example`; it was not changed.

<a id="database-connection-read-only-connection-verification"></a>
### Read-only connection verification

From the repository root:

```bash
.venv/bin/python -m rfq_copilot.database --env-file .env --target development
.venv/bin/python -m rfq_copilot.database --env-file .env --target test
```

Both commands use `Database(..., read_only=True)`. They execute `SELECT 1` with current database/user and role-attribute checks. The JSON result contains only the integer result, database name, username, superuser flag and CREATEDB flag. It contains no password or URL. A role with superuser/CREATEDB/CREATEROLE/replication/BYPASSRLS privilege fails verification. Nonzero exit status means verification failed; sanitized errors go to stderr.

Connection defaults include UTC, application_name=rfq_copilot, parameter hiding, disabled SQL echo and pool_pre_ping. The latter checks idle connections; this layer does not replay application commands after failure. Production TLS/remote transport configuration is outside this local-only component and must be designed explicitly when needed.

<a id="database-connection-session-and-transaction-lifecycle"></a>
### Session and transaction lifecycle

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
- `get_session(database)` provides a yield-style async dependency for a later framework adapter. Framework/manual consumers must close the generator on early exit; the tests exercise this. The current FastAPI session adapters are in `src/rfq_copilot/api/db.py`.
- Wait for all owned session/task scopes to finish before `await database.dispose()`. `async with Database(...)` does this at outer-scope exit. Disposal is idempotent; the wrapper rejects new sessions after disposal. Do not keep an engine alive across different asyncio event loops. SQLAlchemy disposal does not forcibly reclaim a caller's still-checked-out connection.

`read_only=True` sets PostgreSQL default_transaction_read_only on each new connection. Development verification CLIs and development test connections use this protection. Guarded migration, seeding and constraint fixtures use writable connections only to the isolated test database. Backend application engines default to writable transactions, with explicit caller-owned commits; authentication/authorization and business state transitions still belong to later services, not this generic connection layer.

<a id="database-connection-errors-and-credential-handling"></a>
### Errors and credential handling

`DatabaseConfigurationError` identifies missing/invalid settings without echoing the input. `DatabaseError` wraps database operations and `DatabaseConnectionError` wraps verification failures. Their rendered tracebacks suppress the original driver exception chain. The layer writes no driver diagnostics, connection URLs or SQL parameters to logs; callers must not log `config.url`, engine URLs, local variables or private exception context. Normal application exceptions are not rewritten.

Connection credentials stay only in the local environment and driver memory. Tests use fake unit-test values or a temporary deliberately incorrect password for the negative-authentication check; they never alter the actual PostgreSQL role password. No automated credential generation, role reset or database deletion is implemented.

<a id="database-connection-test-execution-and-safety"></a>
### Test execution and safety

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m pytest -m 'not integration' -q
.venv/bin/python -m pytest -v
```

The default pytest discovery root is `tests/`; frozen dataset tests/generators are never included. Integration tests are marked `integration` and require real configured PostgreSQL credentials. Missing credentials fail the required run rather than being skipped. A unit-only selection deliberately deselects integration cases and is not sufficient to claim completion.

The integration suite checks development/test identity, UTC and read-only mode; session creation/closure; early dependency cleanup; normal/exception rollback; explicit commit; recovery after invalid SQL; invalid credentials; unavailable server; database and concurrent-session isolation; cancellation cleanup; and actual pool/backend disposal. The development database is used only for read-only connection/identity checks. All other database tests use the explicitly guarded isolated test target.

Transaction tests use PostgreSQL's transactional `application_name` session setting as observable state. They commit/roll back that connection-local setting and restore it where necessary; they create no tables and write no business rows. Negative connection tests reserve a non-listening local port or supply an intentionally incorrect password. Cancellation uses pg_sleep, then checks pool recovery. The disposal test also confirms the previous backend PID has disconnected.

See [testing](testing.md) for the current verification selections and [limitations](technical_debt.md) for operational boundaries.


## Database migrations

The active development contract is CURRENT (`20261118_01`), defined by [`schema_contract.py`](../../src/rfq_copilot/schema_contract.py). K2/K3/K4/K5 history and decision guards extend the initial schema. Proposed multi-customer revisions remain [deferred](../../alembic/proposed/README.md). The initial-schema design and verification method below are foundation references; their original counts are not the current catalog. See the [project setup](../../README.md#local-setup) and [verification scope](../../README.md#verification).

<a id="database-migrations-architecture-and-files"></a>
### Architecture and files

```text
alembic.ini
alembic/
    env.py
    script.py.mako
    versions/20261108_01_initial_rfq_database_schema.py
    versions/...  # subsequent active revisions through K5
    proposed/README.md
    proposed/deferred/
src/rfq_copilot/migration_safety.py
tests/test_database_migrations.py
```

The documented [Alembic async approach](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic) uses an async connection's `run_sync()` to run the synchronous Alembic migration context. `env.py` imports the existing `rfq_copilot.models.Base.metadata`; importing the models registers the current mappings on that single registry. It obtains an engine through the existing `Database` owner and `DatabaseConfig` validation. It creates no additional Base, global application engine or session factory. Each command disposes its temporary engine when its async context exits, including failures.

Identity/version-table preflight SELECTs start a SQLAlchemy transaction. The environment explicitly ends that read transaction before Alembic starts its transactional DDL, so a successful migration commits and an unsuccessful one rolls back completely. A session advisory lock `(461441, 504)` rejects concurrent migration commands for the same database and is released in `finally`.

`alembic.ini` contains paths and logging configuration, with no `sqlalchemy.url` or credentials. Paths resolve relative to the configuration file. Schema-aware autogeneration uses `include_schemas=True`, type/default comparison and `include_name`: reflection manages only the approved tables in `business` and `orchestration`. Unknown tables and other schemas are never proposed for automatic removal. Independent catalog checks detect unexpected managed objects; filtering is not a substitute for an audit.

<a id="database-migrations-explicit-targets-and-credentials"></a>
### Explicit targets and credentials

Run commands from the repository root with `.venv/bin/python`. Every online command requires exactly one `-x target=development` or `-x target=test`. There is no default target. Only the approved local databases `rfq_copilot_dev` and `rfq_copilot_test` on loopback port 5432 are allowed. Test configuration still requires a matching explicit `TEST_DATABASE_NAME` distinct from development. Duplicate, unknown or ambiguous arguments fail before connecting.

Add `-x env_file=.env` to read the existing private, ignored environment file explicitly. Without it, only process environment variables are consulted. Process variables override file values, following Component 2. There is no implicit dotenv search or fallback from test credentials to development. Do not put connection URLs/passwords in commands, `alembic.ini`, revision files, logs or this guide. The existing `.env` remains mode `0600`; no new credential file was created.

Before migration execution, live PostgreSQL identity must match the selected target, configured user, database ownership, UTF-8, loopback address, port 5432 and PostgreSQL major 18. The role must have none of SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS. Errors report a safe explanation and, for driver failures, SQLSTATE where available; driver exception details and URLs are suppressed. Failed authentication is tested with deliberately invalid credentials and redaction assertions.

Offline CLI `--sql` execution is deliberately refused: it cannot perform the mandatory live identity/ownership checks. PostgreSQL DDL compilation was separately reviewed in memory; this is not an offline deployment path.

<a id="database-migrations-initial-revision-and-schema-ownership"></a>
### Initial revision and schema ownership

| Item | Value |
| --- | --- |
| Revision file | `alembic/versions/20261108_01_initial_rfq_database_schema.py` |
| Initial revision ID (historical head) | `20261108_01` |
| Parent / dependencies / branch labels | None |
| Description | `initial_rfq_database_schema` |
| Application schemas | `business` (32 tables), `orchestration` (`workflow_runs` only) |
| Version tracking | `public.alembic_version`, one row at head |

The revision contains frozen explicit Alembic table, constraint and index operations. It does not recreate schema from live future ORM metadata and does not call `create_all()`. The initial guard refuses pre-existing managed schemas or conflicting history. The revision creates schemas first and marks each with the provenance comment `rfq-copilot:20261108_01`. Alembic owns its tracker in `public`; application tables never use `public`.

Tables follow dependency order. The two deferred composite quote-header pointers to `quote_versions(quote_id, version)` are added explicitly after all tables exist. Autogeneration rendered them inline with `use_alter=True`, which does not reliably emit these operations inside Alembic's individual table creation. Their explicit operations retain MATCH SIMPLE, RESTRICT on delete/update and DEFERRABLE INITIALLY DEFERRED. Downgrade removes them before reversing the table order. The three `orchestration.workflow_runs` foreign keys resolve to `business` normally.

The revision retains exact types, numeric precision/scale, nullability, server defaults, timezone-aware timestamps, native UUIDs, JSONB, composite candidate keys, generated stored expressions, all CHECKs and all explicit index definitions. Application UUIDv4 defaults remain Python-side and have no new server generator. Long convention-generated constraint names use Alembic `op.f()` and PostgreSQL's normal identifier truncation/hash behavior.

<a id="database-migrations-upgrade-and-history-commands"></a>
### Upgrade and history commands

```sh
.venv/bin/python -m alembic -x target=development -x env_file=.env upgrade head
.venv/bin/python -m alembic -x target=development -x env_file=.env current
.venv/bin/python -m alembic -x target=development -x env_file=.env current --check-heads
.venv/bin/python -m alembic -x target=development -x env_file=.env history --verbose
.venv/bin/python -m alembic -x target=development -x env_file=.env heads --verbose
.venv/bin/python -m alembic -x target=development -x env_file=.env check
```

All six commands executed with exit code 0. The initial upgrade and a repeated upgrade each returned 0. Current/head/history identify exactly `20261108_01`; `check` reports `No new upgrade operations detected.` A repeated upgrade leaves the version and physical schema unchanged.

For future approved model changes, generate a new revision rather than editing an applied migration:

```sh
.venv/bin/python -m alembic -x target=development -x env_file=.env revision --autogenerate -m descriptive_change
```

Review every generated operation against the approved design before upgrade. [Alembic autogeneration](https://alembic.sqlalchemy.org/en/latest/autogenerate.html) does not establish complete CHECK-constraint or PostgreSQL-specific parity. Schema creation/removal and cycles need explicit review as in this revision. No additional revision was generated after the successful drift check.

<a id="database-migrations-downgrade-requirements-and-recovery"></a>
### Downgrade requirements and recovery

Development downgrade is prohibited by the revision, even if called directly through Alembic. This initial downgrade is available only for an empty test schema with explicit opt-in:

```sh
.venv/bin/python -m alembic -x target=test -x env_file=.env -x allow_test_downgrade=true downgrade base
```

Before removal, guards require the verified test identity and its live database OID, exact revision, schema owner/provenance, relation ownership, a frozen object-shape fingerprint, no unexpected routines/triggers/policies/domain or enum types, and zero rows in every application table. Unknown names or changed structural shape block removal. The fingerprint is frozen for this revision, excludes native NOT NULL records and covers the 33 tables, 261 ordered column names/nullability flags, 318 named PK/FK/UNIQUE/CHECK constraints and 61 index names. It is an ownership/removal guard; independent catalog verification additionally checks definitions.

The downgrade drops only named migration objects and then schemas with RESTRICT. No `CASCADE` is used. External dependencies cause the entire transaction to roll back; the test suite verifies this using an unrelated dependent view. A separate test adds an extra managed CHECK and verifies removal is refused before destructive DDL. Unrelated schemas survive a successful downgrade.

Normal downgrade leaves an empty `public.alembic_version` so Alembic can locate history consistently. Only the test harness removes that tracker after proving it did not exist before the lifecycle, the target is the verified test database, the managed schemas are absent and history is empty. It never drops either database.

On unexpected identity, objects, history, rows or dependencies, preserve the database and investigate. On a PostgreSQL operation failure, transactional DDL rolls back; inspect `current --check-heads`, history and catalogs before retrying. Do not use `stamp`, force-drop objects, overwrite data or change the recorded revision to hide drift. This revision's empty-schema downgrade is not a recovery mechanism for populated databases. Plan a separate reviewed forward repair or backup/restore procedure before any future data-bearing change.

<a id="database-migrations-isolation-and-automated-tests"></a>
### Isolation and automated tests

The restricted local role cannot create databases and no administration credentials were available. The user-authorized fallback was the existing `rfq_copilot_test`, only after live verification found public-only schemas, zero user relations, no revision history and no data. The database OIDs remained development `16389`, test `16390`; identity checks never substitute name guessing for verification.

The module fixture leases a separate advisory lock `(461441, 505)` for the whole lifecycle. It refuses any existing test objects before creating anything, creates one unrelated empty sentinel schema, and calls official Alembic `upgrade`, `current(check_heads=True)`, `check` and `downgrade` APIs in a worker thread. This avoids nesting event loops while `env.py` owns its async engine. It performs:

1. Empty-state verification and test-only identity/ownership checks.
2. Upgrade to head and complete physical parity checks.
3. Repeated upgrade with a stable full catalog snapshot.
4. Current/head and no-pending-operation checks.
5. Safe refusal for an extra constraint, then removal of that test-owned constraint.
6. Safe rollback for an unrelated dependent public view, then removal of that test-owned view.
7. Downgrade to base; verify no application schemas/tables remain and unrelated sentinel survives.
8. Re-upgrade; verify complete schema equality with the first upgrade and zero drift.
9. Final guarded downgrade, empty tracker cleanup and sentinel removal; verify original schema/owner/comment/relations snapshot restored exactly.

Cleanup does not force removal if a safety guard fails. No application records are inserted; logical probes use SELECT expressions only. All existing Component 2 and Component 3 tests remain byte-identical, with no weakened assertions or skips.

```sh
.venv/bin/python -m pytest tests/test_database_migrations.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests alembic
.venv/bin/ruff format --check src tests alembic
uv lock --check
git diff --check
```

The development parity test obtains a read-only engine; destructive lifecycle operations use only the explicitly verified test target.

<a id="database-migrations-independent-physical-verification-and-drift"></a>
### Independent physical verification and drift

Expectations are parsed from the approved database design Markdown through the existing independently authored ORM-test fixture, including documented common CHECK conventions and the additional-index table. Tests compare these expectations directly to PostgreSQL catalogs and SQLAlchemy reflection, independently of the revision's contents.

Every column's PostgreSQL type, precision/scale, nullability, default and stored generated expression is compared. Every PK/UNIQUE key's ordered columns and every FK's ordered local/remote columns, schema, RESTRICT actions, MATCH SIMPLE, deferred flags, validation and enforcement are checked. Index checks include ordered key columns, ASC/DESC/null ordering, uniqueness, B-tree method, validity, key count and partial predicates.

For all 207 CHECKs, both approved and actual expressions are normalized with PostgreSQL `EXPLAIN (VERBOSE, FORMAT JSON, COSTS FALSE)` in the same typed table context. This is read-only and handles PostgreSQL's casts, parentheses and `IN`/`ANY` rewriting without stripping boolean precedence or altering literal case. Typed SELECT-only probes then compare exact TRUE/FALSE/NULL behavior and demonstrate both rejection and CHECK acceptance for every rule, including sparse NULLs, boundaries, valid/invalid states, JSON shapes, dates and numeric NaN. Catalog flags confirm all constraints are validated and enforced. Application tables are empty within the isolated migration lifecycle; the development check instead fingerprints and preserves its seeded records. Alembic's check alone is not treated as CHECK proof.

The comparisons use the active schema contract and the approved initial-schema documents.
Original execution tables and environment/debugging chronology are retained privately.
A documented comparison method is not a claim that it was rerun against the current database.

<a id="database-migrations-current-integrity-boundary"></a>
### Current integrity boundary

The active contract supplies the accepted revision, schema digest, catalog counts and
ordered guard revisions. Guard migration files are independently SHA-256 pinned; even
comment-only edits would invalidate their exact-byte pins. Preserve every historical
revision and test the deferred contract without activating it in development.

The initial schema remains documented by [table specifications](../database/table_schemas.md)
and [relationships/constraints](../database/relationships_and_constraints.md). Tests consume
those documents; they are not disposable reports. Historical migration execution receipts
are archived privately, and no migration or verification was run for this documentation edit.

<a id="database-migrations-revision-20261109_01-capacity-key-per-snapshot"></a>
### Revision 20261109_01: capacity key per snapshot

This reviewed revision replaces `UNIQUE (workcentre_id, week_start)` with
`UNIQUE (workcentre_id, week_start, snapshot_id)`, allowing overlapping capacity snapshots
without changing their source data. Current revision/digest expectations come from the
active schema contract, not a hardcoded claim that this early revision is still head.

The test-only downgrade restores the older key and refuses overlapping data rather than
deleting it. Later migrations and K2/K5 privilege provisioning remain separate concerns.


## Synthetic dataset seeding

The mapping below describes the frozen v1.1.0 baseline. Current schema expectations come from the [active contract](../../src/rfq_copilot/schema_contract.py); runtime identities, operational RFQs and post-baseline rule/policy registration require separate reviewed provisioning. See [local setup](../../README.md#local-setup).

<a id="database-seeding-mapping-audit--completed-before-the-first-import"></a>
### Baseline import mapping

Authority: the approved [database mapping](../database/dataset_to_database_mapping.md),
source manifests and active schema contract. Seeding requires an explicitly selected,
guarded database and validates its state before import.

Source preflight independently measured **681 files**, **637/637 protected hashes**, and matching archive SHA-256 `681a882fd7c22cd4e874b57fd31ab6bd12602bc826b2c02cfa79eb17a5ad5080`. The archive has 680 entries; the existing extra root `.DS_Store` accounts for the tree difference. All 28 allowlisted source documents also match that authenticated archive, including configuration outside the 637-file manifest. No source is regenerated or repaired.

The mapping covers 17 CSVs (731 records), historical RFQ JSON (80 records), nine rule/contract documents and one generation metadata file: **28 mapped files**, **811 business source records**, **1,089 target records**, **26 populated tables**, **seven intentionally empty runtime tables**. No unresolved mapping remains.

The database design version-field label is incorrect: `properties.schema_version.const = "1.1"` belongs to `contracts/agent_handoff_schema.json`. `contracts/business_constraints.json` has no version property. Its approved identity is `business_constraints@sha256:<full exact-byte digest>`, preserving the complete object without inventing a version. All five contracts use namespaced content-addressed keys; the four policies retain their declared version IDs.

“Direct” below includes explicit wire conversion and the approved application fields; “derived” identifies projections/registries and the current-header join. An RFQ gets `archived`, NULL creator and row_version=1. Inventory/capacity get their documented snapshot token and row_version=1. Quote headers get row_version=1 and NULL release fields. Approval events get NULL approval_request_id. Neither historical actor identity nor approval flags certify current release eligibility.

| Schema.table | Class | Expected | Source file(s), relative to dataset | Source/target identity |
| --- | --- | ---: | --- | --- |
| `business.business_rule_versions` | derived | 9 | `config/manufacturing_rules.json`; `config/pricing_rules.json`; `config/approval_policies.json`; `config/workflow_state_rules.json`; `contracts/entity_schemas.json`; `contracts/tool_schemas.json`; `contracts/business_constraints.json`; `contracts/agent_coordination_policy.json`; `contracts/agent_handoff_schema.json` | `rule_version` |
| `business.customers` | direct | 40 | `master_data/customers.csv` | `customer_id` |
| `business.data_snapshots` | derived | 2 | `operations_data/material_inventory.csv`; `operations_data/production_capacity.csv`; `config/generation_config.yaml` | `snapshot_id` |
| `business.operations` | direct | 8 | `master_data/operations.csv` | `operation_id` |
| `business.permissions` | derived | 12 | `master_data/roles_permissions.csv` | `permission` |
| `business.roles` | derived | 6 | `master_data/users.csv`; `master_data/roles_permissions.csv` | `role` |
| `business.suppliers` | direct | 6 | `master_data/suppliers.csv` | `supplier_id` |
| `business.contacts` | direct | 40 | `master_data/contacts.csv` | `contact_id` |
| `business.finishes` | direct | 7 | `master_data/finishes.csv` | `finish_id` |
| `business.materials` | direct | 15 | `master_data/materials.csv` | `material_id` |
| `business.role_permissions` | direct | 27 | `master_data/roles_permissions.csv` | `role`, `permission` |
| `business.users` | direct | 24 | `master_data/users.csv` | `user_id` |
| `business.workcentres` | direct | 8 | `master_data/workcentres.csv` | `workcentre_id` |
| `business.finish_material_categories` | derived | 19 | `master_data/finishes.csv` | `finish_id`, `material_category` |
| `business.idempotency_records` | empty | 0 | Later authenticated runtime operations | `receipt_id` |
| `business.material_finish_compatibility` | derived | 50 | `master_data/materials.csv` | `material_id`, `finish_id` |
| `business.material_supported_operations` | derived | 90 | `master_data/materials.csv` | `material_id`, `operation_id` |
| `business.material_thickness_options` | derived | 90 | `master_data/materials.csv` | `material_id`, `thickness_mm` |
| `business.operation_routing` | direct | 28 | `operations_data/operation_routing.csv` | `routing_id` |
| `business.production_capacity` | direct | 80 | `operations_data/production_capacity.csv` | `capacity_id` |
| `business.rfqs` | direct | 80 | `transactional_data/historical_rfqs.json` | `rfq_id` |
| `business.material_inventory` | direct | 90 | `operations_data/material_inventory.csv` | `inventory_id` |
| `business.pricing_calculations` | empty | 0 | Later authenticated runtime operations | `calculation_id` |
| `business.quotes` | direct | 80 | `transactional_data/historical_quotes.csv` | `quote_id` |
| `business.rfq_attachments` | empty | 0 | Later authenticated runtime operations | `attachment_id` |
| `business.rfq_line_items` | empty | 0 | Later authenticated runtime operations | `rfq_line_id` |
| `orchestration.workflow_runs` | empty | 0 | Later authenticated runtime operations | `workflow_run_id` |
| `business.procurement_records` | direct | 12 | `operations_data/procurement_records.csv` | `procurement_id` |
| `business.quote_versions` | derived | 96 | `transactional_data/quote_versions.csv`; `transactional_data/historical_quotes.csv` | `quote_version_id` |
| `business.approval_requests` | empty | 0 | Later authenticated runtime operations | `approval_request_id` |
| `business.audit_events` | empty | 0 | Later authenticated runtime operations | `event_id` |
| `business.quote_line_items` | direct | 100 | `transactional_data/quote_line_items.csv` | `quote_line_id` |
| `business.approval_history` | direct | 70 | `transactional_data/approval_history.csv` | `approval_id` |

<a id="database-seeding-per-table-conversion-and-dependency-audit"></a>
### Per-table conversion and dependency audit

CSV records are strict UTF-8 text dictionaries, with exact contracted header sets (column order may differ); historical RFQs are a JSON list of exact entity objects. Catalog array fields and two quote-line payloads are serialized JSON parsed once. IDs are preserved byte-for-byte; no trimming, fuzzy joins or generated replacement IDs. All source keys and target PK/UNIQUE tuples are checked for duplicates. Every immediate and deferred FK, including nullable composite MATCH SIMPLE relationships, is checked as a full tuple before database insertion.

<a id="database-seeding-businessbusiness_rule_versions"></a>
#### `business.business_rule_versions`

Dependencies: none.

Dates: registered_at: UTC aware timestamp.

Nullable fields: none.

<a id="database-seeding-businesscustomers"></a>
#### `business.customers`

Dependencies: none.

Decimals: credit_limit_usd: NUMERIC(18,2); credit_exposure_usd: NUMERIC(18,2).

Dates: created_date: ISO calendar date.

Nullable fields: none.

<a id="database-seeding-businessdata_snapshots"></a>
#### `business.data_snapshots`

Dependencies: none.

Dates: as_of_date: ISO calendar date; registered_at: UTC aware timestamp.

Nullable fields: none.

<a id="database-seeding-businessoperations"></a>
#### `business.operations`

Dependencies: none.

Nullable fields: none.

<a id="database-seeding-businesspermissions"></a>
#### `business.permissions`

Dependencies: none.

Nullable fields: none.

<a id="database-seeding-businessroles"></a>
#### `business.roles`

Dependencies: none.

Nullable fields: none.

<a id="database-seeding-businesssuppliers"></a>
#### `business.suppliers`

Dependencies: none.

Nullable fields: none.

<a id="database-seeding-businesscontacts"></a>
#### `business.contacts`

Dependencies: customer_id → business.customers(customer_id).

Nullable fields: none.

<a id="database-seeding-businessfinishes"></a>
#### `business.finishes`

Dependencies: pricing_rule_version → business.business_rule_versions(rule_version).

Nullable fields: none.

<a id="database-seeding-businessmaterials"></a>
#### `business.materials`

Dependencies: pricing_rule_version → business.business_rule_versions(rule_version).

Decimals: density_kg_m3: NUMERIC(18,6); cost_usd_per_kg: NUMERIC(18,4); min_bend_radius_factor: NUMERIC(12,3).

Nullable fields: none.

<a id="database-seeding-businessrole_permissions"></a>
#### `business.role_permissions`

Dependencies: role → business.roles(role); permission → business.permissions(permission).

Nullable fields: none.

<a id="database-seeding-businessusers"></a>
#### `business.users`

Dependencies: role → business.roles(role).

Nullable fields: none.

<a id="database-seeding-businessworkcentres"></a>
#### `business.workcentres`

Dependencies: operation_id → business.operations(operation_id).

Decimals: weekly_nominal_hours: NUMERIC(12,4); downtime_hours_per_week: NUMERIC(12,4).

Nullable fields: none.

<a id="database-seeding-businessfinish_material_categories"></a>
#### `business.finish_material_categories`

Dependencies: finish_id → business.finishes(finish_id).

Nullable fields: none.

<a id="database-seeding-businessmaterial_finish_compatibility"></a>
#### `business.material_finish_compatibility`

Dependencies: material_id → business.materials(material_id); finish_id → business.finishes(finish_id).

Nullable fields: none.

<a id="database-seeding-businessmaterial_supported_operations"></a>
#### `business.material_supported_operations`

Dependencies: material_id → business.materials(material_id); operation_id → business.operations(operation_id).

Nullable fields: none.

<a id="database-seeding-businessmaterial_thickness_options"></a>
#### `business.material_thickness_options`

Dependencies: material_id → business.materials(material_id).

Decimals: thickness_mm: NUMERIC(12,3).

Nullable fields: none.

<a id="database-seeding-businessoperation_routing"></a>
#### `business.operation_routing`

Dependencies: workcentre_id,operation_id → business.workcentres(workcentre_id,operation_id).

Nullable fields: none.

<a id="database-seeding-businessproduction_capacity"></a>
#### `business.production_capacity`

Dependencies: snapshot_id → business.data_snapshots(snapshot_id); workcentre_id → business.workcentres(workcentre_id).

Decimals: nominal_hours: NUMERIC(12,4); downtime_hours: NUMERIC(12,4); scheduled_hours: NUMERIC(12,4); available_hours: NUMERIC(12,4); remaining_hours: NUMERIC(12,4); utilization_fraction: NUMERIC(9,4).

Dates: week_start: ISO calendar date.

Nullable fields: utilization_fraction.

<a id="database-seeding-businessrfqs"></a>
#### `business.rfqs`

Dependencies: customer_id → business.customers(customer_id); created_by → business.users(user_id).

Dates: submitted_at: UTC aware timestamp.

Nullable fields: raw_request, created_by.

<a id="database-seeding-businessmaterial_inventory"></a>
#### `business.material_inventory`

Dependencies: snapshot_id → business.data_snapshots(snapshot_id); supplier_id → business.suppliers(supplier_id); material_id,thickness_mm → business.material_thickness_options(material_id,thickness_mm).

Decimals: thickness_mm: NUMERIC(12,3); quantity_kg: NUMERIC(18,6); reserved_kg: NUMERIC(18,6); reorder_threshold_kg: NUMERIC(18,6); available_kg: NUMERIC(18,6).

Dates: expected_replenishment_date: ISO calendar date; snapshot_date: ISO calendar date.

Nullable fields: expected_replenishment_date.

<a id="database-seeding-businessquotes"></a>
#### `business.quotes`

Dependencies: released_by → business.users(user_id); submitted_by → business.users(user_id); rfq_id → business.rfqs(rfq_id); quote_id,current_version → business.quote_versions(quote_id,version) [initially deferred]; approver_id → business.users(user_id); created_by → business.users(user_id); quote_id,approved_version → business.quote_versions(quote_id,version) [initially deferred].

Dates: created_at: UTC aware timestamp; approval_timestamp: UTC aware timestamp; released_at: UTC aware timestamp.

Nullable fields: submitted_by, approved_version, approver_id, approval_timestamp, rejection_reason, legacy_idempotency_key, released_by, released_at.

<a id="database-seeding-businessprocurement_records"></a>
#### `business.procurement_records`

Dependencies: inventory_id → business.material_inventory(inventory_id); supplier_id → business.suppliers(supplier_id).

Decimals: quantity_kg: NUMERIC(18,6).

Dates: ordered_date: ISO calendar date; expected_receipt_date: ISO calendar date.

Nullable fields: none.

<a id="database-seeding-businessquote_versions"></a>
#### `business.quote_versions`

Dependencies: created_by → business.users(user_id); manufacturing_rule_version → business.business_rule_versions(rule_version); approval_rule_version → business.business_rule_versions(rule_version); quote_id → business.quotes(quote_id); workflow_rule_version → business.business_rule_versions(rule_version); calculation_id → business.pricing_calculations(calculation_id); pricing_rule_version → business.business_rule_versions(rule_version).

Decimals: total_usd: NUMERIC(18,2).

Dates: created_at: UTC aware timestamp; expiry_date: ISO calendar date.

Nullable fields: total_usd, pricing_rule_version, manufacturing_rule_version, approval_rule_version, workflow_rule_version, expiry_date, estimated_lead_business_days, source_snapshot_versions, calculation_id.

<a id="database-seeding-businessquote_line_items"></a>
#### `business.quote_line_items`

Dependencies: quote_id,quote_version → business.quote_versions(quote_id,version).

Decimals: line_total_usd: NUMERIC(18,2).

Nullable fields: none.

<a id="database-seeding-businessapproval_history"></a>
#### `business.approval_history`

Dependencies: approval_request_id,quote_id,quote_version → business.approval_requests(approval_request_id,quote_id,quote_version); quote_id,quote_version → business.quote_versions(quote_id,version); actor_id → business.users(user_id).

Dates: action_at: UTC aware timestamp.

Nullable fields: approval_request_id.

<a id="database-seeding-transformation-rules"></a>
### Transformation rules

- Decimal rejects nonfinite values, overflow and any nonzero precision beyond the target scale. Currency never passes through float. Nested JSON monetary/mass strings remain exact, including evidence with more than six decimal places. JSON numeric tokens parse as Decimal and bind through exact JSON text cast to JSONB; stored JSONB is read as text and reparsed the same way.
- ISO dates round-trip as YYYY-MM-DD. Timestamp inputs require a timezone and normalize to UTC. Strict integer parsing rejects fractional/bool inputs; CSV booleans accept only `True`/`False`. Empty strings become NULL only for documented nullable fields. Literal `none` remains a finish ID; restrictions prose is preserved.
- Distinct role/user and grant values produce six roles and 12 permissions. Catalog arrays produce 90 thickness options, 50 finish pairs, 90 operation pairs and 19 finish-category pairs. Every composite is unique and resolves to its catalog parents; finish categories agree.
- Inventory available kg and capacity available/remaining/utilization are verified against base values and omitted from INSERT. PostgreSQL recomputes them. Only utilization uses its documented four-place ROUND_HALF_UP policy. Capacity dates cover all ten Mondays beginning 2026-11-09. Snapshot as-of is 2026-11-02 from generation metadata, cross-checked against inventory dates.
- All 96 quote-version identities and flags survive. Only the matching 80 current versions receive supplied header financial fields (`priced_partial`). The 16 earlier versions are `metadata_only`; missing historical amounts, other policy pins, calculation and snapshot references remain NULL. USD is the documented dataset invariant. No old prices or lines are synthesized.
- Validate line ordinals, exact-version references, current sums, nested Part schema, material/thickness/finish/operation compatibility, IDs/quantities/totals, review actors/timestamps/status summaries and independent approvers. Enforce configured scalar hole/bend limits locally, including MFG-001. Header rejection summary and historical event reason are distinct source prose and are both preserved; the approved mapping does not require their text to be equal.

<a id="database-seeding-evaluation-exclusion-boundary"></a>
### Evaluation exclusion boundary

Only 28 fixed source paths can be parsed into a plan. No scenario, attachment, evaluation security/coordination annotation or ground-truth document is a data reader input. All `evaluation/**`, every `rfq_scenarios/**` split, and `attachments/**` are excluded. Hidden assets are streamed in bounded binary blocks solely for SHA-256 verification, never decoded as JSON or copied into records/logs. The complete evaluation ZIP is never archived for runtime access. Quote nested objects have approved field allowlists, preventing indirect benchmark-field additions. Tests enforce parser-call boundaries and adversarial nested-field rejection; this component does not claim deployment of later runtime RBAC.

<a id="database-seeding-dependency-order"></a>
### Dependency order

Computed from all 64 ORM FKs. Only the two documented, genuinely DEFERRABLE INITIALLY DEFERRED quote-header pointers are omitted from the topological graph. They are explicitly checked before import and forced IMMEDIATE before commit. All other FKs remain immediate; no constraint is disabled or dropped. Order (empty tables are visited without inserts):

`business_rule_versions` → `customers` → `data_snapshots` → `operations` → `permissions` → `roles` → `suppliers` → `contacts` → `finishes` → `materials` → `role_permissions` → `users` → `workcentres` → `finish_material_categories` → `idempotency_records` → `material_finish_compatibility` → `material_supported_operations` → `material_thickness_options` → `operation_routing` → `production_capacity` → `rfqs` → `material_inventory` → `pricing_calculations` → `quotes` → `rfq_attachments` → `rfq_line_items` → `workflow_runs` → `procurement_records` → `quote_versions` → `approval_requests` → `audit_events` → `quote_line_items` → `approval_history`.

<a id="database-seeding-architecture-and-resource-ownership"></a>
### Architecture and resource ownership

The `rfq_copilot.seeding` package has eight small modules: `sources` (fixed readers/integrity), `transform` (strict wire conversions and exact JSON encoding), `mapping` (approved projections, native validation and FK order), `validation` (nested and cross-record business facts), `safety` (live identity/head/schema checks), `loader` (snapshots, comparison, transaction and verification), `cli` (explicit actions/JSON summaries), and `__init__` (safe error type). There is no second engine, session factory, declarative base, tracking table or schema-creation path. `DatabaseConfig` and `Database` retain their existing ownership and sanitization behavior.

`jsonschema` and `PyYAML` are declared, locked runtime requirements. JSON Schema validates the actual entity and Part contracts; safe YAML parsing reads generation provenance. Use the locked environment for reproducible dependency selection.

<a id="database-seeding-transaction-concurrency-and-repeatability"></a>
### Transaction, concurrency and repeatability

Component 8 hardened stored JSON comparison to distinguish booleans from numeric zero/one. The seeder and integrity verifier share this type-aware comparison; a changed JSON type is a conflict even where Python's ordinary equality would consider the values equal. The isolated regression reproduced the previous gap before the fix. The comparison implementation and regression coverage remain in `seeding/validation.py`, `integrity.py`, and `tests/test_database_integrity.py`; historical closure evidence is private.

`run_seed` owns `Database`, a session and one explicit `session.begin()` for the entire baseline. `execute_seed` performs no commit; individual table loaders perform no commit. Inserts use the registered ORM Table metadata in batches of at most 100 rows. Owned sessions/pools close on success, error and cancellation. All parents and descendants roll back together on a source, FK, unique, CHECK or post-insert verification error. Deferred quote pointers are forced IMMEDIATE before the final exact-state check. Success is printed only after the transaction has completed.

Before touching business data, live checks verify local database/owner/current role, OID context, UTF-8, loopback:5432, PostgreSQL 18 and restricted role flags using the existing migration guard. Only `rfq_copilot_dev` and `rfq_copilot_test` are supported. Head must be exactly `20261108_01`. Schema verification checks the frozen migration shape, Alembic metadata/default/type drift, actual CHECK/generated expressions and partial-index predicates, and constraint/index validity. Unexpected triggers/policies block seeding.

Transactional advisory locks `(461441,504)` and `(461441,506)` coordinate with the migration lease and other seeders. An apply takes SHARE locks on all 33 tables in sorted order before schema/data verification, blocking concurrent business writes and DDL until completion. This is deliberately a small, maintenance-time baseline operation. Lock wait is bounded to 5 seconds; statement timeout is 30 seconds. A competing migration/import fails clearly without retry. There is no autonomous retry loop.

The recovery policy is deliberately all-or-nothing:

1. All 33 tables empty: import the complete validated baseline atomically.
2. Exact baseline already present: compare every source-derived/default/generated field, complete composite identities and immutable digests, verify archived artifacts, then report every record already existing. There are zero INSERT/UPDATE/DELETE statements on replay.
3. Any missing/extra record, populated runtime table, changed source value or row_version: refuse before business writes. Do not merge a partial database, overwrite operational changes, or hide conflicts with ON CONFLICT. No deletion/reset/truncate/downgrade command exists in this CLI.

`registered_at` is a present-time application registration timestamp, not a historical effective date. Verify it is populated/aware; it is the only non-source registration attribute excluded from canonical source equality. Full-table before/after digests include it, proving a replay preserves it. Every other seeded nullable/default/generated field is compared explicitly.

<a id="database-seeding-immutable-operational-snapshot-artifacts"></a>
### Immutable operational snapshot artifacts

Only the exact approved inventory/capacity CSV bytes are archived under a dedicated private directory, separately from the frozen dataset and evaluation ZIP. Tokens are `inventory-v1.1.0-2026-11-02` and `capacity-v1.1.0-2026-11-02`. Content-addressed filenames include the CSV SHA-256. `artifact_ref` is the stable absolute backend path, which must remain the same on later runs. Source paths in the rule registry remain the approved logical repository-relative paths.

The importer requires directory mode 0700 and publishes mode-0444 files atomically from temporary files with no replacement. Existing artifacts must hash-identically. Temporary files are cleaned on publication failure. A failed database transaction may leave an unreferenced, valid content-addressed CSV; this is harmless and reused only after verification. The filesystem and PostgreSQL do not share a distributed transaction. Do not delete an archive referenced by a committed registry row. The directory's `.gitignore` keeps operational bytes out of Git.

<a id="database-seeding-commands"></a>
### Commands

From the repository root, initialize the dedicated archive directory privately if necessary:

```bash
install -d -m 700 data/rfq_seed_snapshots
```

Use explicit source/archive/snapshot paths and an explicit dotenv file (or inject existing environment variables without `--env-file`). Credentials never belong on the command line. No target, source or environment silently falls back.

```bash
.venv/bin/python -m rfq_copilot.seeding.cli --target development --source data/rfq_synthetic_data --archive data/rfq_synthetic_data_v1.1.0.zip --snapshot-dir data/rfq_seed_snapshots --env-file .env --dry-run

.venv/bin/python -m rfq_copilot.seeding.cli --target development --source data/rfq_synthetic_data --archive data/rfq_synthetic_data_v1.1.0.zip --snapshot-dir data/rfq_seed_snapshots --env-file .env --apply --confirm-database rfq_copilot_dev

.venv/bin/python -m rfq_copilot.seeding.cli --target development --source data/rfq_synthetic_data --archive data/rfq_synthetic_data_v1.1.0.zip --snapshot-dir data/rfq_seed_snapshots --env-file .env --verify
```

Dry-run and verify use server-enforced read-only REPEATABLE READ transactions. Dry-run performs only SELECTs (including evaluation of every target CHECK against typed candidate records); it never creates archives or writes rows. Verify requires the complete identical baseline and existing intact snapshots. Apply requires both `--apply` and the exact deliberate `--confirm-database` name; `--target test` requires its distinct validated test settings and `--confirm-database rfq_copilot_test`. Unknown/production targets and ambiguous actions fail argument parsing.

Success emits one JSON summary to stdout, including identity, revision, integrity, table classifications/counts, insertion/existing totals, state digests and duration. Validated failures emit safe JSON to stderr and return 1; invalid CLI syntax returns 2. Interrupt returns 130 with an instruction to verify the outcome before retrying. No complete database URL, raw driver exception, source row, hidden label or password is logged.

<a id="database-seeding-test-isolation-and-verification"></a>
### Test isolation and verification

```bash
.venv/bin/python -m pytest tests/test_database_seeding.py tests/test_database_seeding_integration.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check src tests alembic
uv lock --check
```

The new integration fixture acquires the existing test lifecycle lease `(461441,505)`, verifies test identity and a completely empty original catalog, and applies only the approved Alembic head. Individual imports execute inside rollback-only test transactions. A real savepoint commit exercises `run_seed`'s success boundary under an outer rollback-only transaction. Negative tests inject failures after parents have been inserted, including immediate and deferred FK failures, CHECK/unique failures, application failure, cancellation and post-insert verification failure. The finalizer verifies zero test rows, uses the existing guarded empty-test downgrade/tracker cleanup, and matches the original catalog, even after assertion failure. It never deletes committed development data.

Development parity verification uses a server-enforced read-only connection and compares
complete server-side data fingerprints before and after inspection. Isolated migration
lifecycle tests require empty application tables and preserve physical-schema, constraint
and target-isolation checks.

<a id="database-seeding-recovery-and-limits"></a>
### Recovery and limits

- Invalid/missing/tampered source: retain the frozen package and manifest, inspect the safe location/rule diagnostic, and correct only importer defects. Obtain the approved original package separately if its bytes were corrupted; never regenerate a manifest or guess replacement data.
- Partial/conflicting database: stop. Investigate ownership/provenance and preserve operational records. There is no automatic merge, reset or destructive recovery option. A changed runtime baseline requires a separately approved design/migration/import policy.
- Missing/different snapshot: preserve registry references; recover the exact approved CSV bytes and original configured backend location under explicit operational control. Never silently overwrite an existing file.
- Interrupt/failure before commit: the owned transaction rolls back and resources close. If an interruption or connection loss occurs at the commit acknowledgement boundary, run verify and independently inspect the database before deciding whether to retry; do not infer rollback from a lost acknowledgement.
- This importer intentionally supports only frozen v1.1.0, the approved local databases, active schema contract and the documented baseline/allowlisted registrations. It is not an incremental loader or deployment/runtime RBAC implementation. Historical pricing/release evidence remains intentionally incomplete. The frozen MFG-001 validator remains unchanged; seeding independently enforces its scalar limits.

<a id="database-seeding-operational-safety"></a>
### Operational safety

Do not infer that a database can be reseeded safely from an old empty-runtime report.
Inspect its current state and follow the guarded importer workflow above.


## Database integrity verification

This guide describes source/schema comparison and transaction-safety methods. The active [schema contract](../../src/rfq_copilot/schema_contract.py) supplies current revision and catalog expectations; original foundation counts are historical context.

This component independently inspects PostgreSQL through read-only SQL and tests actual constraint enforcement only in the isolated test database. The approved [database design](../database/table_schemas.md), [relationships](../database/relationships_and_constraints.md), [mapping](../database/dataset_to_database_mapping.md), ORM, migrations and frozen v1.1.0 sources define the comparison. The integrity verifier does not import or rewrite development business data.

<a id="database-integrity-verification-implementation-and-method"></a>
### Implementation and method

`src/rfq_copilot/integrity.py` provides `fingerprint(session)`, `audit_integrity(session, config, plan)` and `run_integrity(config, plan)`, plus a verification-only module CLI. It reuses DatabaseConfig/Database, metadata, existing target/head/schema safety, checksum-validated source mapping and read-only snapshot validation. It defines no engine factory, session foundation, schema, migration or write operation.

The caller-session functions never commit or modify records. `run_integrity` owns the existing Database resource and explicitly uses server-enforced READ ONLY, REPEATABLE READ. Reports include only identifiers of approved tables/columns, counts, constraint metadata and digests; no business payloads, passwords, URLs or evaluation answers.

Checks cover:

- All 33 PK tuples, including composites, and live COUNT versus COUNT(DISTINCT ROW(PK...)).
- All 64 FK definitions: local/remote columns, schemas, MATCH SIMPLE, ON DELETE/UPDATE RESTRICT and both genuinely deferred quote pointers. FK names use SQLAlchemy's PostgreSQL identifier shortening before comparison. Every populated FK is independently checked with a full-tuple NOT EXISTS anti-join; nullable MATCH SIMPLE tuples are correctly exempt when any component is NULL.
- All 14 additional UNIQUE definitions and 17 data-uniqueness checks (14 constraints plus three unique partial indexes). Grouping respects SQL NULL uniqueness and the exact partial predicate. Existing schema safety also verifies every explicit index and predicate.
- Every non-nullable column and all 207 approved live-equivalent CHECK predicates. Required defaults, computed expressions, numeric types/precision/scale and physical definitions are checked through the existing schema verifier and current metadata; existing independent database design migration tests verify that metadata against the approved documents.
- Actual PostgreSQL numeric results are Decimal. Compare all source-derived/default/generated values, including date, UTC timestamp, boolean, exact JSONB, source IDs, composite projections, snapshot tokens and nullable historical facts. Stored JSONB is selected as text and decoded with Decimal to retain exact numeric tokens and string evidence. Expected values reuse Component 5's checksum-validated audited transformations; actual reads and comparisons are separate from importer counters and its stored-row verifier.
- Registration timestamps are present and aware, without inventing historical effective dates. Full-table fingerprints include them. Seven runtime tables must remain empty; older historical quote facts remain absent as documented.

Inventory follows the actual constraint `0 <= reserved_kg <= quantity_kg`; it does not add an unsupported reserved-versus-remaining comparison. Capacity respects nominal/downtime/scheduled bounds and Monday weeks. Runtime dimensional CHECKs and the source manufacturing validations are distinct: this audit does not add manufacturing limits to the physical schema or implement an engine.

<a id="database-integrity-verification-fingerprints-and-preservation"></a>
### Fingerprints and preservation

For every approved table, PostgreSQL converts complete rows to JSONB, sorts their canonical text, aggregates, and computes a built-in SHA-256 over UTF-8 bytes. Empty tables hash `[]`. A second SHA-256 over the sorted mapping of table names, counts and hashes identifies the whole application state. No pgcrypto extension is required. Registration times, nested JSON, nullable fields and generated values are included.

Identifiers in this audit's metadata-generated SQL come only from the registered approved tables/constraints; no caller input is interpolated. Before/after audit comparisons use one consistent snapshot. Separate development connections capture the state before work and after all tests/queries, so preservation does not depend solely on a same-transaction snapshot comparison.

<a id="database-integrity-verification-commands"></a>
### Commands

```bash
.venv/bin/python -m rfq_copilot.integrity --target development --source data/rfq_synthetic_data --archive data/rfq_synthetic_data_v1.1.0.zip --snapshot-dir data/rfq_seed_snapshots --env-file .env
.venv/bin/python -m pytest tests/test_database_integrity.py -q
```

An explicit supported local target, source directory, authenticated archive and stable operational snapshot directory are required. Success returns JSON and exit 0; safe verification failures return nonzero. This CLI cannot seed, reset, truncate or migrate a database.

<a id="database-integrity-verification-invalid-operations-and-transaction-tests"></a>
### Invalid operations and transaction tests

`tests/test_database_integrity.py` imports and reuses Component 5's guarded test database, bundle, plan and rollback fixtures. Each populated fixture imports only into `rfq_copilot_test`, within a rollback-only test transaction. The fixture checks its original empty catalog, owns the existing lifecycle lease, migrates through the approved Alembic head, and restores the original public-only catalog with zero user relations. Development connections remain read-only.

Actual INSERT/UPDATE tests cover duplicate PKs in all 26 populated tables, distinct-PK unique conflicts, missing required values, missing/simple/composite/cross-schema FKs, wrong route/workcentre pairs, negative and over-reserved stock, invalid capacity, non-Monday weeks, invalid money/NaN/overflow, invalid states, and invalid quote versions/approval references. Tests cover all three unique partial indexes and prove applicable predicate-excluded rows can coexist. Runtime dimensional/composite-uniqueness cases use isolated test-only rows.

PostgreSQL errors are checked by exact SQLSTATE: 23505 unique, 23503 foreign key, 23502 NOT NULL, 23514 CHECK, 22003 numeric overflow, and PostgreSQL 18's 23001 RESTRICT violation for referenced-parent DELETE. Rejection happens inside a savepoint, followed by rollback, SELECT 1 and unchanged table count, proving session reuse. Numeric overflow is checked as a DBAPI error rather than incorrectly assuming every SQLSTATE class is IntegrityError.

Multi-record batches explicitly test whole-savepoint rollback for FK, UNIQUE and CHECK failure, comparing complete fingerprints. Separate tests exercise a real top-level commit, explicit rollback, exception rollback, actual async cancellation after a write and session/pool cleanup. The one committed test customer is the exact approved source row and is removed only by its owning fixture in a finally block on the guarded test target; no database reset or development mutation occurs.

An otherwise legal FK association can still differ from the imported source. Such a case is detected by source parity, rather than pretending a nonexistent cross-record business constraint rejects it. The tests do not alter the approved schema to impose additional rules.

<a id="database-integrity-verification-verification-scope"></a>
### Verification scope

The integrity CLI is a read-only audit of the guarded target, accepted schema contract,
source parity and referenced operational snapshots. Preserve the private dataset and
snapshot bytes, and investigate a mismatch rather than reseeding or resetting the database.

Relevant coverage is in `tests/test_database_integrity.py` and the migration/lifecycle
regression modules. Historical structured receipts and command histories are archived
privately. See the [reported baseline and manual verification guidance](../../README.md#verification);
see [testing](testing.md) for the current checks and their prerequisites.
