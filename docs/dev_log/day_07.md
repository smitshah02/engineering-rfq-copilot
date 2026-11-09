# Development Log: Day 07 — Material Catalog Service

Date: 2026-11-09

## Objective

A deterministic, reusable answer to "is this material, at this thickness, with this finish, in our catalog?" for the Manufacturing Feasibility agent (and alternative suggestions for Intake), plus catalog lookup by ID, grade and specification. Day 8's manufacturing validator will reuse this service instead of re-implementing material checks.

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/services/__init__.py` | New package for deterministic business services. |
| `src/rfq_copilot/services/materials.py` | New: `MaterialSpec`, `MaterialCheck`, `get_material_spec`, `evaluate_material` (pure), `check_part_material`. |
| `src/rfq_copilot/repositories/queries.py` | New `find_materials`; thickness argument validation extracted to `_thickness` and reused by `get_material_availability` (behaviour unchanged). |
| `src/rfq_copilot/repositories/__init__.py` | Export `find_materials`. |
| `tests/test_material_service.py` | New: 36 database-free unit and boundary tests. |
| `tests/test_day07_materials.py` | New: 20 integration and parity tests on the guarded test database. |
| `docs/development/database_queries.md` | Appended the `find_materials` section. |

ORM models, migration `20261108_01`, `seeding/validation.py`, Day 1–6 tests and dataset v1.1.0 are unchanged. No dependencies added.

## Design decisions

- **Repository vs service split.** `find_materials` is a query (repositories). Thickness and finish decisions live in the service. `get_material_spec` reuses Day 5's `get_manufacturing_capabilities` rather than a second material-loading query; cost: two extra small SELECTs (workcentres, routing), marked with a `ponytail:` comment.
- **Pure core, thin async shell.** `evaluate_material(part, rules, spec, *, finish_exists)` does no I/O, so every rule branch is unit tested without a database and Day 8 can call it with facts it already holds. `check_part_material` only gathers those facts.
- **Versioned rules.** The caller passes a `RuleVersionDetails` from Day 6's `get_rule_version` (pinned sha256 in tests). Which material fields are mandatory comes from the rule's `mandatory_fields`; non-manufacturing rules are rejected. Every result carries `rule_version` and `rule_fingerprint` for audit.
- **Reference finding codes.** `UNSUPPORTED_MATERIAL`, `UNSUPPORTED_FINISH`, `MATERIAL_THICKNESS_UNSUPPORTED`, `MATERIAL_FINISH_INCOMPATIBLE`, `MISSING_MATERIAL_ID|THICKNESS_MM|FINISH_ID`; results `valid | unsupported | needs_clarification`. Missing fields short-circuit, as in the reference.
- **Availability is a catalog attribute.** `MaterialSpec.availability_class` (`stocked`/`procure`) states normal sourcing only; free stock remains `get_material_availability` (Day 9).
- **Cost excluded from specs.** `cost_usd_per_kg` and `pricing_rule_version` are pricing data (Day 12) and not part of the `get_material_specs` contract output.
- **No customer scoping.** The catalog is shared master data. The tool contract's role check (sales_representative, estimator, manufacturing_engineer) is a tool-server duty in Phase 3.
- **Decimal thickness equality (deliberate divergence).** `1`, `1.0` and `1.000` mm are the same thickness. The reference compares `str(thickness)` with catalog strings and flags `"1"`; this is pinned by `test_known_divergence_unpadded_thickness`. No dev/validation ground-truth part is affected (all use padded strings).

## Problems found during implementation

- **Test wrote into the frozen dataset.** Loading the reference engine created `data/rfq_synthetic_data/src/__pycache__/` (three `.pyc` files), caught by the existing `test_source_inventory_and_checksums` (684 files vs 681). No dataset file was modified. I deleted exactly those three generated files and the empty directory, and the loader now sets `sys.dont_write_bytecode` while importing. Re-run: inventory 681, protected hashes 637, zero `.pyc` under the dataset.

## Commands and actual results

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_material_service.py` | 36 passed in 0.07s |
| `.venv/bin/python -m pytest -q tests/test_day07_materials.py` | 20 passed in 9.67s |
| `.venv/bin/python -m pytest -q` (first full run) | 1 failed, 697 passed: dataset inventory 684 != 681 (bytecode, above) |
| `.venv/bin/python -m pytest -q` (after fix) | **698 passed in 73.31s** (642 baseline + 56 new) |
| `.venv/bin/ruff check .` | All checks passed! |
| `.venv/bin/ruff format --check src tests alembic` | 47 files already formatted |
| `uv lock --check` | Resolved 23 packages |
| `git diff --check` | clean |

Parity coverage:
- All 15 specs equal `master_data/materials.csv` (grade, category, availability, density, bend factor, thickness, finishes, operations).
- `find_materials` thickness × finish grid (8 × 9 = 72 combinations, including unsupported 2.5 mm and unknown `chrome`) equals the CSV.
- All 180 dev/validation parts: material findings equal ground truth `manufacturing_validation` restricted to material codes (10 `MISSING_MATERIAL_ID` cases).
- Reference `validate_part` over 16 materials × 8 thicknesses × 8 finishes = 1,024 combinations: identical material findings and result.
- Statement counts: valid part 6 SELECTs, incompatible finish 7, unknown material 2, `find_materials` 1.

Read-only demo against development (`day7_demo.py` in the session scratchpad):

```
spec: MAT-009 stainless_steel 430 stocked
  thickness: (Decimal('1.000'), Decimal('1.500'), Decimal('2.000'), Decimal('3.000'), Decimal('4.000'), Decimal('6.000'))
  finishes: ('brushed', 'none', 'passivated')
grade '304': ('MAT-007',)
0.8 mm + brushed: ('MAT-012', 'MAT-013', 'MAT-014', 'MAT-015')
stainless + anodized: ()
            ok: valid                () [mfg-v1.0 48304b0cc773]
 1 mm unpadded: valid                () [mfg-v1.0 48304b0cc773]
        2.5 mm: unsupported          ('MATERIAL_THICKNESS_UNSUPPORTED',) [mfg-v1.0 48304b0cc773]
      anodized: unsupported          ('MATERIAL_FINISH_INCOMPATIBLE',) [mfg-v1.0 48304b0cc773]
        chrome: unsupported          ('MATERIAL_FINISH_INCOMPATIBLE', 'UNSUPPORTED_FINISH') [mfg-v1.0 48304b0cc773]
       MAT-999: unsupported          ('UNSUPPORTED_MATERIAL',) [mfg-v1.0 48304b0cc773]
   no material: needs_clarification  ('MISSING_MATERIAL_ID',) [mfg-v1.0 48304b0cc773]
```

## Limitations

- Only material-dependent findings. Envelope limits, tolerance, bend radius, hole count (MFG-001), part type and operations are Day 8.
- A part missing a non-material mandatory field (e.g. tolerance) still gets material findings here; the reference stops early. Day 8 must merge outcomes so `needs_clarification` wins.
- Grade lookup is exact (case/whitespace-insensitive); no fuzzy matching of free-text grades such as "SS304". That belongs to Intake extraction.
- `get_material_spec` issues 6 SELECTs because it reuses the Day 5 capability read.

## Git

Nothing staged, committed, pushed or stashed. `git status --short` after Day 7:

```
 M .gitignore
 M docs/development/database_queries.md
 M src/rfq_copilot/repositories/__init__.py
 M src/rfq_copilot/repositories/queries.py
 M src/rfq_copilot/repositories/results.py
?? docs/dev_log/day_06.md
?? docs/dev_log/day_07.md
?? src/rfq_copilot/entities.py
?? src/rfq_copilot/services/
?? tests/test_day06_repositories.py
?? tests/test_day07_materials.py
?? tests/test_entities.py
?? tests/test_material_service.py
```
