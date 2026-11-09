# Day 5, Component 5 — Synthetic dataset seeding

## Mapping audit — completed before the first import

Authority: all five unchanged [Day 4 database documents](../database/dataset_to_database_mapping.md). Initial live read-only audit: `rfq_copilot_dev`, authenticated role/owner `rfq_copilot_dev`, PostgreSQL 18.6, revision `20261108_01`, all 33 approved tables empty. The independent Day 4 catalog verifier checked 261 columns, 33 PKs, 64 FKs, 14 additional UNIQUEs, 207 CHECKs and 14 explicit indexes. Test database `rfq_copilot_test` initially had only public and no user relations. Initial working tree already contained the uncommitted Components 1–4; those files are preserved.

Source preflight independently measured **681 files**, **637/637 protected hashes**, and matching archive SHA-256 `681a882fd7c22cd4e874b57fd31ab6bd12602bc826b2c02cfa79eb17a5ad5080`. The archive has 680 entries; the existing extra root `.DS_Store` accounts for the tree difference. All 28 allowlisted source documents also match that authenticated archive, including configuration outside the 637-file manifest. No source is regenerated or repaired.

The mapping covers 17 CSVs (731 records), historical RFQ JSON (80 records), nine rule/contract documents and one generation metadata file: **28 mapped files**, **811 business source records**, **1,089 target records**, **26 populated tables**, **seven intentionally empty runtime tables**. No unresolved mapping remains.

The Day 4 version-field label is incorrect: `properties.schema_version.const = "1.1"` belongs to `contracts/agent_handoff_schema.json`. `contracts/business_constraints.json` has no version property. Its approved identity is `business_constraints@sha256:<full exact-byte digest>`, preserving the complete object without inventing a version. All five contracts use namespaced content-addressed keys; the four policies retain their declared version IDs.

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

## Per-table conversion and dependency audit

CSV records are strict UTF-8 text dictionaries, with exact contracted header sets (column order may differ); historical RFQs are a JSON list of exact entity objects. Catalog array fields and two quote-line payloads are serialized JSON parsed once. IDs are preserved byte-for-byte; no trimming, fuzzy joins or generated replacement IDs. All source keys and target PK/UNIQUE tuples are checked for duplicates. Every immediate and deferred FK, including nullable composite MATCH SIMPLE relationships, is checked as a full tuple before database insertion.

### `business.business_rule_versions`

Dependencies: none.

Dates: registered_at: UTC aware timestamp.

Nullable fields: none.

### `business.customers`

Dependencies: none.

Decimals: credit_limit_usd: NUMERIC(18,2); credit_exposure_usd: NUMERIC(18,2).

Dates: created_date: ISO calendar date.

Nullable fields: none.

### `business.data_snapshots`

Dependencies: none.

Dates: as_of_date: ISO calendar date; registered_at: UTC aware timestamp.

Nullable fields: none.

### `business.operations`

Dependencies: none.

Nullable fields: none.

### `business.permissions`

Dependencies: none.

Nullable fields: none.

### `business.roles`

Dependencies: none.

Nullable fields: none.

### `business.suppliers`

Dependencies: none.

Nullable fields: none.

### `business.contacts`

Dependencies: customer_id → business.customers(customer_id).

Nullable fields: none.

### `business.finishes`

Dependencies: pricing_rule_version → business.business_rule_versions(rule_version).

Nullable fields: none.

### `business.materials`

Dependencies: pricing_rule_version → business.business_rule_versions(rule_version).

Decimals: density_kg_m3: NUMERIC(18,6); cost_usd_per_kg: NUMERIC(18,4); min_bend_radius_factor: NUMERIC(12,3).

Nullable fields: none.

### `business.role_permissions`

Dependencies: role → business.roles(role); permission → business.permissions(permission).

Nullable fields: none.

### `business.users`

Dependencies: role → business.roles(role).

Nullable fields: none.

### `business.workcentres`

Dependencies: operation_id → business.operations(operation_id).

Decimals: weekly_nominal_hours: NUMERIC(12,4); downtime_hours_per_week: NUMERIC(12,4).

Nullable fields: none.

### `business.finish_material_categories`

Dependencies: finish_id → business.finishes(finish_id).

Nullable fields: none.

### `business.material_finish_compatibility`

Dependencies: material_id → business.materials(material_id); finish_id → business.finishes(finish_id).

Nullable fields: none.

### `business.material_supported_operations`

Dependencies: material_id → business.materials(material_id); operation_id → business.operations(operation_id).

Nullable fields: none.

### `business.material_thickness_options`

Dependencies: material_id → business.materials(material_id).

Decimals: thickness_mm: NUMERIC(12,3).

Nullable fields: none.

### `business.operation_routing`

Dependencies: workcentre_id,operation_id → business.workcentres(workcentre_id,operation_id).

Nullable fields: none.

### `business.production_capacity`

Dependencies: snapshot_id → business.data_snapshots(snapshot_id); workcentre_id → business.workcentres(workcentre_id).

Decimals: nominal_hours: NUMERIC(12,4); downtime_hours: NUMERIC(12,4); scheduled_hours: NUMERIC(12,4); available_hours: NUMERIC(12,4); remaining_hours: NUMERIC(12,4); utilization_fraction: NUMERIC(9,4).

Dates: week_start: ISO calendar date.

Nullable fields: utilization_fraction.

### `business.rfqs`

Dependencies: customer_id → business.customers(customer_id); created_by → business.users(user_id).

Dates: submitted_at: UTC aware timestamp.

Nullable fields: raw_request, created_by.

### `business.material_inventory`

Dependencies: snapshot_id → business.data_snapshots(snapshot_id); supplier_id → business.suppliers(supplier_id); material_id,thickness_mm → business.material_thickness_options(material_id,thickness_mm).

Decimals: thickness_mm: NUMERIC(12,3); quantity_kg: NUMERIC(18,6); reserved_kg: NUMERIC(18,6); reorder_threshold_kg: NUMERIC(18,6); available_kg: NUMERIC(18,6).

Dates: expected_replenishment_date: ISO calendar date; snapshot_date: ISO calendar date.

Nullable fields: expected_replenishment_date.

### `business.quotes`

Dependencies: released_by → business.users(user_id); submitted_by → business.users(user_id); rfq_id → business.rfqs(rfq_id); quote_id,current_version → business.quote_versions(quote_id,version) [initially deferred]; approver_id → business.users(user_id); created_by → business.users(user_id); quote_id,approved_version → business.quote_versions(quote_id,version) [initially deferred].

Dates: created_at: UTC aware timestamp; approval_timestamp: UTC aware timestamp; released_at: UTC aware timestamp.

Nullable fields: submitted_by, approved_version, approver_id, approval_timestamp, rejection_reason, legacy_idempotency_key, released_by, released_at.

### `business.procurement_records`

Dependencies: inventory_id → business.material_inventory(inventory_id); supplier_id → business.suppliers(supplier_id).

Decimals: quantity_kg: NUMERIC(18,6).

Dates: ordered_date: ISO calendar date; expected_receipt_date: ISO calendar date.

Nullable fields: none.

### `business.quote_versions`

Dependencies: created_by → business.users(user_id); manufacturing_rule_version → business.business_rule_versions(rule_version); approval_rule_version → business.business_rule_versions(rule_version); quote_id → business.quotes(quote_id); workflow_rule_version → business.business_rule_versions(rule_version); calculation_id → business.pricing_calculations(calculation_id); pricing_rule_version → business.business_rule_versions(rule_version).

Decimals: total_usd: NUMERIC(18,2).

Dates: created_at: UTC aware timestamp; expiry_date: ISO calendar date.

Nullable fields: total_usd, pricing_rule_version, manufacturing_rule_version, approval_rule_version, workflow_rule_version, expiry_date, estimated_lead_business_days, source_snapshot_versions, calculation_id.

### `business.quote_line_items`

Dependencies: quote_id,quote_version → business.quote_versions(quote_id,version).

Decimals: line_total_usd: NUMERIC(18,2).

Nullable fields: none.

### `business.approval_history`

Dependencies: approval_request_id,quote_id,quote_version → business.approval_requests(approval_request_id,quote_id,quote_version); quote_id,quote_version → business.quote_versions(quote_id,version); actor_id → business.users(user_id).

Dates: action_at: UTC aware timestamp.

Nullable fields: approval_request_id.

## Transformation rules

- Decimal rejects nonfinite values, overflow and any nonzero precision beyond the target scale. Currency never passes through float. Nested JSON monetary/mass strings remain exact, including evidence with more than six decimal places. JSON numeric tokens parse as Decimal and bind through exact JSON text cast to JSONB; stored JSONB is read as text and reparsed the same way.
- ISO dates round-trip as YYYY-MM-DD. Timestamp inputs require a timezone and normalize to UTC. Strict integer parsing rejects fractional/bool inputs; CSV booleans accept only `True`/`False`. Empty strings become NULL only for documented nullable fields. Literal `none` remains a finish ID; restrictions prose is preserved.
- Distinct role/user and grant values produce six roles and 12 permissions. Catalog arrays produce 90 thickness options, 50 finish pairs, 90 operation pairs and 19 finish-category pairs. Every composite is unique and resolves to its catalog parents; finish categories agree.
- Inventory available kg and capacity available/remaining/utilization are verified against base values and omitted from INSERT. PostgreSQL recomputes them. Only utilization uses its documented four-place ROUND_HALF_UP policy. Capacity dates cover all ten Mondays beginning 2026-11-09. Snapshot as-of is 2026-11-02 from generation metadata, cross-checked against inventory dates.
- All 96 quote-version identities and flags survive. Only the matching 80 current versions receive supplied header financial fields (`priced_partial`). The 16 earlier versions are `metadata_only`; missing historical amounts, other policy pins, calculation and snapshot references remain NULL. USD is the documented dataset invariant. No old prices or lines are synthesized.
- Validate line ordinals, exact-version references, current sums, nested Part schema, material/thickness/finish/operation compatibility, IDs/quantities/totals, review actors/timestamps/status summaries and independent approvers. Enforce configured scalar hole/bend limits locally, including MFG-001. Header rejection summary and historical event reason are distinct source prose and are both preserved; the approved mapping does not require their text to be equal.

## Evaluation exclusion boundary

Only 28 fixed source paths can be parsed into a plan. No scenario, attachment, evaluation security/coordination annotation or ground-truth document is a data reader input. All `evaluation/**`, every `rfq_scenarios/**` split, and `attachments/**` are excluded. Hidden assets are streamed in bounded binary blocks solely for SHA-256 verification, never decoded as JSON or copied into records/logs. The complete evaluation ZIP is never archived for runtime access. Quote nested objects have approved field allowlists, preventing indirect benchmark-field additions. Tests enforce parser-call boundaries and adversarial nested-field rejection; this component does not claim deployment of later runtime RBAC.

## Dependency order

Computed from all 64 ORM FKs. Only the two documented, genuinely DEFERRABLE INITIALLY DEFERRED quote-header pointers are omitted from the topological graph. They are explicitly checked before import and forced IMMEDIATE before commit. All other FKs remain immediate; no constraint is disabled or dropped. Order (empty tables are visited without inserts):

`business_rule_versions` → `customers` → `data_snapshots` → `operations` → `permissions` → `roles` → `suppliers` → `contacts` → `finishes` → `materials` → `role_permissions` → `users` → `workcentres` → `finish_material_categories` → `idempotency_records` → `material_finish_compatibility` → `material_supported_operations` → `material_thickness_options` → `operation_routing` → `production_capacity` → `rfqs` → `material_inventory` → `pricing_calculations` → `quotes` → `rfq_attachments` → `rfq_line_items` → `workflow_runs` → `procurement_records` → `quote_versions` → `approval_requests` → `audit_events` → `quote_line_items` → `approval_history`.


## Architecture and resource ownership

The `rfq_copilot.seeding` package has eight small modules: `sources` (fixed readers/integrity), `transform` (strict wire conversions and exact JSON encoding), `mapping` (approved projections, native validation and FK order), `validation` (nested and cross-record business facts), `safety` (live identity/head/schema checks), `loader` (snapshots, comparison, transaction and verification), `cli` (explicit actions/JSON summaries), and `__init__` (safe error type). There is no second engine, session factory, declarative base, tracking table or schema-creation path. `DatabaseConfig` and `Database` retain their existing ownership and sanitization behavior.

`jsonschema` and `PyYAML` were already installed for dataset validation and are now declared/locked runtime requirements. JSON Schema validates the actual entity and Part contracts; safe YAML parsing reads generation provenance. All previously installed package versions remain unchanged. `uv sync --locked --inexact` preserves those tools.

## Transaction, concurrency and repeatability

Component 8 hardened stored JSON comparison to distinguish booleans from numeric zero/one. The seeder and integrity verifier share this type-aware comparison; a changed JSON type is a conflict even where Python's ordinary equality would consider the values equal. The isolated regression reproduced the previous gap before the fix. See the [Day 5 closure report](day_05_final_verification_report.md).

`run_seed` owns `Database`, a session and one explicit `session.begin()` for the entire baseline. `execute_seed` performs no commit; individual table loaders perform no commit. Inserts use the registered ORM Table metadata in batches of at most 100 rows. Owned sessions/pools close on success, error and cancellation. All parents and descendants roll back together on a source, FK, unique, CHECK or post-insert verification error. Deferred quote pointers are forced IMMEDIATE before the final exact-state check. Success is printed only after the transaction has completed.

Before touching business data, live checks verify local database/owner/current role, OID context, UTF-8, loopback:5432, PostgreSQL 18 and restricted role flags using the existing migration guard. Only `rfq_copilot_dev` and `rfq_copilot_test` are supported. Head must be exactly `20261108_01`. Schema verification checks the frozen migration shape, Alembic metadata/default/type drift, actual CHECK/generated expressions and partial-index predicates, and constraint/index validity. Unexpected triggers/policies block seeding.

Transactional advisory locks `(461441,504)` and `(461441,506)` coordinate with the migration lease and other seeders. An apply takes SHARE locks on all 33 tables in sorted order before schema/data verification, blocking concurrent business writes and DDL until completion. This is deliberately a small, maintenance-time baseline operation. Lock wait is bounded to 5 seconds; statement timeout is 30 seconds. A competing migration/import fails clearly without retry. There is no autonomous retry loop.

The recovery policy is deliberately all-or-nothing:

1. All 33 tables empty: import the complete validated baseline atomically.
2. Exact baseline already present: compare every source-derived/default/generated field, complete composite identities and immutable digests, verify archived artifacts, then report every record already existing. There are zero INSERT/UPDATE/DELETE statements on replay.
3. Any missing/extra record, populated runtime table, changed source value or row_version: refuse before business writes. Do not merge a partial database, overwrite operational changes, or hide conflicts with ON CONFLICT. No deletion/reset/truncate/downgrade command exists in this CLI.

`registered_at` is a present-time application registration timestamp, not a historical effective date. Verify it is populated/aware; it is the only non-source registration attribute excluded from canonical source equality. Full-table before/after digests include it, proving a replay preserves it. Every other seeded nullable/default/generated field is compared explicitly.

## Immutable operational snapshot artifacts

Only the exact approved inventory/capacity CSV bytes are archived under a dedicated private directory, separately from the frozen dataset and evaluation ZIP. Tokens are `inventory-v1.1.0-2026-11-02` and `capacity-v1.1.0-2026-11-02`. Content-addressed filenames include the CSV SHA-256. `artifact_ref` is the stable absolute backend path, which must remain the same on later runs. Source paths in the rule registry remain the approved logical repository-relative paths.

The importer requires directory mode 0700 and publishes mode-0444 files atomically from temporary files with no replacement. Existing artifacts must hash-identically. Temporary files are cleaned on publication failure. A failed database transaction may leave an unreferenced, valid content-addressed CSV; this is harmless and reused only after verification. The filesystem and PostgreSQL do not share a distributed transaction. Do not delete an archive referenced by a committed registry row. The directory's `.gitignore` keeps operational bytes out of Git.

## Commands

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

## Test isolation and verification

```bash
.venv/bin/python -m pytest tests/test_database_seeding.py tests/test_database_seeding_integration.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check src tests alembic
uv lock --check
```

The new integration fixture acquires the existing test lifecycle lease `(461441,505)`, verifies test identity and a completely empty original catalog, and applies only the approved Alembic head. Individual imports execute inside rollback-only test transactions. A real savepoint commit exercises `run_seed`'s success boundary under an outer rollback-only transaction. Negative tests inject failures after parents have been inserted, including immediate and deferred FK failures, CHECK/unique failures, application failure, cancellation and post-insert verification failure. The finalizer verifies zero test rows, uses the existing guarded empty-test downgrade/tracker cleanup, and matches the original catalog, even after assertion failure. It never deletes committed development data.

The former development-empty migration assertion is obsolete after this component. It now verifies the same exact physical design in a server-enforced read-only connection and compares complete server-side data fingerprints before/after. Every isolated migration lifecycle test still requires empty application tables. No earlier physical, constraint or isolation assertion was removed.

Independent actual post-import inspection used fresh read-only SQL queries: per-table COUNT versus COUNT(DISTINCT ROW(PK...)), every NOT NULL column, 64 full-tuple anti-join FK checks, all 207 live CHECK predicates, and complete table fingerprints using ordered `to_jsonb` aggregation. A separately written source projection compared 9,587 values, including exact decimals, aware timestamps, generated fields and nullable historical facts. It did not use importer counters or importer transformation functions. All 33 fingerprints matched before/after replay and after regression testing.

## Recovery and limits

- Invalid/missing/tampered source: retain the frozen package and manifest, inspect the safe location/rule diagnostic, and correct only importer defects. Obtain the approved original package separately if its bytes were corrupted; never regenerate a manifest or guess replacement data.
- Partial/conflicting database: stop. Investigate ownership/provenance and preserve operational records. There is no automatic merge, reset or destructive recovery option. A changed runtime baseline requires a separately approved design/migration/import policy.
- Missing/different snapshot: preserve registry references; recover the exact approved CSV bytes and original configured backend location under explicit operational control. Never silently overwrite an existing file.
- Interrupt/failure before commit: the owned transaction rolls back and resources close. If an interruption or connection loss occurs at the commit acknowledgement boundary, run verify and independently inspect the database before deciding whether to retry; do not infer rollback from a lost acknowledgement.
- This importer intentionally supports only frozen v1.1.0, the approved local databases, current initial head and the documented baseline. It is not an incremental loader or deployment/runtime RBAC implementation. Historical pricing/release evidence remains intentionally incomplete. The frozen MFG-001 validator remains unchanged; seeding independently enforces its scalar limits.

## Executed result

On 2026-11-08 (America/New_York), the real read-only dry run passed, the initial development transaction committed 1,089 rows, verification passed, and the second apply inserted zero rows with all 1,089 already identical. Seven runtime tables remain empty. The focused suite passed 123 tests in 11.67s; the full suite passed 468 tests in 19.73s, with no failures/errors/skips. See the complete [verification report](database_seeding_verification_report.md) and [structured execution evidence](database_seeding_results.json). Component 6 was not started.
