# Technical Debt and Deferred Decisions

Known gaps in the deterministic business services, each with an owner day. Add an entry when a limitation is deliberately deferred. Mark it resolved, with a link to the dev log, rather than deleting it.

| ID | Item | Found | Scheduled | Status |
| --- | --- | --- | --- | --- |
| TD-001 | `requested_operations` not modelled | Day 8 | **Phase 3 (Intake contract)** | **Deferred**: the Day 13 decision stands; Phase 2.5 added a fail-closed boundary (see below) |
| TD-002 | Procurement-delay default: dataset doc vs reference code | Day 9 | **Day 11** | Resolved (Day 11, [log](../dev_log/day_11.md)) |
| TD-003 | Reference-only constants not in a versioned rule (finish and pack minutes, 15-day procurement default) | Day 10 | **Day 12** | Resolved (Day 12, [log](../dev_log/day_12.md)) |
| TD-004 | `borderline_bend_radius` has no approved threshold | Phase 2.5 | Phase 2.5 follow-up | Resolved (`approval-v1.2`, see below) |
| TD-005 | Overlapping capacity snapshots cannot be stored (`UNIQUE (workcentre_id, week_start)`) | Phase 2.5 | Phase 2.5 follow-up | Resolved (revision `20261109_01`, see below) |
| TD-006 | Live snapshot freshness and the planning date belong at the request boundary | Phase 2.5 | Phase 3 (API and tool server) | **Open** |
| TD-007 | Read consistency and revalidation before persistence | Phase 2.5 | Phase 3 (API), quote-persistence phase | **Open: contract defined** |
| TD-009 (OBS-1) | Caller-injected `Database` is not verified read-only | Day 18 | Phase 3.5 | RESOLVED_VERIFIED (2026-11-10, see Phase 3.5) |
| TD-010 (OBS-2) | `GET /quotes/{quote_id}` searches quotation history in Python | Day 18 | Phase 3.5 | RESOLVED_VERIFIED (2026-11-10) |
| TD-011 (OBS-3) | `GET /materials` paginates in Python, not SQL | Day 18 | Phase 3.5 | RESOLVED_VERIFIED (2026-11-10) |
| TD-012 | No authoritative subject-to-customer binding; verified users get 403 on customer-scoped routes | Day 20 | Day 20 (approved 2026-11-10) | Resolved (revision `20261110_01`, see below); development DB migrated 2026-11-10 |
| TD-013 | Single static verification key; no JWKS rotation or token revocation | Day 20 | Phase 3.5 | RESOLVED_VERIFIED for rotation (2026-11-10); revocation split out as TD-024 |
| TD-014 | No read permission for the material catalog or released quotes; both mapped to `view_assigned_rfqs` | Day 21 | Permission-policy decision | **BLOCKED_APPROVAL**: implemented and tested behind `RFQ_API_PERMISSION_POLICY=v2`; policy data migration `20261111_01` unapplied (Decision A) |
| TD-015 | "Assigned" RFQs means the bound customer's RFQs; there is no per-RFQ staff assignment | Day 21 | Future authorization model | **BLOCKED_APPROVAL**: implemented and tested behind `RFQ_API_RFQ_ASSIGNMENTS=required`; table migration `20261111_02` unapplied (Decision B) |
| TD-016 | Attachment upload mapped to `create_rfq`; no RFQ-edit or attach permission exists | Day 21 | Permission-policy decision | **BLOCKED_APPROVAL**: as TD-014 (`attach_rfq_document`, Decision A) |
| TD-017 | One active customer per user; no approved model for staff working across customers | Day 21 | Future authorization model | **BLOCKED_APPROVAL**: implemented and tested behind `RFQ_API_CUSTOMER_CONTEXT=selectable`; index migration `20261111_05` unapplied (Decision B) |
| TD-018 | Orphan attachment file after a hard crash between storing and COMMIT | Day 19 | Phase 3.5 | RESOLVED_VERIFIED (operator reconciler; corrected 2026-11-11: inode reuse, storage/database pairing) |
| TD-019 | Ambiguous COMMIT outcome triggered blind file deletion | Day 19 / Phase 3.5 review | Phase 3.5 | RESOLVED_VERIFIED |
| TD-020 | Ancestors of the storage directory and of the JWT key's directory were not verified | Day 19 / Day 20 | Phase 3.5 | RESOLVED_VERIFIED |
| TD-021 | PDF parsing had no wall-clock bound (a thread cannot be stopped) | Day 19 | Phase 3.5 | RESOLVED_VERIFIED (killable child process) |
| TD-022 | Customer bindings can be provisioned only by direct SQL | Day 20 | Phase 3.5 | **BLOCKED_APPROVAL**: operator CLI `rfq_copilot.identity_admin` implemented and tested; needs a grant of `manage_customer_bindings` (Decision C) |
| TD-023 | Development backup verified readable but never test-restored | Day 20 | Phase 3.5 | **BLOCKED_EXTERNAL**: backup provenance re-verified 2026-11-11; restore needs a DBA-provided disposable database (Decision D) |
| TD-024 | No token revocation before `exp` except deactivating the user | Day 20 (split from TD-013) | Identity-provider integration | **BLOCKED_APPROVAL**: relying-party revocation implemented and tested behind `RFQ_API_TOKEN_REVOCATION=enforced`; table migration `20261111_03` unapplied (Decision E) |
| TD-025 | Validation-error registry: a forgotten registration was not detected | Day 17 | Phase 3.5 | RESOLVED_VERIFIED (detection test) |
| TD-026 | Request/response models are frozen but hold mutable lists | Day 17 | Phase 3.5 | RESOLVED_VERIFIED (2026-11-11): tuples in all request/response models; JSON contract byte-identical |
| TD-027 | Reconciler accepted binding markers without a validated version; fingerprint (name, OID) not unique across clusters | Phase 3.5 review | Phase 3.5 completion | RESOLVED_VERIFIED (2026-11-11) |
| TD-028 | Upload retry after an ambiguous 503 can create a duplicate | Phase 3.5 (TD-019 residual) | Phase 3.5 completion | **BLOCKED_APPROVAL**: idempotent replay implemented and tested behind `RFQ_API_UPLOAD_IDEMPOTENCY=enabled`; CHECK migration `20261111_04` unapplied (Decision F) |
| TD-029 | Rows whose attachment file is missing were only counted | Phase 3.5 (TD-018 residual) | Phase 3.5 completion | RESOLVED_VERIFIED (2026-11-11): `--list-missing` report plus manual audited runbook; no automatic repair by design |

## TD-001 — `requested_operations` not modelled

**Found:** Day 8 (Manufacturing Capability Validation). **Review and resolve:** Day 13 (Deterministic Pricing Engine).

**Current behaviour.** The application validator (`services/manufacturing.py`) derives a part's operations only from the stored `operation_routing` for its part type and finish. It reports `UNSUPPORTED_OPERATION` when a routed operation is not supported by the material or appears in the rule's `unsupported_operations`. Operations that a customer explicitly asks for (for example machining, casting or anodizing steel) are not represented.

**Why it was deferred.**
- `requested_operations` is not a field of the `calculate_quote` `Part` tool contract or of the Day 6 `Part` entity, and adding it would change both.
- No development or validation ground-truth extraction contains it, so there is nothing to check parity against.
- The reference `validate_part` checks it only against `unsupported_operations`. Day 5 seed validation allows it in stored historical quote-line snapshots (`part_json`) and checks it against the material's supported operations.
- Whether an operation can be priced (`operations.is_quoted`; `punch` and `machine` are catalog-only) is a pricing question.

**Day 13 must decide and record:**
1. Whether `requested_operations` becomes a `Part` field (an entity change plus a documented contract extension) or stays out of scope.
2. If it is added: validate it against the material's supported operations, the rule's `unsupported_operations` and `operations.is_quoted`, and define how requested operations combine with routed ones.
3. Regression tests for each of those three cases, plus a parity check against the historical `part_json` snapshots that carry the field.
4. How pricing treats an operation that is routed or requested but not quoted.

**Constraints.** Dataset v1.1.0, the frozen reference engines and the tool contracts stay unchanged. Any entity or contract change needs explicit approval first.

**Resolution (Day 13).**
1. **Not added in Phase 2.** `requested_operations` stays out of the `Part` entity and contract. It is absent from the `calculate_quote` contract and from every dev/validation extraction, so nothing could validate it, and adding it requires an approved entity and contract change. Revisit when Intake extraction (Phase 3) can produce requested operations; items 2 and 3 above then apply.
2. **Unquoted operations (item 4) are decided and enforced.** Pricing reads `operations.is_quoted` for every routed operation (`list_operations`, one SELECT) and refuses to price a line whose route contains an operation that is not quoted (`punch` and `machine` are catalog-only). This is a catalog defect, so it raises an error instead of producing a customer finding. Today's routing never routes them; the guard keeps it that way. Tests: `tests/test_pricing_service.py::test_unquoted_routed_operations_are_refused` and `tests/test_day13_pricing.py::test_unquoted_operation_in_the_catalog_blocks_pricing`.

## TD-002 — Procurement-delay default: dataset doc vs reference code

**Found:** Day 9 (Inventory Availability). **Review and resolve:** Day 11 (Lead-Time Estimation).

`docs/business_rules.md` §4 says that when stock is short, the procurement delay is the maximum supplier lead time of the matching stock rows, "otherwise 15 business days when no row exists". The reference `capacity_engine.check_inventory` instead uses 15 whenever no matching row has a **positive** `supplier_lead_business_days` (rows with a lead of 0 are ignored). The two differ only when every matching row has a lead of 0. v1.1.0 has no such row (leads are 7, 11, 15 or 19), and every catalog thickness has exactly one stock row. The 15-day default is also not in any versioned rule document.

**Day 9 behaviour:** no delay is computed. Each `MaterialRequirement` carries `shortage_kg`, `status`, `inventory_ids`, `snapshot_ids` and `supplier_lead_business_days` for every matching row. The Day 9 integration test shows these facts reproduce every labelled `procurement_business_days` in development and validation.

**Day 11 must decide and record:** which rule wins for zero-lead rows, where the 15-day default lives (a versioned rule rather than a constant), and regression tests for no row, zero-lead rows and the maximum of several rows.

**Resolution (Day 11).** `services/lead_time.py` follows the reference code: a short line waits for the maximum **positive** supplier lead among its matching stock rows, and 15 business days when there is none (no row, or only zero-lead rows). Reasons: the code is the executable oracle the ground truth was generated with, and it is the conservative reading (an unknown lead never means "immediate"). The documentation's "when no row exists" is the subset of this rule that v1.1.0 actually exercises. Regression tests: `tests/test_lead_time_service.py` (no row → 15, zero-lead row → 15, maximum of 7/19/0 → 19, sufficient stock → 0) and `tests/test_day11_lead_time.py::test_zero_lead_short_stock_uses_the_default_delay` against the test database. The 15-day value itself is still a constant because no versioned rule holds it; it moves to TD-003.

## TD-003 — Reference-only constants not in a versioned rule

**Found:** Day 10 (Production Capacity). **Review and resolve:** Day 12 (Manufacturing Cost Modelling), when operation minutes are reused for labour cost.

The reference `pricing_engine.operation_minutes` uses two values that appear in no versioned rule document: finishing at `2 sides × 0.30 min/m²` of blank area per unit, and packing at `0.75 min/unit`. Every other rate (cutting, hole allowance, bend, weld, inspection, setup minutes) comes from the pinned `price-v1.0` document, and bends per part type come from `mfg-v1.0`. `services/capacity.py` keeps the two values as named constants (`FINISH_SIDES`, `FINISH_MINUTES_PER_M2`, `PACK_MINUTES_PER_UNIT`), marked `ponytail:`, so capacity hours match the ground truth exactly.

**Added on Day 11:** `DEFAULT_PROCUREMENT_BUSINESS_DAYS = 15` in `services/lead_time.py` (see TD-002), from the reference code and `docs/business_rules.md`.

**Day 12 must decide and record:** whether these belong in a new, approved rule version (the seeded `price-v1.0` document cannot change; it is hashed from the frozen dataset), or stay as documented constants tied to `price-v1.0`. Add a guard so a different pricing rule version cannot silently reuse them.

**Resolution (Day 12).** The constants stay as named constants, because no approved rule version holds them and the seeded `price-v1.0` document cannot change (its sha256 comes from the frozen dataset). They are now bound to the rule set they came from: `capacity.REFERENCE_PRICING_VERSIONS = {"price-v1.0"}`, and `require_reference_constants` raises for any other pricing version. It is called by `operation_minutes` (finish and pack minutes, used for capacity on Day 10 and for unit run minutes on Day 12) and by `lead_time.plan_start` (15-day default). A future rule set must state these values explicitly, in a new approved rule version, and add itself to the guard; it cannot inherit them silently. Regression test: `tests/test_costing_service.py::test_td_003_reference_constants_are_refused_for_other_pricing_versions`.

## Phase 2.5 additions

The [Phase 2.5 hardening report](phase_02_5_hardening_report.md) gives the evidence for each item below.

### TD-001: Phase 2.5 update (still deferred)

`Part.from_mapping` now refuses a `requested_operations` key with an explicit TD-001 message. Before, the key was refused only through the generic "unknown fields" error, so it was never silently dropped. Now the reason is named. Tests: `tests/test_hardening_public.py::test_requested_operations_are_refused_at_the_part_boundary`.

**Until Phase 3 implements the feature, Intake must:**
- extract requested operations as evidence;
- never pass them to `calculate_quote`;
- route an RFQ that contains them to human engineering review, as an unresolved requirement.

**Proposed contract change** (needs approval; owner Phase 3 Intake):
- Add an optional `requested_operations: string[]` to the `calculate_quote` Part schema (tool contract 1.1 → 1.2) and to `Part`.
- Validate each operation against the material's supported operations, the rule's `unsupported_operations` and `operations.is_quoted`.
- A requested operation that the route does not include is a finding (`UNSUPPORTED_OPERATION` or `REQUESTED_OPERATION_NOT_ROUTED`). It is never added to the route silently.
- Add a parity test against the historical `part_json` snapshots that carry the field.

### TD-004: `borderline_bend_radius` has no approved threshold

`approval-v1.1.engineering_review_conditions` lists `borderline_bend_radius`. No approved document defines "borderline": not the dataset docs, the rule contracts or the reference engines. `BEND_RADIUS_TOO_SMALL` already declines radii below the minimum. Inventing a margin would be a business rule, and adding it to the outcome would change the labelled outcomes.

**Phase 2.5 behaviour.** Evaluation never reports the condition as passed. When a priced line is bent, `RfqEvaluation.unevaluated_review_conditions` contains `borderline_bend_radius`. An engineering condition that the service does not know raises an error.

**To resolve:**
1. The rule owner defines the threshold in a new, approved rule version, for example a radius within X % of `factor × thickness`.
2. The new version is registered in `rule_manifest.py`.
3. Evaluation implements the condition.
4. The Phase 3 review UI shows unevaluated conditions to the human reviewer.

### TD-005: Overlapping capacity snapshots cannot be stored

Migration `20261108_01` declares `production_capacity UNIQUE (workcentre_id, week_start)`. A second capacity snapshot cannot hold the same workcentre-week as the first. Reads are now filtered by snapshot, so the services already support several snapshots: weeks of different snapshots are never mixed, and duplicate weeks raise an error. Storing a newer forecast for the same weeks is what remains blocked.

**Proposed migration** (not applied; needs approval): a new revision that replaces the constraint with `UNIQUE (workcentre_id, week_start, snapshot_id)` and adds an index on `(snapshot_id, workcentre_id, week_start)`. `material_inventory` has no such constraint, so several inventory snapshots can already coexist.

### TD-006: Live freshness and the planning date at the request boundary

`lead_time.validate_planning_dates` checks the selected snapshots against the explicit `horizon_start`:
- both snapshots belong to one dataset version;
- neither snapshot is dated after the horizon;
- the capacity snapshot still covers the horizon;
- stock rows agree with their snapshot's as-of date.

It never reads the clock, so it stays deterministic.

**The future API must:**
1. Derive `horizon_start` from the request's evaluation date: the next Monday on or after it.
2. Reject a selected snapshot that is older than a business-approved maximum age, measured against that date. No such maximum is approved yet.
3. Pin snapshot IDs explicitly once more than one snapshot exists.

### TD-007: Read consistency and revalidation before persistence

One evaluation issues about 34 SELECTs (two-line RFQ). Under READ COMMITTED, a concurrent commit can land between two of them. `Database.snapshot_session()` gives a REPEATABLE READ, READ ONLY transaction, in which every statement sees one committed state. Services still never create sessions.

**Contract:**
- The Phase 3 API must run each evaluation inside `snapshot_session()`, one evaluation per session.
- Before persisting a draft, the quote-persistence phase must re-evaluate and compare `evaluation_fingerprint`. If it differs, it must not persist.

Material costs (`materials.cost_usd_per_kg`) have no version or snapshot. The fingerprint captures the values that were used, but nothing pins them.

## Phase 2.5 follow-up: TD-004 and TD-005 resolved (approved 2026-11-09)

Details and evidence are in the [hardening report addendum](phase_02_5_hardening_report.md#k-addendum-td-004-and-td-005-approved-2026-11-09).

**TD-004.**
- The approved threshold is a new policy version, `approval-v1.2`. It is identical to `approval-v1.1` except for:
  - `version`;
  - `supersedes`;
  - `change_reference`;
  - `engineering_review_thresholds.borderline_bend_radius.radius_below_minimum_ratio = "1.25"`.
- A bent line is borderline when `minimum <= stated radius < 1.25 x minimum`, where `minimum = max(rule factor, material factor) x thickness`. An unstated radius is not borderline.
- A borderline line routes the RFQ to `engineering_review_required`, at the same outcome tier as capacity and delivery problems. Its escalation reason, `BORDERLINE_BEND_RADIUS`, ranks last. Drafting is unaffected.
- The source is `src/rfq_copilot/rules/approval-v1.2.json`. It is registered in `rule_manifest.py` and stored by `python -m rfq_copilot.seeding.rules approval-v1.2`. It is registered in development.
- Evaluations pinned to `approval-v1.1` behave exactly as before.

**TD-005.**
- Alembic revision `20261109_01` replaces the capacity key with `UNIQUE (workcentre_id, week_start, snapshot_id)`. The ORM matches it.
- Downgrade restores the old key, and refuses while two snapshots share a week.
- Development has been upgraded.

## Pre-commit review findings (2026-11-09)

### TD-008: Public dataset subset (deferred)

**Decision, 2026-11-09:** the complete dataset v1.1.0 stays private, and `.gitignore` excludes the directory and the archive. Publishing a sanitized, development-only subset will be reconsidered later.

**Before any subset is published:**
- Leave out everything that reveals frozen answers:
  - `evaluation/ground_truth/frozen_evaluation/` and `rfq_scenarios/frozen_evaluation/`;
  - the frozen rows in `evaluation/agent_coordination_cases.json` and `evaluation/failure_scenarios.json`;
  - `evaluation/scenario_manifest.json` and `evaluation/checksums.sha256.json`;
  - the dataset's generator (`src/`, `config/generation_config.yaml`, `tests/`), which rebuilds the frozen answers exactly.
- Add an approved verification profile. Today `load_sources` requires the full archive, matched by its SHA-256, and all 637 protected hashes, frozen files included.
- Choose a licence. Neither the dataset nor the repository has one.

**Resolved in the same review:**
- **File-count portability.** The archive holds 680 files; the 681st in the working copy is a macOS `.DS_Store`.
  - `load_sources` now requires the extracted files to equal the archive's file list exactly. Files named exactly `.DS_Store` are ignored, in any folder.
  - Any other extra, missing or changed file fails. All 637 protected SHA-256 checks still run.
  - Tests cover a clean extraction, `.DS_Store` in two places, six kinds of unexpected file, missing files and a changed protected file.
- **Snapshot directory mode.** A clone creates `data/rfq_seed_snapshots/` as 0755 and seeding requires 0700 (fail-closed). The README documents `chmod 700`.

## Day 18 API review observations (recorded 2026-11-10, Day 19)

Verified, non-blocking findings from the Day 18 review. All three stay **OPEN** until a fix lands with the tests below.

### TD-009 (OBS-1): Caller-injected `Database` is not verified read-only

- **Origin:** Day 18, `create_app(database=...)` and `api/db.py`.
- **Risk:** the lazily built database is created with `read_only=True`, so PostgreSQL refuses writes. A `Database` passed to `create_app` is trusted as is. A writable one would serve the protected GET routes through a session that can write, so one mistaken call in a read path could change data.
- **Recommended correction:** at first use, check `SHOW default_transaction_read_only` (or `SHOW transaction_read_only`) on the injected database, and refuse with 503 when it is off. Alternatively, run `SET TRANSACTION READ ONLY` at the start of every read session.
- **Verification:** a test that injects a writable `Database` and gets 503 (or a refused write) from a GET route. The existing read-only lazy-database test still passes.
- **Day 19 note:** attachment uploads use a separate write path (`get_write_session`). They never reuse the read session or the injected read `Database`, so this finding is neither widened nor fixed.

### TD-010 (OBS-2): Quote lookup loads the customer's history

- **Origin:** Day 18, `GET /api/v1/quotes/{quote_id}`.
- **Risk:** the route loads every quotation of the customer and searches in Python. Cost grows with history size, and a large history can become a slow request.
- **Recommended correction:** a repository function that selects one released quote by `(quote_id, customer_id)` in SQL, with the release filter in the WHERE clause.
- **Verification:** a query-count test with a bounded number of SELECTs, independent of history size. The existing visibility tests still pass: released only, own customer only, 404 otherwise.

### TD-011 (OBS-3): Material pagination happens in Python

- **Origin:** Day 18, `GET /api/v1/materials`.
- **Risk:** the route fetches every matching material identifier and then slices the page. Cost grows with catalog size.
- **Recommended correction:** apply `ORDER BY material_id LIMIT/OFFSET` (limit + 1 for `has_more`) in the repository query.
- **Verification:** a test that the SQL carries LIMIT/OFFSET, plus a parity test showing pages match the current ordering and `has_more` behaviour.

## Day 20 additions (2026-11-10)

TD-009, TD-010 and TD-011 stay **Open**; Day 20 did not change them. The Day 19 limitations also still stand: orphan files after a hard process crash, an unchecked parent of the storage directory, and file/row consistency after an ambiguous commit (see `day_19_file_uploads_extraction.md`).

### TD-012: No authoritative subject-to-customer binding (blocker)

- **Origin:** Day 20. Bearer tokens are verified, and `sub` resolves to an active row in `business.users` through the unique `authentication_subject`.
- **Gap:** `Principal` needs exactly one customer. But `users` has no customer column, `contacts` (which belong to customers) has no authentication subject, and the dataset's security oracle takes customer scope from per-request session context, not from a stored record. No table relates an authenticated subject to a customer.
- **Current behaviour:** fail closed. `repositories.customer_binding` returns `None`, so every verified user gets 403 on customer-scoped routes. Customer claims in the token are ignored.
- **Rejected shortcuts:** matching email, region or RFQ authorship (`rfqs.created_by`), or trusting a token claim. Each would invent a relationship.
- **Recommended correction (needs approval):** a new Alembic revision with a table such as `business.customer_identities (authentication_subject TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES business.customers, contact_id TEXT REFERENCES business.contacts, active BOOLEAN NOT NULL)`, read by `customer_binding`. It should also require `customers.account_status` to allow access. Alternatively, Day 21 RBAC could define per-user customer assignments for internal roles.
- **Verification:** with the table, `test_day20_auth_database.py` uses the real binding instead of the stub. The stub-based tests already show the scoped SQL, the cross-customer 404 and the released-only quotes behind real tokens.

### TD-013: Static verification key, no rotation or revocation

- **Risk:** one PEM public key is read at startup. Rotation needs a restart with the new key, and tokens cannot be revoked before `exp` except by deactivating the user, which is checked on every request.
- **Recommended correction:** a configured local key set with explicit `kid` entries, never fetched from a URL a token names, plus a short `jwt_max_lifetime_seconds`.
- **Verification:** tests that an unknown `kid` is rejected and that two configured keys both verify.

### TD-012: resolved (approved 2026-11-10)

- **Revision `20261110_01`** adds `business.user_customer_bindings`:
  - columns `user_id` and `customer_id` (both TEXT, with RESTRICT foreign keys to `users` and `customers`), `created_at` (TIMESTAMPTZ, default `CURRENT_TIMESTAMP`) and `revoked_at` (TIMESTAMPTZ, nullable; null means active);
  - primary key `(user_id, customer_id)`;
  - identity checks on both ids, and a `revoked_at >= created_at` check;
  - a partial unique index `uq_user_customer_bindings_active_user (user_id) WHERE revoked_at IS NULL`, so a user has at most one active binding;
  - an index on `customer_id`.
- The migration is additive only. Its downgrade refuses while any binding row exists.
- `repositories.customer_binding` returns the customer only for exactly one unrevoked binding whose customer status is not `inactive`. Missing, revoked, ineligible or ambiguous bindings fail closed with 403.
- The seeding contract pins the new head and shape digest (`seeding/safety.py`). The table is a runtime table and is never seeded. Integrity catalog counts are updated.
- **Pending:** the development database stays at `20261109_01` until its migration is separately authorized. Until then, `integrity --target development` reports "Alembic revision is not current", 3 development-database tests fail, and authentication against development answers 503 (the bindings table is missing).

**TD-012 follow-up (2026-11-10):** the development database was migrated to `20261110_01` with authorization, after a verified `pg_dump` backup. Integrity PASS (1090 records); all pre-existing table fingerprints unchanged; the bindings table is empty. TD-009, TD-010, TD-011 and TD-013 remain **Open**.

## Day 21 additions (2026-11-10)

TD-009, TD-010, TD-011 and TD-013 stay **Open**; Day 21 did not change them. The Day 19 limitations still stand: orphan files after a hard process crash, parent-directory trust assumptions, and file/row reconciliation after an ambiguous commit.

Authorization uses only the 12 permission records in `business.permissions` and the grants in `business.role_permissions` (`allowed = true`). No permission, role or grant was added. The gaps below were mapped to the closest existing permission, not to new ones.

### TD-014: No read permission for the catalog or released quotes

- **Gap:** the vocabulary has `manage_catalog` (administration) but no catalog-read permission, and no quote-read permission. `GET /materials` and `GET /quotes/{quote_id}` require `view_assigned_rfqs`, since both serve work on the customer's RFQs.
- **Risk:** this differs from the dataset's reference tool policy (`security_policy.py`, test reference only). There, `get_material_specs` excludes `sales_manager`, and `get_quote_status` includes `auditor` and excludes `manufacturing_engineer`. Under the current mapping, sales managers can read the catalog, manufacturing engineers can read released quotes, and auditors cannot.
- **Proposed correction (needs approval of policy data, not code):** add permission records such as `view_catalog` and `view_quote`, with grants. Then change the two `require_permission` arguments and the matrix test.
- **Verification:** `test_rbac.py::MATRIX` and the per-role database test pass with the new names, and the per-role expectations match the approved grants.

### TD-015: "Assigned" means the bound customer

- **Gap:** `view_assigned_rfqs` implies an assignment, but the schema has no RFQ-to-staff assignment. `rfqs.created_by` is authorship, not assignment, and was rejected as an identity source on Day 20. Today a permitted user sees every RFQ of the one customer they are bound to.
- **Risk:** broader than per-RFQ assignment once several staff are bound to one customer.
- **Verification:** an approved assignment table or rule, plus a test in which a bound and permitted user is denied an unassigned RFQ of the same customer.

### TD-016: Upload mapped to `create_rfq`

- **Gap:** attaching a document to an existing RFQ has no dedicated permission. `create_rfq` (RFQ intake) is the closest: sales representatives and estimators hold it, and the reference policy's write tools agree.
- **Risk:** sales managers and engineers cannot attach documents, and any future intake role that may create RFQs can also attach.
- **Verification:** an approved permission (for example `attach_rfq_document`) mapped in the matrix, with the database test showing the new per-role outcome.

### TD-017: Staff across customers

- **Gap:** all six roles are internal staff, but each user acts for at most one customer (the TD-012 single-active-binding invariant). Managers, auditors or administrators working across customers have no supported model. No impersonation or delegation exists, by design.
- **Verification:** an approved, audited delegation model, with tests showing that the customer is still never taken from client input.

## Phase 3.5 hardening (2026-11-10)

States: `OPEN`, `IN_PROGRESS`, `BLOCKED_APPROVAL`, `RESOLVED_VERIFIED`. The earlier entries above are kept as history. Full report: [phase_3_technical_debt_resolution.md](phase_3_technical_debt_resolution.md).

### TD-009: RESOLVED_VERIFIED

- **Root cause:** read routes trusted whatever `Database` was injected; only the lazily built one was read-only.
- **Correction:** `api/db.py::get_read_session` registers an `after_begin` listener that issues `SET TRANSACTION READ ONLY` as the first statement of every read transaction. It runs only when a route queries, so invalid requests still do no I/O. Uploads keep their separate write path. `snapshot_session()` and the evaluation sessions are untouched.
- **Tests:** `test_phase3_hardening_database.py`:
  - `test_real_writable_database_injected_into_reads_answers_503`: a writable `Database` was injected, the write is refused (503), and the `Database` itself stays writable;
  - `test_read_session_refuses_writes_on_a_writable_injected_database`: data unchanged, fixture transaction still writable.
  - Mutation "listener removed": 2 failed.
- **Residual:** none known. A caller can still pass a writable `Database`, but read routes cannot write through it.

### TD-010: RESOLVED_VERIFIED

- **Correction:** `repositories.get_released_quotation(session, quote_id, customer_id=...)` filters id, owner, `status = 'released'` and `released_at IS NOT NULL` in SQL. It shares the result builder with `list_quotation_history`. The route keeps its release and approved-version checks as defence in depth.
- **Query behaviour:** a released quote takes 3 SELECTs (quote with RFQ, versions, lines), independent of history size. A missing, unreleased or foreign quote takes 1 SELECT. Before, a missing quote took 3 SELECTs that loaded the whole history.
- **Tests:** keyed and bounded; one-query 404s; a foreign user with the same role gets 404; the Day 18 count updated 3 → 1 for a missing quote. Mutation "customer filter dropped": 2 failed.

### TD-011: RESOLVED_VERIFIED

- **Correction:** `find_materials(..., limit=, offset=)` orders by `material_id` and pages in SQL. Paging arguments are validated before any query. The route asks for `limit + 1` rows to compute `has_more`. Calls without `limit` are unchanged.
- **Tests:**
  - every page concatenated equals the full ordered catalog;
  - every identifier query carries LIMIT and OFFSET;
  - exact-size page, offset past the end, filtered paging, empty results;
  - bad paging arguments are refused without a session.
  - Mutation "back to Python paging": 1 failed.

### TD-013: RESOLVED_VERIFIED (rotation only)

- **Correction:** `RFQ_API_JWT_PUBLIC_KEYS="kid=/abs/a.pem,kid2=/abs/b.pem"` (1 to 4 keys), as an alternative to `RFQ_API_JWT_PUBLIC_KEY_FILE`; configuring both is refused.
  - Each key passes the descriptor-based secure loader.
  - The token's `kid` must equal a configured id exactly and selects only that key: no fallback, nothing fetched, `jku`/`jwk`/`x5u`/`x5c` still refused.
  - A key removed from the configuration stops verifying after restart.
  - Single-key mode is unchanged.
- **Tests:** overlap, wrong key for a kid, missing, unknown, near-miss and non-string kids, removal, environment parsing, duplicate, relative and too many entries, a symlinked key in a set, immutable key map. Live Uvicorn check with two keys. Mutation "kid fallback": 6 failed.
- **Residual:** revocation is a separate problem (TD-024). Rotation needs a restart.

### TD-014 to TD-017, TD-022: BLOCKED_APPROVAL

See Decisions A to C in the Phase 3.5 report. Nothing was changed: no permission, grant, assignment, table or binding was created.

### TD-018: RESOLVED_VERIFIED

- **Correction:** `python -m rfq_copilot.reconcile_attachments --storage-dir DIR [--grace-hours 24] [--delete]`.
  - Dry run by default; never runs at startup.
  - Opens the directory with the upload path's own checks, holds `flock`, considers only server-named regular files older than the grace period (minimum 1 h).
  - Before each removal it re-asks the database and re-checks the inode.
  - Prints aggregate counts only, including rows whose file is missing.
- **Tests:** dry run, delete, a row appearing or the file being replaced before removal, too-short grace, unsafe or symlinked directory, concurrent lock, counts-only output, and the DB lost-acknowledgement scenario.
- **Residual:** an operator must schedule it. A row whose file is missing is reported, never repaired.

### TD-019: RESOLVED_VERIFIED

- **Correction:** the upload flushes the INSERT first (a failure there is certain, so the file is removed). If COMMIT itself fails or is cancelled, the outcome is unknown, so the file is kept and logged, without its name, for the reconciler.
- **Tests:** a failed or cancelled commit keeps the file; a failed flush removes it; in the DB test, a commit that succeeds and then raises leaves both row and file, and the reconciler keeps them. Day 19 failure tests were moved to the flush stage, keeping their invariant. Mutation: 3 failed.
- **Residual:** the client gets 503 for an upload that may exist. A retry creates a second attachment, because uploads have no idempotency key (Decision C notes `idempotency_records`).

### TD-020: RESOLVED_VERIFIED

- **Correction:** new `rfq_copilot/paths.py::open_trusted_parent` resolves the parent once, then opens every component from `/` with `O_NOFOLLOW`. Each directory must be owned by root or the service user, and not writable by group or others unless sticky. The JWT key loader keeps its strict parent rule (no sticky parent), and the storage directory keeps 0700, owned by the service user.
- **Tests:** writable grandparents (777, 770, 757) refused; sticky ancestor allowed but not as the key's parent; foreign-owned ancestor refused; a symlink swapped in after resolution refused; storage under an untrusted ancestor refused (503, no file, no row). Mutation: 5 failed.
- **Residual:** an ancestor owned by a different non-root user is refused by design. Symlinked final paths, such as Kubernetes `..data`, are still refused.

### TD-021: RESOLVED_VERIFIED

- **Correction:** `attachments.extract_pdf_isolated` runs `python -I -m rfq_copilot.pdf_worker` with an empty environment, a 10 s CPU rlimit and a 1 GiB address-space limit (enforced on Linux, ignored by macOS). `subprocess.run(timeout=15)` kills and reaps the child at the deadline. The child's output is validated strictly. A timeout or signal gives the new fixed reason `pdf_too_complex` (422).
- **Tests:** results identical to in-process extraction; a runaway worker killed with its pid gone; crash, signal, bad JSON, bad reason, bad types and oversized text; no server environment passed; an upload timeout leaves no file and no row. Mutation: 1 failed.
- **Residual:** about 0.1 s of process start-up per PDF. Concurrency is bounded by the thread pool that runs the check.

### TD-023: BLOCKED_APPROVAL

Neither configured role has CREATEDB (checked read-only), and restoring into the guarded test database would overwrite it. A disposable database needs your decision (Decision D).

### TD-024: OPEN

Revoking a token before `exp` needs identity-provider integration (introspection or a revocation list) or new persistence. Today: deactivate the user, revoke the binding or remove a grant (all take effect on the next request), and keep lifetimes short (default 900 s).

### TD-025: RESOLVED_VERIFIED

`test_every_declared_parameter_is_a_trusted_error_segment` fails when a route declares a parameter that the registry does not trust. `test_unregistered_nested_model_fields_are_redacted` proves that a forgotten nested model stays redacted (fails closed). Messages remain static, and codes remain allowlisted.

### TD-026: OPEN

The frozen Pydantic models hold lists. No service consumes request models yet, and responses are built then serialized, so nothing relies on deep immutability. Switching to tuples is possible without changing the JSON. Whether to accept the boundary or convert is Decision E.

TD-001, TD-006 and TD-007 (pre-Day-16) are outside this phase and unchanged.

### TD-018 correction (2026-11-11, independent audit)

Two audit findings reopened TD-018 until the tests below passed.

1. **Inode reuse.** The reconciler compared `(st_ino, st_dev)` before unlinking, and Linux can give a just-freed inode to a replacement file.
   - **Correction:** removal is now rename, verify, unlink.
     - After the database recheck, the name is atomically renamed to an unpredictable private name (`.reconcile-<128-bit hex>`).
     - The renamed file is opened with `O_NOFOLLOW` and checked with `fstat`: a regular file, with the same device, inode, size and nanosecond mtime as at the scan, and still older than the grace period.
     - On any doubt it is restored with `link` (which never overwrites) or, if the name was taken meanwhile, kept under the private name and counted (`quarantined_kept`, `quarantine_entries`). It is never deleted.
     - The check-to-unlink window is closed because only this process knows the private name, and the directory is 0700 and owned by the service user.
2. **Database/storage pairing.** The target and the storage directory were independent.
   - **Correction:** `--delete` requires a marker in the directory (`.rfq-copilot-storage-binding`, 0600) that holds a hash of the queried database's name and OID. No credentials are stored.
     - `--bind` writes it only for the development target, and only when `--storage-dir` is the same directory, by device and inode, as the process's `RFQ_API_ATTACHMENT_DIR` (the API's own setting). Rebinding to another database is refused.
     - A missing, unsafe, malformed or different marker refuses deletion (exit 2, `pairing_unverified`). A dry run still works and reports `pairing`.
- **Tests:** `tests/test_phase3_reconcile.py` (19):
  - inode reuse simulated on macOS and Linux by rewriting the reported identity: a fresh replacement and a backdated replacement are both kept;
  - name taken during quarantine; symlink swap; age revalidation; an unchanged orphan is still deleted;
  - binding (correct, inode alias, rebind refused), refusals (test target, missing, relative or other directory), unsafe markers;
  - CLI: unbound delete refused while the dry run works, development storage with the test database refused, bind then explicit delete.
  - Plus a real fingerprint test (test ≠ development) in `test_phase3_hardening_database.py`.
  - Mutations: pre-correction removal 4 failed, pairing check removed 2, bind directory check removed 1, age not revalidated 5.
- **Residual:**
  - A replacement written by the service user itself, with identical size, nanosecond mtime and a reused inode, is indistinguishable from the original. Only the service user can write in the 0700 directory, and uploads always use new random names.
  - The binding trusts the operator's process environment, the same trust as the API's own configuration.
  - Dropping and recreating the database changes its OID and needs a fresh `--bind` (fails closed).

## Phase 3.5 completion (2026-11-11)

Full report and approval table: [phase_3_technical_debt_resolution.md](phase_3_technical_debt_resolution.md), section "Phase 3.5 completion". Nothing below was applied to any database. Each capability sits behind a setting whose default keeps the approved behaviour. The proposed migrations live in `alembic/proposed/`, which Alembic does not load, and are tested inside rolled-back transactions of the guarded test database (`tests/test_phase35_completion_database.py`).

| ID | Before | Correction (code) | Pending | Tests (mutations) |
| --- | --- | --- | --- | --- |
| TD-014/016 | Catalog and quotes on `view_assigned_rfqs`, uploads on `create_rfq` | `ROUTE_PERMISSIONS` v1/v2 table, `require_route_permission`; v2 uses `view_catalog`, `view_quote`, `attach_rfq_document` | migration `20261111_01` (data: 5 permissions, 9 grants derived from the reference policy) | per-role v2 matrix on PostgreSQL; exact grant diff; v1 permissions no longer open v2 routes (escalation mutation: 6 failed; check bypass: 11) |
| TD-015 | "Assigned" meant all RFQs of the bound customer | `assigned_to()` SQL EXISTS added to RFQ, attachment, upload and quote queries, on top of the customer filter | migration `20261111_02` (`rfq_assignments`); no backfill | narrowing only, a foreign-customer assignment grants nothing, revocation, the SQL contains the table (filter dropped: 1 failed) |
| TD-017 | One customer per user | `eligible_customers` and `X-RFQ-Customer-Context`, checked against active bindings; several bindings with no header gives 403 | migration `20261111_05` (drops the one-active index) | select each, stranger 403, revoked 403, single mode 403 (header trusted: 2 failed) |
| TD-022 | SQL only | `identity_admin` grant/revoke/inspect: dry run by default, table-privilege check, DB-granted operator permission, no self-administration, conflict refusal, idempotent repeats, audit row in the same transaction, denied refusals audited separately | a role grant of `manage_customer_bindings` (none in the migration) | authority, self, conflict, repeat, reactivate, inactive user/customer, audit (authority skipped: 1; self allowed: 1) |
| TD-024 | Only user deactivation | `token_revocations` check in `get_principal` (SHA-256 of the token, or user-wide `not_before`); unknown status 503; `identity_admin revoke-token` | migration `20261111_03`, a grant of `revoke_tokens`, IdP decision | single token, user-wide, later token accepted, missing table 503 (skipped: 4; fail-open: 2) |
| TD-026 | Frozen models with lists | Tuples; request models validate as lists (same 422 codes), store tuples, serialize lists | none | mutation attempts; schema byte-identical (lists stay mutable: 1 failed) |
| TD-027 | Marker version unchecked; identity name+OID | Marker must be exactly `{"version": 2, "database": <hex>}`; v1 is `legacy`, kept and renamed only by `--bind --replace-legacy`; fingerprint adds `system_identifier` | none | 9 malformed versions, legacy flow, invalid marker blocks bind, cluster/recreation mismatch (version unchecked: 5 failed) |
| TD-028 | Retry duplicated uploads | `Idempotency-Key` per (actor, customer, operation): replay the stored 201; a different upload gets 409; the loser of a race replays the winner and its file is removed; lost-ack retry replays | migration `20261111_04` | replay, conflict, lost ack, race, per-customer scope (digest ignored: 1 failed) |
| TD-029 | Count only | `--list-missing` (attachment ids only) plus runbook | none | listing, rows unchanged |
| TD-023 | Readable, not restored | Provenance re-verified (sha256 matches, 34 table-data entries, 0600) | a DBA disposable database | — |

Rollback: every setting reverts by configuration alone. Every proposed downgrade refuses while it would drop provisioned records (tested).

### Preflight corrections D1–D3 (2026-11-11)

- **D1 (fresh-database failure) and D2 (baseline conflict):** RESOLVED_VERIFIED.
  - The permission rows left Alembic. The former `20261111_01` was removed and replaced by the pinned **`rbac-policy-v2` overlay**: `src/rfq_copilot/policies/rbac-policy-v2.json`, SHA-256 `3e2ac34c…b74d`, in `seeding/policy.py::TRUSTED_POLICIES`.
  - It is applied only after the baseline seed, by `python -m rfq_copilot.seeding.policy`, and only when the active schema contract accepts it.
  - The loader and the integrity audit accept exactly baseline + the complete overlay. A partial, modified or extended overlay fails both. Dataset v1.1.0's 12 permissions and 27 grants and their checks are unchanged.
- **D3 (designated operators):** RESOLVED_VERIFIED as schema and policy support. Revision `20261111_06` widens the roles CHECK for `identity_operator`. The overlay grants `manage_customer_bindings` and `revoke_tokens` only to that role, which has no other permission. No account holds it, and none was provisioned.
- **Decision 3 (auditors under assignment enforcement):** no change needed. The enforcement code already applies the assignment condition to quote reads for every role. It is still off.
- **New: `schema_contract.py`.**
  - `APPROVED` (active, unchanged pins) and `PROPOSED` (revision `20261111_06`, measured digest `ed7ba80f…6125`, catalog p36/f71/u14/c216, two CHECK overrides).
  - `seeding/safety.py`, `integrity.py` and `migration_safety.include_name` read the active contract.
- **Defects found by the new lifecycle test, and fixed:**
  1. Unwrapped constraint names in the proposals were re-prefixed by `env.py`'s naming convention (SQLSTATE 42704). Every name now uses `op.f()`.
  2. The Core definitions now use the ORM naming convention.
- **Status:** TD-014/016 and TD-022 move from "migration unapplied" to "overlay plus approved migrations unapplied". They remain **BLOCKED_APPROVAL** only for the development application step.

### Phase 3.5 code activation (2026-11-11)

- **Revisions:** `20261111_02`, `_03`, `_04` and `_06` moved to `alembic/versions/`. Single head `20261111_06`. `_05` stays in `alembic/proposed/deferred/`.
- **Revision `_03` amended before any application:** identity checks on `user_id` and `created_by` (shared Day 4 convention), and the hex check in convention form.
- **ORM:**
  - role CHECK admits `identity_operator`;
  - `idempotency_records` operation CHECK admits `attach_rfq_document` (required for ORM/DB parity with `_04`);
  - new models `RfqAssignment` and `TokenRevocation` (no relationships); 36 models.
- **Contract:** `schema_contract.CURRENT` is active: revision `20261111_06`, shape digest `51362661…6319f`, catalog p36/f71/u14/c218, accepts `rbac-policy-v2`. `PREVIOUS` (`20261110_01`) is kept. `EMPTY_TABLES` gains both tables.
- **Not done (by instruction):** the development database is still at `20261110_01` (12 permissions, 27 grants, 0 bindings, no new tables). Its integrity check and 3 development tests fail ("Alembic revision is not current" or missing relation) until the upgrade is authorized. No users, bindings, assignments or overlay rows were created.

### Day 22 status correction (2026-11-11)

- The "Not done" note above is superseded: the development database is at `20261111_06`
  (head) with the `rbac-policy-v2` overlay applied. Integrity: PASS, 1,107 records,
  0 violations. Bindings, assignments, revocations and operators: none.
- TD-014/016, TD-024 and TD-028 are now blocked only on **activation** (the
  `RFQ_API_PERMISSION_POLICY`, `RFQ_API_TOKEN_REVOCATION` and
  `RFQ_API_UPLOAD_IDEMPOTENCY` settings). Their schema and policy data are in place.
- TD-015 is blocked on provisioning assignments. TD-017 is blocked on migration `_05`
  (deferred). TD-022 is blocked on provisioning operators. TD-023 is still
  BLOCKED_EXTERNAL.
- No new debt from the Day 22 audit. See `phase_3_final_backend_report.md`.

## Day 23 note (2026-11-11)

- TD-007's "quote-persistence phase" is **Phase 8** (quotation persistence and human
  approval). Phase 4 (MCP) adds no persistence. See F5 in `phase_04_findings_tracker.md`.
- Phase 4 security findings F1–F4 are tracked in `phase_04_findings_tracker.md`, not here.
