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
