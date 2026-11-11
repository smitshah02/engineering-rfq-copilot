# Phase 3.5: Backend Technical Debt Resolution

Date: 2026-11-10. Scope: findings from Days 16–21. Baseline: 1631 passing tests. Result: 1685 passing (+54), 0 failed, 0 skipped, 0 warnings.

**Verdict: PARTIAL — APPROVAL REQUIRED.** Every safe in-scope correction is implemented and verified. Six findings need a policy or schema decision (Decisions A–E); nothing was changed for them.

## Finding inventory (before → after)

| ID | Source | Before | Risk | After | Approval |
| --- | --- | --- | --- | --- | --- |
| TD-009 | Day 18 | Injected `Database` trusted as read-only | A read path could write | RESOLVED_VERIFIED: every read transaction is `READ ONLY` | no |
| TD-010 | Day 18 | Quote lookup loaded the whole history | Slow requests as history grows | RESOLVED_VERIFIED: keyed SQL, 3 SELECTs (1 for 404) | no |
| TD-011 | Day 18 | Catalog paged in Python | Cost grows with catalog size | RESOLVED_VERIFIED: LIMIT/OFFSET in SQL | no |
| TD-013 | Day 20 | One static key | Rotation needs a hard cut-over | RESOLVED_VERIFIED: trusted key set with exact `kid` | no |
| TD-024 | Day 20 (split) | No revocation before `exp` | Stolen token valid until `exp` | OPEN: needs identity-provider integration | design |
| TD-022 | Day 20 | Bindings only via SQL | Unaudited manual grants | BLOCKED_APPROVAL (Decision C) | yes |
| TD-014 | Day 21 | Catalog and quotes reuse `view_assigned_rfqs` | Mismatch with the reference policy | BLOCKED_APPROVAL (Decision A) | yes |
| TD-016 | Day 21 | Upload reuses `create_rfq` | Imprecise least privilege | BLOCKED_APPROVAL (Decision A) | yes |
| TD-015 | Day 21 | "Assigned" means the bound customer | Over-broad once many staff share a customer | BLOCKED_APPROVAL (Decision B) | yes |
| TD-017 | Day 21 | One customer per user | No multi-customer staff model | BLOCKED_APPROVAL (Decision B) | yes |
| TD-018 | Day 19 | Crash leaves an orphan file | Disk growth, stale documents | RESOLVED_VERIFIED: operator reconciler | no |
| TD-019 | Day 19 / review | Failed COMMIT deleted the file | Committed row without its file | RESOLVED_VERIFIED: no blind delete | no |
| TD-020 | Days 19–20 | Ancestor directories unchecked | Path redirection by another user | RESOLVED_VERIFIED: descriptor walk from `/` | no |
| TD-021 | Day 19 | PDF parse had no deadline | A hostile PDF pins a worker thread | RESOLVED_VERIFIED: killable child process | no |
| TD-023 | Day 20 | Backup never test-restored | An unrestorable backup goes unnoticed | BLOCKED_APPROVAL (Decision D) | yes (privilege) |
| TD-025 | Day 17 | Forgotten registry entry undetected | Unhelpful redacted errors | RESOLVED_VERIFIED: detection test | no |
| TD-026 | Day 17 | Frozen models hold lists | None today (no reliance) | OPEN (Decision E) | confirm |

Also reviewed:
- **Development dependencies:** every imported module is declared. Pillow and ReportLab are not used, so they were not added.
- **Validation-message usability:** messages stay static, with no limits or values, to avoid reflection. No change.
- **Markers:** the `ponytail:` comments in `routers/quotes.py` (the TD-010 shortcut) are removed. Those in `attachments.py`, `schemas/rfq.py` and `services/materials.py` are bounded, documented shortcuts and are kept.
- TD-001, TD-006 and TD-007 predate Day 16 and are unchanged.

## Implemented corrections

| Area | Change | Files |
| --- | --- | --- |
| Read-only reads (TD-009) | `after_begin` listener issues `SET TRANSACTION READ ONLY` in every read session, only when the route queries | `api/db.py` |
| Quote lookup (TD-010) | `get_released_quotation(session, quote_id, customer_id=)`: id, owner, `released` and `released_at` in the WHERE clause; shared result builder | `repositories/queries.py`, `repositories/__init__.py`, `api/routers/quotes.py` |
| Catalog paging (TD-011) | `find_materials(..., limit=, offset=)`, validated; the route asks for `limit + 1` | `repositories/queries.py`, `api/routers/materials.py` |
| Key rotation (TD-013) | `RFQ_API_JWT_PUBLIC_KEYS` (1–4 named keys, each through the secure loader); exact-`kid` selection, no fallback | `api/config.py`, `api/tokens.py` |
| Path trust (TD-020) | `open_trusted_parent`: realpath once, then `O_NOFOLLOW` per component from `/`; root or service owner; no group/other write unless sticky | new `paths.py`; `api/tokens.py`, `attachments.py` |
| Ambiguous COMMIT (TD-019) | Flush first, so a certain failure removes the file; a COMMIT failure or cancellation keeps it for reconciliation | `api/routers/attachments.py` |
| Orphans (TD-018) | `python -m rfq_copilot.reconcile_attachments`: dry run, grace ≥ 1 h, `flock`, DB and inode recheck, counts only | new `reconcile_attachments.py` |
| PDF deadline (TD-021) | `python -I -m rfq_copilot.pdf_worker`, empty env, CPU and memory rlimits, killed at 15 s; strict output validation; reason `pdf_too_complex` | new `pdf_worker.py`; `attachments.py`, `api/routers/attachments.py` |
| Registry (TD-025) | Tests: every declared parameter is trusted; unregistered nested fields stay redacted | tests only |

## Security review

- **Authentication:**
  - Day 20 behaviour is unchanged in single-key mode.
  - Key-set mode adds exact `kid` selection only. Unknown, missing, near-miss or non-string `kid` gives 401; `jku`/`jwk`/`x5u`/`x5c` are still refused.
  - Key files and all their ancestors are verified through descriptors.
- **RBAC:** unchanged. No permission, grant or role was added, and the matrix tests still pass.
- **Customer isolation:** the quote lookup now enforces ownership in SQL. A same-role user bound to another customer gets 404 (tested), and the cross-customer 404 tests from Days 18–21 pass.
- **Uploads:**
  - Authorization still happens before the body is read.
  - The storage path is fully verified; PDF parsing is isolated and time-bounded.
  - Uncertain commits never delete; certain failures still leave nothing behind.
- **Approval policy:** no approval, release or draft route exists. `approval-v1.2`/`v1.1`, `snapshot_session()`, REPEATABLE READ evaluation, Decimal pricing and the fingerprints are untouched.

## Performance

| Operation | Before | After |
| --- | --- | --- |
| `GET /quotes/{id}`, released | All quotes of the customer, plus their versions and lines | 3 SELECTs, one quote |
| `GET /quotes/{id}`, missing or unreleased | 3 SELECTs over the whole history | 1 SELECT, keyed |
| `GET /materials` | Every matching id fetched, sliced in Python | `ORDER BY material_id LIMIT n+1 OFFSET k`, then the page rows |
| Read sessions | — | +1 `SET TRANSACTION READ ONLY` per queried request (same round trip pattern as BEGIN) |
| PDF upload | In-thread parse | About 0.1 s of child-process start-up; hard deadline |

## Approval decisions required

### Decision A — Permission vocabulary for the catalog, quotes and uploads (TD-014, TD-016)

**Differences, current mapping vs the dataset's reference tool policy (`src/security_policy.py`, test reference only):**

| Operation | Current (DB grants of `view_assigned_rfqs` / `create_rfq`) | Reference | Difference |
| --- | --- | --- | --- |
| Catalog read | sales rep, estimator, mfg engineer, **sales manager** | `get_material_specs`: sales rep, estimator, mfg engineer | sales manager has extra access |
| Released quote read | sales rep, estimator, **mfg engineer**, sales manager | `get_quote_status`: sales rep, estimator, sales manager, **auditor** | mfg engineer has extra access; auditor is missing |
| Attachment upload | sales rep, estimator (`create_rfq`) | no attachment tool; `create_rfq`: sales rep, estimator | none in effect; the semantics are imprecise |

- **Recommended:** a new, versioned policy revision outside the frozen dataset (an Alembic data revision plus updated seeding and integrity contracts) that:
  - adds the permissions `view_catalog`, `view_quote` and `attach_rfq_document`;
  - grants `view_catalog` to sales rep, estimator and mfg engineer, `view_quote` to sales rep, estimator, sales manager and auditor, and `attach_rfq_document` to sales rep and estimator;
  - then switches the three `require_permission` arguments.

  Net effect: least privilege that matches the reference. Sales managers lose catalog reads, mfg engineers lose quote reads, auditors gain released-quote reads, and uploads are unchanged.
- **Alternatives:**
  1. Keep the current mapping and accept the documented mismatch.
  2. Restrict only, with no new grants, by mapping quotes to a permission auditors lack. This cannot express the reference without new records.
- **Schema/policy changes:** 3 `permissions` rows and 9 `role_permissions` rows. Pinned counts change (permissions 12 → 15, grants 27 → 36, integrity record count) and need a documented post-dataset policy layer.
- **Tests:** a matrix update; the per-role database test derives the new expectations automatically; the reference-parity table becomes a test.
- **Migration and rollback risk:** additive data only. A downgrade deletes the new grants and permissions after switching the routes back. The risk is a mismatch between seeded data and pinned counts if the migration is applied without the code.

### Decision B — Staff assignment and multi-customer access (TD-015, TD-017)

- **Recommended:** keep today's model for Day 22. Defer both to an explicit authorization phase, with:
  - a new table `business.rfq_staff_assignments (rfq_id, user_id, assigned_by, assigned_at, revoked_at)` with a partial unique index on active rows. RFQ reads for holders of `view_assigned_rfqs` would require an active assignment in addition to customer ownership in SQL;
  - multi-customer staff through several active bindings, plus a customer selected in the URL path (`/customers/{customer_id}/…`), validated against the caller's active bindings on every request. Never from headers or claims. This replaces the single-active-binding index only together with that check.
- **Alternatives:**
  1. Assignment by region (`users.assigned_region`), rejected: it invents a relationship.
  2. A "global staff" permission, rejected: implicit all-customer access.
- **Security:** both changes narrow or explicitly widen access. Each needs audit events and tests proving that ownership is still enforced.
- **Migration risk:** a new table (additive). Dropping the unique index is a behavioural change that needs a coordinated code release; rollback requires removing extra active bindings first.
- **Tests:** an unassigned RFQ of the same customer gives 404; selecting a customer the caller has no binding to gives 403 or 404; forged headers or claims give no effect.

### Decision C — Binding provisioning and upload idempotency (TD-022, TD-019 residual)

- **Recommended:** an operator command `python -m rfq_copilot.bindings grant|revoke --user U --customer C --operator O --reason R`, run with a writable maintenance role, in one transaction:
  - **Customer verification:** the customer exists and is not `inactive`, and the user is active.
  - **Conflicts:** refuse a second active binding (the existing partial index) unless `--replace` revokes the old one in the same transaction.
  - **Revocation:** sets `revoked_at`; history is never deleted.
  - **Audit:** writes an `audit_events` row (`actor_id` = operator, `outcome`, a reason in `event_data`).
  - **Failure:** a single transaction, so rollback is all or nothing; no partial state.
- **Decisions needed:**
  - **Operator authority:** a new `manage_bindings` permission, or DBA-only.
  - **Audit vocabulary:** which `event_type` values to use.
  - **Upload idempotency:** whether uploads should accept an idempotency key stored in the existing `idempotency_records`, so a retry after an ambiguous 503 returns the original attachment.
- **Alternatives:** keep SQL provisioning with a written runbook.
- **Tests:** grant, duplicate, replace, revoke, inactive customer, unknown user, audit row present, rollback on failure.

### Decision D — Restore verification of the development backup (TD-023)

- **Recommended:** a DBA creates a disposable database (for example `rfq_copilot_restore_check`) owned by a maintenance role. `pg_restore` into it, compare the per-table fingerprints with those recorded on 2026-11-10, then drop it.
- **Alternative:** grant CREATEDB to a separate maintenance role (never to the application roles).
- **Risk:** none to development or test data. It must never target `rfq_copilot_dev` or the guarded test database.

### Decision E — Confirm intentional constraints (TD-026 and deployment assumptions)

These are recorded as intentional; they are **not** marked accepted:
- frozen models with lists, versus switching to tuples (same JSON);
- symlinked secret layouts are refused (point to the resolved file);
- ancestors owned by another non-root user are refused;
- POSIX only;
- key rotation needs a restart.

## Verification (actual)

| Command | Result |
| --- | --- |
| `pytest -q tests/test_phase3_hardening.py tests/test_phase3_hardening_database.py` | 54 passed in 6.30 s |
| Days 16–21 API and security tests (10 files) | 512 passed in 25.36 s |
| Public smoke (9 files, no database variables) | 548 passed in 5.77 s |
| `pytest -q` | **1685 passed** in 167.15 s; 0 failed, 0 skipped, 0 warnings |
| Mutations: read-only listener / quote owner filter / SQL paging / kid fallback / ancestor checks / uncertain-commit delete / in-process PDF / grace ignored | 2 / 2 / 1 / 6 / 5 / 3 / 1 / 3 failed (of 54) |
| `ruff check .` / `ruff format --check src tests alembic` / `uv lock --check` | pass / 117 files formatted / pass |
| `integrity --target development` (read only) | PASS, 1090 records, 0 violations |
| Reconciler dry run (scratch directory, development DB read only) | counts only; nothing deleted |
| Uvicorn, production, two-key set, development DB read only | live and info 200; kid a and kid b: 403 (verified, unbound); kid a header with a key b signature, unknown kid, no kid, bad signature: 401; no token, subject or error in the log |
| Guarded test database afterwards | 0 user relations |

## Correction — 2026-11-11: reconciler audit findings (TD-018)

- **Inode reuse:** a replacement that reuses the original inode was deletable.
  - **Fix:** removal is now an atomic rename to a private random name, then `fstat` verification on the opened descriptor (device, inode, size, mtime in nanoseconds, still past the grace period), then unlink.
  - On doubt the file is restored without overwriting, or kept under the private name and reported.
- **Wrong database/storage pairing:** development storage could be reconciled against the test database.
  - **Fix:** `--delete` requires a binding marker matching the queried database's fingerprint (name and OID, hashed).
  - `--bind` is allowed only for the API's pairing: the development target and the same directory as `RFQ_API_ATTACHMENT_DIR`, compared by inode.
  - A dry run is unaffected.

| Command | Result |
| --- | --- |
| `pytest -q tests/test_phase3_reconcile.py` | 19 passed in 0.30 s |
| Phase 3.5 tests (3 files) | 74 passed in 6.31 s |
| Attachment security tests | 133 passed in 5.08 s |
| Days 16–21 API and security tests | 512 passed in 25.18 s |
| Public smoke (10 files) | 567 passed in 5.79 s |
| `pytest -q` | **1705 passed** in 166.13 s; 0 failed, 0 skipped, 0 warnings |
| Mutations: old removal / no pairing check / bind without directory check / no age recheck | 4 / 2 / 1 / 5 failed |
| `ruff check .` / `ruff format --check` / `uv lock --check` | pass / 118 files formatted / pass |
| `integrity --target development` | PASS, 1090 records, 0 violations |
| CLI on a scratch directory, development DB read only | dry run: `pairing: unbound`; `--delete`: refused, exit 2 |

TD-018 stays RESOLVED_VERIFIED on the strength of these tests. The residual risks are in `technical_debt.md`.

## Remaining risks and Day 22 readiness

- **Day 22 can begin** on the current authorization model. Decisions A–C change least-privilege precision and operations, not isolation. Customer isolation, authentication, upload safety and approval protections are verified.
- Open: TD-024 (revocation), TD-026 (Decision E), TD-001/006/007 (earlier phases).
- New routes in Day 22 must use `require_permission`, appear in the RBAC matrix test, and keep read paths on `get_read_session`.

## Phase 3.5 completion (2026-11-11)

**Verdict: IMPLEMENTED — MIGRATION APPROVAL REQUIRED.**
- Every remaining item is implemented in code and tested against PostgreSQL with real signed JWTs.
- Five proposed migrations (`alembic/proposed/`) and two operator-permission grants await approval.
- One check (TD-023, restore) needs a DBA.
- No migration, grant, binding or assignment was applied to any database. Development still has 27 grants and 0 bindings, and integrity passes.

### Final permission matrix

Active policy **v1** (approved, unchanged): ✔ means the role's database grants open the route.

| Route | Permission (v1) | SR | EST | ME | SM | ADM | AUD |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /customers/me` | view_customer | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| `GET /rfqs`, `/rfqs/{id}`, `/rfqs/{id}/attachments` | view_assigned_rfqs | ✔ | ✔ | ✔ | ✔ | — | — |
| `GET /materials` | view_assigned_rfqs | ✔ | ✔ | ✔ | ✔ | — | — |
| `GET /quotes/{id}` (released only) | view_assigned_rfqs | ✔ | ✔ | ✔ | ✔ | — | — |
| `POST /rfqs/{id}/attachments` | create_rfq | ✔ | ✔ | — | — | — | — |

Proposed policy **v2** (`20261111_01` + `RFQ_API_PERMISSION_POLICY=v2`); only the changed rows are shown:

| Route | Permission (v2) | SR | EST | ME | SM | ADM | AUD | Source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /materials` | view_catalog | ✔ | ✔ | ✔ | **—** | — | — | reference `get_material_specs` |
| `GET /quotes/{id}` | view_quote | ✔ | ✔ | **—** | ✔ | — | **✔** | reference `get_quote_status` |
| `POST /rfqs/{id}/attachments` | attach_rfq_document | ✔ | ✔ | — | — | — | — | current `create_rfq` holders (no reference tool) |

The test `test_v1_v2_differences_are_exactly_the_documented_ones` pins the three differences: sales manager loses catalog reads, manufacturing engineer loses quote reads, auditor gains quote reads. `manage_customer_bindings` and `revoke_tokens` are created with no grant. No approval permission opens any route.

### Database and migration proposals (all unapplied)

| Revision | Change | Upgrade | Downgrade |
| --- | --- | --- | --- |
| `20261111_01` | 5 permissions, 9 grants | INSERT only | DELETE exactly those rows |
| `20261111_02` | `business.rfq_assignments` (PK rfq_id+user_id, RESTRICT FKs, partial index on active rows) | additive | refuses while rows exist |
| `20261111_03` | `business.token_revocations` (one-kind CHECK, hex CHECK, unique digest) | additive | refuses while rows exist |
| `20261111_04` | `idempotency_records` rule_01 gains `attach_rfq_document` | replaces one CHECK | refuses while upload receipts exist |
| `20261111_05` | drops `uq_user_customer_bindings_active_user` | drops an index | refuses while any user has more than one active binding |

**Applying an approved subset:**
1. Move the files into `alembic/versions/` in chain order.
2. Update the contract pins: `seeding/safety.py` REVISION and SHAPE_DIGEST, the `integrity.py` counts, the schema tests and `EMPTY_TABLES`.
3. Run the guarded migration tests.
4. Back up development, then upgrade.
5. Switch the matching setting.

Rollback is by setting first, then a guarded downgrade.

### Approval requests

| # | Decision required | Recommended | Alternatives | Security implications | Proposed change | Implementation |
| --- | --- | --- | --- | --- | --- | --- |
| A | Activate dedicated permissions (TD-014/016) | Apply `20261111_01`, set policy v2 | Keep v1 | Least privilege matching the reference: SM loses catalog, ME loses quotes, AUD gains released quotes. Open question: is `get_quote_status` (a status read) meant to include released totals and lines for auditors? If not, drop the auditor grant. | 5 permissions, 9 grants | complete, unapplied |
| B1 | Per-RFQ assignment (TD-015) | Apply `20261111_02`; provision assignments explicitly; then set `required` | Keep customer-wide | Narrows access, never widens it. Turning it on before assignments exist hides every RFQ (404) from every user. | new table | complete, unapplied; no assignment CLI or backfill (assignment authority is undecided) |
| B2 | Multi-customer staff (TD-017) | Apply `20261111_05` together with `selectable` | Keep one binding | Selection only among verified bindings; several bindings without a header gives 403 on legacy routes | drop index | complete, unapplied |
| C | Binding provisioning authority (TD-022) | Grant `manage_customer_bindings` to a named operator role, never to agents; run the CLI with a maintenance DB role | DBA-only SQL runbook | Authority from DB grants; audit vocabulary `customer_binding.grant` / `.revoke` / `token.revoke` (new names, `operation` is free text) | 1 grant | complete, unapplied |
| D | Restore check (TD-023) | DBA provides `rfq_copilot_restore_check` | maintenance role with CREATEDB (never the app roles) | none to dev or test | — | procedure below; blocked on external access |
| E | Token revocation (TD-024) | Apply `20261111_03`, grant `revoke_tokens`, set `enforced` | Rely on the IdP (introspection or short lifetimes) if it supports revocation | Fails closed (503) if status is unknown; one indexed query per request | new table, 1 grant | complete, unapplied; IdP capability unknown |
| F | Upload idempotency (TD-028) | Apply `20261111_04`, set `enabled` | Reuse `create_rfq` operation (rejected: conflates key spaces) | Customer and actor scoped; replays only an identical request | CHECK change | complete, unapplied |

### External requirements

- **DBA restore procedure (TD-023):**
  1. Create an empty database `rfq_copilot_restore_check`, owned by a maintenance role (not the app roles).
  2. Run `pg_restore --no-owner --dbname=rfq_copilot_restore_check ~/.rfq_copilot_backups/rfq_copilot_dev-pre-20261110_01-20261110T202936.dump`. Before restoring, confirm the dump's sha256 is `3ea5e81e8ab5fb83035fd9b6a8bfeb82959bb7429a2decc1521e03d071bf044f`.
  3. Compare the 33 per-table md5 and row-count fingerprints with those recorded before the 2026-11-10 migration. Expect revision `20261109_01`, and integrity PASS with 1090 records when the CLI is pointed at that database.
  4. Drop the database.
- **Identity provider:** confirm whether it offers token introspection or revocation, and whether it sets `jti`. Relying-party revocation works without either (it uses the token digest and user-wide cut-offs). An IdP logout does not revoke access tokens already issued.

### Operator runbooks

- **Reconciler scheduling:**
  - Bind once: `--bind` as the API's user, with `RFQ_API_ATTACHMENT_DIR` set.
  - Schedule a daily dry run (cron or a systemd timer) and alert when `orphans_eligible`, `quarantine_entries`, `rows_missing_file` or `pairing != verified` is nonzero or unexpected.
  - Run `--delete` only by hand after reviewing a dry run. Never at application startup.
- **Missing-file rows** (`--list-missing`):
  - Look up each attachment id in the database.
  - Ask the customer to re-upload, or restore the file from backup.
  - Never fabricate content.
  - Remove or annotate the metadata only through an approved change with an audit event.
- **JWT rotation limits:** a key-set change needs a restart. Keep the old `kid` configured until every token signed with it has expired (at most `RFQ_API_JWT_MAX_LIFETIME_SECONDS`). Rotation does not revoke individual tokens (that is TD-024).
- **Database clones:** the storage fingerprint includes the cluster `system_identifier`. A physical clone (`pg_basebackup`, filesystem snapshot) keeps it, so a clone must never share or point at another environment's storage. Re-bind each environment to its own directory.

### Verification (actual)

| Command | Result |
| --- | --- |
| `pytest -q tests/test_phase35_completion.py tests/test_phase35_completion_database.py tests/test_phase3_reconcile.py` | 91 passed in 11.54 s |
| All Phase 3.5 tests (5 files) | 146 passed in 16.28 s |
| Days 16–21 API and security tests (10 files) | 512 passed in 27.37 s |
| Public smoke (11 files, no database variables) | 618 passed in 6.21 s |
| `pytest -q` | **1777 passed** in 162.48 s; 0 failed, 0 skipped, 0 warnings |
| Mutations (11), failing tests out of 91 | v2 catalog escalation 6; route check bypassed 11; assignment filter dropped 1; revocation skipped 4; revocation fail-open 2; header trusted 2; replay ignores digest 1; operator authority skipped 1; self-administration 1; marker version unchecked 5; request lists mutable 1 |
| OpenAPI and all model JSON schemas before and after TD-026 | identical |
| `ruff check .` / `ruff format --check src tests alembic` / `uv lock --check` | pass / 129 files formatted / pass |
| `integrity --target development` | PASS, 1090 records, 0 violations |
| Uvicorn, production, two-key set, development read only | unchanged: 401/403 matrix as before, no secrets in the log |
| Backup provenance | sha256 matches the recorded value; 34 table-data entries; mode 0600 |
| Guarded test database afterwards | 0 user relations |

## Preflight corrections D1–D3 (2026-11-11)

**Approved decisions implemented:**
1. Policy overlay instead of `_01`.
2. A dedicated `identity_operator` role.
3. No auditor bypass under assignment enforcement.

**Safe execution order for the development database** (each step needs its own authorization; none was performed):
1. Take a verified `pg_dump` backup.
2. Activation change: move `approved/` 02, 03, 04, 06 into `alembic/versions/`; set the Role CHECK in `models/identity.py` to `OPERATOR_ROLE_CHECK`; promote the two proposed tables to ORM models; set `schema_contract._ACTIVE = PROPOSED`; update the schema tests' pins.
3. `alembic upgrade head` (→ `20261111_06`), then run integrity: PASS, `policy_overlay: absent`.
4. `python -m rfq_copilot.seeding.policy --target development --confirm-database rfq_copilot_dev rbac-policy-v2`, then integrity again: PASS, `applied:rbac-policy-v2`, records +17.
5. Provision human operator accounts with role `identity_operator`, separately and with audit.
6. Enable settings one at a time: `TOKEN_REVOCATION=enforced`, then `UPLOAD_IDEMPOTENCY=enabled`, then `PERMISSION_POLICY=v2` (after binding auditors).
7. Keep `RFQ_ASSIGNMENTS=off` until assignments exist, keep `CUSTOMER_CONTEXT=single`, and leave `deferred/` 05 unapplied.

**Rollback:** switch the settings back, delete the overlay rows (the 06 downgrade refuses while `identity_operator` is used), then run the guarded downgrade.

**Verification (actual):**

| Command | Result |
| --- | --- |
| `pytest -q tests/test_policy_overlay_lifecycle.py` (fresh upgrade → seed → overlay → integrity → guarded downgrade) | 12 passed in 8.08 s |
| New and updated focused tests (lifecycle, completion public and DB) | 75 passed in 20.89 s |
| All Phase 3.5 tests (6 files) | 162 passed in 24.49 s |
| Database contract tests (6 files) | 536 passed in 51.26 s |
| Days 16–21 API and security tests | 512 passed in 27.75 s |
| Public smoke (11 files) | 622 passed in 6.29 s |
| `pytest -q` | **1793 passed** in 191.04 s; 0 failed, 0 skipped, 0 warnings |
| Mutations: integrity completeness / loader completeness / SHA pin / overlay under any contract / role CHECK override / role-in-use downgrade guard | 2 / 2 / 2 / 2 / 12 / 1 failed |
| ruff / format / lock | pass / 132 files / pass |
| `integrity --target development` | PASS, 1090 records, revision `20261110_01`, contract `approved-20261110_01`, overlay `absent` |
| Development catalog | 12 permissions, 27 grants, 6 roles, 0 bindings: unchanged |
| Guarded test database afterwards | 0 user relations |
