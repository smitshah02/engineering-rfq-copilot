# Development Log: Day 13 — Deterministic Pricing Engine

Date: 2026-11-09

## Objective

The deterministic `calculate_quote` decision for the Pricing agent: selling line totals, advisory unit prices, the quote total and a stable `calculation_id`, priced from Day 12 manufacturing cost under the pinned `price-v1.0` rule, with reference-engine parity. TD-001 resolved.

Baseline before editing: 891 passed (Day 12 final).

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/services/pricing.py` | New: `PricedLine`, `PriceCalculation`, `canonical_part`, `calculation_id`, `gross_margin`, `evaluate_pricing` (pure), `display_quote`, `calculate_quote`. |
| `src/rfq_copilot/repositories/queries.py`, `__init__.py` | New `list_operations` (one SELECT), exported. |
| `src/rfq_copilot/services/costing.py` | Bug fix: `display_costs` now quantizes inside the fixed `PLANNING` context (see Problems). |
| `tests/test_pricing_service.py` | New: 26 database-free unit and boundary tests. |
| `tests/test_costing_service.py` | Context test now also covers `display_costs` (regression for the fix). |
| `tests/test_day13_pricing.py` | New: 7 integration and parity tests on the guarded test database. |
| `docs/development/database_queries.md` | Appended `list_operations`. |
| `docs/development/technical_debt.md` | TD-001 resolved. No open technical debt remains. |

No ORM, migration, dataset, contract or dependency changes. No quote persistence and no approval.

## Design

**Flow.** `calculate_quote(session, parts, costs, *, pricing_rules)` reads `is_quoted` for the routed operations (one SELECT), then calls the pure `evaluate_pricing`. That function validates the rule kind, that the cost estimate was made under the same pricing fingerprint, and that the parts and cost lines correspond in order. Uncosted requests (needing clarification, or not manufacturable) pass through with no ID, lines or total.

**Pricing** (reference `calculate_part` and `calculate_quote`; dataset `business_rules.md` §3):
- line total = `quantize(manufacturing cost / (1 − gross_margin), line_total_scale)`: a **gross margin**, not a markup ($100 → $131.58, not $124.00)
- unit price = `quantize(line total / quantity, unit_display_scale)`, advisory only; the line total is authoritative
- quote total = Σ **already rounded** line totals
- `gross_margin` (bounded to [0, 1)), scales, rounding mode (allow-list), `currency` and the `tax_included`, `freight_included` and `shipping_included` booleans all come from the pinned rule; malformed values raise
- division happens in the fixed `PLANNING` context (28 digits, the reference's default), then quantizing uses the rule's mode (`ROUND_HALF_UP`)

**`calculation_id` decision.** The reference hashes the part dictionaries exactly as passed, so the same part spelled `"1.0"` or `"1.00"`, or read back from a `Numeric(12,3)` column as `1.000`, gets a different reference ID. That is a weakness for an identifier meant to be stable. Here the hash covers a **canonical, value-based spelling**:
- integral dimensions become JSON integers
- `thickness_mm` and `tolerance_mm` keep at least 1 decimal place and `bend_radius_mm` at least 2 (the contract examples' convention); any other value uses its normalized digits
- unstated fields are omitted, keys are sorted, compact separators, then `sha256(...)[:20]`, upper-case, prefixed `CALC-`
Each value has exactly one spelling (any number of trailing zeros maps to the same string), and the canonical form equals the dataset's own extraction format, so **all 100 labelled IDs are reproduced exactly**.

**TD-001 decision.** `requested_operations` is not added in Phase 2 (not in the contract, absent from all data, needs an approved entity and contract change). Pricing refuses any routed operation the catalog marks `is_quoted = false` (`punch`, `machine`); this raises because it is a catalog defect.

## Problems found during implementation

- **Real bug, found by a test:** the quote total was summed outside the fixed context. Under a caller context of precision 4 it became `2381` instead of `2380.74`. Fixed by summing inside `PLANNING`. The same weakness existed in Day 12 `display_costs`: `quantize` obeys the active context's precision and would raise `InvalidOperation` under a low one. It now quantizes inside `PLANNING`, and the Day 12 context test covers it.
- **Test mistakes:** several tests built costs for the default part `P1` and priced `PART-000010`; the service correctly refused the mismatch. One example used 131.58 ÷ 3, which is exactly 43.86, so it showed no rounding; changed to quantity 7.

## Commands and actual results

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_pricing_service.py` (first run) | 6 failed, 20 passed (bug and test issues above) |
| `.venv/bin/python -m pytest -q tests/test_pricing_service.py tests/test_costing_service.py` (after fixes) | 44 passed in 0.11s |
| `.venv/bin/python -m pytest -q tests/test_day13_pricing.py` | 7 passed in 4.25s |
| `.venv/bin/python -m pytest -q` | **924 passed in 95.73s** (891 baseline + 33 new) |
| `.venv/bin/ruff check .` | All checks passed! |
| `.venv/bin/ruff format --check src tests alembic` | 65 files already formatted |
| `uv lock --check` | Resolved 23 packages |
| `git diff --check` | clean |
| dataset bytecode/cache scan | 0 files |
| `pytest tests/test_database_seeding.py::test_source_inventory_and_checksums` | 1 passed (681 files, 637 protected hashes) |

Parity coverage:
- **Ground truth:** all 100 dev/validation `expected_pricing` objects (120 lines) match field for field: `calculation_id`, currency, rule version, the inclusion flags, quote total, and every line's money strings, operations and quantity. The three mass fields are compared by value, because the reference's strings keep the scale of its own arithmetic.
- **Reference `calculate_quote`:** 180 single-part quotes (6 types × 15 materials × quantities 1 and 77) and 3 six-part quotes, identical including `calculation_id`.
- **Spelling:** the reference gives two IDs for one part spelled two ways; this engine gives one, equal to the reference's canonical one. A part read back from database lines (`450.000`, `1.000`, `1.500`) keeps the same ID as the original request.
- **Independent arithmetic:** `business_rules.md`'s worked example (10 aluminium panels $414.37 + 2 powder-coated steel L-brackets $107.49 = **$521.86**) reproduced.
- **Database cases:** `list_operations` flags (`punch` and `machine` not quoted); marking `inspect` unquoted blocks pricing.
- **Read only:** pricing issues 1 SELECT; operation rows unchanged; repeat calls equal; the display is JSON-serialisable.

Read-only demo against development (`day13_demo.py` in the session scratchpad):

```
RFQ-0001: priced CALC-72E1F2A14C7EDD41FDF4 (labelled CALC-72E1F2A14C7EDD41FDF4)  margin 0.24, USD, tax/freight/shipping included: False/False/False
  PART-000010 x85: cost 1809.3610 / (1 - 0.24) -> line total 2380.74 (unit 28.0087, advisory)
  quote total 2380.74 USD (labelled 2380.74)
RFQ-0041: priced CALC-0972B1CBC7F92B938A06 (labelled CALC-0972B1CBC7F92B938A06)  margin 0.24, USD, tax/freight/shipping included: False/False/False
  PART-000410 x9: cost 125.6604 / (1 - 0.24) -> line total 165.34 (unit 18.3711, advisory)
  PART-000411 x71: cost 633.5690 / (1 - 0.24) -> line total 833.64 (unit 11.7414, advisory)
  quote total 998.98 USD (labelled 998.98)
RFQ-0007: priced CALC-9C9AA11F8BF9F3D6AF3A (labelled CALC-9C9AA11F8BF9F3D6AF3A)  margin 0.24, USD, tax/freight/shipping included: False/False/False
  PART-000070 x10: cost 896.3194 / (1 - 0.24) -> line total 1179.37 (unit 117.9370, advisory)
  quote total 1179.37 USD (labelled 1179.37)
same part, different spelling -> CALC-72E1F2A14C7EDD41FDF4 CALC-72E1F2A14C7EDD41FDF4
```

## Limitations

- Prices are non-binding estimates (`estimate_is_nonbinding`); nothing is persisted, approved or released. The $7,500 managerial-review threshold and other approval rules (`approval-v1.1`) are out of Phase 2 scope.
- Tax, freight, shipping, tooling and duties are excluded, as the rule's flags state.
- The mass display strings may carry different trailing zeros from the reference (their values are equal).
- The `calculation_id` is a fingerprint of the request and pricing version, not of catalog costs. A cost change under the same pricing version yields a different total with the same ID; pinning costs per calculation belongs to quote persistence.

## Git

Nothing staged, committed, pushed or stashed.
