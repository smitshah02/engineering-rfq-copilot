# Development Log: Day 12 — Manufacturing Cost Modelling

Date: 2026-11-09

## Objective

What each quote line costs to make, for the Pricing agent. The cost is broken down into material, labour, finishing, packaging and setup, matching the dataset's reference cost model, before Day 13 applies margin and totals. TD-003 resolved.

Baseline before editing: 867 passed (Day 11 final).

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/services/costing.py` | New: `OperationCost`, `LineCost`, `CostEstimate`, `line_cost`, `evaluate_costs` (pure), `rounding`, `display_costs`, `estimate_costs`. |
| `src/rfq_copilot/repositories/queries.py`, `__init__.py` | New `list_materials` (one SELECT), exported. |
| `src/rfq_copilot/services/capacity.py` | TD-003 guard: `REFERENCE_PRICING_VERSIONS`, `require_reference_constants`, called by `operation_minutes`. |
| `src/rfq_copilot/services/lead_time.py` | `plan_start` calls the TD-003 guard for the 15-day default. |
| `tests/test_costing_service.py` | New: 18 database-free unit, boundary and TD-003 tests. |
| `tests/test_day12_costing.py` | New: 6 integration and parity tests on the guarded test database. |
| `docs/development/database_queries.md` | Appended `list_materials`. |
| `docs/development/technical_debt.md` | TD-003 resolved. |

No ORM, migration, dataset, contract or dependency changes. TD-001 stays for Day 13.

## Design

**Flow.** `estimate_costs(session, parts, inventory, *, manufacturing_rules, pricing_rules)`:
1. Validates the rule kind and that the inventory covers these parts.
2. If the parts are manufacturable, reads their catalog rows with `list_materials`.
3. Calls the pure `evaluate_costs`, which checks that the inventory was computed under the same rule fingerprints. Requests that need clarification or cannot be manufactured pass through uncosted.

**Reuse.** Blank area, net and scrap-allowance mass come from Day 9 `PartRequirement`, and the route from Day 8. Per-unit run minutes come from Day 10 `operation_minutes` with quantity 1, as the reference computes them.

**Cost model** (reference `calculate_part`; rates from the pinned `price-v1.0`):

| Component | Formula |
| --- | --- |
| material / unit | allowance kg × `cost_usd_per_kg` |
| labour / unit | Σ over routed operations except finish and pack of `(run minutes ÷ 60) × workcentre_hourly_rates_usd[op]` |
| finish / unit | `blank area × finishing_usd_per_m2[finish]` when finish is routed, else 0 |
| packaging / unit | `packaging_usd_per_unit` |
| variable / unit | material + labour + finish + packaging |
| setup / line | Σ over every routed operation (finish and pack included) of `setup_minutes[op] × rate ÷ 60`, once per line |
| manufacturing cost | quantity × variable / unit + setup / line |

**Decisions.**
- **Cost is pricing data.** Day 7's `MaterialSpec` excludes cost. `list_materials` reads it, and each material's `pricing_rule_version` must equal the pinned pricing rule (`price-v1.0`). A cost row from another pricing version is rejected, so costs and rates never mix versions. Missing rates or setups raise instead of defaulting to 0 (the reference defaults setups).
- **Unrounded values, rounded display.** All arithmetic runs in the Day 10 `PLANNING` context (precision 28, `ROUND_HALF_EVEN`, the reference's default) in the reference's order of operations, and results are kept unrounded for Day 13. `display_costs` rounds with the rule's own declared `rounding` (`ROUND_HALF_UP`, from an allow-list), `unit_display_scale` (0.0001) and `line_total_scale` (0.01 for setup), giving the ground truth's exact strings.
- **Explainability.** `OperationCost` per routed operation: unit run minutes, hourly rate, unit labour (0 for finish and pack, which are charged per m² and per unit) and setup minutes and cost.
- **TD-003.** Reference-only constants stay as constants but are bound to `price-v1.0`. Any other pricing version raises in `operation_minutes` and `plan_start` instead of silently inheriting them.

## Commands and actual results

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_capacity_service.py tests/test_lead_time_service.py` (after adding the guard) | 62 passed in 0.13s |
| `.venv/bin/python -m pytest -q tests/test_costing_service.py` | 18 passed in 0.10s |
| `.venv/bin/python -m pytest -q tests/test_day12_costing.py` | 6 passed in 4.37s |
| `.venv/bin/python -m pytest -q` | **891 passed in 92.34s** (867 baseline + 24 new) |
| `.venv/bin/ruff check .` | All checks passed! |
| `.venv/bin/ruff format --check src tests alembic` | 62 files already formatted |
| `uv lock --check` | Resolved 23 packages |
| `git diff --check` | clean |
| dataset bytecode/cache scan | 0 files |
| `pytest tests/test_database_seeding.py::test_source_inventory_and_checksums` | 1 passed (681 files, 637 protected hashes) |

Parity coverage:
- **Ground truth:** all 120 priced dev/validation lines. Every cost field string (material, labour, finish, packaging and variable per unit, setup per line, total manufacturing cost), plus part ID, quantity, operations and pricing rule version, is identical.
- **Reference `calculate_part`:** 6 part types × 15 materials × 2 compatible finishes × quantities 1 and 250 = 360 lines, identical on every cost field.
- **Database cases:** `list_materials` (dedupe, order, unknown ID absent, one SELECT, no SQL for an empty list); a changed catalog cost is used live; a material re-pointed to another rule version (`mfg-v1.0`) is rejected.
- **Read only:** costing issues 1 SELECT; material rows unchanged; repeat calls equal.

Read-only demo against development (`day12_demo.py` in the session scratchpad):

```
RFQ-0001: costed, total manufacturing cost 1809.3610 USD (price-v1.0)
  PART-000010 x85: material 2.636984 kg x $6.4000/kg = 16.8767 | labour 3.1417 | finish 0.0000 | pack 0.6500 | variable/unit 20.6684 | setup 52.55 | line cost 1809.3610
      laser    0.9760 min/unit @ $85/h -> labour 1.3827/unit, setup 18 min = 25.5000
      bend     0.3200 min/unit @ $72/h -> labour 0.3840/unit, setup 14 min = 16.8000
      inspect  1.5000 min/unit @ $55/h -> labour 1.3750/unit, setup 8 min = 7.3333
      pack     0.7500 min/unit @ $35/h -> labour 0.0000/unit, setup 5 min = 2.9167
RFQ-0041: costed, total manufacturing cost 759.2294 USD (price-v1.0)
  PART-000410 x9: material 0.296281 kg x $6.3000/kg = 1.8666 | labour 2.2930 | finish 2.8101 | pack 0.6500 | variable/unit 7.6197 | setup 57.08 | line cost 125.6604
  PART-000411 x71: material 0.280930 kg x $8.1000/kg = 2.2755 | labour 3.7497 | finish 1.2077 | pack 0.6500 | variable/unit 7.8829 | setup 73.88 | line cost 633.5690
RFQ-0007: costed, total manufacturing cost 896.3194 USD (price-v1.0)
  PART-000070 x10: material 6.303360 kg x $10.2000/kg = 64.2943 | labour 9.1323 | finish 6.0970 | pack 0.6500 | variable/unit 80.1736 | setup 94.58 | line cost 896.3194
      weld     3.6000 min/unit @ $90/h -> labour 5.4000/unit, setup 25 min = 37.5000
```

## Limitations

- An illustrative cost model (as the reference states): no machine depreciation, overhead allocation, tooling, freight, tax or duties.
- Costs are not money totals: margin, line selling totals, the quote total and `calculation_id` are Day 13.
- `total_manufacturing_cost` across lines is informational; the quote total will sum rounded line selling totals (Day 13), as the dataset specifies.
- Material cost is read live from the catalog. Point-in-time cost pinning beyond the pricing-version check is part of quote persistence, which is out of Phase 2 scope.
- Finishing labour is not charged by the hour (reference behaviour); finishing is priced per m² of blank area.

## Git

Nothing staged, committed, pushed or stashed.
