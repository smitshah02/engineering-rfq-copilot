# Component 5: Synthetic Dataset Seeding Verification Report

**Executed:** 2026-11-08 (America/New_York). All results below are actual executions. Full machine-readable results are in [database_seeding_results.json](database_seeding_results.json); mapping, conversion/dependency audit and recovery instructions are in [database_seeding.md](database_seeding.md).

## 1. Environment

| Item | Verified value |
| --- | --- |
| python | `3.12.14` |
| sqlalchemy | `2.1.8` |
| asyncpg | `0.32.0` |
| alembic | `1.20.1` |
| postgresql | `18.6` |
| dataset | `1.1.0` |
| revision | `20261108_01` |
| database | `rfq_copilot_dev` |

The authenticated database role/owner is `rfq_copilot_dev`; development OID 16389 and test OID 16390. PostgreSQL uses UTF-8, loopback port 5432 and the existing restricted nonsuperuser role. No credential is reproduced here.

## 2. Seeding Architecture

Eight modules under `src/rfq_copilot/seeding/` implement allowlisted UTF-8 readers, strict exact-number/UTC/NULL conversion, approved source projections, FK-based topological ordering, nested/cross-record validation, live target/head/schema safety, a locked atomic loader and an explicit CLI. Reuses the existing DatabaseConfig/Database engine/session owner and registered ORM metadata; no additional engine/base/table/create_all path.

One explicit async transaction inserts bounded batches of 100 in dependency order. Only the two approved quote-header pointers are deferred; all constraints are forced immediate and exact stored rows checked before commit. Transactional advisory locks coordinate imports and migrations; sorted SHARE table locks prevent concurrent writes/DDL during apply. Dry-run/verify use server-enforced read-only REPEATABLE READ. All-empty imports, all-identical replays and conflicting/partial-state rejection are distinct outcomes. Replays issue zero business writes. Validation and driver errors are sanitized; sessions/pools and temporary files close on failure/cancellation.

## 3. Source-to-Database Mapping

Paths below are relative to `data/rfq_synthetic_data/`. Every row is accounted for; “imported” refers to the committed initial development import.

| Source file(s) | Target table | Expected records | Actual imported | Status |
| --- | --- | ---: | ---: | --- |
| `transactional_data/approval_history.csv` | `business.approval_history` | 70 | 70 | PASS |
| `config/manufacturing_rules.json`<br>`config/pricing_rules.json`<br>`config/approval_policies.json`<br>`config/workflow_state_rules.json`<br>`contracts/entity_schemas.json`<br>`contracts/tool_schemas.json`<br>`contracts/business_constraints.json`<br>`contracts/agent_coordination_policy.json`<br>`contracts/agent_handoff_schema.json` | `business.business_rule_versions` | 9 | 9 | PASS |
| `master_data/contacts.csv` | `business.contacts` | 40 | 40 | PASS |
| `master_data/customers.csv` | `business.customers` | 40 | 40 | PASS |
| `operations_data/material_inventory.csv`<br>`operations_data/production_capacity.csv`<br>`config/generation_config.yaml` | `business.data_snapshots` | 2 | 2 | PASS |
| `master_data/finishes.csv` | `business.finish_material_categories` | 19 | 19 | PASS |
| `master_data/finishes.csv` | `business.finishes` | 7 | 7 | PASS |
| `master_data/materials.csv` | `business.material_finish_compatibility` | 50 | 50 | PASS |
| `operations_data/material_inventory.csv` | `business.material_inventory` | 90 | 90 | PASS |
| `master_data/materials.csv` | `business.material_supported_operations` | 90 | 90 | PASS |
| `master_data/materials.csv` | `business.material_thickness_options` | 90 | 90 | PASS |
| `master_data/materials.csv` | `business.materials` | 15 | 15 | PASS |
| `operations_data/operation_routing.csv` | `business.operation_routing` | 28 | 28 | PASS |
| `master_data/operations.csv` | `business.operations` | 8 | 8 | PASS |
| `master_data/roles_permissions.csv` | `business.permissions` | 12 | 12 | PASS |
| `operations_data/procurement_records.csv` | `business.procurement_records` | 12 | 12 | PASS |
| `operations_data/production_capacity.csv` | `business.production_capacity` | 80 | 80 | PASS |
| `transactional_data/quote_line_items.csv` | `business.quote_line_items` | 100 | 100 | PASS |
| `transactional_data/quote_versions.csv`<br>`transactional_data/historical_quotes.csv` | `business.quote_versions` | 96 | 96 | PASS |
| `transactional_data/historical_quotes.csv` | `business.quotes` | 80 | 80 | PASS |
| `transactional_data/historical_rfqs.json` | `business.rfqs` | 80 | 80 | PASS |
| `master_data/roles_permissions.csv` | `business.role_permissions` | 27 | 27 | PASS |
| `master_data/users.csv`<br>`master_data/roles_permissions.csv` | `business.roles` | 6 | 6 | PASS |
| `master_data/suppliers.csv` | `business.suppliers` | 6 | 6 | PASS |
| `master_data/users.csv` | `business.users` | 24 | 24 | PASS |
| `master_data/workcentres.csv` | `business.workcentres` | 8 | 8 | PASS |

**Total: 1,089 committed rows in 26 tables.** Inputs: 17 CSV files/731 rows, historical RFQ JSON/80 rows, nine registry documents and one generation provenance document (28 mapped files). The 278 additional normalized/support rows are only the documented catalog/RBAC projections and registries.

**Intentionally empty (expected/actual 0 each):** `business.rfq_line_items`, `business.rfq_attachments`, `business.pricing_calculations`, `business.approval_requests`, `business.idempotency_records`, `business.audit_events`, `orchestration.workflow_runs`. No scenario/attachment/evaluation fixture table exists. **Blocked/unsupported approved mappings: none.** Later runtime tables deliberately have no seed records.

The 96 source versions remain 80 `priced_partial` current versions and 16 `metadata_only` older versions. Missing historical policy/price/snapshot/calculation facts remain NULL. No queue decisions, actors, receipts or audit/workflow events were fabricated.

## 4. Dry-Run Verification

- Integrity: **PASS**, 681 dataset files, 637/637 manifest hashes, matching archive SHA-256 and all 28 mapped files matched to the frozen archive, including configuration outside the manifest.
- Version: **1.1.0**. Validated target records: **1,089**. All immediate/deferred FK tuples and dependency order passed; database CHECKs evaluated using SELECTs.
- Database changes: **zero**; no snapshot artifact created. Before/after digest: `2e1fadaeba00288473aea3cac35735b57cae9738a0e0e4cfb0d804e6601d3687`.
- Real dry-run exit: **0**, **PASS**; read-only transaction, 0.4199s database-operation duration. A dry run was never counted as an import.

## 5. Initial Import

Exact command:

```bash
.venv/bin/python -m rfq_copilot.seeding.cli --target development --source data/rfq_synthetic_data --archive data/rfq_synthetic_data_v1.1.0.zip --snapshot-dir data/rfq_seed_snapshots --env-file .env --apply --confirm-database rfq_copilot_dev
```

Target **rfq_copilot_dev**, revision **20261108_01**, initially all 33 application tables empty. Entire transaction **committed** after validation. Inserted **1,089**; already-existing **0**; conflicted **0**; invalid **0**. Duration **0.4258s** (database operation, excludes source preparation). Exit **0**; **PASS**.

## 6. Post-Import Integrity

Fresh read-only connections independently ran per-table COUNT, COUNT(DISTINCT ROW(PK...)), all required-column NULL predicates, all 64 full-tuple FK anti-joins and all 207 actual live CHECK predicates. A separately written source projection compared **9,587 values**, without importer counters/readers/transform functions. It covered IDs, direct fields, catalog projections, header/version joins, generated arithmetic, full-precision JSON strings, Decimal values, ISO dates, UTC timestamps, absent historical facts and snapshot byte hashes.

| Verification | Actual |
| --- | ---: |
| Application records / independently expected | 1,089 / 1,089 |
| Application tables checked | 33 (32 business, 1 orchestration) |
| Populated / intentionally empty | 26 / 7 |
| Duplicate primary keys | 0 |
| NOT NULL violations | 0 |
| FK relationships checked / violations | 64 / 0 |
| CHECK predicates checked / violations | 207 / 0 |
| Source-value comparisons / mismatches | 9,587 / 0 |
| Older metadata-only / current priced-partial versions | 16 / 80 |
| Scenario RFQ identities in business.rfqs | 0 |

Every table's expected, actual and PK-distinct counts and server-side whole-row fingerprint are recorded in the JSON evidence. Decimal precision and aware time/calendar-date preservation passed exact typed equality. All generated inventory/capacity values matched source arithmetic. Schema stayed at **20261108_01** with no drift. Seven runtime tables stayed empty. Public Alembic metadata is outside these application-record counts.

## 7. Idempotency Verification

The exact apply command in section 5 ran a second time against the unchanged source. Total **1,089 before / 1,089 after**; second-run inserted **0**; already-existing **1,089**; changed existing records **0**; duplicates **0**. Exit **0**; **PASS** (0.3866s database operation).

Importer digest before/after: `266643daaee70e9a2442c11af09e0af33a72ffeac07a9e6e26e2d4b3579a5073`. Independent PostgreSQL whole-row fingerprints matched across **all 33 tables** before/after replay and after full regression. The test suite separately observed zero INSERT/UPDATE/DELETE statements on replay. Deliberate conflicts in customer data and row_version were rejected without overwriting them in isolated test transactions.

## 8. Evaluation Data Protection

**Confirmed:** no frozen evaluation ground-truth records, hidden expected outputs, hidden labels or security/coordination answers were imported or logged. All scenario splits remain offline; no development/validation/frozen fixtures were seeded. No hidden label was copied indirectly into JSON/metadata/audit fields.

Method: exact allowlist of 28 operational/provenance paths; no evaluation/scenario/attachment JSON reader inputs; reader instrumentation tests; rejection tests for hidden fields injected into nested Part/cost objects; source/archive integrity verification; only HRFQ IDs in the actual RFQ table; exact full-field database/source equality and seven empty runtime tables. Hidden files were accessed only as bounded binary SHA-256 streams, never decoded as answers. Only the two operational CSVs were archived for backend replay. This verifies the seeding boundary, not later runtime deployment permissions.

## 9. Automated Testing

| Test suite | Passed | Failed | Errors | Skipped | Duration |
| --- | ---: | ---: | ---: | ---: | --- |
| Focused seeding tests (98 source/unit + 25 PostgreSQL) | 123 | 0 | 0 | 0 | 11.67s |
| Existing tests, executed within the full run | 345 | 0 | 0 | 0 | 6.822s summed JUnit case time |
| New tests, executed within the full run | 123 | 0 | 0 | 0 | 12.626s summed JUnit case time |
| Full regression suite | 468 | 0 | 0 | 0 | 19.73s elapsed pytest time |

Summed case times exclude runner overhead and are not separate rerun wall times. Commands:

```bash
.venv/bin/python -m pytest tests/test_database_seeding.py tests/test_database_seeding_integration.py -q --junitxml=/tmp/rfq_component5_seeding_tests.xml
.venv/bin/python -m pytest -q --junitxml=/tmp/rfq_component5_full_tests.xml
```

JUnit XML stayed in `/tmp`. All test imports/DDL/negative cases used the explicitly guarded test target. The test catalog was restored to public-only with **zero user relations**, exactly matching its original schema/owner/comment snapshot. Development fingerprint checks retained all 1,089 committed records. The existing migration fixture still tests genuinely empty tables; only its obsolete development-empty assertion was replaced by read-only whole-record preservation.

## 10. Files Created or Modified

| File | Purpose |
| --- | --- |
| `src/rfq_copilot/seeding/__init__.py` | Seeding package and sanitized validation error |
| `src/rfq_copilot/seeding/sources.py` | Fixed source allowlist, strict CSV/JSON access and archive/manifest integrity |
| `src/rfq_copilot/seeding/transform.py` | Exact decimals/JSON, dates, timezones, booleans and NULL conversion |
| `src/rfq_copilot/seeding/mapping.py` | Complete Day 4 projections, historical joins, native/key validation and dependency ordering |
| `src/rfq_copilot/seeding/validation.py` | Nested Part/scalar guards and cross-record catalog/quote/approval validation |
| `src/rfq_copilot/seeding/safety.py` | Live identity, approved local target, head and physical schema verification |
| `src/rfq_copilot/seeding/loader.py` | Whole-baseline transaction, locking, snapshots, exact idempotency and verification |
| `src/rfq_copilot/seeding/cli.py` | Explicit dry-run/apply/verify actions, confirmation and safe JSON exit reporting |
| `data/rfq_seed_snapshots/.gitignore` | Keep the two backend operational CSV artifacts outside Git |
| `pyproject.toml` | Declare the already-installed jsonschema/PyYAML runtime validators |
| `uv.lock` | Lock those validators and their existing transitive dependencies |
| `tests/test_database_seeding.py` | 98 source/mapping/conversion/security/CLI cases |
| `tests/test_database_seeding_integration.py` | 25 isolated real PostgreSQL atomicity/conflict/rollback/cancellation cases |
| `tests/test_database_migrations.py` | Retain full physical checks and fingerprint seeded development rows; isolated tests still require empty tables |
| `docs/development/database_seeding.md` | Pre-import mapping audit and complete operator/developer runbook |
| `docs/development/database_seeding_verification_report.md` | This executed thirteen-part report |
| `docs/development/database_seeding_results.json` | Structured CLI, independent query, regression and Git evidence |
| `docs/dev_log/day_05.md` | Append-only Component 5 execution record |

Two content-addressed operational CSV snapshots were created under the ignored private `data/rfq_seed_snapshots/` directory (mode 0700; files 0444). They are byte-identical to the approved inventory/capacity inputs. Temporary audits, JSON and JUnit artifacts are under `/tmp/rfq_component5_*`; the independent query implementation remains `/tmp/rfq_component5_verify.py` for inspection. No dataset file was changed.

## 11. Preservation and Git

- **Dataset:** all 681 original path/byte hashes match; 637/637 protected manifest entries and the ZIP checksum pass. Protected manifests are unchanged. All 28 mapped files also match the authenticated ZIP.
- **Design:** all five Day 4 documents are byte-identical. Every ORM, existing config/engine/migration-safety module and Alembic file is byte-identical. Components 1–4 log text is retained as the exact prefix of the appended log.
- **Dependencies:** jsonschema/PyYAML declared and locked; `uv lock --check` and `uv sync --locked --inexact` returned 0. All **26** original installed distribution versions remain unchanged.
- **Checks:** root-wide Ruff lint, Python formatting for src/tests/alembic, application/model imports, SQLAlchemy connectivity and final head verification passed. `git diff --check` returned 0. Credential-bearing `.env` stays ignored, mode **0600**. Actual credentials/URLs are checked in-memory against versionable sources/docs and test evidence without printing them.
- **Git:** the initially dirty/untracked Components 1–4 remain; Component 5 adds the listed files. The Git index fingerprint is unchanged. No staging, commit, push, reset, restore or clean occurred. Exact status snapshots follow and are also in JSON evidence.

Before:

```text
 M pyproject.toml
 M uv.lock
?? .env.example
?? alembic.ini
?? alembic/
?? docs/data/
?? docs/database/
?? docs/dev_log/
?? docs/development/database_connection.md
?? docs/development/database_migrations.md
?? docs/development/database_models.md
?? src/rfq_copilot/__init__.py
?? src/rfq_copilot/config.py
?? src/rfq_copilot/database.py
?? src/rfq_copilot/migration_safety.py
?? src/rfq_copilot/models/
?? tests/conftest.py
?? tests/test_database_config.py
?? tests/test_database_integration.py
?? tests/test_database_migrations.py
?? tests/test_orm_models.py
```

After:

```text
 M pyproject.toml
 M uv.lock
?? .env.example
?? alembic.ini
?? alembic/
?? data/
?? docs/data/
?? docs/database/
?? docs/dev_log/
?? docs/development/database_connection.md
?? docs/development/database_migrations.md
?? docs/development/database_models.md
?? docs/development/database_seeding.md
?? docs/development/database_seeding_results.json
?? docs/development/database_seeding_verification_report.md
?? src/rfq_copilot/__init__.py
?? src/rfq_copilot/config.py
?? src/rfq_copilot/database.py
?? src/rfq_copilot/migration_safety.py
?? src/rfq_copilot/models/
?? src/rfq_copilot/seeding/
?? tests/conftest.py
?? tests/test_database_config.py
?? tests/test_database_integration.py
?? tests/test_database_migrations.py
?? tests/test_database_seeding.py
?? tests/test_database_seeding_integration.py
?? tests/test_orm_models.py
```

## 12. Problems Encountered

1. **Incorrect Day 4 version-field label:** the actual `properties.schema_version.const="1.1"` is in `contracts/agent_handoff_schema.json`, not `contracts/business_constraints.json`. The latter has no version field. The approved namespaced SHA-256 identity resolves the mapping without adding/defaulting a value or editing Day 4/source files.
2. **Distinct rejection prose:** an early preflight incorrectly demanded identical historical event reason and header rejection summary. The approved mapping preserves these as separate facts. That extra assumption was removed before any import; both supplied strings are retained without normalization. Review identities, versions, chronology and action summaries are validated.
3. **Native-value guard test fixture:** one intermediate focused run had 118 passes and one failure because a negative CHECK test supplied Python int -1 to a Decimal field; the newly added native validation correctly rejected it earlier. The fixture now supplies Decimal(-1), exercising the intended SQL CHECK path. Final focused and regression runs pass all cases.
4. **Schema race closed before import:** apply now acquires table locks after live identity and before schema verification, so concurrent DDL cannot slip between validation and writes. No schema/constraint is disabled or modified to seed data.
5. **Sandbox-only failures:** initial local PostgreSQL and uv-cache attempts were blocked by sandbox access. Explicitly approved retries succeeded. No credential, source or schema workaround was used.
6. **Known historical limits:** no complete older pricing snapshots, release-clearance evidence or historical runtime queues exist. Those fields/tables stay absent as specified. The frozen MFG-001 reference validator is not edited; the importer independently enforces scalar limits. Filesystem archive publication and database commit are separate resource boundaries; valid unreferenced archives can survive a rolled-back import. Interrupted commit acknowledgements require verification before retry.

There are **no unresolved approved mappings, missing records, failed final tests or import blockers**. No FastAPI endpoint, MCP server, pricing engine, multi-agent workflow or Component 6 was implemented.

## 13. Final Decision

**COMPONENT 5 COMPLETE:** All approved mapped records imported, integrity verified, idempotency confirmed and tests passed.
