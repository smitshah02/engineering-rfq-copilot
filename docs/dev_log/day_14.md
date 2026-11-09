# Development Log: Day 14 — Multi-Part Quotations

Date: 2026-11-09

## Objective

Turn an RFQ's lines into one quotation: every line evaluated, sharing stock and workcentre hours, costed and priced together, with a per-line explanation and a single all-or-nothing result. Quotations can be built from supplied parts or from a stored RFQ's runtime lines, scoped by customer.

Baseline before editing: 924 passed (Day 13 final).

## What was already multi-part, and what Day 14 adds

Days 9–13 already handle several lines: stock demand is summed per material and thickness (Day 9), lines share workcentre hours under one forward-only week position (Day 10), the RFQ waits for its longest procurement delay (Day 11), setup is charged once per line (Day 12), and the quote total is the sum of rounded line totals (Day 13). The dataset defines no other multi-part rule; its 20 multi-part RFQs are each two fully valid lines, and every RFQ is either fully priced or not priced.

Day 14 adds the quotation as a unit:
- one composition that runs each service **exactly once** and threads the same results through
- line-set integrity: at least one line, unique part IDs, positive ascending line numbers
- the all-or-nothing result with the **blocking line numbers** named
- a per-line breakdown for explanation
- quoting a stored RFQ by ID, scoped by customer, keeping its real line numbers

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/services/quotation.py` | New: `LineOutcome`, `Quotation`, `validate_lines`, `assemble_quotation` (pure), `quote_parts`, `quote_rfq`. |
| `tests/test_quotation_service.py` | New: 19 database-free unit tests. |
| `tests/test_day14_quotation.py` | New: 5 integration and parity tests on the guarded test database. |

No repository, ORM, migration, dataset, contract or dependency changes. The technical-debt register stays empty.

## Design

**Flow.** `quote_parts(session, parts, *, manufacturing_rules, pricing_rules, horizon_start, line_numbers=None, requested_delivery_date=None, inventory_snapshot=None, capacity_snapshot=None)`:
1. `validate_lines` runs before any SQL.
2. Day 11 `estimate_lead_time` runs once; it runs Days 8, 9 and 10 once each, with the snapshot pins applied.
3. If the lines are manufacturable, Day 12 `estimate_costs` runs on **the same** `InventoryCheck`, then Day 13 `calculate_quote` on that cost estimate.
4. The pure `assemble_quotation` builds the result.

**`quote_rfq(session, rfq_id, *, customer_id, ...)`** reuses Day 5 `get_rfq`: `None` for a missing RFQ or another customer's, `ValueError` for an RFQ with no runtime lines (for example a historical RFQ). Lines become `Part.from_line` with their stored `line_number`s.

**Result (all or nothing).**
- Any line `needs_clarification` → `needs_clarification` (takes precedence).
- Otherwise any line `unsupported` → `not_manufacturable`.
- Otherwise `quoted`.
- `blocking_lines` lists the line numbers responsible. Nothing is priced unless every line is manufacturable: a partial quote would be a commercial policy the dataset does not define.
- Capacity and delivery problems do **not** block pricing (as in the ground truth, where the capacity-infeasible RFQ is still priced). They appear in `findings` (`CAPACITY_OUT_OF_HORIZON`, `DELIVERY_DATE_INFEASIBLE`).

**`LineOutcome` per line:** line number, part ID, the Day 8 check, the shared stock line it draws on (material and thickness), that line's status and procurement days, its planned operations, its unscheduled operation if any, the week of its last operation, and its priced line.

**`Quotation`:** result, lines, blocking lines, calculation ID, quote total, currency, ready date, lead business days, findings, and the full Day 11, Day 12 and Day 13 results.

**Integrity.** `assemble_quotation` refuses a cost estimate not built on this lead time's stock check, pricing not built on this cost estimate, a lead time covering other lines, and a manufacturable quotation without a price.

**Line order is significant and preserved.** It changes the weekly plan (earlier lines take earlier hours) and the `calculation_id`, but not line prices or the quote total. A unit test demonstrates each.

## Commands and actual results

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_quotation_service.py` | 19 passed in 0.12s |
| `.venv/bin/python -m pytest -q tests/test_day14_quotation.py` | 5 passed in 3.81s |
| `.venv/bin/python -m pytest -q` | **948 passed in 96.19s** (924 baseline + 24 new) |
| `.venv/bin/ruff check .` | All checks passed! |
| `.venv/bin/ruff format --check src tests alembic` | 68 files already formatted |
| `uv lock --check` | Resolved 23 packages |
| `git diff --check` | clean |
| dataset bytecode/cache scan | 0 files |
| `pytest tests/test_database_seeding.py::test_source_inventory_and_checksums` | 1 passed (681 files, 637 protected hashes) |

Parity and behaviour coverage:
- **Ground truth, end to end:** all 100 priced dev/validation RFQs, 20 of them multi-part, through `quote_parts` with the real snapshot pins and requested delivery dates. `calculation_id`, quote total, every line total, ready date, lead days, each line's stock sufficiency and the draft-creation gate (`findings == ()`) all match. The 40 incomplete or unsupported RFQs give the right result with line 1 blocking and nothing priced.
- **Mixed validity:** RFQ-0041 with line 2's tolerance removed → `needs_clarification`, blocking line 2; line 1 still reports `valid`; nothing is costed or priced.
- **Stored RFQs:** RFQ-0041's lines stored as runtime lines 10 and 20 (`Numeric(12,3)` values) → quoted, line numbers kept, and the **same calculation ID and total (998.98) as the labelled RFQ**. Another customer, or a missing RFQ → `None`. The Day 6 runtime RFQ with an incomplete line 2 → blocking line 2. A historical RFQ → `ValueError`.
- **Unit:** shared stock lines, per-line operations, precedence, capacity-infeasible but priced, a late delivery as a finding, line-order effects, explicit line numbers, 7 invalid line sets, mismatched service results, immutability.
- **Each service once, read only:** exactly 32 SELECTs for RFQ-0041 (Day 8 7 × 2, stock 2 × 2, routing 2, workcentres 5 × 2, materials 1, operations 1), no writes, repeat calls equal.

Read-only demo against development (`day14_demo.py` in the session scratchpad; real snapshot pins and requested dates):

```
RFQ-0041: quoted  blocking lines ()  CALC-0972B1CBC7F92B938A06  total 998.98 USD  ready 2026-11-13 (4 bd)  findings ()
  line 1 PART-000410: valid () | stock MAT-003 sufficient +0d | ops [laser,finish,inspect,pack] last week 2026-11-09 | line total 165.34
  line 2 PART-000411: valid () | stock MAT-002 sufficient +0d | ops [laser,bend,finish,inspect,pack] last week 2026-11-09 | line total 833.64
RFQ-0150: quoted  blocking lines ()  CALC-7E873697AB1A19E9E74C  total 45349.09 USD  ready 2026-12-18 (29 bd)  findings ('DELIVERY_DATE_INFEASIBLE',)
  line 1 PART-001500: valid () | stock MAT-002 sufficient +0d | ops [laser,weld,finish,inspect,pack] last week 2026-12-07 | line total 2483.29
  line 2 PART-001501: valid () | stock MAT-008 insufficient +19d | ops [laser,weld,finish,inspect,pack] last week 2026-12-14 | line total 42865.80
RFQ-0041 with line 2's tolerance removed: needs_clarification  blocking lines (2,)  None  total None   ready None (None bd)  findings ()
  line 1 PART-000410: valid () | stock None None +Noned | ops [] last week None | not priced
  line 2 PART-000411: needs_clarification ('MISSING_TOLERANCE_MM',) | stock None None +Noned | ops [] last week None | not priced
```

RFQ-0150 shows a multi-part effect: line 1's material is in stock, yet its work starts in December, because the RFQ waits for line 2's 19-day procurement (reference behaviour: one start week per RFQ).

## Limitations

- All or nothing: no partial quotes or line-level declines; that is a commercial policy decision outside the dataset.
- One start week per RFQ (the longest procurement delay); lines whose material is in stock are not started earlier. Same as the reference.
- Line order is the RFQ's; no reordering to improve the schedule.
- Part IDs must be unique within a quotation; repeated parts must be merged by the caller (for example by Intake).
- No persistence, draft creation, approval or customer and commercial gates. Combining the gates with these results is Day 15.

## Git

Nothing staged, committed, pushed or stashed.
