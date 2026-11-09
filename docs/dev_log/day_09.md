# Development Log: Day 09 — Inventory Management and Material Availability

Date: 2026-11-09

## Objective

A deterministic, read-only answer to "do we have the sheet metal for this RFQ?" for the Supply and Scheduling agent. The service computes kilograms required per (material, thickness), compares them with free stock, and reports `sufficient | insufficient | unavailable` per line. It also carries the procurement facts Day 11 needs, without estimating lead time.

Baseline before editing: `.venv/bin/python -m pytest -q` → 746 passed in 84.62s.

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/services/inventory.py` | New: `PartRequirement`, `MaterialRequirement`, `InventoryCheck`, `scrap_fraction`, `blank_area_m2`, `part_requirement`, `evaluate_inventory` (pure), `check_inventory`. |
| `tests/test_inventory_service.py` | New: 40 database-free unit and boundary tests. |
| `tests/test_day09_inventory.py` | New: 8 integration and parity tests on the guarded test database. |
| `tests/test_day07_materials.py` | Test helper generalized: `reference_module(name)` loads any reference module with bytecode writing disabled (`reference_business_rules()` delegates to it). |
| `docs/development/technical_debt.md` | Added TD-002 (procurement-delay default), scheduled for Day 11. |

No repository, ORM, migration, dataset, contract or dependency changes. `seeding/validation.py` is unchanged.

## Design

**Flow.** `check_inventory(session, parts, *, manufacturing_rules, pricing_rules)`:
1. Validates the pricing rule and the parts before any SQL.
2. Runs Day 8 `check_manufacturing_capability` per part. Day 8 runs Day 7, which supplies the material spec, including density.
3. If every part is `valid`, reads free stock once per distinct (material, thickness) with Day 5 `get_material_availability`.
4. Calls the pure `evaluate_inventory`.

**Formulas** (reference `pricing_engine.blank_area_m2` and `capacity_engine.check_inventory`):
- Blank area (mm² ÷ 1,000,000 = m²): flat panel and mounting plate `L×W`; L-bracket `(L+H)×W`; U-bracket `(W+2H)×L`; enclosure `L×W + 2H(L+W)`; fabricated assembly `2×L×W`.
- Net kg per unit = area (m²) × thickness (mm) ÷ 1000 × density (kg/m³).
- Allowance per unit = net × (1 + `scrap_fraction`). Required kg = allowance × quantity, summed per (material, thickness) across the RFQ's parts.

**Decisions.**
- **Scrap from the versioned rule.** The reference hard-codes `1.12`; here it is `price-v1.0`'s `scrap_fraction` (`"0.12"`), parsed exactly and bounded to [0, 1). The result records the pricing rule version and fingerprint. Manufacturing provenance stays on each attached `ManufacturingCheck`.
- **Stock accounting is not re-invented.** Free stock is the database-computed `available_kg = quantity_kg − reserved_kg`, summed over every matching row by the existing Day 5 read. This is the reference's rule.
- **Statuses.** `sufficient` when available ≥ required (an exact match passes); `insufficient` when 0 < available < required; `unavailable` when free stock is 0, including no stock row at all. `shortage_kg = max(required − available, 0)`. The RFQ is `sufficient` only if every line is.
- **Gate on manufacturability.** Any `needs_clarification` part → `needs_clarification` (takes precedence); any `unsupported` part → `not_manufacturable`. In both cases no lines are produced, as in the ground truth. Zero, negative or over-limit quantities are caught here through Day 8 (`OUT_OF_RANGE_QUANTITY`).
- **Exact arithmetic.** Calculations run in a Decimal context with 60-digit precision that traps `Inexact`, `InvalidOperation` and `DivisionByZero`. Rounding raises instead of happening silently. No display rounding is applied; values keep the database column scales and are compared by value.
- **Day 11 facts, not decisions.** Each line carries `shortage_kg`, `availability_class` (catalog stocked/procure), `inventory_ids`, `snapshot_ids` (data provenance) and `supplier_lead_business_days`.
- **Read only.** No reservation, deduction, procurement record or capacity call. Tests prove only SELECTs are issued and inventory rows are byte-identical afterwards.
- `blank_area_m2` and `part_requirement` are public so Day 12 cost modelling can reuse them.

## Problems found during implementation

- My own unit test expected 250 kg to cover a 100-unit aggregate that needs 263.6984 kg; the service was correct. I fixed the test and added the assertion that 250 kg is short.
- Loading `capacity_engine` through the Day 7 helper would have imported it after the bytecode guard was restored, writing `.pyc` into the dataset. The helper now loads any module under the guard. Zero `.pyc` or `__pycache__` under the dataset after the full run.

## Commands and actual results

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_inventory_service.py` | 40 passed in 0.10s (after the test-arithmetic fix; first run 1 failed, 39 passed) |
| `.venv/bin/python -m pytest -q tests/test_day09_inventory.py` | 8 passed in 4.97s |
| `.venv/bin/python -m pytest -q` | **794 passed in 79.85s** (746 baseline + 48 new) |
| `.venv/bin/ruff check .` | All checks passed! |
| `.venv/bin/ruff format --check src tests alembic` | 53 files already formatted |
| `uv lock --check` | Resolved 23 packages |
| `git diff --check` | clean |
| dataset bytecode/cache scan | 0 files |
| `pytest tests/test_database_seeding.py::test_source_inventory_and_checksums` | 1 passed (681 files, 637 protected hashes) |

Parity coverage:
- **Ground truth inventory:** all 100 dev/validation RFQs with `expected_inventory` (48 short) give identical lines: material, thickness, required kg, available kg, sufficient. The carried facts reproduce every labelled `procurement_business_days`. The 20 incomplete RFQs give `needs_clarification` and the 20 unsupported RFQs give `not_manufacturable`, with no lines.
- **Ground truth pricing masses:** all 120 priced lines match `blank_area_m2`, `net_material_kg_per_unit` and `material_allowance_kg_per_unit` exactly.
- **Reference `check_inventory`:** 6 part types × 15 materials × quantities 1/37/1000, plus 5 multi-part RFQs sharing a key = 275 RFQs, identical. All three line statuses are exercised.
- **Database cases:** exact boundary (30.24 kg free → sufficient, 30.239999 → insufficient), a second stock row summed, a zero-stock `procure` material reported `unavailable` with supplier facts, and swapped rule kinds rejected.
- **Read only:** 9 SELECTs for one part (7 Day 8 + 2 stock), 16 for two parts sharing a key; repeat calls give equal results; inventory rows unchanged.

Read-only demo against development (`day9_demo.py` in the session scratchpad):

```
RFQ-0001: sufficient  (scrap 0.12 from price-v1.0)
  part PART-000010: area 0.3038 m2, net 2.3544500000000 kg, +scrap 2.636984000000000 kg x 85 = 224.143640000000000 kg
  MAT-009 1.0 mm [stocked]: need 224.143640000000000 kg, free 298.000000 kg -> sufficient, shortage 0 kg, rows ('INV-0049',), supplier lead days (7,)
RFQ-0002: insufficient  (scrap 0.12 from price-v1.0)
  part PART-000020: area 0.303 m2, net 9.393000000000 kg, +scrap 10.52016000000000 kg x 37 = 389.24592000000000 kg
  MAT-009 4.0 mm [stocked]: need 389.24592000000000 kg, free 100.000000 kg -> insufficient, shortage 289.24592000000000 kg, rows ('INV-0053',), supplier lead days (7,)
RFQ-0041: sufficient  (scrap 0.12 from price-v1.0)
  part PART-000410: area 0.0969 m2, net 0.2645370000000 kg, +scrap 0.296281440000000 kg x 9 = 2.666532960000000 kg
  part PART-000411: area 0.0929 m2, net 0.2508300000000 kg, +scrap 0.280929600000000 kg x 71 = 19.946001600000000 kg
  MAT-002 1.0 mm [stocked]: need 19.946001600000000 kg, free 81.000000 kg -> sufficient, shortage 0 kg, rows ('INV-0007',), supplier lead days (11,)
  MAT-003 1.0 mm [stocked]: need 2.666532960000000 kg, free 112.000000 kg -> sufficient, shortage 0 kg, rows ('INV-0013',), supplier lead days (15,)
quantity 0: not_manufacturable
```

## Limitations

- No lead time or procurement delay (Day 11; see TD-002). `expected_replenishment_date` is descriptive metadata and not used.
- Stock is a point-in-time snapshot (`snapshot_ids` recorded); concurrent reservations are not considered because nothing reserves on quotation.
- Within one session, rows updated earlier in the same session can be served stale from the identity map, because the Day 5 read does not refresh loaded objects. Services never write, so this only affects tests that modify rows (they call `expunge_all()`).
- Query count grows linearly with parts (Day 8 runs per part). This is acceptable for RFQ sizes; batching is a later optimization.
- Values carry database scales (for example `224.143640000000000`); display rounding belongs to presentation and pricing.

## Git

Nothing staged, committed, pushed or stashed.
