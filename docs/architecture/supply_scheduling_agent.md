# Supply and Scheduling Agent (A3, Day 39)

Module: `src/rfq_copilot/supply_agent.py`. Tests: `tests/test_supply_agent.py` (offline) and
`tests/test_supply_agent_live_mcp.py` (opt-in live MCP).

For the parts A2 found manufacturable, A3 asks the existing supply tools four questions:

- Is the material in stock?
- When can the job start?
- Is there workcentre capacity in the planning horizon?
- When will the parts be ready, and does that meet the customer's requested delivery date?

The answer is a planning estimate. A3 does not reserve stock, change capacity, create an
order, set a price or approve anything.

## Interface (for the future Supervisor)

```python
await evaluate_supply(
    a2: ManufacturingAssessment,         # Day 38 output
    execute: ToolExecutor | None,        # trusted, principal-bound: graph.mcp_executor(...)
    *, horizon_start: date | None,       # trusted planning policy (a Monday)
    extraction: IntakeResult | None = None,  # Day 36 result of the same source
    timeout_seconds: float = 60.0,
) -> SupplyAssessment
```

### Input gate

If any of these checks fails, the result is `not_evaluated` and no tool is called:

- The A2 status must be `feasible`, and every part must be `feasible`, without an error,
  and backed by a `valid` tool result (`manufacturing_<status>`,
  `manufacturing_evidence_missing` or `no_parts`).
- The tool result's rule version and fingerprint must equal `mfg-v1.0` and the fingerprint
  Intake validated with (`manufacturing_rule_mismatch`).
- A2's evidence must be bound to this part (D39-01). Otherwise the reason is
  `manufacturing_evidence_invalid`. Binding means all of these hold:
  - `part_id` equals `specification.part_id`
  - `arguments_sha256` is the sha256 of `PartCheckInput(part=specification)`, so a
    specification changed after evaluation, or another part's evidence, is rejected
  - A2's own evidence checks (`manufacturing_agent._forged`) accept the result for this
    specification: consistent findings and status, the route, the material evidence, and
    the catalog entry matching the part's material, thickness, finish and operations
- Part IDs must be unique (`duplicate_part_ids`).
- `horizon_start` must be given (`planning_horizon_missing`).
- `horizon_start` must be a plain `date` that falls on a Monday (`invalid_planning_horizon`).

A3 schedules all parts of the RFQ or none of them. It never schedules a subset, because that
would describe a different order.

### Dates

- **Planning horizon.** It comes only from the trusted caller policy, never from customer
  text.
- **Requested delivery date.**
  - It is used only when `extraction` is the Day 36 result of the same `source_id` and
    `rfq_id`, and holds exactly one `requested_delivery_date` with status `verified` and a
    `date` value.
  - If the date is stated but not verified, A3 returns `needs_clarification` and calls
    nothing.
  - A3 never reads dates from any other field or from part IDs.
  - If `extraction` comes from another source, A3 raises `ValueError`.

## Tools and permissions

| Tool | Permission | Service it runs (in its own READ ONLY snapshot) |
|---|---|---|
| `check_inventory` | `view_assigned_rfqs` | `services.inventory.check_inventory` on the server-selected inventory snapshot |
| `check_capacity` | `view_assigned_rfqs` | `services.lead_time.estimate_lead_time` → `services.capacity.check_capacity` from the material-ready week |
| `estimate_lead_time` | `view_assigned_rfqs` | `services.lead_time.estimate_lead_time` (procurement delay, start week, ready date, delivery feasibility) |

- **Allow-list.** It is `ALLOWED_TOOLS`. A test asserts that it is a subset of
  `graph.READ_ONLY_TOOLS`. The module does not import the graph.
- **Call scope.** Each tool is called once for the whole RFQ, because stock is aggregated by
  material and thickness and capacity is shared by every part. The calls carry A2's verified
  specifications as prospective `parts`, plus the horizon and, for lead time only, the
  verified delivery date.
- **No `rfq_id`.** It is never sent, so A3 does no stored-RFQ lookup.
- **Pinned on the server.** The rule versions (`mfg-v1.0`, `price-v1.0`) and snapshot
  selection are pinned on the server and never arrive as arguments.
- No MCP contract, service or rule was changed.

## Execution flow

1. Apply the gate and resolve the dates (see above).
2. Call `check_inventory`, `check_capacity` and `estimate_lead_time`, in that order, once
   each, with no retries, inside one `asyncio.timeout`. Re-validate each result against its
   output contract. These map to `error` and keep no evidence:
   - any `McpCallError` (A3 keeps its code)
   - an off-contract result (`invalid_output`)
   - an executor fault (`unavailable`; its text is never shown)
   - a timeout
   - a missing executor (`unauthenticated`)
3. Check the evidence. These results become `error / invalid_evidence`, and their evidence is
   not kept:
   - the inventory and capacity results use different pricing rules (version or fingerprint)
   - the horizon or requested delivery date in a result differs from what A3 sent
   - the lead-time snapshot IDs differ from those in the inventory or capacity results
   - a stock row comes from another snapshot
   - the three results do not agree:
     - if the stock check says `needs_clarification` or `not_manufacturable`, the other two
       must say the same
     - otherwise the capacity plan must be `feasible` (lead time `estimated`) or `infeasible`
       (lead time `infeasible`), with the same start week
   - the stock lines do not cover each requested part exactly once with that part's own
     material and thickness
   - there is more than one line for the same material and thickness. Parts sharing stock
     must share its one aggregated line (D39-02).
   - a line breaks the stock service's own rules (D39-02):
     - required kg must be positive
     - `shortage = max(required - available, 0)`
     - the status must be `sufficient` when available ≥ required, `insufficient` when
       available > 0, and `unavailable` otherwise
   - the overall stock result does not match the lines
   - the procurement delay does not match the stock: it must be 0 exactly when all stock is
     sufficient
   - an `infeasible` lead time does not carry the service's finding for the capacity reason
     (`services.lead_time.CAPACITY_FINDINGS`)
   - the dates are out of order (D39-03). This checks that:
     - the earliest start equals `add_business_days(horizon, procurement days)`, using the
       service's own helper
     - no planned capacity week is before `horizon + start_week` weeks
     - `final_week_start` is the last planned week
     - the ready date is not before the Friday of that week (`FRIDAY_OFFSET`) or the
       earliest start
     - the lead business days equal `business_days_between(horizon, ready)`
   - an `estimated` lead time is missing a ready date or business days
   - a delivery verdict is given with no requested date, or is missing when one was requested
   - `delivery_feasible` does not equal `ready <= requested`, or the findings disagree with it

   These checks only test consistency. Nothing is recalculated.
4. Set the status:

| Condition | Status |
|---|---|
| Pricing version is not the server pin (`price-v1.0`) | `requires_review`, evidence kept |
| The supply tools agree the parts are `needs_clarification` or `not_manufacturable`, contradicting A2 | `requires_review` |
| The capacity plan is `infeasible` with `NO_CAPACITY_IN_HORIZON` | `constrained` |
| The capacity plan is `infeasible` with `MISSING_CAPACITY_DATA` | `requires_review` |
| Stock is short (`insufficient` or `unavailable` lines), or the requested delivery date cannot be met | `constrained` |
| Otherwise (estimated, all in stock, delivery met or not requested) | `available` |

5. Write the explanation from fixed templates. It includes:
   - the tools, pricing version, snapshots and horizon
   - stock status and shortages per catalog material and thickness, with the procurement days
   - the ready date and business days, or the capacity constraint
   - whether the requested delivery date can be met

   It never includes customer text.

## Output: `SupplyAssessment` (frozen, `extra="forbid"`, JSON round-trips)

| Field | Meaning |
|---|---|
| `source_id`, `rfq_id` | From A2, which takes them from the trusted Intake validation |
| `status` | `available`, `constrained`, `needs_clarification`, `requires_review`, `not_evaluated` or `error` (RFQ-level, because stock and capacity are shared) |
| `horizon_start` | The trusted policy horizon that was used |
| `requested_delivery_date` | The verified Intake date, or `None` |
| `parts[]` | `PartSupply`: `index`, `part_id`, and `stock`, the `InventoryLineOutput` covering this part (shared when parts use the same material and thickness) |
| `explanation` | Deterministic text |
| `evidence` | `SupplyEvidence`, or `None` when nothing was concluded (fields below) |
| `not_evaluated_reason`, `error` | Why nothing was concluded |

`SupplyEvidence` holds:

- `arguments_sha256`
- the exact `InventoryOutput`: lines, required, available and shortage kg, supplier lead
  days, snapshot, pricing rule version and fingerprint
- the exact `CapacityOutput`: workcentre weeks, start week, final week, capacity snapshots
- the exact `LeadTimeOutput`: ready date, lead and procurement days, earliest start,
  requested date, delivery feasibility, findings, snapshot IDs

All quantities stay exact decimal strings, in kg, hours and business days, as the contracts
define them.

## Security and authority

- There is no LLM. The order of calls is fixed, so a model would add risk and nothing else.
  The tests make every model `ainvoke` and direct MCP client call fail.
- The customer comes only from the executor's verified principal. The arguments have no
  identity, role, permission, rule-version or snapshot field (`extra="forbid"`).
- A3 makes no writes. Every tool is read-only, its calls run in a `READ ONLY` snapshot, and
  the services reserve nothing.
- An unverified schedule is never reported as achievable. `delivery_feasible` comes only from
  validated, consistent `estimate_lead_time` evidence.
- A3 never substitutes materials and never overrides A2.

## Testing

- `tests/test_supply_agent.py` (offline). A fake executor runs the real pure chain:
  - the manufacturing check over the frozen catalog
  - `evaluate_inventory`
  - `plan_start`
  - `evaluate_capacity`
  - `evaluate_lead_time`

  It maps each result with `mcp.runtime.project`, as the supply server does. A2 assessments
  come from the real A2 agent. The tests cover:
  - available, matching the RFQ-0001 ground truth (ready 2026-11-13, 4 business days)
  - insufficient and unavailable stock
  - no capacity in the horizon and missing capacity data
  - delivery date met, met on the day and missed
  - missing or invalid horizons
  - an unverified or duplicate delivery date
  - dates in customer data being ignored
  - an extraction from another source
  - every A2 gate
  - each tool failure code, a failure partway through, executor faults, timeouts and a
    missing executor
  - off-contract results
  - 22 inconsistent-evidence cases and a forged "achievable"
  - D39-01 to D39-03 regressions, with positive controls from the real services:
    - in stock, insufficient and unavailable stock
    - shared stock
    - delayed procurement (start 2026-11-18, ready 2026-11-27)
    - multi-week capacity (ready 2026-12-04)
    - delivery met and missed
    - valid A2 evidence for several part shapes
  - pricing rule drift and an A2 contradiction
  - per-part stock attribution and shared stock
  - argument scope, the allow-list and read-only permissions, and principal binding
  - injection in `part_id`
  - serialization, determinism and the A1 → A2 → A3 handoff
- `tests/test_supply_agent_live_mcp.py` (`RFQ_AGENT_LIVE_MCP=1`). It runs A1 → A2 → A3
  through the real MCP host and seeded test database, and checks the snapshot pairing, the
  pricing pin, part attribution and a revoked subject.

## Known limitations

- The supply outputs carry no manufacturing rule version, so manufacturing rule drift between
  A2 and A3 cannot be detected here. The supply servers pin `mfg-v1.0` themselves.
- The three tools run in separate snapshots. The server picks the same registered snapshots
  each time, and A3 checks that the snapshot IDs pair up. A snapshot changing between calls
  shows up as `invalid_evidence`, not as a retry.
- The status is per RFQ, not per part, because capacity is planned jointly. Per-part data is
  limited to the stock line.
- A3 schedules all parts or none. It does not plan partial orders or alternative materials.
