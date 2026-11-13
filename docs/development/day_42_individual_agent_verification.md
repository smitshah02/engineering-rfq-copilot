# Day 42: Individual Agent Verification (A1 to A5)

Verification status: **NOT RUN**. No pytest, Ruff, live MCP or Git command was executed. The
only checks run were a Python `ast.parse` of the changed modules and a scan for long lines.

## 1. Executive summary

**Verdict: PASS WITH CONDITIONS (UNTESTED).**

- No CRITICAL or HIGH defect was found.
- Two MEDIUM evidence-consistency gaps were confirmed, both in the same class as D38-01 and
  D39-03. Both were fixed in the owning agent, with regression tests written first. Both
  fixes are untested until the user runs the suite.
- All seven earlier findings (D38-01 to D41-02) still hold.
- The condition on the verdict is the user's terminal run of the commands in section 10.

**Scope and method.** Each agent was audited on its own, then the shared boundaries were
checked. For every agent, the implementation was read in full and its status, evidence,
authorization and failure paths were traced by hand. Each check was compared with the
service that produces the evidence. The tests were then read to see whether their assertions
could actually catch the defect under audit. A passing test was not treated as proof.

**Inspected:**

- `intake.py`, `intake_validation.py`, `manufacturing_agent.py`, `supply_agent.py`,
  `pricing_agent.py`, `quotation_agent.py`
- `mcp/contracts.py` (supply, cost, price and quotation outputs, the Monday horizon)
- `services/lead_time.py`, `services/manufacturing.py` (bend minimum),
  `services/evaluation.py` (`borderline_bend_lines`), `services/capacity.py` (reference
  constants)
- `graph.READ_ONLY_TOOLS`
- The offline A2, A3 and A5 suites, focused on the D38 to D41 regressions, and the A4 test
  inventory
- The frozen routing and manufacturing-rule data (read only)

**Not executed:** the test suites, Ruff, the live MCP tests, servers, models and Git.

## 2. Individual agent verification

### A1: RFQ Intake and Validation (`intake.py`, `intake_validation.py`)

- **Functional.**
  - The model proposal is untrusted. `verify_extraction` gives each value a status:
    - The quote must occur in the source, inside the part's own segment.
    - The value must occur in the quote.
    - Numbers are parsed exactly, and units come from the fixed `_UNIT_MM` table.
    - Negation, alternatives, attribution, sign, correction and shared-span rules downgrade
      the value. It is never defaulted.
  - `validate_intake` adds four things:
    - mandatory fields from the pinned rule document
    - contradiction scans inside the part segment and across the whole RFQ
    - an exact single-row catalog match through a paginated, response-checked
      `search_materials` lookup
    - fixed-template clarifications
  - A `PartSpecification` is built only when the part is `ready_for_manufacturing_evaluation`.
- **Evidence.** The source is bound by its SHA-256 (`validate_intake` refuses other text).
  Values found by a source scan carry the `source_scan` provenance and never model text.
  Grades are never derived. Duplicate labels leave `label_verified` false, which raises a
  `part_id` clarification.
- **Authorization and security.** The one catalog tool is called through the trusted
  executor. Credential-like text is rejected before the graph runs. A suspected injection
  raises an RFQ-level review finding, so the RFQ is never ready. The proposal schema has no
  identity, price or decision fields.
- **Reliability.**
  - A catalog timeout re-validates every part without the catalog and downgrades any ready
    part.
  - A call-budget, pagination or inconsistent response is treated as `unavailable`.
  - With no rules there is no readiness at all.
- **Tests.** D36-01 to D36-13 and D37-01 to D37-09 each have negative and positive controls.
- **Limitations kept.** Customer text is never authority, there are no approvals, and A1 makes
  no decisions about manufacturability or price.
- **New findings:** none.

### A2: Manufacturing Feasibility (`manufacturing_agent.py`)

- **Functional.**
  - The gate requires the whole intake and every part to be ready. Otherwise the result is
    `not_evaluated` and no tool is called.
  - The tool is called once per part, in sequence, with no retries and under one timeout.
  - The RFQ status follows a fixed precedence order (`_PRECEDENCE`).
- **Evidence.**
  - `_forged` (line 144) checks contract validity, one rule throughout, the services' status
    rule for both levels, exact finding tables, the catalog entry matching the part, findings
    carried through, and catalog coverage when the result is `valid`.
  - A rule version or fingerprint that differs from Intake's gives `requires_review`.
- **D42-A2-01 (fixed).** `min_bend_radius_mm` was not checked (see section 5).
- **Authorization.** `ALLOWED_TOOLS` is `{check_manufacturing_feasibility}`. Customer scope
  comes only from the principal. Customer text never appears in the explanation.
- **Reliability.**
  - `McpCallError` returns its code.
  - A `ValidationError` gives `invalid_output`.
  - Any other exception gives `unavailable`, and its text is never surfaced.
  - A timeout fails every remaining part closed.
- **Tests.** The D38-01 suite has 14 negative and 11 real positive cases. The new D42-A2-01
  tests add 3 forgeries and 3 real controls.

### A3: Supply and Scheduling (`supply_agent.py`)

- **Functional.**
  - The A2 gate (`_gate`, line 133) requires a feasible A2 with valid, pinned, bound evidence
    and unique part IDs.
  - The horizon must be a trusted Monday.
  - A requested delivery date is used only when it is the single verified value in an
    `IntakeResult` with the same source.
  - Three read-only calls are made, in a fixed order, under one timeout.
- **Evidence.** `_inconsistent` (line 151) checks:
  - one pricing rule
  - the horizon, delivery date and snapshot pairs
  - per-line snapshots
  - exact part coverage, one line per material and thickness, and each line's material and
    thickness
  - stock semantics (`_line_impossible`)
  - capacity agreeing with lead time
  - date chronology
- **D42-A3-01 (fixed).** The procurement delay, start week and exact ready date were not tied
  to the stock lines (see section 5).
- **Authorization.** `ALLOWED_TOOLS` is the three supply tools, all in `READ_ONLY_TOOLS`. The
  arguments contain only parts and dates (`extra="forbid"`). Nothing is reserved or written.
- **Reliability.**
  - A call failure keeps nothing.
  - The error code is fixed.
  - Pricing-rule drift gives `requires_review` and the evidence is kept.
  - A supply verdict that contradicts A2 gives `requires_review`.
- **Tests.** D39-01 to D39-03 have negative and real controls. This audit added two A3-gate
  rows for D42-A2-01 and four D42-A3-01 schedules.

### A4: Pricing (`pricing_agent.py`)

- **Functional.**
  - The rule is checked against the manifest first (`_trusted`, line 143).
  - The gate (`_gate`, line 157) re-runs A3's gate and A3's own checks, and requires the
    status and explanation A3 derives, matching per-part stock lines, and the A3 pricing rule
    equal to the trusted one.
  - The tools are `estimate_costs` then `calculate_price`, called once each.
- **Evidence.**
  - Cost lines must be exactly A2's parts, in order, with their quantity and A2's route. The
    components must add up, and the line and RFQ totals must hold.
  - Price lines must equal `price_line` under the trusted margin, scales and rounding mode.
    The calculation ID, currency, margin and flags must be the trusted rule's.
  - A non-priced verdict is accepted only when both tools agree and carry no amounts. It
    then gives `requires_review` (the corrected D40 case).
- **Authorization.**
  - The tools need `view_quote`. A principal without it gets `forbidden` and no amounts.
  - On drift, the raw evidence still carries amounts (accepted limitation 8). This stays
    inside the `view_quote` boundary that was already authorized for this principal.
  - Top-level amounts are suppressed on drift.
  - A5 never copies these amounts (see A5).
- **Reliability.** The failure handling is the same as A3. `price failure after costs` keeps
  no cost.
- **Tests.** The tests cover forged amounts, other-part data, the incompatible finish (the D40
  case), drift, two rules, `forbidden`, the allowlist and principal binding.
- **New findings:** none.

### A5: Quotation and Review (`quotation_agent.py`)

- **Functional.**
  - Both rule sets must be trusted.
  - `_upstream` (line 281) applies status precedence: clarification, then review, then
    `not_evaluated`.
  - `_pricing_verified` (line 226) replays `evaluate_pricing` over A4's own evidence, without
    the network, and requires an equal result. This re-runs the A2 and A3 gates as well.
  - A4 `requires_review` is kept as a review with no amount.
  - One `evaluate_quotation` call is cross-checked by `_inconsistent` (line 242).
- **Evidence.** Line numbers, part IDs and order must match. Manufacturing results and
  findings must match per line, and stock status and line totals must match A4. D41-01 checks
  each line's delay through `procurement_business_days` and requires the maximum to equal
  A3's RFQ delay. The calculation ID, total, currency, ready date, lead days and findings
  must match.
- **Policy.**
  - `approval-v1.2` is used through the service helpers.
  - At or above the manager threshold, manager review is required.
  - Borderline bends are checked with `borderline_bend_lines` over A2's verified minimum.
    D42-A2-01 now ensures that minimum is present and consistent.
  - Customer account status and credit are always reported as unevaluated.
- **Human boundary.**
  - `approved` and `released` are `Literal[False]`, and `human_approval_required` is
    `Literal[True]`.
  - The only approver role is `sales_manager`.
  - Release prerequisites are listed as outstanding.
  - There is no write, number or validity period.
- **Customer view.** It shows selling amounts only. `conditional` (D41-02) is true whenever a
  review reason, a manager requirement or an unevaluated condition remains, which is true of
  every current draft.
- **Tests.** The tests cover D41-01 (6 contradictions, the sufficient-with-delay case and 3
  positives) and D41-02 (3 drafts). They also cover the forged A4 replay, drift without
  amounts, no internal cost, no approval claim, and principal binding.
- **New findings:** none.
- **Note (not a defect).** A5 decides whether a part is bent with `"bend" in operations`. In
  the frozen routing (`operation_routing.csv`), the parts with a bend count above zero are
  exactly the ones whose route has `bend`. A2 now relies on the same fact.

## 3. Cross-cutting assessment

- **Identity and provenance.** Every hand-off is checked for internal consistency:
  - source and RFQ identity
  - the argument SHA-256 values
  - part order and IDs
  - nested evidence
  - a replay where one exists

  The principal identity is the executor's verified credential, and no agent input carries a
  customer, role or rule. There is **no cryptographic authenticity**: the assessments are
  in-process Pydantic objects, and the hashes prove only that arguments are bound to evidence.
- **Rule trust.**
  - Manufacturing: the `mfg-v1.0` pin plus the fingerprint Intake validated against.
  - Pricing and approval: `TRUSTED_RULES` (kind, source SHA-256 and the re-derived
    fingerprint).
  - Drift gives `requires_review`, never a promoted amount.
- **Financial authority.**
  - Amounts come only from tools, re-checked with `price_line`, and no model is involved.
  - A5 copies only A4's selling amounts, and only after a replay has verified them.
  - Internal cost never reaches A5's result or the customer view.
  - Pricing is never approval.
- **MCP permissions.**
  - Every agent's `ALLOWED_TOOLS` is a subset of `graph.READ_ONLY_TOOLS`.
  - Permissions: `view_catalog` (A2), `view_assigned_rfqs` (A3), `view_quote` (A4 and A5).
  - No write tool exists in any allowlist, and there is no agent-controlled customer scope.
- **Human authority.** No agent can approve, reject, release, sign or send. Policy evaluation
  is reported as requirements, never as recorded approval.
- **Failure behavior.** Every invalid, missing or contradictory input ends in
  `not_evaluated`, `needs_clarification`/`clarification_required`, `requires_review` or
  `error`, and the error results keep no evidence. No path fabricates a positive conclusion.

## 4. Regression verification (D38 to D41)

| Finding | Source function | Regression tests | Still enforced | Gap |
|---|---|---|---|---|
| D38-01 | `manufacturing_agent._forged` | `test_d38_01_inconsistent_evidence_is_refused`, `test_d38_01_real_service_outcomes_pass_the_strict_checks` | Yes | The adjacent bend-minimum field was unchecked. Fixed as D42-A2-01 |
| D39-01 | `supply_agent._gate`, `_bound` (reuses `_forged`) | `test_d39_01_*` (4 tests) | Yes | None |
| D39-02 | `supply_agent._inconsistent`, `_line_impossible` | `test_d39_02_*` (4 tests) | Yes | None |
| D39-03 | `supply_agent._inconsistent` (dates) | `test_d39_03_*` (3 tests) | Yes, for the checks it named | The delay, start week and exact ready date were not derived from the stock lines. Fixed as D42-A3-01 |
| D40 test regression | `pricing_agent._blocked_result`, `evaluate_pricing` | `test_server_seeing_an_incompatible_finish_requires_review_without_amounts`, `test_server_pricing_other_part_data_is_refused` | Yes | None |
| D41-01 | `quotation_agent._inconsistent` | `test_contradictory_line_procurement_is_refused`, `test_sufficient_line_reporting_a_delay_is_refused`, positive delay tests | Yes | None |
| D41-02 | `QuotationReviewAssessment.customer_view` | `test_ordinary_draft_is_conditional_*`, `test_manager_threshold_draft_is_conditional`, `test_supply_constrained_draft_is_conditional` | Yes | None |

## 5. New findings

### D42-A2-01: MEDIUM, a forged bend minimum suppresses A5's borderline-bend review (FIXED, UNTESTED)

1. **Location.** `src/rfq_copilot/manufacturing_agent.py`, `_forged`, the `valid` branch
   (lines 173 to 190).
2. **Root cause.** `_forged` checked the result, findings, catalog and route, but never
   `ManufacturingFeasibilityOutput.min_bend_radius_mm`. A3 (`_bound`) and A4 (A3's gate) only
   reuse `_forged`. A5 passes this value to `borderline_bend_lines`.
3. **Trigger.** A `valid` result for a bent part (for example an `l_bracket` with a `bend`
   route and a stated radius just above the true minimum) arrives with `min_bend_radius_mm`
   set to `None`, or below `thickness x min_bend_radius_factor`. This can come from a faulty
   or compromised tool path, or from an in-process A2 object whose evidence was altered.
4. **Actual behavior.** A2 reports `feasible`, and A3, A4 and A5 accept it. Because
   `borderline_bend_lines` skips a missing minimum and compares against a lowered one, A5
   drops `BORDERLINE_BEND_RADIUS`. The draft becomes `draft_ready_for_review`, with no
   engineering review reason.
5. **Expected behavior.** A minimum that contradicts the result's own route or catalog
   evidence gives `invalid_evidence`.
6. **Impact.** An engineering review condition is bypassed. This is limited because human
   approval by `sales_manager` is always required, and nothing can be released.
7. **Fix.** For `valid` results, `_forged` now also refuses:
   - a `bend` route with no minimum
   - a minimum below `thickness x` the catalog factor (the service's floor: a material
     factor can only tighten the rule)
   - a stated bend radius below the minimum, which would make the result invalid

   These are consistency checks over the evidence only. Nothing is recalculated.
8. **Tests.**
   - `test_d42_a2_01_forged_bend_minimum_is_refused`: 3 cases.
   - `test_d42_a2_01_real_bend_minimum_passes`: 3 real controls (no radius, radius at the
     minimum, radius above it).
   - 2 rows in `test_d39_01_unbound_or_altered_a2_evidence_is_not_evaluated`, showing that the
     fix carries through to A3, A4 and A5.

### D42-A3-01: MEDIUM, a schedule not planned from the stock lines is accepted (FIXED, UNTESTED)

1. **Location.** `src/rfq_copilot/supply_agent.py`, `_inconsistent` (lines 199 to 219).
2. **Root cause.** `_inconsistent` checked that the earliest start follows from the *reported*
   procurement delay. It never checked that delay against the stock lines' supplier lead
   days, and never checked the start week against the earliest start. Both are
   `services.lead_time.plan_start`. It also accepted any ready date at or after the last
   planned Friday, while the service sets exactly `max(final Friday, earliest start)`.
3. **Trigger.** With 100 kg of stock, the supplier lead is 7 business days, giving start week
   2 and a ready date of Nov 27. The lead-time result instead reports a delay of 3, an
   earliest start of Nov 12, start week 1 (in both tools), capacity in week 1, a ready date
   of Nov 20 and 9 lead days. Every D39-03 check passes.
4. **Actual behavior.** A3 returns `constrained`, with a ready date one week early. Delivery
   can be marked achievable when it is not. A4 replays the same checks, so it prices the
   result too.
5. **Expected behavior.** `invalid_evidence`.
6. **Impact.** A wrong planning estimate and delivery verdict. This is limited because A5
   cross-checks the delay against its stock lines (D41-01) and the ready date against the
   quotation service. A3 or A4 consumed without A5 stays exposed.
7. **Fix.**
   - Under the pinned pricing rule, `(procurement delay, start week)` must equal
     `plan_start(inventory, horizon)`, called directly on the inventory output, whose fields
     match what `plan_start` reads.
   - The ready date must equal `max(final week Friday, earliest start)`.
   - Under drift, `plan_start`'s default delay is undefined (TD-003), so only that comparison
     is skipped. The drift result stays `requires_review`.
8. **Tests.** `test_d42_a3_01_schedule_not_from_the_stock_lines_is_refused`, 4 cases:
   - a delay that contradicts the supplier lead
   - the same, plus a week-1 plan with an early ready date
   - capacity planned late with no material delay
   - a ready date later than the service's

   The existing `test_d39_03_real_schedules_pass` controls cover the exact-ready rule, both
   when the ready date is the final Friday and when material is delayed.

No other confirmed defects.

## 6. Known limitations register

| Limitation | Agent | Current control | Integration impact | Action |
|---|---|---|---|---|
| Customer text is not authority, and extraction may be incomplete | A1 | Deterministic verification, readiness gate | None | None |
| `IntakeValidation` does not carry `source_sha256`. A3 binds `IntakeResult` by `(source_id, rfq_id)` only | A1/A3 | Same-source ID check | The Supervisor could pair two extractions of one source | Day 43: pass the one Intake result and validation through, never re-fetch |
| Evidence is unsigned, so a privileged caller can fabricate a fully consistent assessment | A2 to A5 | Consistency checks, replay | Inter-agent state must not cross a trust boundary unsealed | Future: signed or server-held evidence if state is persisted or remote |
| Rule or fingerprint drift | A2 to A5 | `requires_review`, manifest checks | None | None |
| Separate snapshots per call, no atomic transaction | A3/A4/A5 | Snapshot IDs paired within A3; A5 cross-checks fail closed | A data change between calls gives `invalid_evidence`, not a retry | Day 43: surface it as "re-run", never auto-merge |
| Scheduling is RFQ-level, with no partial orders or substitutions | A3 | Documented | None | None |
| The ready date is an estimate, with no reservation | A3/A5 | Explanation and notice text | None | None |
| Pricing cannot prove snapshot identity with A3 | A4 | A3 snapshot IDs recorded in the evidence | None | None |
| Raw drift evidence carries amounts | A4 | `view_quote` boundary; A5 never copies it | Do not show A4 evidence to principals without `view_quote` | Day 43: keep evidence internal |
| No finer financial-disclosure permission | A4/A5 | `view_quote` | Same | Future RBAC decision |
| Rates are not independently re-derived | A4 | Arithmetic and `price_line` checks | None | None |
| No persistence, quote number, version or validity | A5 | `Literal` fields, notice | None | None |
| No customer standing or credit source | A5 | Always unevaluated, `conditional=True` | All drafts stay conditional | None |
| No recorded-approval read | A5 | Approval is never claimed | None | None |
| Only `sales_manager` is defined as an approver | A5 | `APPROVER_ROLE` | Engineering and commercial reasons have no named role | Future policy decision |
| "Bent" means "a `bend` operation in the route" | A2/A5 | Matches the frozen routing data | A routing change would need re-review | None now |

## 7. Code changes

- `src/rfq_copilot/manufacturing_agent.py`: `_forged` bend-minimum checks (D42-A2-01).
- `src/rfq_copilot/supply_agent.py`: `plan_start` import, the delay and start-week check, and
  the exact ready date (D42-A3-01).
- No MCP contract, RBAC, migration, dependency, rule or dataset change.

## 8. Tests added (unverified)

- `tests/test_manufacturing_agent.py`: 6 cases (D42-A2-01).
- `tests/test_supply_agent.py`: 6 cases (2 D39-01 gate rows for D42-A2-01, 4 for D42-A3-01).
- Expected: **+12 cases**, for 3,212 passed and 8 skipped against the 3,200 / 8 baseline.
  Unverified.

## 9. Integration readiness

| Agent | Verdict | Reason |
|---|---|---|
| A1 | PASS | No defect; the limitations are documented |
| A2 | PASS WITH CONDITIONS | D42-A2-01 fixed but untested |
| A3 | PASS WITH CONDITIONS | D42-A3-01 fixed but untested |
| A4 | PASS | No defect. It inherits the A2 and A3 fixes through the gate replay |
| A5 | PASS | No defect. It inherits the fixes through the A4 replay |

**Recommendation.** Day 43 can begin once the commands in section 10 pass in the user's
terminal. That includes the full suite and the opt-in live MCP tests, which exercise the
stricter A3 schedule check against the real servers.

## 10. Manual verification commands

See the "Commands" section of `docs/dev_log/day_42.md`.
