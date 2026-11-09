# Technical Debt and Deferred Decisions

Known gaps in the deterministic business services, each with an owner day. Add an entry when a limitation is deliberately deferred. Mark it resolved, with a link to the dev log, rather than deleting it.

| ID | Item | Found | Scheduled | Status |
| --- | --- | --- | --- | --- |
| TD-001 | `requested_operations` not modelled | Day 8 | **Day 13** | Resolved (Day 13, [log](../dev_log/day_13.md)) |
| TD-002 | Procurement-delay default: dataset doc vs reference code | Day 9 | **Day 11** | Resolved (Day 11, [log](../dev_log/day_11.md)) |
| TD-003 | Reference-only constants not in a versioned rule (finish and pack minutes, 15-day procurement default) | Day 10 | **Day 12** | Resolved (Day 12, [log](../dev_log/day_12.md)) |

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
