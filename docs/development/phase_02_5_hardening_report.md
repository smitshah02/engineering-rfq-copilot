# Phase 2.5 Hardening Report

**Scope.** This report covers ten findings from an independent review of the deterministic business services (Days 6–15). For each finding it records whether the problem was confirmed, the root cause, the fix or fail-closed mitigation, and the regression evidence. No Phase 3 work was started.

**Constraints observed.** No Git operations. Dataset v1.1.0 was read only, and no bytecode or caches were written into it. The ORM models and migration `20261108_01` were not changed. No dependencies were added. The development database was not touched: all database tests ran in the guarded `rfq_copilot_test` database, inside transactions that were rolled back. No existing test was deleted or weakened. Five exact query-count assertions were updated to new measured values, with the reason recorded beside each one (§E).

---

## A. Executive summary

All ten findings were checked against the source code. Eight were confirmed outright. Finding 6 was mostly already protected. Finding 10 (documentation) was confirmed.

**Fixed in code:**
- **Finding 1, snapshot consistency.** Each calculation now reads one explicitly selected inventory snapshot and one explicitly selected capacity snapshot.
- **Finding 2, release authorization.** Agent release is now an architectural invariant in code, not a policy setting.
- **Finding 3, rule integrity.** Rule content is verified against a version-controlled trust anchor.
- **Finding 4, evaluation identity.** Each evaluation now has its own deterministic fingerprint, separate from `calculation_id`.
- **Finding 9, query efficiency.** Duplicate catalog reads were removed.

**Mitigated, with the remaining work tracked:**
- **Finding 5, engineering-review conditions.** `borderline_bend_radius` has no approved threshold, so it is now reported as unevaluated rather than silently ignored.
- **Finding 8, planning dates.** A deterministic planning-date policy was added. Live freshness checks belong to the future API.
- **Finding 9, read consistency.** A REPEATABLE READ, read-only session and a usage contract were added. Phase 3 must adopt them.

**Deferred:** Finding 6, customer-requested operations. The boundary is now fail-closed with an explicit message. Full support needs an approved change to the Intake and tool contracts.

**Proposed but not applied:** one schema change, a capacity uniqueness constraint that includes `snapshot_id` (TD-005). It needs approval.

**Test results:**
- Baseline: **974 passed**.
- Final: **1075 passed**, 0 failed, 0 errors, 0 skipped, 0 warnings reported.
- New tests: **101**.
- Reference and ground-truth parity is unchanged for all 160 dev/validation RFQs.
- Every known `calculation_id` is unchanged.

**Verdict: READY WITH DOCUMENTED CONDITIONS** (§J).

---

## B. Findings matrix

> **Update, 2026-11-09:** after approval, Finding 5 (TD-004) and the Finding 1 schema limitation (TD-005) were resolved. See [§K](#k-addendum-td-004-and-td-005-approved-2026-11-09). The matrix below records the state at the end of the original pass.

| ID | Original finding | Confirmed? | Severity | Status | Changed files | Tests | Remaining risk |
|---|---|---|---|---|---|---|---|
| 1 | Inventory and capacity reads ignore the snapshot | **Yes.** Inventory summed the rows of every snapshot; capacity silently overwrote duplicate weeks and combined weeks from different snapshots | High | **FIXED** | `repositories/queries.py`, `results.py`, `__init__.py`; `services/inventory.py`, `capacity.py`, `lead_time.py` | `test_hardening_database.py` (two-snapshot tests), `test_hardening_services.py` (pure guards) | Overlapping capacity snapshots cannot be **stored** under the current schema (TD-005, proposed migration) |
| 2 | Policy settings can grant agents release authority | **Yes.** `quote_release_authorized_for_agent` was computed from `agent_forbidden_actions` and `human_release_approval_required` | High (security) | **FIXED** | `services/evaluation.py` | `test_hardening_services.py` (release tests); `test_hardening_database.py::test_seeded_policy_never_authorizes_agent_release_and_fingerprints_are_stable` | None in Phase 2. Phase 3 must keep approval and release execution human-only |
| 3 | Stored sha256 does not authenticate JSONB content | **Yes.** The pin compared the caller's sha to the stored sha; the fingerprint was computed from the stored (possibly altered) text | High | **FIXED** | `canonical.py` (new), `repositories/rule_manifest.py` (new), `repositories/queries.py`, `results.py` | `test_hardening_database.py` (seven tamper cases, a control, unregistered version, stored sha changed); `test_hardening_services.py::test_manifest_matches_the_frozen_rule_sources` | The trust anchor is only as strong as code review of `rule_manifest.py` (§C3) |
| 4 | `calculation_id` does not identify the full evaluation | **Yes.** It covers parts and the pricing version only. Material cost, customer facts, snapshots and other rules could change while it stayed the same | High before persistence | **FIXED** (identity); persistence and idempotency not implemented by design | `canonical.py`, `services/evaluation.py`, `services/quotation.py`, `services/lead_time.py` | `test_hardening_services.py` (fingerprint tests), `test_hardening_public.py` (canonical JSON) | Persistence must revalidate the fingerprint (TD-007) |
| 5 | Engineering-review conditions are not evaluated | **Yes.** `engineering_review_conditions` was never read | Medium | **MITIGATED** | `services/evaluation.py` | `test_hardening_services.py` (review-condition tests) | `borderline_bend_radius` is not evaluated, because no threshold is defined (TD-004) |
| 6 | `requested_operations` could be lost | **Partly.** `Part.from_mapping` already refused unknown keys, so nothing was silently dropped, but the refusal did not name TD-001 | Medium | **DEFERRED**, with a fail-closed boundary | `entities.py`, `technical_debt.md` | `test_hardening_public.py::test_requested_operations_are_refused_at_the_part_boundary` | Feature absent until the Phase 3 contract change |
| 7 | Public clone cannot reproduce the suite | **Yes.** Only 19 tests (one module) ran without the dataset and `.env` | Medium | **MITIGATED** | `README.md`, `.github/workflows/smoke.yml` (new), `tests/test_hardening_public.py` (new) | The smoke command ran in a clean copy without `data/` or `.env`: 42 passed | The full suite still needs the private dataset and PostgreSQL. The CI workflow has not yet run on GitHub |
| 8 | Horizon not validated against snapshot dates | **Yes.** Only "the horizon is a Monday" was checked | Medium | **MITIGATED** | `services/lead_time.py`, `services/inventory.py` | `test_hardening_public.py` (date policy); `test_hardening_database.py` (date cases) | No wall-clock maximum-age check; that belongs to the future API (TD-006) |
| 9 | Per-part queries and cross-statement inconsistency | **Yes.** Each additional part with the same material and type re-read its catalog data (7 SELECTs); routing was read twice; reads ran at READ COMMITTED | Medium/Low | **FIXED** (duplicate reads); **MITIGATED** (consistency contract) | `services/materials.py`, `manufacturing.py`, `inventory.py`, `capacity.py`, `lead_time.py`; `database.py` | `test_hardening_database.py::test_parts_sharing_material_and_type_share_catalog_reads`, `::test_snapshot_session_is_repeatable_read_and_read_only`; updated exact counts | The API must actually use `snapshot_session()` (TD-007) |
| 10 | README and technical-debt register are stale | **Yes.** The README said Day 2 and called PostgreSQL "planned"; TD-001 was marked resolved | Medium | **FIXED** | `README.md`, `docs/development/technical_debt.md` | n/a (documentation) | Historical logs were left as written |

---

## C. Root-cause analysis

### C1. Snapshot consistency (Finding 1)

**Defective assumption.** "There is only one snapshot." `get_material_availability` and `get_workcentre_capacity` filtered by material/thickness or by workcentre/week, never by `snapshot_id`.

**Previous behaviour.**
- `evaluate_inventory` summed `available_kg` over every row returned, so with two inventory snapshots, stock was double counted.
- `evaluate_capacity` built `free[(workcentre, week)]`, letting a later row silently overwrite an earlier one for the same week. Weeks from different snapshots were merged into one plan.
- `lead_time._check_snapshots` compared row snapshot IDs only after the totals had been computed, and only when the caller had pinned a snapshot.

**Corrected behaviour.**
- `repositories.get_snapshot(session, kind, snapshot_id=None)` is one SELECT. It returns the pinned snapshot, or the only registered snapshot of that kind. It raises `SnapshotMismatchError` when no snapshot is registered, when several are registered and none is pinned, when the pin is unknown, or when the pin is of the wrong kind.
- Both stock and capacity reads accept `snapshot_id=` and filter in SQL.
- `estimate_lead_time` resolves both snapshots once (`select_snapshots`), then passes the selected `SnapshotDetails` to `check_inventory` and `check_capacity`.
- The pure evaluators reject rows from more than one snapshot, rows that do not match the selected snapshot, and two rows for the same workcentre-week (defence in depth).
- `InventoryCheck.snapshot_id`, `CapacityCheck.snapshot_id` and `LeadTimeEstimate.snapshots` record what was used.
- Existing single-snapshot behaviour is unchanged: an unpinned call selects the only snapshot.

**Why this approach.** Selecting the snapshot first and filtering in SQL is the only way to stop data from two snapshots being mixed, because a check after the fact cannot undo a sum. Refusing ambiguity, instead of choosing the latest snapshot, avoids an implicit policy. The ORM and migration are unchanged.

**Regression tests.** In `tests/test_hardening_database.py`:
- `test_two_inventory_snapshots_are_never_mixed`: snapshot B holds 1 kg; pinned A uses only A's row, pinned B uses only B's row, and an unpinned call raises.
- `test_two_capacity_snapshots_are_never_mixed`: snapshot B takes over the first WC-LAS week with 0 free hours; under A that week is invisible, and under B the plan is infeasible.
- `test_single_snapshot_is_selected_without_a_pin`
- `test_missing_or_wrong_kind_pins_fail`

In `tests/test_hardening_services.py`, the pure guards:
- `test_stock_rows_of_two_snapshots_are_never_summed`
- `test_two_capacity_rows_for_one_week_are_rejected`
- `test_capacity_weeks_of_two_snapshots_are_rejected`

**Schema limitation (not changed).** `production_capacity` is `UNIQUE (workcentre_id, week_start)`, so a second capacity snapshot cannot cover the same weeks. The proposed migration is in TD-005 and has not been applied.

### C2. Agent release authorization (Finding 2)

**Defective assumption.** "The policy document decides whether an agent may release."

**Previous behaviour.** The value was `quote_release_authorized_for_agent = "release_quote" not in agent_forbidden_actions and not human_release`. Editing the policy could make it `True`.

**Corrected behaviour.**
- The flag is the constant `AGENT_MAY_RELEASE_QUOTE = False` (`services/evaluation.py`).
- `require_human_release` runs first in `evaluate_outcome`. It rejects a policy whose `agent_forbidden_actions` is not a list or lacks `approve_quote` or `release_quote`, and a policy whose `human_release_approval_required` is anything other than literal `true`.
- Extra keys in the policy cannot grant anything.
- Drafting (`draft_creation_allowed`), review (`manager_review_required`), human approval (`human_release_approval_required`, `approval_must_be_human`) and release remain separate fields. Nothing is executed.

**Why this approach.** A security invariant should not depend on a configuration value. Rejecting a policy that contradicts the invariant fails loudly instead of being quietly overridden.

**Regression tests.** In `tests/test_hardening_services.py`:
- `test_agent_release_is_never_authorized_for_a_valid_policy` (four scenarios)
- `test_policies_granting_autonomous_release_are_rejected` (eight modified or contradictory policies)
- `test_missing_release_settings_are_rejected`
- `test_unknown_policy_keys_cannot_grant_release`
- `test_draft_review_human_approval_and_release_stay_separate`

On the database, `test_tampered_policy_cannot_reach_an_evaluation` shows that an altered stored policy fails earlier still, at rule loading.

### C3. Rule document integrity (Finding 3)

**Defective assumption.** "If the stored sha256 matches the pin, the document is authentic." The sha256 is the source *file's* hash and cannot be recomputed from JSONB. The old `fingerprint` was computed from the stored, possibly altered, JSONB text, so it proved nothing.

**Corrected behaviour.**
- `repositories/rule_manifest.py` (version controlled) lists every approved rule version as `(kind, source sha256, canonical document fingerprint)`.
- `get_rule_version` keeps all its earlier checks: the rule kind, the sha format, the optional pin and the declared version. It then requires `(kind, stored sha256, fingerprint(document))` to equal the manifest entry, and refuses any version not in the manifest.
- `RuleVersionDetails.fingerprint` is now `canonical.fingerprint(document)`. It does not depend on PostgreSQL's text formatting, and it can be re-derived from the frozen source file.
- Provenance (the source sha256) and content (the canonical fingerprint) stay distinct. Exact `Decimal` parsing and the immutable `MappingProxyType` document are kept.

**Trust boundary and limits.** The anchor is the repository. `test_manifest_matches_the_frozen_rule_sources` re-derives every manifest entry from the frozen dataset files, whose hashes are themselves protected by the 637-hash manifest, so the chain runs from the frozen file to the manifest to the database row.

What it protects against:
- changes to database content made by anyone who can write the rule table but cannot change the deployed code.

What it does not protect against:
- someone who can change both the code and the database. That threat needs signed releases or code review, which is outside this phase.
- a compromised Python process.

**Future versions.** A new rule version must be added to the manifest, with a test that re-derives its values. Otherwise it is refused with `not a registered, approved version`.

**Regression tests.** In `tests/test_hardening_database.py`:
- `test_tampered_rule_content_is_rejected_despite_unchanged_sha`: changes the pricing margin and an hourly rate, the manufacturing hole limit and unsupported operations, and the approval human-release flag, forbidden actions and threshold. Each change is made server side with `jsonb_set`, and the test asserts the stored sha256 is unchanged.
- `test_rewriting_an_identical_value_is_accepted`: a control showing that the tamper helper by itself does not trip the check.
- `test_unregistered_rule_version_is_rejected`
- `test_changed_stored_sha_is_rejected_even_without_a_pin`

### C4. Evaluation fingerprint (Finding 4)

**Why `calculation_id` is not enough for idempotency.** `calculation_id = sha256(canonical parts in order + pricing_rule_version)[:20]`. Two evaluations of the same parts under `price-v1.0` share an ID even when any of the following differ:
- material cost per kg;
- customer status or credit exposure;
- the inventory or capacity snapshot;
- the manufacturing or approval rule;
- the requested delivery date;
- the horizon.

Each of those can change the price, the dates or the gate outcome. An idempotency key built from it would treat a different quotation as a retry of an earlier one. `test_material_cost_change_keeps_calculation_id_but_changes_fingerprint` demonstrates this.

**Implementation.**
- `canonical.py` produces canonical JSON with sorted keys and no whitespace. Decimals are written as number tokens from `str()`, which does not depend on the context, and dates as ISO strings. Frozen dataclasses become objects. Floats, NaN, datetimes, non-text keys and unknown types, including ORM rows, raise an error.
- `RfqEvaluation.evaluation_fingerprint` is the sha256 of the canonical JSON of `{"fingerprint_version": "rfq-evaluation-v1", ...every other RfqEvaluation field}`. That covers:
  - the parts and line numbers;
  - the customer's standing (ID, status, limit, exposure, but no names or contacts);
  - the full quotation tree: material specs and cost per kg, stock lines, snapshot IDs, the capacity plan, costs and totals;
  - the snapshot selection and the horizon;
  - the requested date;
  - all three rule fingerprints;
  - the gates.
- `Quotation` gains `parts` and `line_numbers`. `RfqEvaluation` gains `parts`, `line_numbers` and `unevaluated_review_conditions`.
- `calculation_id` is unchanged.

**Regression tests.** In `tests/test_hardening_services.py`:
- identical evaluations give identical fingerprints;
- eight relevant changes each change the fingerprint: commercial fact, account status, delivery date, snapshot pin, rule fingerprint, lines, line order and a part field;
- a snapshot change alters the fingerprint but not the calculation ID;
- the caller's Decimal context (precision 6, ROUND_FLOOR) does not change the result;
- contradiction-only evaluations are fingerprinted from their parts;
- the company name is excluded;
- canonical JSON refuses ORM rows.

In `tests/test_hardening_public.py`, canonical JSON is checked for order and spelling stability and refuses inexact values. On the database, `test_seeded_policy_never_authorizes_agent_release_and_fingerprints_are_stable` shows a stable fingerprint across reads, and a changed fingerprint when credit exposure changes while the `calculation_id` stays the same.

### C5. Engineering-review conditions (Finding 5)

**Previous behaviour.** `approval-v1.1.engineering_review_conditions` (`unsupported_or_unknown_geometry`, `borderline_bend_radius`, `unresolved_spec_conflict`) was never read. Any new condition added to it would have been ignored without warning.

**Semantics found in the approved sources.** None of the three is defined anywhere else in the dataset: not the docs, the contracts or the reference engines. The labels do not use them (day 15 log).
- **Unsupported or unknown geometry** is already handled by a stronger gate: an unsupported part type or out-of-range dimension leads to `unsupported_declined`, and missing geometry leads to `clarification_required`. Both outrank `engineering_review_required`.
- **Unresolved specification conflict** is Intake's `contradictions`, which lead to `clarification_required`.
- **Borderline bend radius** has no approved threshold. `BEND_RADIUS_TOO_SMALL` already declines a radius below `factor × thickness`, but "borderline" is undefined.

**Corrected behaviour.**
- `engineering_conditions()` accepts only the two conditions covered by gates and the one that cannot be evaluated. An unknown condition, or a value that is not a list, raises.
- `borderline_bend_radius` is never reported as passed. Whenever a priced line is routed through `bend`, it appears in `RfqEvaluation.unevaluated_review_conditions`.
- Outcomes and escalation precedence are unchanged, which keeps parity.

**Why not more.** Implementing it would mean inventing a manufacturing threshold, which the brief forbids. The threshold is tracked as TD-004.

**Regression tests.** In `tests/test_hardening_services.py`:
- `test_borderline_bend_radius_is_reported_unevaluated_for_bent_lines`
- `test_borderline_bend_radius_does_not_apply_without_bends`
- `test_policy_without_the_condition_reports_nothing`
- `test_unknown_or_malformed_engineering_conditions_fail_closed`
- `test_gate_covered_conditions_route_to_stronger_gates`

### C6. Requested operations (Finding 6)

**Finding.** `Part.from_mapping` already rejected every unknown key, including `requested_operations`, so an explicit request could not be silently dropped through the contract path. The error was generic, and TD-001 was marked "Resolved", which hid the fact that the feature is still missing.

**Change.**
- `Part.from_mapping` now refuses the key, even with an empty list, with an explicit TD-001 message that says to route the request to human review.
- TD-001 has been reopened as **Deferred**. The register records the exact proposed contract change: tool contract 1.1 → 1.2, an optional `requested_operations` field, the three validation sources, and a finding for a requested operation that is not routed. The owner is Phase 3 Intake. The register also records how Intake must handle these requests until then.
- The tool contract and `Part` fields are unchanged.

**Tests.** In `tests/test_hardening_public.py`:
- `test_requested_operations_are_refused_at_the_part_boundary` (three cases)
- `test_other_unknown_part_fields_still_fail`

### C7. Reproducibility (Finding 7)

**Measured.** Every test module was run in a clean copy that had no `data/` and no `.env`:
- `test_database_config.py` passed completely (19 tests).
- Twenty modules fail while being imported, because they read the dataset at module level.
- Five database modules partly pass and partly error.

**Change.**
- New `tests/test_hardening_public.py` (23 tests) has no dataset or database imports.
- The smoke command, `pytest tests/test_database_config.py tests/test_hardening_public.py`, gave **42 passed** in the clean copy.
- `.github/workflows/smoke.yml` runs `uv lock --check`, `uv sync --locked`, ruff and the smoke command. It has no secrets and no database.
- The README documents the prerequisites, the authorized way to install the dataset (archive SHA-256 and the integrity test, with no dataset content included), and the difference between the smoke suite and the full suite.

**Limits.** The workflow has not yet run on GitHub. Its commands were verified locally. The smoke suite is not end-to-end verification, and the README says so.

### C8. Planning-date policy (Finding 8)

**Previous behaviour.** `horizon_start` had to be a Monday, and nothing else was checked. A horizon before the snapshot's observation date, or after the last stored capacity week, was evaluated without complaint.

**Policy** (`lead_time.validate_planning_dates`, applied in `select_snapshots` and in `evaluate_lead_time` whenever a selection is supplied):
1. The horizon must be a Monday `date`.
2. The selection is exactly one inventory snapshot and one capacity snapshot.
3. Both snapshots belong to the same `dataset_version`.
4. Neither snapshot's `as_of_date` is after the horizon. A snapshot observed after planning starts is inconsistent with that plan.
5. The capacity snapshot holds weeks on or after the horizon (`last_week_start ≥ horizon`). Otherwise it has no capacity facts for the plan and is stale.
6. In `check_inventory`, each stock row's `snapshot_date` equals its snapshot's `as_of_date`.

**Determinism.** The clock is never read. The dataset case (observed 2026-11-02, horizon 2026-11-09, last week 2027-01-11) and both boundaries pass. The existing `CAPACITY_DATA_MISSING` and `CAPACITY_OUT_OF_HORIZON` findings are preserved.

**Deferred to the API (TD-006).** The API must derive the horizon from the request date, enforce a business-approved maximum snapshot age (no value is approved yet) and pin snapshots once several exist.

**Tests.**
- In `tests/test_hardening_public.py`: `test_consistent_snapshot_dates_are_accepted` (3 cases), `test_inconsistent_snapshot_dates_are_rejected` (7 cases: stale, future-dated and mismatched) and `test_invalid_horizons_are_rejected`.
- In `tests/test_hardening_database.py`: `test_horizons_inconsistent_with_the_snapshots_are_rejected`, `test_last_stored_week_is_a_valid_horizon`, `test_stock_rows_contradicting_the_snapshot_date_are_rejected` and `test_snapshots_of_different_dataset_versions_are_rejected`.
- In `tests/test_hardening_services.py`: `test_pins_contradicting_the_selection_are_rejected` and `test_checks_made_from_another_snapshot_are_rejected`.

### C9. Query efficiency and read consistency (Finding 9)

**Measured** (exact counts from the SQL statement trace in the existing tests):

| Call | Before | After | Change |
|---|---|---|---|
| `check_inventory`, 1 part | 9 | 10 | +1 snapshot selection |
| `check_inventory`, 2 parts, same material and type | 16 | 10 | +1 snapshot, −7 duplicate catalog reads |
| `check_capacity`, 1 part | 9 | 10 | +1 snapshot selection |
| `estimate_lead_time`, 1 part | 16 | 17 | +2 snapshots, −1 routing reused by capacity |
| `quote_parts`, RFQ-0041 (2 lines) | 32 | 32 | +2 snapshots, −2 routing reads |
| `evaluate_rfq`, 2 lines | 34 | 34 | +2 snapshots, −2 routing reads |
| `quote_parts`, 3 parts vs 1 part (same material and type) | grew by 7 per extra part | equal | asserted in the new test |

**Change.** `services/materials.memo` is a request-scoped memo of immutable catalog reads: material spec, finish existence and part routing. One dict is created per `estimate_lead_time` call (or per `check_inventory` call) and shared by the stock and capacity checks. It is dropped when the call returns. There is no module-level or long-lived cache, so prices, stock and customer data cannot go stale. Stock and capacity rows were already read once per key.

The remaining duplicates are deliberate:
- `get_material_spec` still reuses the broad capability read, which costs two extra SELECTs per distinct material (an existing `ponytail:` note).
- Costing re-reads `materials` for cost and pricing version (one SELECT for all parts).

**Read consistency.** Under PostgreSQL's default READ COMMITTED, each of the roughly 34 statements sees the latest committed data, so a concurrent commit can land between two of them. Services must not open sessions, so the fix is at the infrastructure layer. `Database.snapshot_session()` yields a session whose transaction is `REPEATABLE READ, READ ONLY`, where all statements see one snapshot and nothing can be written. The contract for Phase 3 (TD-007):
- use one `snapshot_session()` per evaluation;
- before persisting, re-evaluate and compare `evaluation_fingerprint`.

**Tests.**
- `test_hardening_database.py::test_snapshot_session_is_repeatable_read_and_read_only`: the isolation level and the read-only flag are asserted, and an UPDATE fails.
- `test_hardening_database.py::test_parts_sharing_material_and_type_share_catalog_reads`
- `test_hardening_public.py::test_memo_loads_once_per_calculation_and_never_without_a_scope`
- The five exact counts in the existing tests, updated with comments.

A test with a second connection committing concurrently was not written: it would need commits in the shared test database, which the fixtures forbid. Visibility under REPEATABLE READ is therefore asserted from the session settings, not observed.

### C10. Documentation (Finding 10)

The README has been rewritten (§I). The technical-debt register reopens TD-001 and adds TD-004 to TD-007. The historical dev logs (Days 3–15) are unchanged.

---

## D. Architecture changes

```
 evaluate_rfq / evaluate_stored_rfq (customer_id verified by caller; scope enforced in SQL)
   │  get_customer ──────────────────────────────────────────────┐
   ▼                                                             │
 quote_parts ─► estimate_lead_time                               │
                 │ select_snapshots ── get_snapshot(inventory) ──┤ SnapshotSelection
                 │                  └─ get_snapshot(capacity)  ──┤ + validate_planning_dates
                 │ reads = {} (request-scoped catalog memo)      │
                 ├─► check_inventory(snapshot=inventory, reads)  │
                 │     check_manufacturing_capability ×N (memo: spec, finish, routing)
                 │     get_material_availability(snapshot_id=…) ─► single-snapshot guard
                 ├─► plan_start (procurement delay → start week)
                 ├─► check_capacity(snapshot=capacity, reads)
                 │     routing (memo) · get_workcentre_capacity(snapshot_id=…) ─► duplicate-week guard
                 └─► evaluate_lead_time(snapshots=…) → LeadTimeEstimate.snapshots
             ─► estimate_costs ─► calculate_quote (calculation_id unchanged)
             ─► assemble_quotation (Quotation.parts, .line_numbers)
   ▼
 evaluate_outcome
   require_human_release (policy invariant) · engineering_conditions (known set only)
   gates → RfqEvaluation(quote_release_authorized_for_agent=False,
                         unevaluated_review_conditions, evaluation_fingerprint)

 Rule loading (before any of the above):
 get_rule_version → kind · sha format · pin · declared version · TRUSTED_RULES(kind, sha, fingerprint)
```

| Change | Before | After |
|---|---|---|
| Snapshot propagation | Optional string pins, checked after the fact in lead time only | Selected first, `SnapshotDetails` passed down, SQL filters, recorded on every result |
| Rule integrity | Stored sha256 against the caller's pin; fingerprint of the stored text | The manifest anchors both the provenance hash and the canonical content fingerprint |
| Fingerprints | `calculation_id` only | `calculation_id` (unchanged) plus `evaluation_fingerprint` over every input and result |
| Authorization | Derived from policy values | Constant `False`; a contradicting policy is rejected |
| Read consistency | Undefined | `Database.snapshot_session()` plus a documented contract |

Public signatures changed only by gaining optional keyword arguments: `snapshot_id=`, `snapshot=`, `reads=`, `snapshots=`, `parts=` and `line_numbers=`. Result dataclasses gained fields (`InventoryCheck.snapshot_id`, `CapacityCheck.snapshot_id`, `LeadTimeEstimate.snapshots`, `Quotation.parts`, `Quotation.line_numbers`, `RfqEvaluation.unevaluated_review_conditions`, `.parts`, `.line_numbers`, `.evaluation_fingerprint`). `RuleVersionDetails.fingerprint` changed meaning: it is now the canonical document fingerprint, not a hash of PostgreSQL's text. It is only ever compared to itself across a single calculation. No tool contract, ORM model or migration changed.

---

## E. Test evidence

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q` (baseline, before edits) | **974 passed** in 110.80 s |
| `.venv/bin/python -m pytest -q -p no:cacheprovider` (final) | **1075 passed** in 124.28 s; 0 failed, 0 errors, 0 skipped, no warnings reported |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/ruff format --check src tests alembic` | 76 files already formatted |
| `uv lock --check` | Resolved 23 packages; lockfile current |
| `git diff --check` | No output, exit 0 |
| Smoke in a clean copy without `data/` and `.env`: `pytest tests/test_database_config.py tests/test_hardening_public.py` | **42 passed** |
| `pytest tests/test_database_seeding.py::test_source_inventory_and_checksums` | 1 passed |

**New tests (101):**
- `tests/test_hardening_public.py`: 23 tests, no dataset or database needed.
- `tests/test_hardening_services.py`: 53 tests, which read the frozen rule files but need no database.
- `tests/test_hardening_database.py`: 25 tests on the guarded test database.

The test names are listed in §C.

**Existing tests changed** (assertions updated to new exact measured values; nothing deleted, relaxed or skipped):

| Test | Old value | New value |
|---|---|---|
| `test_day09_inventory.py::test_reads_only_bounded_and_reproducible` | 9 / 16 | 10 / 10 |
| `test_day10_capacity.py::test_reads_only_bounded_reproducible_with_provenance` | 9 | 10 |
| `test_day11_lead_time.py::test_reads_only_bounded_and_reproducible` | 16 | 17 |
| `test_day14_quotation.py::test_each_service_runs_once_and_only_reads` | 32 | 32 (breakdown comment corrected) |
| `test_day15_evaluation.py::test_evaluation_reads_only_bounded_and_reproducible` | 34 | 34 (comment added) |

Every one of them still asserts that each statement is a SELECT and that results are reproducible.

**Parity, all unchanged and passing:**
- `test_day14_quotation.py::test_ground_truth_quotations_end_to_end`: every known `calculation_id` and quote total.
- `test_day15_evaluation.py::test_business_labels_for_every_dev_and_validation_rfq`: 160 RFQs, all gates including `quote_release_authorized_for_agent`.
- `test_day11_lead_time.py::test_ground_truth_lead_times_and_delivery_findings`
- `test_day09` inventory parity
- the reference-engine parity tests
- `test_day08_manufacturing.py::test_mfg_001_regression_against_the_reference`: MFG-001 is still fixed.

---

## F. Dataset and database verification

**Dataset.**
- `find data/rfq_synthetic_data -type f | wc -l` gives **681**.
- `test_source_inventory_and_checksums` passed: 681 files and 637 protected hashes.
- The archive SHA-256 is `681a882f…5080`, which matches.
- No `__pycache__`, `.pyc` or `.pytest_cache` exists inside `data/rfq_synthetic_data`.
- Manifest values were computed by reading the source files only, with `PYTHONDONTWRITEBYTECODE=1`.
- No frozen-evaluation file was opened.

**Database safety.**
- All database tests used `rfq_copilot_test`, through the existing fixtures. Those fixtures verify the identity of the test database, take an advisory lock, apply the migration, roll back every test transaction and downgrade at teardown, asserting that every table is empty.
- The new tests write only inside the `populated` transaction: extra snapshot rows, a duplicate stock row, rule tampering and an exposure update.
- The development database was never opened in this phase. No `.env` value or URL was printed.

**Schema.** `git status` shows no change under `src/rfq_copilot/models/` or `alembic/`. The proposed capacity-uniqueness migration exists only as text in TD-005.

---

## G. Future-phase integration requirements

**Phase 3, the tool server and API, must:**
1. Authenticate the user and pass a verified `customer_id`. Every RFQ and quotation read is scoped by it.
2. Derive `horizon_start` from the request's evaluation date.
3. Pin `inventory_snapshot` and `capacity_snapshot` explicitly. They become mandatory as soon as a second snapshot is registered, because unpinned calls then raise.
4. Run each evaluation in `Database.snapshot_session()`.
5. Treat `SnapshotMismatchError` and `RuleIntegrityError` as hard failures. Never retry them with a different pin or rule.

**Phase 3, Intake, must:**
1. Never send `requested_operations` to `calculate_quote`. Route RFQs that contain them to human engineering review until the TD-001 contract revision is approved.
2. Supply `contradictions` as findings.

**The agents and the review UI must:**
1. Keep approval and release as authenticated human actions. No tool may expose `approve_quote` or `release_quote` to an agent.
2. Show `unevaluated_review_conditions` to the human reviewer.

**The quote-persistence phase must:**
1. Store `evaluation_fingerprint` alongside `calculation_id`.
2. Re-evaluate before writing, and refuse the write if the fingerprint changed.
3. Build idempotency keys from `evaluation_fingerprint`, never from `calculation_id` alone.

**Rule governance.** A new rule version requires an entry in `rule_manifest.py` and a re-derivation test, and the TD-003 constants guard must be extended for it.

---

## H. Known limitations

- **TD-004:** `borderline_bend_radius` is not evaluated, because no threshold is defined. It is visible, not hidden.
- **TD-005:** overlapping capacity snapshots cannot be stored. The proposed migration needs approval.
- **TD-006:** there is no wall-clock maximum-age check. It needs an approved value and the API.
- **TD-007:** the consistent-read session exists, but nothing forces a caller to use it. Material costs are not versioned.
- **TD-001:** customer-requested operations are refused rather than supported.
- **TD-003:** the reference-only constants (finish and pack minutes, the 15-day default) remain guarded constants.
- **Smoke CI:** it covers 42 tests. The full suite needs the private dataset and PostgreSQL, and the workflow has not yet run on GitHub.
- **Rule manifest:** it trusts the repository. It does not defend against someone who can change both the code and the database.
- **Concurrency:** no concurrent-commit test was written (§C9).

---

## I. Changed-file manifest

**Created:**

| File | Reason |
|---|---|
| `src/rfq_copilot/canonical.py` | Canonical JSON and sha256 fingerprints, used for rule integrity and the evaluation fingerprint |
| `src/rfq_copilot/repositories/rule_manifest.py` | Trusted manifest of the nine approved rule documents |
| `tests/test_hardening_public.py` | 23 hardening tests that need no dataset or database (the smoke suite) |
| `tests/test_hardening_services.py` | 53 pure hardening tests that use the frozen rule fixtures |
| `tests/test_hardening_database.py` | 25 hardening tests on the guarded test database |
| `.github/workflows/smoke.yml` | Minimal public CI: lock, lint, format and smoke |
| `docs/development/phase_02_5_hardening_report.md` | This report |

**Modified:**

| File | Reason |
|---|---|
| `src/rfq_copilot/repositories/queries.py` | `get_snapshot`, `SnapshotMismatchError`, snapshot filters on the stock and capacity reads, manifest verification in `get_rule_version` |
| `src/rfq_copilot/repositories/results.py` | `SnapshotDetails`; fingerprint docstring |
| `src/rfq_copilot/repositories/__init__.py` | Exports |
| `src/rfq_copilot/services/inventory.py` | Snapshot selection and guard; stock rows checked against the snapshot date; shared catalog memo |
| `src/rfq_copilot/services/capacity.py` | Snapshot selection, duplicate-week and mixed-snapshot guards, shared routing |
| `src/rfq_copilot/services/lead_time.py` | `SnapshotSelection`, `select_snapshots`, `validate_planning_dates`, request-scoped memo |
| `src/rfq_copilot/services/evaluation.py` | Release invariant, engineering conditions, evaluation fingerprint |
| `src/rfq_copilot/services/quotation.py` | `Quotation.parts` and `Quotation.line_numbers` |
| `src/rfq_copilot/services/materials.py` | `memo` and `Reads`; memoized spec and finish reads |
| `src/rfq_copilot/services/manufacturing.py` | Memoized routing read |
| `src/rfq_copilot/entities.py` | Explicit TD-001 refusal of `requested_operations` |
| `src/rfq_copilot/database.py` | `Database.snapshot_session()` |
| `tests/test_day09_inventory.py`, `test_day10_capacity.py`, `test_day11_lead_time.py`, `test_day14_quotation.py`, `test_day15_evaluation.py` | Exact SELECT counts updated to measured values (§E) |
| `README.md` | Accurate status, architecture, setup, testing, boundaries and limitations |
| `docs/development/technical_debt.md` | TD-001 reopened as Deferred; TD-004 to TD-007 added |

**Removed:** none.

---

## J. Final verdict

**READY WITH DOCUMENTED CONDITIONS**

**Evidence:**
- The two high-severity correctness and security risks are closed in code and covered by regression tests: snapshot mixing and agent release authority.
- Rule tampering is detected even when the stored sha256 is unchanged.
- Evaluations have an identity suitable for later idempotency.
- All 974 baseline tests still pass, alongside 101 new tests.
- Reference parity, `calculation_id` values and MFG-001 are unchanged.
- The dataset is intact.
- No schema or contract changed.

**Conditions** (owners in §G):
1. Phase 3 adopts the request-boundary duties: verified customer, explicit snapshot pins, `snapshot_session()`, and the Intake handling of `requested_operations`.
2. `borderline_bend_radius` is surfaced to human reviewers until a threshold is approved (TD-004).
3. A decision on the TD-005 migration is made before a second capacity snapshot is needed.

---

## K. Addendum: TD-004 and TD-005 (approved 2026-11-09)

The user approved both items. The approved threshold is "within 25 % above the minimum"; an unstated radius is not borderline.

### K1. TD-004: `borderline_bend_radius` (Finding 5 is now FIXED)

**Where the rule lives.** The frozen `approval-v1.1` cannot change, so the threshold is a new approval policy version, `src/rfq_copilot/rules/approval-v1.2.json`.
- It equals `approval-v1.1` plus `version`, `supersedes`, `change_reference` and `engineering_review_thresholds.borderline_bend_radius.radius_below_minimum_ratio = "1.25"`. A test asserts this exact delta.
- Its trusted-manifest entry is source sha256 `6864a140…aacc`, fingerprint `33b8ea36…0ead`.
- It is stored in `business_rule_versions` by `seeding/rules.py`, which has a CLI and an idempotent registration function. Registration checks the file against the manifest, requires the baseline policy to be seeded, and reads the row back through `get_rule_version`. Rule kind `approval` is already allowed by the schema.

**Baseline audits.** The seed `--verify` and integrity audits demand an exact match with the frozen baseline. They now accept exactly one kind of extra row: a rule row that is not in the baseline, is listed in `seeding/rules.REGISTERED`, and matches its manifest entry in kind, sha256 and fingerprint (`is_registered_extra`). Any other extra row is still refused (tested).

**Semantics.**
- `ManufacturingCheck.min_bend_radius_mm` records `max(rule factor, material factor) × thickness` for bent parts. It is now computed in an exact Decimal context; before, it was computed inline in the caller's context.
- `evaluation.borderline_bend_lines` names each line where `minimum ≤ stated radius < ratio × minimum`.
- Any borderline line sets `engineering_review_required`, at the same outcome tier as capacity and delivery problems. The escalation reason `BORDERLINE_BEND_RADIUS` ranks last, so existing reason precedence is unchanged.
- Drafting and manager review are unaffected, because review stays separate.
- A radius below the minimum is still `unsupported_declined`.
- Thresholds are validated whether or not they are used. A ratio of 1 or less, a float, a missing value, an unknown condition, or a threshold for an undeclared condition all raise an error.
- Under `approval-v1.1`, behaviour is byte-for-byte the previous behaviour: the condition is reported as unevaluated.

**Parity.** All 160 dev/validation RFQs were evaluated under both versions. Outcomes, reasons, draft gates and manager reviews are identical. **0 of 160 are borderline**, so v1.2 changes no labelled outcome. A seeded RFQ with a bracket radius set to exactly the minimum moves to `engineering_review_required` / `BORDERLINE_BEND_RADIUS`.

**Tests.**
- In `tests/test_hardening_services.py`, 18 new tests: the document delta, the recorded minimum, the boundaries (exactly the minimum, 1.249×, 1.25×, 2×), below the minimum, an unstated radius, flat parts, per-line naming, precedence, v1.1 unchanged, six malformed thresholds, and an undeclared condition.
- In `tests/test_hardening_database.py`, 6 new tests: register once and confirm idempotence, tampered v1.2, unapproved version, the baseline checks accepting only the registered extra, v1.2 versus v1.1 over 160 RFQs, and a borderline bend on seeded data.

### K2. TD-005: the capacity key now includes the snapshot (Finding 1's limitation is resolved)

**Migration.** Alembic revision `20261109_01` (`capacity_unique_per_snapshot`, down revision `20261108_01`) drops `uq_production_capacity_workcentre_id_week_start` and creates `uq_production_capacity_workcentre_id_week_start_snapshot_id`.
- The initial migration file is unchanged.
- The downgrade restores the old key; PostgreSQL refuses it while two snapshots share a week, and nothing is deleted.
- The ORM `ProductionCapacity` constraint matches, and `alembic check` reports no drift.

**Pins updated.**
- `seeding/safety.py`: `REVISION = "20261109_01"`, and `SHAPE_DIGEST` set to the shape `6cc43f0e…590d`, measured by a real upgrade of the guarded test database.
- The tests now expect two revisions in the history (`test_configuration_metadata_and_revision`).
- `test_orm_models.approved` applies the single approved delta to the frozen Day 4 document, after asserting the original key.
- The revision literals in `test_database_end_to_end.py` and `test_database_integrity.py`.

The full migration lifecycle passes: upgrade, idempotent upgrade, check, guarded downgrade refusals, downgrade to base, re-upgrade, and cleanup.

**Tests.**
- `test_two_capacity_snapshots_are_never_mixed` now stores a complete second capacity snapshot over the **same** weeks, with every week fully booked. Snapshot A's plan is identical to the plan before B existed. Snapshot B is infeasible. Unpinned selection refuses.
- New: `test_a_week_stays_unique_within_one_snapshot`, which checks that a duplicate week inside one snapshot violates the new key.

### K3. Development database (writes made after approval)

| Step | Command | Result |
|---|---|---|
| Migration | `.venv/bin/python -m alembic -x target=development -x env_file=.env upgrade head` | `20261108_01 -> 20261109_01`; `current --check-heads` gives `20261109_01 (head)` |
| Policy registration | `.venv/bin/python -m rfq_copilot.seeding.rules approval-v1.2 --target development --env-file .env --confirm-database rfq_copilot_dev` | `registered`; a second run gives `already present` |
| Integrity audit (read only) | `.venv/bin/python -m rfq_copilot.integrity --target development …` | PASS; revision `20261109_01`; 1,090 records (1,089 baseline + 1 registered rule) |
| Seed verify (read only) | `.venv/bin/python -m rfq_copilot.seeding.cli --target development … --verify` | PASS; 0 inserted; read-only transaction |

Two development tests pinned the record count to the bare baseline. They now expect `1089 + len(REGISTERED)` (`test_current_development_integrity_and_preservation` and `test_all_development_reads_preserve_records_and_close_sessions`).

**Rollback, if needed:**
1. Delete the `approval-v1.2` row.
2. Run `alembic … downgrade 20261108_01`.
3. Restore `REVISION` and `SHAPE_DIGEST`.

### K4. Final evidence

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q -p no:cacheprovider` | **1100 passed**, 0 failed, 0 errors, 0 skipped (117.43 s) |
| `.venv/bin/ruff check .` / `ruff format --check src tests alembic` | All checks passed; 78 files already formatted |
| `uv lock --check` / `git diff --check` | Current; clean |
| Smoke in a clean copy without `data/` and `.env` | 42 passed |
| Dataset | 681 files; no caches; `test_source_inventory_and_checksums` passes in the full run |

**New test totals:** 126 hardening tests (public 23, services 71, database 32). The suite went from 974 at baseline to 1100.

**Updated verdict: READY FOR PHASE 3.** Findings 1–5 and 10 are fixed. The remaining open items carry approved owners in later phases, which the user accepted on 2026-11-09:
- Findings 6–9 and TD-001: the Intake contract;
- TD-006: freshness checks at the API boundary;
- TD-007: `snapshot_session` adoption and revalidation before persistence;
- TD-003: the guarded reference constants.

The rest of §G still applies.

## L. Pre-commit review (2026-11-09)

- **Approval pinning (PASS).**
  - `approval_rules` is required and has no default.
  - Nothing selects a "latest" version.
  - A throwaway check against the test database evaluated all 160 development and validation RFQs under `approval-v1.1`, both before and after registering `approval-v1.2`. All 160 `evaluation_fingerprint`s are identical.
- **Fresh install (PASS, with one known test failure).**
  - The documented README sequence ran from a clean copy of the commit-equivalent tree against the guarded test database: migrate to `20261109_01`, seed 1089, register `approval-v1.2`, verify PASS, integrity PASS with 1090 records.
  - The test database was then returned to empty. The development database was not modified.
  - In that copy, `test_source_inventory_and_checksums` fails `680 == 681` (`.DS_Store`; TD-008).
- **Dataset publication (FAIL as a whole-dataset publish).** Frozen labels are present in the frozen folders, in two scorer files, in the manifest and checksums, and they can be regenerated with the bundled generator. See README "Dataset distribution" and TD-008.

**Pre-commit cleanup (2026-11-09).**
- **Dataset kept private.** `.gitignore` excludes the directory and the archive again, and is identical to HEAD. TD-008 (a public subset) is deferred.
- **Inventory fix.** Seeding now requires the extracted files to equal the archive's 680 files exactly. Only files named `.DS_Store` are ignored, and all 637 hash checks still run. Added 11 regression tests.
- **Results.**
  - Full suite: 1111 passed.
  - `ruff check .`: clean.
  - Smoke in a clean copy without the dataset or `.env`: 42 passed.
  - Development database (read only): verify PASS with 680 files; integrity PASS with 1090 records.
  - `approval-v1.1`: 160/160 evaluation fingerprints unchanged by registering `approval-v1.2`.
