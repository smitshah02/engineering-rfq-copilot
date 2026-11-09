# Development Log: Day 11 — Lead-Time Estimation

Date: 2026-11-09

## Objective

The deterministic `estimate_lead_time` decision for the Supply and Scheduling agent: when material can be ready, which week production can start, the estimated ready date and lead business days, and whether the customer's requested delivery date is achievable. It is the first service that runs the whole deterministic chain (Days 8 → 9 → 10) end to end. It also resolves TD-002.

Baseline before editing: 828 passed (Day 10 final).

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/services/lead_time.py` | New: `MaterialDelay`, `StartPlan`, `LeadTimeEstimate`, `SnapshotMismatchError`, `add_business_days`, `business_days_between`, `procurement_business_days`, `plan_start`, `evaluate_lead_time` (pure), `estimate_lead_time`. |
| `tests/test_lead_time_service.py` | New: 34 database-free unit and boundary tests. |
| `tests/test_day11_lead_time.py` | New: 5 integration and parity tests on the guarded test database. |
| `docs/development/technical_debt.md` | TD-002 resolved; TD-003 widened to include the 15-day default. |

No repository, ORM, migration, dataset, contract or dependency changes. TD-001 stays scheduled for Day 13.

## Design

**Flow.** `estimate_lead_time(session, parts, *, manufacturing_rules, pricing_rules, horizon_start, requested_delivery_date=None, inventory_snapshot=None, capacity_snapshot=None)`:
1. Validates the horizon (a Monday) and the snapshot pins before SQL.
2. Runs Day 9 `check_inventory`, which runs Day 8 and Day 7.
3. `plan_start` computes the procurement delay and the start week.
4. Runs Day 10 `check_capacity` from that start week.
5. The pure `evaluate_lead_time` produces the estimate.

**Procurement delay (TD-002 decision).** Per material line: 0 business days if `sufficient`; otherwise the maximum positive `supplier_lead_business_days` among its stock rows; otherwise `DEFAULT_PROCUREMENT_BUSINESS_DAYS = 15`. The RFQ delay is the maximum over its lines. The reference code's rule is adopted over the documentation's wording because it generated the ground truth and is the conservative choice.

**Calendar** (reference `weekday_advance` and `estimate_schedule`):
- earliest start = horizon start + delay in Monday–Friday business days
- start week = ⌈(earliest − horizon start) / 7 days⌉, the first Monday on or after the earliest date. Passed to Day 10 as `start_week`.
- ready date = Friday of the week of the last planned operation (`final_week_start + 4 days`), never before the earliest start
- lead business days = Monday–Friday days in (horizon start, ready date]; no holidays, as the dataset specifies

**Delivery date.** With a requested date, `delivery_feasible = ready ≤ requested` (ready on the day is on time). A late ready date adds `DELIVERY_DATE_INFEASIBLE`. If the plan is infeasible there is no ready date: the finding is `CAPACITY_OUT_OF_HORIZON` (or `CAPACITY_DATA_MISSING` for Day 10's `MISSING_CAPACITY_DATA`), and `delivery_feasible` is `None` (unknown) rather than a guess.

**Integrity.**
- The Day 10 plan must have been made from the same `InventoryCheck` object, the same horizon start and exactly the computed start week, otherwise `ValueError`. A plan from another start week cannot produce a ready date.
- Optional snapshot pins (the contract's `inventory_snapshot` and `capacity_snapshot`): every stock row and capacity row read must come from the pinned snapshot, otherwise `SnapshotMismatchError`.
- Requests that need clarification or cannot be manufactured return that result with no dates and no start week.

**Outputs.** `LeadTimeEstimate`: `result` (`estimated | infeasible | needs_clarification | not_manufacturable`), `estimated_ready_date`, `lead_business_days` (the contract's two outputs), `procurement_business_days`, per-line `material_delays`, `horizon_start`, `earliest_start_date`, `start_week`, `requested_delivery_date`, `delivery_feasible`, `findings`, and the full Day 9 `inventory` and Day 10 `capacity` results for explanation and audit.

## Problems found during implementation

- A unit-test helper always planned capacity for a single part. Day 10's guard correctly rejected the two-part inventory check. The helper now passes the parts through.
- Removed a filler unit test that asserted nothing meaningful.

## Commands and actual results

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_lead_time_service.py` | 34 passed in 0.11s (first run: 1 failed, 34 passed, the helper above; then the filler test removed) |
| `.venv/bin/python -m pytest -q tests/test_day11_lead_time.py` | 5 passed in 4.12s |
| `.venv/bin/python -m pytest -q` | **867 passed in 94.21s** (828 baseline + 39 new) |
| `.venv/bin/ruff check .` | All checks passed! |
| `.venv/bin/ruff format --check src tests alembic` | 59 files already formatted |
| `uv lock --check` | Resolved 23 packages |
| `git diff --check` | clean |
| dataset bytecode/cache scan | 0 files |
| `pytest tests/test_database_seeding.py::test_source_inventory_and_checksums` | 1 passed (681 files, 637 protected hashes) |

Parity coverage:
- **Ground truth:** all 100 dev/validation RFQs with `expected_schedule`, run end to end with the real snapshot pins (`inventory-v1.1.0-2026-11-02`, `capacity-v1.1.0-2026-11-02`) and each RFQ's `expected_delivery_date`. Ready date, lead business days, procurement days and feasibility all match.
  - Lead-time findings are empty exactly when `draft_creation_allowed` is true, for all 100.
  - Both `DELIVERY_DATE_INFEASIBLE` RFQs (RFQ-0069, RFQ-0150) and the `CAPACITY_OUT_OF_HORIZON` RFQ give exactly that finding.
  - The 40 incomplete or unsupported RFQs give no dates.
- **Reference `check_inventory` + `estimate_schedule`:** 182 RFQs (6 part types × 15 materials × quantities 40 and 400, plus two 6-part mixes): identical ready date, lead days, procurement days, feasibility and per-line delays. Delays 0, 7, 11, 15 and 19 days are all exercised.
- **Database cases:** TD-002 regression (a zero-lead stock row that is short → 15 days, earliest start 2026-11-30), and wrong inventory or capacity snapshot pins rejected.
- **Read only:** 16 SELECTs for one flat panel (Day 8: 7, Day 9 stock: 2, Day 10: 1 routing + 3 workcentres × 2); repeat calls give equal results; inventory and capacity rows unchanged.

Read-only demo against development (`day11_demo.py` in the session scratchpad; real snapshot pins and requested dates):

```
pinned snapshots: {'inventory': 'inventory-v1.1.0-2026-11-02', 'capacity': 'capacity-v1.1.0-2026-11-02'}
RFQ-0001: estimated; materials [MAT-009 1.0mm sufficient->0d]; earliest start 2026-11-09 (week 0)
   ready 2026-11-13, lead 4 business days; requested 2026-12-22 -> feasible True ()
RFQ-0002: estimated; materials [MAT-009 4.0mm insufficient->7d]; earliest start 2026-11-18 (week 2)
   ready 2026-11-27, lead 14 business days; requested 2027-01-11 -> feasible True ()
RFQ-0062: infeasible; materials [MAT-010 2.0mm insufficient->11d]; earliest start 2026-11-24 (week 3)
   ready None, lead None business days; requested 2027-01-07 -> feasible None ('CAPACITY_OUT_OF_HORIZON',)
RFQ-0069: estimated; materials [MAT-012 3.0mm insufficient->19d]; earliest start 2026-12-04 (week 4)
   ready 2027-01-08, lead 44 business days; requested 2026-12-31 -> feasible False ('DELIVERY_DATE_INFEASIBLE',)
```

## Limitations

- A planning estimate, not a commitment: nothing is reserved, ordered or scheduled. Other open quotes are not considered.
- No holidays, plant shutdowns or shipping transit time; Monday–Friday only, as the dataset specifies.
- Material readiness is weekly: production starts on the Monday on or after the earliest material date, even if material arrives mid-week.
- `horizon_start` is an input. Choosing the planning week (and checking it against the capacity snapshot's `as_of_date`) belongs to the Day 15 integration.
- The 15-day default, like the finish and pack minutes, is a reference-only constant (TD-003, Day 12).
- Lead-time findings are one input to draft creation; customer, credit and approval gates are later services.

## Git

Nothing staged, committed, pushed or stashed.
