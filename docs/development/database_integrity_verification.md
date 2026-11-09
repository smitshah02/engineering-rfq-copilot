# Day 5, Component 6 — Database integrity verification

This component independently inspects PostgreSQL through read-only SQL and tests actual constraint enforcement only in the isolated test database. The approved [Day 4 design](../database/table_schemas.md), [relationships](../database/relationships_and_constraints.md), [mapping](../database/dataset_to_database_mapping.md), ORM, migrations and frozen v1.1.0 sources remain unchanged. No development import is run.

## Implementation and method

`src/rfq_copilot/integrity.py` provides `fingerprint(session)`, `audit_integrity(session, config, plan)` and `run_integrity(config, plan)`, plus a verification-only module CLI. It reuses DatabaseConfig/Database, metadata, existing target/head/schema safety, checksum-validated source mapping and read-only snapshot validation. It defines no engine factory, session foundation, schema, migration or write operation.

The caller-session functions never commit or modify records. `run_integrity` owns the existing Database resource and explicitly uses server-enforced READ ONLY, REPEATABLE READ. Reports include only identifiers of approved tables/columns, counts, constraint metadata and digests; no business payloads, passwords, URLs or evaluation answers.

Checks cover:

- All 33 PK tuples, including composites, and live COUNT versus COUNT(DISTINCT ROW(PK...)).
- All 64 FK definitions: local/remote columns, schemas, MATCH SIMPLE, ON DELETE/UPDATE RESTRICT and both genuinely deferred quote pointers. FK names use SQLAlchemy's PostgreSQL identifier shortening before comparison. Every populated FK is independently checked with a full-tuple NOT EXISTS anti-join; nullable MATCH SIMPLE tuples are correctly exempt when any component is NULL.
- All 14 additional UNIQUE definitions and 17 data-uniqueness checks (14 constraints plus three unique partial indexes). Grouping respects SQL NULL uniqueness and the exact partial predicate. Existing schema safety also verifies every explicit index and predicate.
- Every non-nullable column and all 207 approved live-equivalent CHECK predicates. Required defaults, computed expressions, numeric types/precision/scale and physical definitions are checked through the existing schema verifier and current metadata; existing independent Day 4 migration tests verify that metadata against the approved documents.
- Actual PostgreSQL numeric results are Decimal. Compare all source-derived/default/generated values, including date, UTC timestamp, boolean, exact JSONB, source IDs, composite projections, snapshot tokens and nullable historical facts. Stored JSONB is selected as text and decoded with Decimal to retain exact numeric tokens and string evidence. Expected values reuse Component 5's checksum-validated audited transformations; actual reads and comparisons are separate from importer counters and its stored-row verifier.
- Registration timestamps are present and aware, without inventing historical effective dates. Full-table fingerprints include them. Seven runtime tables must remain empty; older historical quote facts remain absent as documented.

Inventory follows the actual constraint `0 <= reserved_kg <= quantity_kg`; it does not add an unsupported reserved-versus-remaining comparison. Capacity respects nominal/downtime/scheduled bounds and Monday weeks. Runtime dimensional CHECKs and the source manufacturing validations are distinct: this audit does not add manufacturing limits to the physical schema or implement an engine.

## Fingerprints and preservation

For every approved table, PostgreSQL converts complete rows to JSONB, sorts their canonical text, aggregates, and computes a built-in SHA-256 over UTF-8 bytes. Empty tables hash `[]`. A second SHA-256 over the sorted mapping of table names, counts and hashes identifies the whole application state. No pgcrypto extension is required. Registration times, nested JSON, nullable fields and generated values are included.

Identifiers in this audit's metadata-generated SQL come only from the registered approved tables/constraints; no caller input is interpolated. Before/after audit comparisons use one consistent snapshot. Separate development connections capture the state before work and after all tests/queries, so preservation does not depend solely on a same-transaction snapshot comparison.

## Commands

```bash
.venv/bin/python -m rfq_copilot.integrity --target development --source data/rfq_synthetic_data --archive data/rfq_synthetic_data_v1.1.0.zip --snapshot-dir data/rfq_seed_snapshots --env-file .env
.venv/bin/python -m pytest tests/test_database_integrity.py -q
```

An explicit supported local target, source directory, authenticated archive and stable operational snapshot directory are required. Success returns JSON and exit 0; safe verification failures return nonzero. This CLI cannot seed, reset, truncate or migrate a database.

## Invalid operations and transaction tests

`tests/test_database_integrity.py` imports and reuses Component 5's guarded test database, bundle, plan and rollback fixtures. Each populated fixture imports only into `rfq_copilot_test`, within a rollback-only test transaction. The fixture checks its original empty catalog, owns the existing lifecycle lease, migrates through the approved Alembic head, and restores the original public-only catalog with zero user relations. Development connections remain read-only.

Actual INSERT/UPDATE tests cover duplicate PKs in all 26 populated tables, distinct-PK unique conflicts, missing required values, missing/simple/composite/cross-schema FKs, wrong route/workcentre pairs, negative and over-reserved stock, invalid capacity, non-Monday weeks, invalid money/NaN/overflow, invalid states, and invalid quote versions/approval references. Tests cover all three unique partial indexes and prove applicable predicate-excluded rows can coexist. Runtime dimensional/composite-uniqueness cases use isolated test-only rows.

PostgreSQL errors are checked by exact SQLSTATE: 23505 unique, 23503 foreign key, 23502 NOT NULL, 23514 CHECK, 22003 numeric overflow, and PostgreSQL 18's 23001 RESTRICT violation for referenced-parent DELETE. Rejection happens inside a savepoint, followed by rollback, SELECT 1 and unchanged table count, proving session reuse. Numeric overflow is checked as a DBAPI error rather than incorrectly assuming every SQLSTATE class is IntegrityError.

Multi-record batches explicitly test whole-savepoint rollback for FK, UNIQUE and CHECK failure, comparing complete fingerprints. Separate tests exercise a real top-level commit, explicit rollback, exception rollback, actual async cancellation after a write and session/pool cleanup. The one committed test customer is the exact approved source row and is removed only by its owning fixture in a finally block on the guarded test target; no database reset or development mutation occurs.

An otherwise legal FK association can still differ from the imported source. Such a case is detected by source parity, rather than pretending a nonexistent cross-record business constraint rejects it. The tests do not alter the approved schema to impose additional rules.

## Execution evidence

The initial independent audit confirmed 33 tables, 261 columns, 1,089 records, seven empty runtime tables, 64 FK checks, 207 CHECKs, 17 unique-data checks, 9,587 matching source-derived values and 1,353 Decimal values. Development was current at `20261108_01`, authenticated as the restricted `rfq_copilot_dev` role. Source integrity matched 681 files, 637 protected hashes and the archive.

Final executed results follow below. Combined evidence is recorded in [database_components_6_7_results.json](database_components_6_7_results.json) and the Component 6/7 entries in the [Day 5 log](../dev_log/day_05.md).


## Component 6: Database Integrity Verification Report

**Executed:** 2026-11-08 (America/New_York). **Status: COMPONENT 6 COMPLETE.** Component 6 passed before Component 7 implementation began.

| Verification | Actual result |
| --- | --- |
| Database / authenticated role | rfq_copilot_dev / rfq_copilot_dev |
| Revision | 20261108_01 (current head) |
| Tables / columns / records | 33 / 261 / 1,089 |
| Populated / intentionally empty | 26 / 7 |
| Primary keys | All 33 exact definitions; zero duplicate identities |
| Foreign keys | All 64 definitions and data checks pass; actions, timing, composite and cross-schema links match |
| UNIQUE constraints / partial unique indexes | 14 / 3; all 17 data checks pass |
| CHECK constraints | All 207 definitions/data checks pass; actual PostgreSQL rejection tests pass |
| Decimal-compatible values | 1,353 exact Decimal results; schema precision/scale verified |
| Source/database value parity | 9,587 matching values; zero discrepancies |
| Invalid INSERT / UPDATE / DELETE | Real PostgreSQL rejection checks passed; session reused after savepoint rollback |
| Transaction behavior | Commit, explicit/exception/FK/UNIQUE/CHECK/multi-row rollback, cancellation and resource cleanup passed |
| Focused tests | 80 passed, 0 failed/errors/skipped; 31.24s |
| Before / after records | 1,089 / 1,089 |
| Before / after fingerprint | Identical SHA-256 `05f65a5515aae4c277f8e08a4437dcb7255772b69e19935fabb30902637aa6ee` |
| Test cleanup | Original public-only catalog restored; zero user relations |
| Source integrity | 681 original files, 637 protected hashes and archive verified |

A second independently written SELECT-only projection also confirmed all 9,587 values, every FK/CHECK/required field and whole-row table fingerprints before and after the full suite. Development data was never reimported or modified. Both the separate audit and the query example use the same complete SHA-256 algorithm; the prior Component 5 independent verifier supplies an additional unchanged per-table server digest comparison.

**Files created/modified:** `src/rfq_copilot/integrity.py`, `tests/test_database_integrity.py`, this guide, the combined `database_components_6_7_results.json`, and the separate Component 6 entry in `docs/dev_log/day_05.md`. Existing ORM, seeding implementation, migration/tests, connection/configuration and dependency files are unchanged.

**Resolved findings:** PostgreSQL shortens long naming-convention identifiers, so compare dialect-rendered names. Actual PostgreSQL 18 DELETE RESTRICT reports 23001, not the initially assumed 23503; numeric overflow is 22003 and may be a base DBAPIError. A DELETE fixture must choose a genuinely referenced user. A source-association fixture must avoid first violating the unrelated primary-contact index. These were test/audit assumptions, not schema faults; no schema or source was changed. Intermediate fixture runs had failures, followed by clean 80-test and 593-test final runs. JSONB comparison additionally rejects boolean/numeric type substitutions that plain Python equality would consider equal.

**Unresolved problems: none.** No Component 8 work was performed.
