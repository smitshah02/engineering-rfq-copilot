# Development Log: Day 08 — Manufacturing Capability Validation

Date: 2026-11-09

## Objective

The deterministic `check_manufacturing_capability` decision for the Manufacturing Feasibility agent: `valid | unsupported | needs_clarification` with reference finding codes, plus the part's operation route for Days 10–13. Fix MFG-001 (configured `hole_count_max = 300` never enforced by the reference) with boundary regression tests. Reuse the Day 7 material service; leave `seeding/validation.py` unchanged.

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/services/manufacturing.py` | New: `ManufacturingCheck`, `route_operations`, `evaluate_manufacturing` (pure), `check_manufacturing_capability`. |
| `src/rfq_copilot/services/materials.py` | `_require_manufacturing` renamed to public `require_manufacturing_rules` (shared with Day 8; behaviour unchanged). |
| `src/rfq_copilot/repositories/queries.py` | New `list_part_routing`. |
| `src/rfq_copilot/repositories/__init__.py` | Export `list_part_routing`. |
| `tests/test_manufacturing_service.py` | New: 43 database-free unit, boundary and MFG-001 tests. |
| `tests/test_day08_manufacturing.py` | New: 5 integration and parity tests on the guarded test database. |
| `docs/development/database_queries.md` | Appended the `list_part_routing` section. |

ORM models, migration `20261108_01`, `seeding/validation.py`, Day 1–7 tests and dataset v1.1.0 are unchanged. No dependencies added.

## Design decisions

- **Reuse Day 7.** `check_manufacturing_capability` calls `check_part_material`; material, thickness and finish findings, the material spec and its `supported_operations` all come from there. `evaluate_manufacturing` refuses a `MaterialCheck` produced under a different rule version or fingerprint.
- **Not reusing `seeding/validation.py`.** It is a seed-time guard that raises on the first violation and reads seed-plan rows; the runtime validator must return every finding. It stays unchanged, per project rules.
- **MFG-001 fix.** `hole_count` (rule default 0 when unstated) must satisfy `0 <= holes <= limits.hole_count_max`, else `OUT_OF_RANGE_HOLE_COUNT` (the reference's `OUT_OF_RANGE_<FIELD>` convention). Non-integer, string, bool and Decimal hole counts are rejected earlier by `Part.from_mapping`.
- **Fail closed on unknown limits.** The root cause of MFG-001 was a generic loop silently skipping a scalar limit. Every key in `limits` must be a two-value range on a `Part` field or one of the four scalar limits this module enforces (`min_tolerance_mm`, `hole_count_max`, `bends_max`, `min_bend_radius_factor`); anything else raises `ValueError`.
- **Rule-driven everything.** Mandatory fields, conditional fields (`height_required`, `weld_length_required`), allowed part types, ranges, bend counts per part type, bend factor and `unsupported_operations` all come from the pinned `mfg-v1.0` document. Parts with bends are those whose `bend_counts` is above 0 (the reference hard-codes the same three types).
- **Missing fields win.** Any `MISSING_*`, from Day 7 or Day 8, returns `needs_clarification` with only the missing findings, as the reference does. The Day 7 `MaterialCheck` is still attached for the caller. This resolves the Day 7 limitation.
- **Bend radius.** `bend_radius_mm >= thickness_mm × max(rule factor, material factor)`. A material-specific factor can only tighten the global rule; all seeded materials use 1.0, so parity is unaffected.
- **Operation route.** Built from stored `operation_routing` (`always`, `finish_not_none`; any other condition raises). `UNSUPPORTED_OPERATION` when a routed operation is not supported by the material or is listed in the rule's `unsupported_operations`. A routable part type with no stored routing raises (catalog defect, not a customer finding).
- **Out of scope by design.** `bends_max` is checked against the rule's per-type bend count (never exceeded in v1.1.0). Customer-requested operations (`requested_operations`) are not a `Part` field or a contract field and are not modelled; `Operation.is_quoted` belongs to pricing (Day 13).

## Commands and actual results

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_manufacturing_service.py` | 43 passed in 0.10s |
| `.venv/bin/python -m pytest -q tests/test_day08_manufacturing.py` (first run) | 1 failed, 4 passed: my guessed minimum `compared > 250` was wrong; all 243 comparisons had matched. Changed to the exact count `6 × 39 + 3 × 3`. |
| `.venv/bin/python -m pytest -q` | **746 passed in 82.50s** (698 baseline + 48 new) |
| `.venv/bin/ruff check .` | All checks passed! |
| `.venv/bin/ruff format --check src tests alembic` | 50 files already formatted |
| `uv lock --check` | Resolved 23 packages |
| `git diff --check` | clean |
| `find data/rfq_synthetic_data -name "*.pyc"` | 0 (dataset untouched) |

Parity coverage:
- All 180 dev/validation parts: `(result, findings)` exactly equal ground truth `manufacturing_validation` (140 valid, 20 unsupported, 20 needs_clarification).
- All 120 priced ground-truth lines: routed operations exactly equal `expected_pricing.lines[].operations`.
- Stored routing for all six part types equals `operation_routing.csv`.
- Reference `validate_part`: 243 single-field changes over one valid part per type (dimension, thickness, tolerance and quantity boundaries, hole counts 0 and 300, part type, material, finish, bend radius, missing fields): identical result and findings.
- MFG-001: 300 holes valid, 301 `OUT_OF_RANGE_HOLE_COUNT`; the frozen reference still returns `valid` for 301 (asserted).
- Statement counts: valid part 7 SELECTs (6 material + 1 routing), unsupported part type 6, unknown material 3.

Read-only demo against development (`day8_demo.py` in the session scratchpad), RFQ-0001's part with single changes:

```
      RFQ-0001 as sent: valid                () ('laser', 'bend', 'inspect', 'pack')
            passivated: valid                () ('laser', 'bend', 'finish', 'inspect', 'pack')
             300 holes: valid                () ('laser', 'bend', 'inspect', 'pack')
   301 holes (MFG-001): unsupported          ('OUT_OF_RANGE_HOLE_COUNT',) ('laser', 'bend', 'inspect', 'pack')
           length 1201: unsupported          ('OUT_OF_RANGE_LENGTH_MM',) ('laser', 'bend', 'inspect', 'pack')
            radius 0.9: unsupported          ('BEND_RADIUS_TOO_SMALL',) ('laser', 'bend', 'inspect', 'pack')
         tolerance 0.1: unsupported          ('TOLERANCE_UNSUPPORTED',) ('laser', 'bend', 'inspect', 'pack')
 no tolerance + chrome: needs_clarification  ('MISSING_TOLERANCE_MM',) ()
               casting: unsupported          ('UNSUPPORTED_PART_TYPE',) ()
```

## Limitations

- Deliberate divergences from the reference: hole counts above 300 or below 0 (MFG-001), and unpadded thickness such as `"1"` (Day 7). Both are pinned by tests.
- MFG-001 remains open in the frozen dataset's reference code; it is fixed in the application validator only. Older documents (`docs/database/database_architecture.md`, `docs/data/data_integrity_report.md`) still describe it as open and were not edited.
- Single-part decisions only; aggregating lines into an RFQ outcome is Day 14/15.
- Simplified envelope checks, not CAD or certified engineering analysis (as the rule's `interpretation` states).

## Git

Nothing staged, committed, pushed or stashed. `git status --short` after Day 8:

```
 M .gitignore
 M docs/development/database_queries.md
 M src/rfq_copilot/repositories/__init__.py
 M src/rfq_copilot/repositories/queries.py
 M src/rfq_copilot/repositories/results.py
?? docs/dev_log/day_06.md
?? docs/dev_log/day_07.md
?? docs/dev_log/day_08.md
?? src/rfq_copilot/entities.py
?? src/rfq_copilot/services/
?? tests/test_day06_repositories.py
?? tests/test_day07_materials.py
?? tests/test_day08_manufacturing.py
?? tests/test_entities.py
?? tests/test_manufacturing_service.py
?? tests/test_material_service.py
```
