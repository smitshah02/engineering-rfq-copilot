# Specialized Agent Integration (A1 → A5)

`rfq_copilot.agent_integration` (Day 43). One RFQ evaluation runs through all five
specialized agents in a fixed order, with checked handoffs. It returns one typed result.

This is not a Supervisor, a planner or a router. It does not approve, release, persist or
number anything. A human decides every quotation.

```text
customer text + trusted source_id / rfq_id + intake graph + trusted ToolExecutor
    + trusted horizon_start + pinned mfg-v1.0 / price-v1.0 / approval-v1.2 rules
    -> evaluate_rfq_workflow(...) -> RFQWorkflowAssessment
```

## 1. End-to-end architecture

```text
preflight ─► A1 extraction ─► A1 validation ─► A2 ─► A3 ─► A4 ─► A5
   │              │                │            │     │     │     │
   └──────────────┴────────────────┴────────────┴─────┴─────┴─────┴─► stop + typed result
```

- One module, one public coroutine, one result model. No new framework and no second
  LangGraph graph. A1 keeps its existing four-node graph, and the other agents are plain
  coroutines.
- Every agent runs **at most once**. There are no retries and no loops. The only model call
  is A1's single extraction.
- After each stage, that stage's own status is looked up in the fixed table `_NEXT`. Anything
  other than "continue" ends the workflow, so no later agent and no later tool runs.

## 2. Entry point

```python
async def evaluate_rfq_workflow(
    text: str,                                    # untrusted customer content, used only by A1
    *,
    source_id: str,                               # trusted, caller-assigned
    rfq_id: str | None,                           # trusted, caller-assigned
    intake_graph: CompiledStateGraph,             # intake.build_intake_graph(model)
    execute: ToolExecutor | None,                 # graph.mcp_executor(mcp, principal, credential)
    horizon_start: date | None,                   # trusted planning policy (a Monday)
    manufacturing_rules: RuleVersionDetails | None,  # pinned_rules(session, "mfg-v1.0")
    pricing_rules: RuleVersionDetails | None,        # pinned_rules(session, "price-v1.0")
    approval_rules: RuleVersionDetails | None,       # pinned_rules(session, "approval-v1.2")
    timeout_seconds: float = 390.0,
) -> RFQWorkflowAssessment
```

The caller cannot pass any of the following:

- an `IntakeResult` or `IntakeValidation`
- any earlier stage result
- a customer id, role or permission

A test pins this parameter list.

## 3. Execution order

| # | Stage | Agent call | Tools (RBAC v2) |
|---|---|---|---|
| 0 | `preflight` | none: checks `execute`, `price-v1.0` and `approval-v1.2` against the manifest | none |
| 1 | `intake_extraction` | `intake.run_intake(intake_graph, text, ...)` | none (one model call) |
| 2 | `intake_validation` | `intake_validation.validate_intake(extraction, text, rules=mfg, catalog=execute)` | `search_materials` |
| 3 | `manufacturing` | `evaluate_manufacturing_feasibility(intake, execute)` | `check_manufacturing_feasibility` (`view_catalog`), once per part |
| 4 | `supply` | `evaluate_supply(a2, execute, horizon_start=, extraction=<same object>)` | `check_inventory`, `check_capacity`, `estimate_lead_time` (`view_assigned_rfqs`) |
| 5 | `pricing` | `evaluate_pricing(a2, a3, execute, pricing_rules=)` | `estimate_costs`, `calculate_price` (`view_quote`) |
| 6 | `quotation` | `evaluate_quotation_review(a2, a3, a4, execute, pricing_rules=, approval_rules=)` | `evaluate_quotation` (`view_quote`) |

The preflight runs before the model call. A missing executor, or an untrusted pricing or
approval rule, therefore costs nothing.

## 4. Input/output contracts

| Agent | Receives (from the integration) | Returns (kept unchanged) |
|---|---|---|
| A1 extraction | text, trusted `source_id` / `rfq_id` | `IntakeResult` (with `source_sha256`) |
| A1 validation | that `IntakeResult`, the same text, mfg rules, executor | `IntakeValidation` |
| A2 | that `IntakeValidation` | `ManufacturingAssessment` |
| A3 | that `ManufacturingAssessment`, trusted horizon, **the same `IntakeResult` object** | `SupplyAssessment` |
| A4 | the A2 and A3 objects, trusted pricing rule | `PricingAssessment` |
| A5 | the A2, A3 and A4 objects, trusted pricing and approval rules | `QuotationReviewAssessment` |

`RFQWorkflowAssessment` is frozen, has `extra="forbid"`, and contains:

- **Identity and provenance:** `source_id`, `rfq_id`, `source_sha256`.
- **Outcome:** `status`, `stopped_at`, and `stages`, the trace of each stage's own status
  and reason code.
- **Diagnostics:** `error` (a fixed code), `clarifications`, `review_reasons`,
  `unevaluated_conditions`.
- **Rules and draft:** `rules`, the (version, fingerprint) pairs used; `draft_available`.
- **Human boundary:** `human_approval_required: Literal[True]`, `approved: Literal[False]`,
  `released: Literal[False]`.
- **Internal stage results:** every stage result that passed its handoff checks.

Workflow statuses:

| Status | Meaning |
|---|---|
| `draft_ready_for_review` | A5 assembled a verified, non-binding draft. Human approval is still required. |
| `requires_review` | A human must review. A draft may exist; `draft_available` says whether it does. |
| `clarification_required` | The customer must answer `clarifications`. |
| `not_quotable` | A2 found a part not manufacturable. |
| `not_evaluated` | A gate refused: an untrusted rule, a forged or stale upstream result, or a bad horizon. |
| `error` | A tool, timeout, identity or agent failure; see `error`. |

## 5. Verified Intake pairing

`IntakeValidation` has no `source_sha256` (Day 42). The workflow therefore never accepts an
Intake pair from outside:

- The extraction and its validation are produced here, from the same `text`, within one
  call.
- `validate_intake` re-checks `extraction.source_sha256` against that text. A test shows
  that an extraction of other text under the same ids is refused: `internal_error` at
  `intake_validation`, and no tool runs.
- **The same `IntakeResult` object** goes to A3. It is never re-fetched or rebuilt; a test
  asserts object identity.
- Each stage result must carry the requested `(source_id, rfq_id)`, else
  `identity_mismatch`. A2 must carry exactly A1's specifications, else
  `specification_mismatch`.
- A result that fails a handoff check is not kept in the workflow result. Nothing downstream
  can read it.

**Accepted limitation.** This protects pairing inside one workflow call. It does not
cryptographically authenticate assessment objects built elsewhere: upstream evidence is
unsigned.

## 6. Trusted principal and tool authorization

- There is one executor per evaluation (`graph.mcp_executor`, bound to the verified
  `Principal` and its delegated credential), and every stage uses it. Results from different
  principals cannot be mixed within one result. Two concurrent workflows for two customers
  stay separate (tested).
- No tool argument carries identity, role, permission or rule version. The contracts use
  `extra="forbid"`, and a test inspects every request.
- Authorization stays with each MCP server. No later stage is authorized by an earlier stage
  having succeeded. A principal without `view_quote` gets `forbidden` at A4 or A5 and sees no
  cost or price.
- A missing or malformed principal makes the executor raise `unauthenticated` before any
  call. A1's catalog lookup reports that as unavailable, the workflow stops at
  `intake_validation`, and no tool is reached.
- The integration has no database access. Rules are loaded by the caller with
  `mcp.runtime.pinned_rules`.

## 7. Rule pinning and verification

- `price-v1.0` and `approval-v1.2` are checked against `TRUSTED_RULES` in the preflight. The
  check uses A4's and A5's own helpers (`pricing_agent._trusted`,
  `quotation_agent._approval_trusted`), so there is one implementation of each check.
- A4 and A5 then repeat their own checks. A3's acceptance of the pricing rule (version and
  cross-tool fingerprint) is **not** treated as proof of authenticity. A4's manifest check
  still runs (Day 42).
- `mfg-v1.0` is not manifest-checked here. `pinned_rules` checks its source sha256, and A2
  compares every tool result's server-pinned rule and fingerprint with the ones Intake
  validated against. A difference means `requires_review`.
- The same pricing rule object is passed to A4 and A5. Customer text never supplies a rule.
  A test shows that a prompt asking for `price-v9.9` and zero margin changes nothing.

## 8. Stage gating and status transitions

| Stage | Continue on | Stop with |
|---|---|---|
| A1 extraction | `extracted`, `extracted_with_issues`, `no_parts_found` | `error` → `error` |
| A1 validation | `ready_for_manufacturing_evaluation` | `needs_clarification` → `clarification_required`; `requires_review` → `requires_review`; `not_evaluated` → `not_evaluated` |
| A2 | `feasible` | `infeasible` → `not_quotable`; `needs_clarification` → `clarification_required`; `requires_review` → `requires_review`; `not_evaluated`; `error` |
| A3 | `available`, `constrained` | `needs_clarification` → `clarification_required`; `requires_review`; `not_evaluated`; `error` |
| A4 | `priced`, `requires_review` | `not_evaluated`; `error` |
| A5 | (final) | its status, unchanged |

- **Unknown status:** stops with `error` / `unexpected_stage_status`.
- **A4 `requires_review`** (rule drift or contradiction) continues to A5. A5 replays A4 over
  its recorded evidence, returns a no-amount `requires_review`, and **does not call its
  tool**. A forged A4 status fails that replay (`pricing_assessment_invalid`).
- **A forged A3 `constrained`** fails A4's gate (`supply_evidence_invalid`). No pricing tool
  runs.
- **`stopped_at`** is `None` only when A5 assembled a verified draft (`draft_ready_for_review`
  or `requires_review` with lines). Otherwise it names the stage that ended the workflow.

## 9. Business-authority boundaries

Every decision belongs to the agent, or the service behind its tool, that owns it:

| Decision | Owner |
|---|---|
| Completeness and catalog | A1 |
| Feasibility, routing and bend minimum | A2 |
| Stock, capacity and dates | A3 |
| Cost and price | A4 |
| Draft and approval-policy conditions | A5 |

The integration compares identities and specifications only. It computes no feasibility,
schedule, amount, discount, tax, freight, validity or approval.

## 10. Cross-agent evidence propagation

The integration hands over the agents' own objects. It does not copy or transform them, so
every existing check still applies:

- **D38-01 and D42-A2-01:** A2 refuses forged nested evidence and a missing, lowered or
  contradicting `min_bend_radius_mm`. Stops at A2 (tested through the workflow).
- **D39-01:** A2 evidence that is no longer bound to its specification. Stops at A3:
  `manufacturing_evidence_invalid`.
- **D39-02, D39-03 and D42-A3-01:** inconsistent stock lines, chronology, procurement delay,
  start week or ready date. A3 `invalid_evidence`.
- **D41-01:** per-line procurement delay agrees with the stock line and the RFQ delay (A5).
- **D41-02:** the customer `conditional` flag reflects outstanding review and the
  unevaluated customer checks (A5's `customer_view`).

## 11. Snapshot consistency limitations

- A3's three tools each run in their own read-only snapshot. The integration never claims
  they are atomic and never invents a shared snapshot id.
- A4's tools do not report snapshot ids. A4 records A3's.
- A5's quotation service reads its own snapshot. A disagreement with A3 or A4 is
  `invalid_evidence` (tested with another schedule and other stock).
- `invalid_evidence` is propagated as a stop. A new attempt is a fresh, caller-controlled
  `evaluate_rfq_workflow` call. Results from different calls are never spliced together.

## 12. Internal versus customer-facing outputs

| Output | Contents | Audience |
|---|---|---|
| `RFQWorkflowAssessment` | All stage results, including A4 internal costs and raw tool evidence | Internal only (Supervisor, audit). Never serialize it to a customer. |
| `.summary()` | Trace, statuses, codes, reasons, rule provenance. No amount and no evidence. | Logs, handoffs |
| `.customer_view()` | A5's `CustomerQuotationDraft` (selling lines, total, exclusions, estimate, `conditional`, notice). `None` unless `draft_available`. | Customer-facing draft, after a human decision |

A4 diagnostic amounts kept under rule drift stay inside `pricing.evidence`. They never reach
`summary()` or `customer_view()` (tested).

## 13. Human approval and release

- `approved` and `released` are `Literal[False]`, and `human_approval_required` is
  `Literal[True]`, both on the workflow and on A5. Validation refuses any other value.
- There is no quote number, version, validity or expiry, and there is no persistence. The
  module source has no database session, commit or rule registration, and a test checks this.
- `unevaluated_conditions` lists `customer_account_status` and `credit_limit` on every
  draft. No trusted source evaluates them.

## 14. Failure and timeout handling

- **Overall deadline** (`timeout_seconds`): each agent gets `min(its default, remaining)` and
  fails closed through its own timeout path:
  - A1 extraction: `execution_limit`.
  - A1 validation: `catalog_unavailable` → review.
  - A2 to A5: `timeout`.
  - If no time is left before a stage, it is recorded as `not_run` / `timeout` and nothing is
    called.
- **Tool failures** keep the agent's fixed code (`forbidden`, `unauthenticated`, `busy`,
  `invalid_output`, `unavailable`, ...). There are no retries.
- **An exception raised by an agent** stops with `internal_error`. Exception text never
  reaches the result (tested with a credential-shaped message).
- **No partial result counts as complete.** The stages that did run stay visible in `stages`
  and in the stage results.

## 15. Testing and live MCP strategy

- **Offline:** `tests/test_specialized_agent_integration.py`. One `Router` executor sends
  each tool to the existing Day 37–41 fake, which runs the real services. A1 runs its real
  graph on the Day 36 fake chat model. Bent-part scenarios replace only A1's validation
  output. Status-override tests patch one agent's result to check gating. Every stop asserts
  that no later stage's tool ran.
- **Live (opt-in):** `tests/test_specialized_agent_integration_live_mcp.py`, with
  `RFQ_AGENT_LIVE_MCP=1` and marked `integration`. It uses the Day 41 host fixtures over the
  guarded test database, with no paid or local model:
  - An authorized principal reaches A5.
  - A restricted principal (`manufacturing_engineer`) and a revoked principal get no price
    and no draft.
  - `approval-v1.2` is registered with the existing idempotent `register_rule_version`, as
    on Day 41.
- **Observability:** the agents have no logging convention, so none was added. `stages` plus
  `summary()` is the structured trace a caller may log. It never contains credentials,
  customer text, amounts or raw evidence.

## 16. Accepted limitations and Supervisor handoff

| Limitation | Day 43 handling |
|---|---|
| `IntakeValidation` has no source sha | The pair is created in-process from one text; the same extraction goes to A3 |
| Upstream evidence is unsigned | Consistency is validated; no cryptographic authenticity is claimed |
| A2 bend-radius evidence | D42-A2-01 is enforced by A2 and reached through the workflow |
| A3 separate snapshots | No atomicity claimed; no merge, no hidden retry |
| A3 RFQ-level scheduling | No per-part or partial schedule is produced |
| A3 planning estimates | `estimated_ready_date` is an estimate in A5's draft and notice |
| A3 rule verification | A4's manifest check is preserved and also run in the preflight |
| A4 raw monetary evidence | Internal only; drift amounts are never promoted |
| A4 prices are advisory | The draft needs human approval; `approved` is always `False` |
| A5 procurement checks | D41-01 is unchanged |
| A5 customer conditional | D41-02 is unchanged; `conditional` is always true while checks are unevaluated |
| A5 has no credit/account source | `unevaluated_conditions` |
| A5 has no validity rule | No expiry |
| A5 has no persistence | No quote id or version |
| Human approval | Never automatic |

What a future Supervisor may and may not do:

| The Supervisor may | The Supervisor must never |
|---|---|
| Call `evaluate_rfq_workflow` with trusted ids, a principal-bound executor, a trusted horizon and the results of `pinned_rules` | Pass caller- or model-supplied rules, ids, roles or Intake objects |
| Read `status`, `stopped_at`, `clarifications`, `review_reasons`, `summary()` | Treat `draft_ready_for_review` as approval, or release or send a draft |
| Show `customer_view()` after a human decision | Serialize the full `RFQWorkflowAssessment` (A4 costs, raw evidence) to a customer |
| Start a **new** evaluation after a clarification or failure | Retry a stage, splice results across calls, or override a stage status |
| Route review to humans (`required_approver_role`) | Approve, persist, number or set expiry for a quotation |
