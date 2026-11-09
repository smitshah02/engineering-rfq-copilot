# Day 3 Data Integrity and Deterministic Business Logic Report

**Project:** Engineering RFQ Multi-Agent Copilot  
**Dataset:** Frozen synthetic RFQ dataset v1.1.0  
**Assessment:** Local validation passed with one confirmed prospective manufacturing-validation defect (`MFG-001`). Update (Day 8): fixed in the application validator; the frozen reference engine keeps the original limitation; see [Day 8 resolution](#day-8-resolution-application-validator).\
**Scope:** Dataset files and standalone reference functions, not a live application deployment

## Executive result

**The existing 681-file dataset remained unchanged.** The original validator passed, 38 non-regenerating tests passed, the isolated reproducibility test passed, and all 637 SHA-256 manifest entries matched. A separate Day 3 boundary test exposed a genuine policy-enforcement gap: `validate_part` accepts 301 holes despite a configured maximum of 300. The sampled structured dataset records did not exhibit this prohibited value.

It would be incorrect to report a fully defect-free business logic implementation; it would be equally incorrect to report dataset corruption based on this one unused policy condition.

## Validation record

| Check | Recorded result | Source/evidence |
|---|---|---|
| Official validator | Exit code 0 | `python -B -m src.validate_data` run with dataset as working directory |
| Referential integrity | Pass | Official validator output |
| Capacity equations | Pass | Official validator output |
| Inventory equations | Pass | Official validator output |
| Historical quote recalculation | Pass | Official validator output |
| Approval chronology | Pass | Official validator output |
| Permission/state transition checks | Pass | Official validator output |
| Scenario price/inventory/schedule consistency | Pass | Official validator output |
| Scenario label recalculations | 240 | Official validator output |
| Label leakage checks | Pass | Official validator output |
| Versioned human approval checks | Pass | Official validator output |
| Non-regenerating Pytests | 38 passed in 0.53 s | `--ignore=tests/test_reproducibility.py` |
| Isolated reproducibility Pytest | 1 passed in 0.88 s | Test executed against temporary copy |
| SHA-256 expected / actual | 637 / 637 | Independent hash script |
| Missing / extra / mismatched hashes | 0 / 0 / 0 | Independent hash script |
| Dataset files before / after validator | 681 / 681 | SHA-256 snapshot wrapper |
| Dataset files modified by validator or tests | 0 | Snapshot wrappers |
| Final Git status after tests | Clean | `git status --short --untracked-files=all` |

### Coverage returned by official validator

- 240 RFQ scenarios: 120 development, 40 validation, 80 frozen evaluation.
- 26 scenario families, 124 referenced attachments (79 PDFs).
- 36 fault instances, 18 linked to RFQs.
- 60 security fixtures: 16 allowed and 44 denied.
- 240 agent coordination fixtures and 12 exception cases.
- 240 distinct submission timestamps and 49 requested delivery dates.
- Nine configured workflow states.

## Independent business-rule verification

| Domain | Checks performed | Recorded result |
|---|---|---|
| Manufacturing boundary | Normal part; maximum/excess length; minimum/excess precision; missing material | Six expected outcomes passed |
| Inventory arithmetic | All 90 material inventory records: nonnegative stock and `available = quantity - reserved` | Zero issues |
| Capacity arithmetic | All 80 capacity records: nominal/downtime/available/scheduled/remaining; utilization rounding | Zero issues |
| Independent pricing | Actual `MAT-001` material row, 200 x 100 mm panel, 1.0 mm thick, quantity 10 | Independent $83.45; engine $83.45; $0.00 difference |
| Scheduling | Available workcentre slots, lead-day calculation and a sample flat panel | Feasible; ready 2026-11-13; no overbooking found |
| Release safeguards | Authorized manager; stale approval; on-hold customer; no clearance; self-approval; AI agent | Six expected outcomes passed |

The existing pricing tests additionally verify fixed reference prices of $414.37 (panel), $107.49 (bracket), and $521.86 (combined quote). Prices use `Decimal`, `ROUND_HALF_UP`, one-time setup cost per line, and a 24% **gross margin**, rather than 24% markup.

### Example observed record equations

- `INV-0002`: 63.00 kg quantity - 6.30 kg reserved = 56.70 kg available.
- `CAP-0001`: 80 nominal hours - 6 downtime hours = 74 available hours; 74 - 57 scheduled = 17 remaining; 57/74 rounds to 0.7703 utilization.

## Finding MFG-001: Configured hole-count maximum not enforced

**Status:** Fixed in the application manufacturing validator (Day 8). Still present, by design, in the frozen v1.1.0 reference engine, which must remain unchanged.\
**Category:** Manufacturing capability validation / policy coverage.  
**Location:** `src/business_rules.py` function `validate_part`; `config/manufacturing_rules.json` key `limits.hole_count_max`.  
**Configured maximum:** 300 holes.  
**Reproduction:** Use an otherwise valid flat panel and set `hole_count = 301`.

Observed:

```python
{'result': 'valid', 'findings': []}
```

**Root cause (from inspected implementation):** The generic loop in `validate_part` only checks `limits` values that are two-element lists. `hole_count_max` is a scalar value and has no separate validation branch, so it is skipped. This is a concrete failure to enforce the configured policy for otherwise valid input.

**Impact assessment:** An agent calling a future manufacturing-check tool backed by this unchanged function could mark an unsupported 301-hole request as valid. The Day 3 structured impact scan inspected 368 `hole_count` values from scenario JSON, hidden label JSON, and serialized `part_json` within historical quote lines: maximum observed 12; zero above 300, zero negative, zero nonnumeric. Thus **no affected record was observed in that scan**. The scan did not independently parse every attachment, every embedded prose requirement, or future API input, so it does not prove all possible inputs are safe.

**Recommended response:**

1. Keep v1.1.0 frozen and record this finding in project documentation.
2. Implement a server-side positive integer and max-300 validation rule in the future application validation layer before accepting geometry as manufacturable.
3. Add regression cases for 300 (accept), 301 (reject), negative, non-integral, missing, and malformed hole counts.
4. If updating the reference `src.business_rules.validate_part` itself, make the change in an explicitly versioned subsequent dataset release with regenerated, verified checksums and test evidence; do not silently patch v1.1.0.
5. Revisit other scalar manufacturing limits as part of future hardening; do not claim they are defective without a separate reproduction.

**Severity:** Prospective moderate business-validation risk. **Current scanned dataset impact:** None observed.

### Day 8 resolution (application validator)

- **Where it is fixed:** `src/rfq_copilot/services/manufacturing.py` (`evaluate_manufacturing`, called by `check_manufacturing_capability`). The configured `limits.hole_count_max` from the pinned `mfg-v1.0` rule version is enforced: a part passes only when `0 <= hole_count <= 300`, otherwise it gets `OUT_OF_RANGE_HOLE_COUNT` and result `unsupported`. An unstated hole count uses the rule default `defaults.hole_count = 0`.
- **Root cause closed, not just the symptom:** the validator raises an error if the rule document contains any limit it does not explicitly enforce. A future scalar limit therefore cannot be silently skipped the way `hole_count_max` was.
- **Regression tests** (recommended responses 2 and 3 above):
  - `tests/test_manufacturing_service.py`: 0, 300 and unstated → `valid`; 301, 10,000 and −1 → `OUT_OF_RANGE_HOLE_COUNT`; malformed values (`300.0`, `300.5`, `"300"`, `True`, `Decimal(300)`) are rejected by `Part.from_mapping` before validation; the maximum is read from the rule.
  - `tests/test_day08_manufacturing.py::test_mfg_001_regression_against_the_reference`: against the seeded test database, **300 holes passes and 301 fails**, and the same test asserts that the frozen reference still returns `valid` for 301.
- **Frozen reference engine:** `data/rfq_synthetic_data/src/business_rules.py` (`validate_part`) still contains the original limitation and **must remain unchanged**. Dataset v1.1.0 is frozen; per recommended response 4, any reference fix belongs only in a separately versioned dataset release with regenerated, verified checksums. The reference is a test oracle only and is never imported by application code, so its limitation cannot reach runtime decisions.
- **Deliberate divergence:** for hole counts above 300 or below 0, application results intentionally differ from the reference. Parity tests cover only values within the limit.

## Reproducibility and source safety

The original `tests/test_reproducibility.py` invokes `src.generate_data`; the dataset generator deletes and recreates generated directories. For Day 3, that test was executed only inside a disposable temporary clone. The other 38 tests were run against the original tree with bytecode and pytest cache generation disabled. Hash snapshots verified preservation. Run future destructive regeneration only in disposable copies or a separately versioned dataset.

## Approval, security, and leakage boundaries

- Agent-visible requests are stored separately from hidden `evaluation/ground_truth` expected outcomes.
- Independent path/ID audit of all 240 scenario-manifest pairs found **zero** structural issues and zero top-level `expected_*` fields in visible scenario JSON.
- The dataset's 14 tool contracts include only three state-changing operations (`create_rfq`, `create_quote_draft`, `request_approval`), all requiring idempotency.
- No `approve_quote` or `release_quote` tool is exposed to AI agents. Release must be performed through a separately authenticated, human-authorized interface enforcing current-version approval, independent approver identity, active customer status, and commercial clearance.
- Existing checks demonstrate file separation and fixture behavior; they do not constitute a deployed access-control proof.

## Disposition and remaining work

**Day 3 integrity validation:** Completed with passing local evidence.  
**Day 3 deterministic reference review:** Completed with open `MFG-001` defect documented.  
**MFG-001 (Day 8):** Fixed in the application manufacturing validator with 300/301 boundary regression tests; the frozen reference engine still contains the limitation and stays unchanged.\
**Dataset v1.1.0:** Preserved, not regenerated or altered.  
**Production readiness:** Not asserted. Database import, secure MCP gateway, authenticated roles, actual LangGraph routing, persistence, and end-to-end deployment belong to later roadmap stages.

See [`dataset_inventory.md`](dataset_inventory.md), [`dataset_schema_analysis.md`](dataset_schema_analysis.md), and [`multiagent_data_mapping.md`](multiagent_data_mapping.md).
