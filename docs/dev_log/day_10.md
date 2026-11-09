# Development Log: Day 10 — Production Capacity and Operation Routing

Date: 2026-11-09

## Objective

A deterministic, read-only answer to "can our workcentres fit this RFQ's operations, and in which weeks?" for the Supply and Scheduling agent. The service converts each routed operation into workcentre hours and plans them first-fit into weekly free capacity. It returns a structured plan for Day 11 lead-time estimation, without computing dates or lead days.

Baseline before editing: 794 passed (Day 9 final).

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/services/capacity.py` | New: `OperationLoad`, `WeekLoad`, `CapacityCheck`, `operation_minutes`, `evaluate_capacity` (pure), `check_capacity`. |
| `tests/test_capacity_service.py` | New: 28 database-free unit and boundary tests. |
| `tests/test_day10_capacity.py` | New: 6 integration and parity tests on the guarded test database. |
| `docs/development/technical_debt.md` | Added TD-003 (finish and pack minutes), scheduled for Day 12. |

No repository, ORM, migration, dataset, contract or dependency changes. TD-001 (Day 13) and TD-002 (Day 11) unchanged.

## Design

**Flow.** `check_capacity(session, parts, inventory, *, manufacturing_rules, pricing_rules, horizon_start, start_week=0)`:
1. Validates everything before SQL. The pricing rule kind and fingerprint must equal those recorded in the Day 9 `InventoryCheck`; every Day 8 check must carry the manufacturing fingerprint; the inventory must cover exactly these parts; `horizon_start` must be a Monday; `start_week` must be an int ≥ 0.
2. If the inventory result is `needs_clarification` or `not_manufacturable`, passes it through with no plan.
3. Otherwise reads routing once per part type (Day 8 `list_part_routing`) and capacity once per routed workcentre (Day 5 `get_workcentre_capacity`, from `horizon_start`). Then calls the pure `evaluate_capacity`.

**Reuse.** Operations come from Day 8 (`ManufacturingCheck.operations`); routing logic is not repeated. Stored `operation_routing` supplies only each operation's workcentre. Blank area comes from Day 9 (`PartRequirement.blank_area_m2`). Free hours are PostgreSQL's computed `remaining_hours = nominal − downtime − scheduled`. The planning horizon is each workcentre's `planning_horizon_weeks` (10).

**Hours per operation** (reference `operation_minutes` and `estimate_schedule`), for the whole quantity:
- laser: `q × perimeter / cutting_mm_per_min`, perimeter = `2(L+W)` + `2H` (L-bracket) / `4H` (U-bracket) / `8H` (enclosure), doubled for fabricated assemblies, plus `holes × hole_cut_extra_mm`
- bend: `q × bends × bend_minutes_per_bend`, bends per part type from `mfg-v1.0` `bend_counts`
- weld: `q × weld_length / weld_mm_per_min`; inspect: `q × inspection_minutes_per_unit`
- finish: `q × area × 2 × 0.30`; pack: `q × 0.75` (reference-only constants, TD-003)
- required hours = `(run minutes + setup_minutes[op]) / 60`, setup minutes from `price-v1.0`. A missing setup or workcentre raises.

**Planning algorithm.** Operations are taken in part order, then route order. Each takes the first week `w` in `[cursor, horizon)` whose free hours are ≥ its hours. That week's free hours are reduced in memory, and the cursor becomes `w`. The cursor never moves back, across operations and across parts, and hours are shared by all of the RFQ's operations. A missing week row counts as 0 hours. The first operation that fits nowhere makes the RFQ `infeasible`, returning the partial plan and the unscheduled operation, with reason `NO_CAPACITY_IN_HORIZON`, or `MISSING_CAPACITY_DATA` when the workcentre has no capacity rows inside its horizon at all.

**Precision decision.** Hours repeat forever (`/60`, `/65`), so the Day 9 exact context cannot be used. Calculations run in an explicit context: precision 28, `ROUND_HALF_EVEN` (the reference's Decimal default), traps `InvalidOperation`. Operations follow the reference's exact order, so required hours match the ground truth **as exact strings**. A test proves the result does not depend on the caller's Decimal context.

**Outputs for Day 11.** `CapacityCheck` carries `result`, `reason`, planned `operations` (minutes, setup, hours, week), `unscheduled`, the `weeks` touched (free hours before and hours planned), `horizon_start`, `start_week`, `final_week_start` (week of the last planned operation), `capacity_snapshot_ids`, the Day 9 `inventory`, and the pricing rule version and fingerprint.

**Boundaries kept.** The start week is an input: turning procurement days into a week (and TD-002) is Day 11, as are the ready date and lead business days. `horizon_start` is also an input; Day 11 and Day 15 decide the planning week. No capacity or inventory rows are written; consumption exists only in memory.

## Problems found during implementation

- One unit test expected `NO_CAPACITY_IN_HORIZON` for a workcentre whose only capacity row lay beyond a 2-week horizon. The code correctly said `MISSING_CAPACITY_DATA`. I fixed the test to cover both cases.

## Commands and actual results

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_capacity_service.py` | 28 passed in 0.11s (first run 1 failed, 27 passed: the test above) |
| `.venv/bin/python -m pytest -q tests/test_day10_capacity.py` | 6 passed in 4.14s |
| `.venv/bin/python -m pytest -q` | **828 passed in 82.90s** (794 baseline + 34 new) |
| `.venv/bin/ruff check .` | All checks passed! |
| `.venv/bin/ruff format --check src tests alembic` | 56 files already formatted |
| `uv lock --check` | Resolved 23 packages |
| `git diff --check` | clean |
| dataset bytecode/cache scan | 0 files |
| `pytest tests/test_database_seeding.py::test_source_inventory_and_checksums` | 1 passed (681 files, 637 protected hashes) |

Parity coverage:
- **Ground truth schedules:** all 100 dev/validation `expected_schedule` labels (98 feasible, 2 infeasible on `weld`), with the start week derived from the labelled procurement days. Every planned operation matches part, operation, workcentre, week and required hours as an exact string, along with feasibility, reason and missing operation.
- **Reference `estimate_schedule`:** 26 RFQs × procurement days 0, 7, 19 and 45 = 104 comparisons, identical. The RFQs are 6 types × quantities 1, 100 and 1000, a 6-part mix, a 6-part ×1000 mix, and 3 identical ×1000 parts per type. Both feasible and infeasible outcomes are exercised.
- **Database cases:** exact boundary (0.5 h free → week 0, 0.4999 h → week 1, for 10 panels needing exactly 0.5 laser hours), a full inspection workcentre → infeasible after laser, and an inventory check for other parts rejected.
- **Read only:** 9 SELECTs for one bracket (1 routing + 2 × 4 workcentres); repeat calls give equal results; capacity rows unchanged; snapshot IDs recorded.

Read-only demo against development (`day10_demo.py` in the session scratchpad; start weeks passed explicitly, RFQ-0062's from its 11 procurement days):

```
RFQ-0001 (stock sufficient, start week 0): feasible  final week 2026-11-09
  PART-000010 laser   WC-LAS  run 82.960 + setup 18 min = 1.6827 h -> week 2026-11-09
  PART-000010 bend    WC-BND  run 27.20 + setup 14 min = 0.6867 h -> week 2026-11-09
  PART-000010 inspect WC-QA   run 127.5 + setup 8 min = 2.2583 h -> week 2026-11-09
  PART-000010 pack    WC-PCK  run 63.75 + setup 5 min = 1.1458 h -> week 2026-11-09
RFQ-0041 (stock sufficient, start week 0): feasible  final week 2026-11-09
  PART-000410 laser   WC-LAS  run 5.832 + setup 18 min = 0.3972 h -> week 2026-11-09
  PART-000410 finish  WC-FIN  run 0.523260 + setup 20 min = 0.3421 h -> week 2026-11-09
  PART-000410 inspect WC-QA   run 13.5 + setup 8 min = 0.3583 h -> week 2026-11-09
  PART-000410 pack    WC-PCK  run 6.75 + setup 5 min = 0.1958 h -> week 2026-11-09
  PART-000411 laser   WC-LAS  run 42.032 + setup 18 min = 1.0005 h -> week 2026-11-09
  PART-000411 bend    WC-BND  run 90.88 + setup 14 min = 1.7480 h -> week 2026-11-09
  PART-000411 finish  WC-FIN  run 3.957540 + setup 20 min = 0.3993 h -> week 2026-11-09
  PART-000411 inspect WC-QA   run 106.5 + setup 8 min = 1.9083 h -> week 2026-11-09
  PART-000411 pack    WC-PCK  run 53.25 + setup 5 min = 0.9708 h -> week 2026-11-09
RFQ-0062 (stock insufficient, start week 3): infeasible NO_CAPACITY_IN_HORIZON final week 2026-11-30
  PART-000620 laser   WC-LAS  run 867.344 + setup 18 min = 14.7557 h -> week 2026-11-30
  UNSCHEDULED PART-000620 weld at WC-WLD: needs 38.5259 h, no week in horizon has room
    touched WC-LAS 2026-11-30: free 26.0000 h, planned 14.7557 h
```

## Limitations

- **First-fit, sequential and greedy, as in the reference.** No splitting of an operation across weeks, no parallel operations within a week, no backtracking. One large operation can be infeasible even when total free hours would suffice.
- **Per-RFQ simulation only.** Hours consumed by other open quotes are not considered; nothing is reserved.
- **Week granularity.** No day-level sequencing, shifts or holidays.
- `MISSING_CAPACITY_DATA` is an addition the reference cannot express; it would report `NO_CAPACITY_IN_HORIZON`. It never occurs in v1.1.0.
- Finish and pack minutes are reference-only constants (TD-003, Day 12).
- Query count grows with distinct part types and workcentres; the Day 9 inventory check, and with it Day 8, is computed by the caller beforehand.

## Git

Nothing staged, committed, pushed or stashed.
