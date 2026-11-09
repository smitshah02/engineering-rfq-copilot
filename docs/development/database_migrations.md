# Database migrations — Day 5, Component 4

Verified on 2026-11-08 (America/New_York). Component 4 deploys the approved Day 4 design and Component 3 models; its original evidence below predates seeding. Completed development now contains 1,089 approved records at the same revision, preserved by read-only migration checks. Isolated migration lifecycle tables still start and finish empty. See the [final Day 5 verification report](day_05_final_verification_report.md) for current consolidated results.

## Architecture and files

```text
alembic.ini
alembic/
    env.py
    script.py.mako
    README
    versions/20261108_01_initial_rfq_database_schema.py
src/rfq_copilot/migration_safety.py
tests/test_database_migrations.py
```

The documented [Alembic async approach](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic) uses an async connection's `run_sync()` to run the synchronous Alembic migration context. `env.py` imports the existing `rfq_copilot.models.Base.metadata`; importing the models registers all 33 tables on that single registry. It obtains an engine through the existing `Database` owner and `DatabaseConfig` validation. It creates no additional Base, global application engine or session factory. Each command disposes its temporary engine when its async context exits, including failures.

Identity/version-table preflight SELECTs start a SQLAlchemy transaction. The environment explicitly ends that read transaction before Alembic starts its transactional DDL, so a successful migration commits and an unsuccessful one rolls back completely. A session advisory lock `(461441, 504)` rejects concurrent migration commands for the same database and is released in `finally`.

`alembic.ini` contains paths and logging configuration, with no `sqlalchemy.url` or credentials. Paths resolve relative to the configuration file. Schema-aware autogeneration uses `include_schemas=True`, type/default comparison and `include_name`: reflection manages only the approved tables in `business` and `orchestration`. Unknown tables and other schemas are never proposed for automatic removal. Independent catalog checks detect unexpected managed objects; filtering is not a substitute for an audit.

## Explicit targets and credentials

Run commands from the repository root with `.venv/bin/python`. Every online command requires exactly one `-x target=development` or `-x target=test`. There is no default target. Only the approved local databases `rfq_copilot_dev` and `rfq_copilot_test` on loopback port 5432 are allowed. Test configuration still requires a matching explicit `TEST_DATABASE_NAME` distinct from development. Duplicate, unknown or ambiguous arguments fail before connecting.

Add `-x env_file=.env` to read the existing private, ignored environment file explicitly. Without it, only process environment variables are consulted. Process variables override file values, following Component 2. There is no implicit dotenv search or fallback from test credentials to development. Do not put connection URLs/passwords in commands, `alembic.ini`, revision files, logs or this guide. The existing `.env` remains mode `0600`; no new credential file was created.

Before migration execution, live PostgreSQL identity must match the selected target, configured user, database ownership, UTF-8, loopback address, port 5432 and PostgreSQL major 18. The role must have none of SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS. Errors report a safe explanation and, for driver failures, SQLSTATE where available; driver exception details and URLs are suppressed. Failed authentication is tested with deliberately invalid credentials and redaction assertions.

Offline CLI `--sql` execution is deliberately refused: it cannot perform the mandatory live identity/ownership checks. PostgreSQL DDL compilation was separately reviewed in memory; this is not an offline deployment path.

## Initial revision and schema ownership

| Item | Value |
| --- | --- |
| Revision file | `alembic/versions/20261108_01_initial_rfq_database_schema.py` |
| Revision ID / sole head | `20261108_01` |
| Parent / dependencies / branch labels | None |
| Description | `initial_rfq_database_schema` |
| Application schemas | `business` (32 tables), `orchestration` (`workflow_runs` only) |
| Version tracking | `public.alembic_version`, one row at head |

The revision contains frozen explicit Alembic table, constraint and index operations. It does not recreate schema from live future ORM metadata and does not call `create_all()`. The initial guard refuses pre-existing managed schemas or conflicting history. The revision creates schemas first and marks each with the provenance comment `rfq-copilot:20261108_01`. Alembic owns its tracker in `public`; application tables never use `public`.

Tables follow dependency order. The two deferred composite quote-header pointers to `quote_versions(quote_id, version)` are added explicitly after all tables exist. Autogeneration rendered them inline with `use_alter=True`, which does not reliably emit these operations inside Alembic's individual table creation. Their explicit operations retain MATCH SIMPLE, RESTRICT on delete/update and DEFERRABLE INITIALLY DEFERRED. Downgrade removes them before reversing the table order. The three `orchestration.workflow_runs` foreign keys resolve to `business` normally.

The revision retains exact types, numeric precision/scale, nullability, server defaults, timezone-aware timestamps, native UUIDs, JSONB, composite candidate keys, generated stored expressions, all CHECKs and all explicit index definitions. Application UUIDv4 defaults remain Python-side and have no new server generator. Long convention-generated constraint names use Alembic `op.f()` and PostgreSQL's normal identifier truncation/hash behavior.

## Upgrade and history commands

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

## Downgrade requirements and recovery

Development downgrade is prohibited by the revision, even if called directly through Alembic. This initial downgrade is available only for an empty test schema with explicit opt-in:

```sh
.venv/bin/python -m alembic -x target=test -x env_file=.env -x allow_test_downgrade=true downgrade base
```

Before removal, guards require the verified test identity and its live database OID, exact revision, schema owner/provenance, relation ownership, a frozen object-shape fingerprint, no unexpected routines/triggers/policies/domain or enum types, and zero rows in every application table. Unknown names or changed structural shape block removal. The fingerprint is frozen for this revision, excludes native NOT NULL records and covers the 33 tables, 261 ordered column names/nullability flags, 318 named PK/FK/UNIQUE/CHECK constraints and 61 index names. It is an ownership/removal guard; independent catalog verification additionally checks definitions.

The downgrade drops only named migration objects and then schemas with RESTRICT. No `CASCADE` is used. External dependencies cause the entire transaction to roll back; the test suite verifies this using an unrelated dependent view. A separate test adds an extra managed CHECK and verifies removal is refused before destructive DDL. Unrelated schemas survive a successful downgrade.

Normal downgrade leaves an empty `public.alembic_version` so Alembic can locate history consistently. Only the test harness removes that tracker after proving it did not exist before the lifecycle, the target is the verified test database, the managed schemas are absent and history is empty. It never drops either database.

On unexpected identity, objects, history, rows or dependencies, preserve the database and investigate. On a PostgreSQL operation failure, transactional DDL rolls back; inspect `current --check-heads`, history and catalogs before retrying. Do not use `stamp`, force-drop objects, overwrite data or change the recorded revision to hide drift. This revision's empty-schema downgrade is not a recovery mechanism for populated databases. Plan a separate reviewed forward repair or backup/restore procedure before any future data-bearing change.

## Isolation and automated tests

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

Actual final migration-only run: **71 passed in 1.36s**, no failures/skips. Complete suite: **345 passed in 6.96s**, including all **274 unchanged regressions**, no failures/skips. The development parity test obtains a read-only engine; destructive lifecycle operations use only the explicitly verified test target.

## Independent physical verification and drift

Expectations are parsed from the approved Day 4 Markdown through the existing independently authored ORM-test fixture, including documented common CHECK conventions and the additional-index table. Tests compare these expectations directly to PostgreSQL catalogs and SQLAlchemy reflection, independently of the revision's contents.

Every column's PostgreSQL type, precision/scale, nullability, default and stored generated expression is compared. Every PK/UNIQUE key's ordered columns and every FK's ordered local/remote columns, schema, RESTRICT actions, MATCH SIMPLE, deferred flags, validation and enforcement are checked. Index checks include ordered key columns, ASC/DESC/null ordering, uniqueness, B-tree method, validity, key count and partial predicates.

For all 207 CHECKs, both approved and actual expressions are normalized with PostgreSQL `EXPLAIN (VERBOSE, FORMAT JSON, COSTS FALSE)` in the same typed table context. This is read-only and handles PostgreSQL's casts, parentheses and `IN`/`ANY` rewriting without stripping boolean precedence or altering literal case. Typed SELECT-only probes then compare exact TRUE/FALSE/NULL behavior and demonstrate both rejection and CHECK acceptance for every rule, including sparse NULLs, boundaries, valid/invalid states, JSON shapes, dates and numeric NaN. Catalog flags confirm all constraints are validated and enforced. Application tables are empty within the isolated migration lifecycle; the development check instead fingerprints and preserves its seeded records. Alembic's check alone is not treated as CHECK proof.

| Application object | Expected | Actual | Result |
| --- | ---: | ---: | --- |
| Business tables | 32 | 32 | PASS |
| Orchestration tables | 1 | 1 | PASS |
| Total tables | 33 | 33 | PASS |
| Columns | 261 | 261 | PASS |
| Primary keys | 33 | 33 | PASS |
| Foreign keys | 64 | 64 | PASS |
| Additional UNIQUE constraints | 14 | 14 | PASS |
| CHECK constraints | 207 | 207 | PASS |
| Explicit indexes | 14 | 14 | PASS |

The 14 explicit indexes include seven partial indexes, three of them unique. PostgreSQL additionally creates 47 supporting PK/UNIQUE indexes, yielding **61 application indexes**. `public.alembic_version` adds one framework table/column, one PK and its supporting index; these are excluded from application counts. PostgreSQL 18 also reports **215 native NOT NULL constraints** (`contype='n'`) for application columns; these are distinct from the 207 CHECKs. See the [PostgreSQL 18 constraint catalog](https://www.postgresql.org/docs/18/catalog-pg-constraint.html).

There are no pending Alembic operations, missing constraints/indexes, unexplained differences or unexpected managed objects. The physical names/nullability fingerprint matches the frozen revision value `8ef8e5a94fd625915001aa834f4a37cddf20abc122a087f934d227b7424da9ff`.

## Environment, preservation and resolved issues

Python 3.12.14; SQLAlchemy 2.1.8; asyncpg 0.32.0; PostgreSQL 18.6; Alembic 1.20.1; uv 0.12.12. The existing virtual environment was reused. Only Alembic and its Mako 1.4.3 / MarkupSafe 3.0.4 dependencies were added. All 23 previously installed distribution versions were preserved. Installation used `uv add --no-sync 'alembic>=1.16,<2'` and `uv sync --locked --inexact` (both exit 0); `uv lock --check` passed.

Issues resolved during implementation:

- Identity preflight initially rejected PostgreSQL's `127.0.0.1/32` text rendering. Using `host(inet_server_addr())` compares the actual address correctly; no migration ran on the rejected attempt.
- The generated cyclic FKs required explicit late create/drop operations before deployment, as described above.
- Early behavior-probe verification needed BIGINT values and sparse NULL combinations for compound approval rules. Only the verifier changed; schema definitions remained correct.
- The first lifecycle fixture compared reflected TypeEngine object identities. It now snapshots exact compiled PostgreSQL type spellings. Its guarded finalizer successfully restored the test database even on that failed attempt.
- `uv lock --check` first hit the filesystem sandbox's uv-cache restriction, then passed with authorized cache access. A package-preservation audit initially compared differently cased distribution names; normalized package names showed zero version changes.
- A supplemental `ruff format --check .` also inspected Markdown examples and flagged existing quote styles in `docs/data/data_integrity_report.md` and `docs/development/database_connection.md`. Those earlier-component documents were preserved byte-for-byte. The existing Python formatting scope (`src tests`), expanded to `alembic`, passes; root-wide lint also passes.

Final preservation checks matched all 681 frozen dataset files, all 637 protected asset checksums and the archive SHA-256. The five Day 4 documents, all ORM files and Component 2 code/tests/configuration are byte-identical. Development was upgraded only and retains its original database OID/owner; no pre-existing data existed, and no rows were imported. Test is again public-only with zero relations and no version tracker. Secrets remain private/ignored. No Git staging, resetting, cleaning, commit, push or Component 5 work occurred. See the appended [Day 05 log](../dev_log/day_05.md) for command results and Git status snapshots.

## Revision 20261109_01: capacity key per snapshot (approved TD-005, 2026-11-09)

`production_capacity` now has `UNIQUE (workcentre_id, week_start, snapshot_id)`, which replaces `(workcentre_id, week_start)`. Several capacity snapshots can therefore hold the same week. No data changes. `seeding/safety.py` pins `REVISION = "20261109_01"` and the measured shape digest `6cc43f0e...`.

The Day 4 schema documents are unchanged. The tests apply this single approved delta to them, and assert the original key first.

The downgrade back to `20261108_01` restores the old key. PostgreSQL refuses it while overlapping snapshots exist; nothing is deleted.

Development was upgraded with:

```
.venv/bin/python -m alembic -x target=development -x env_file=.env upgrade head
```
