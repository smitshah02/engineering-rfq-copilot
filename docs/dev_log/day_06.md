# Development Log: Day 06 — Business Entities and Repository Pattern

**Date:** 2026-11-09. **Phase 2, Day 6.** Append-only.

## Objective

Make the repository layer the single typed, scoped path from PostgreSQL to the business entities that Days 7–15 consume: customers, RFQs, parts, quotations and pinned rule versions. The seven Day 5 reads already cover customers, RFQs, inventory, capabilities, capacity, quotation history and approvals; they are reused unchanged. Two gaps were found and closed:

1. **No part entity.** `rfq_line_items` is empty by design; scenario parts exist as raw text and as ground-truth extractions. Services need one part type buildable from a stored line or from the `calculate_quote` Part contract mapping.
2. **No rule-version read.** All nine rule documents were seeded but nothing in `src/` read them.

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/entities.py` | New. Frozen `Part`; `from_mapping` (contract shape, strict types, reuses `seeding.transform.decimal_value`/`integer`) and `from_line` (stored row). |
| `src/rfq_copilot/repositories/queries.py` | Added `get_rfq_parts`, `get_rule_version`, `RuleIntegrityError`. Existing reads untouched. |
| `src/rfq_copilot/repositories/results.py` | Added frozen `RuleVersionDetails`. |
| `src/rfq_copilot/repositories/__init__.py` | Exports. |
| `tests/test_entities.py` | New, 30 unit tests (no database). |
| `tests/test_day06_repositories.py` | New, 17 integration tests on the guarded test database, reusing Day 5 fixtures. |
| `docs/development/database_queries.md` | Appended Day 6 section. |
| `CLAUDE.md` | New project instructions (user added it to `.gitignore`). |

Unchanged: ORM models, migration `20261108_01`, `seeding/validation.py`, all Day 5 tests, dataset v1.1.0. No dependencies added.

## Design decisions

- **Part validates types, not business ranges.** An out-of-range thickness or an unknown part type is a manufacturing finding (Day 8), so it must reach the validator intact. Missing fields stay `None` (10 of 180 ground-truth parts are deliberately incomplete). Floats, bools, NaN/Infinity, more than 3 decimal places, string quantities and unknown keys are rejected.
- **Rule numbers are exact.** The engine-wide JSONB decoder returns floats (`min_tolerance_mm: 0.2`). `get_rule_version` selects `document::text` and parses with `parse_float=Decimal`.
- **Honest integrity checks.** The stored `sha256` hashes the source file's bytes and cannot be recomputed from JSONB. Verified instead: kind, sha format, optional caller pin, version identity (the declared `version` for the four versioned rules; the hash embedded in `<kind>@sha256:<digest>` names for the five contracts). `fingerprint` (sha256 of canonical jsonb text) is recomputable for later audit evidence. Tests prove stored sha256 equals each dataset file's hash.
- **Deeply immutable documents** (`MappingProxyType`/tuples) so a service cannot mutate a shared rule set.
- `get_rfq_parts` delegates to `get_rfq`: no second RFQ query and no second scoping rule.

## Problems found during implementation

- First version checked `document["version"] == rule_version` whenever present. `agent_coordination_policy.json` carries `"version": "1.1"` (contract version) under a hash-derived rule name, so this failed. Replaced with the seeding naming contract above, a stronger check, plus a tamper test.
- A test multi-row `insert()` with differing keys silently dropped the second row's columns (SQLAlchemy executemany uses one key set). Test fixed with explicit NULL keys; not a code defect.

## Commands and actual results

```bash
.venv/bin/python -m pytest -q tests/test_entities.py tests/test_day06_repositories.py   # 47 passed in 6.93s
.venv/bin/python -m pytest -q                                                            # 642 passed in 70.75s
.venv/bin/ruff check .                                                                   # All checks passed
.venv/bin/ruff format --check src tests alembic                                         # 43 files already formatted
uv lock --check                                                                          # ok
git diff --check                                                                         # ok
```

Baseline was 595 tests; 47 added, 0 failed, 0 skipped. Live read-only demonstration on development (server read-only session) returned `mfg-v1.0` with Decimal limits `(0.8, 6.0)` and `min_tolerance_mm = 0.2`, rejected a wrong sha pin, returned `()` parts for historical `HRFQ-0001`, and converted the RFQ-0001 development extraction to a `Part`.

## Limitations

- Pricing rule values are JSON strings (`"0.24"`); converting them to Decimal is Day 12's job.
- `data_snapshots` pinning for inventory/capacity is deferred to Days 9–10.
- Reference `calculation_id` hashes part dicts as passed; Day 13 must define a canonical Part serialisation for parity.
- Frozen evaluation data was not opened.

## Git

No staging, commit or push. Working tree after Day 6: modified `.gitignore` (user change), `repositories/__init__.py`, `queries.py`, `results.py`, `docs/development/database_queries.md`; untracked `entities.py`, `tests/test_entities.py`, `tests/test_day06_repositories.py`, `docs/dev_log/day_06.md`.
