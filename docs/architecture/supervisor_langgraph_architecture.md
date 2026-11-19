# LangGraph Supervisor and durable workflow processing

The Supervisor coordinates six stages: intake extraction, intake validation, manufacturing,
supply/scheduling, pricing, and quotation/review. It validates trusted context before running,
keeps specialist custody outside untrusted graph input, derives route outcomes from verified
results, bounds concurrency/time, and exposes separate internal/customer projections.

## Execution and persistence boundaries

`SupervisorContext` carries a verified Principal, current source, pinned rules, injected model
graph and principal-bound MCP executor. The worker may additionally pass persisted structured
facts and trusted part IDs. Browser/model fields cannot supply rules, custody or authority.
The Supervisor's assessment is non-binding; it neither approves nor releases quotations.

PostgreSQL `orchestration.workflow_runs` provides the durable queue. RFQ submission enqueues
atomically after explicit queue activation. Separate consumers claim with `FOR UPDATE SKIP
LOCKED`, bounded attempts, 120-second leases and generation fences; the evaluation worker
heartbeats its lease. Only eligible completed outcomes or recoverable drafting leases reach
the deterministic drafter. It recomputes the stored RFQ and atomically links a quotation,
receipt and audit settlement. The continuous lifecycle is documented in
[background processing](../development/background_processing.md).

LangGraph state, live specialist custody and process-local recovery have a different scope.
A stored recovery metadata record cannot recreate trusted specialist authorship after process
loss. Queue restart recovery re-evaluates interrupted work; read-only tools/model execution
may repeat. Fencing and idempotency protect authoritative writes, not exactly-once inference.

The following contracts cover initialization, routing, delegation, concurrency, escalation,
recovery and optional telemetry. They preserve each layer's narrower responsibilities.



## Supervisor Foundation

This document describes the initialization-only foundation runner. The complete Supervisor
adds [routing](supervisor_langgraph_architecture.md), [delegation](supervisor_langgraph_architecture.md),
[escalation](supervisor_langgraph_architecture.md), and [recovery](supervisor_langgraph_architecture.md).
**Code:** `src/rfq_copilot/supervisor.py`. **Tests:** `tests/test_supervisor.py`.
**Related:** `supervisor_langgraph_architecture.md` (conceptual design),
`specialized_agent_integration.md`, `specialized_agent_integration.md` (specialist workflow).

<a id="supervisor-foundation-1-responsibility"></a>
### 1. Responsibility

The Supervisor (A0) coordinates A1–A5 through explicit LangGraph nodes. Its foundation runner:

- admits one workflow attempt from a trusted run context;
- fixes its identity, source provenance, rule provenance and budget;
- lays out the six specialist stages as `pending`;
- returns a result that says only "initialised".

It owns the workflow lifecycle, attempt identity, stage transitions, state consistency,
failure containment and budgets. It never owns business results: those stay with the
deterministic services behind A1–A5 and are produced only when an agent actually runs.

<a id="supervisor-foundation-2-topology"></a>
### 2. Topology

```
START -> trusted_entry -> initialize_supervisor -> END
```

- `trusted_entry` refuses a pre-seeded *recognised* state key (`invalid_initial_state`). It
  derives the trusted keys from the context and mints a fresh `attempt_id`
  (`secrets.token_hex(16)`).
- `initialize_supervisor` writes the phase, the pending stages and empty clarification/review
  indicators, then runs `validate_state` over the whole state.
- There are no conditional edges, no cycles and no checkpointer. `RECURSION_LIMIT = 4`.
  `asyncio.timeout(budget)` bounds every run.

`run_supervisor_foundation(context)` is the supported entry point. It takes a trusted
context and **no caller-supplied state**. It validates the context before the graph starts
(it needs the budget for the deadline) and runs the graph. It then validates the **final
state** with `validate_state` before reporting `initialized`. A supplied compiled graph that
returns another customer's identity, another RFQ or source, fabricated rules, a bad attempt
id, a forged stage or indicator, or a missing key fails closed with `invalid_state`.

<a id="supervisor-foundation-direct-graph-invocation-internal-interface"></a>
#### Direct graph invocation (internal interface)

The compiled graph is not an independently authorised entry point. When it is called
directly:

- Any input key that is a `SupervisorState` key fails with `invalid_initial_state`.
- LangGraph drops input keys that are not state keys before any node runs; they are
  *filtered, not rejected*. They never become state and grant nothing: identity comes only
  from the context. `test_security_unknown_input_keys_never_become_state` pins this.
- A dict `context` is coerced into `SupervisorContext` (LangGraph `_coerce_context`); the
  runner refuses dicts.

<a id="supervisor-foundation-3-state-supervisorstate-a-typeddict-like-conversational-agents-graphstate"></a>
### 3. State (`SupervisorState`, a TypedDict like conversational agent's `GraphState`)

| Key | Class | Writer |
|---|---|---|
| `identity: WorkflowIdentity` (customer_id, rfq_id, source_id, source_sha256) | trusted, immutable | `trusted_entry`, once |
| `attempt_id` (32 hex) | trusted, immutable | `trusted_entry`, once |
| `rules: ((version, fingerprint), ...)` | trusted provenance | `trusted_entry`, once |
| `budget: Budget` (timeout ≤ 390 s, recursion limit 4) | trusted execution metadata | `trusted_entry`, once |
| `phase: "initialized"` | mutable workflow state | `initialize_supervisor` |
| `stages: (StageState, ...)` (stage, agent, status, reason) | mutable workflow state | `initialize_supervisor` |
| `clarifications`, `review_reasons` (empty in the foundation runner) | mutable indicators | `initialize_supervisor` |

The table classifies everything else too:

- **Validated business evidence**: none in state. The typed handoff contracts are the
  existing agent result types (`STAGE_PLAN[*].produces`).
- **Internal sensitive data** (A4 costs, raw evidence): none in state. L06 still applies to
  `RFQWorkflowAssessment`.
- **Customer-safe data**: none produced. There is no `customer_view` on the foundation result.
- **Ephemeral dependencies** (principal, executor, customer text, rule documents): context
  only.

All values are frozen Pydantic models or tuples, so a state cannot be mutated in place or
shared mutably between runs. `TypeAdapter(SupervisorState)` with `extra="forbid"` does the
type check of a state *value* (for example a final state). It rejects unknown keys, missing
keys and bad statuses. This is separate from graph input, where LangGraph filters unknown
keys (§2).

<a id="supervisor-foundation-ownership-owned_update-validate_state"></a>
#### Ownership (`owned_update`, `validate_state`)

- Each node may write only the keys listed in `_WRITES`. Trusted keys can be written once
  only. Any other write raises `ownership_violation`. Specialists never write foundation state directly.
- `validate_state(state, context)` re-derives identity, rules and budget from the context
  and compares them. It requires the stage plan in order. It enforces the **foundation
  state**: every stage `pending` with `reason=None`, and empty `clarifications` and
  `review_reasons`. No validated evidence exists, so a status, reason or indicator would be
  a forgery. The execution layer records only stages that ran and produced validated
  results, each with its own evidence check.

<a id="supervisor-foundation-4-trusted-runtime-context-supervisorcontext"></a>
### 4. Trusted runtime context (`SupervisorContext`)

This uses the conversational agent `TurnContext` pattern: `StateGraph(..., context_schema=...)` with
`Runtime[...]` in nodes (LangGraph 1.2.20, locked). The context is a frozen dataclass. Its
dependency fields are `repr=False`.

| Field | Use |
|---|---|
| `principal: Principal` | Verified caller. `identity.customer_id` comes from here only. |
| `execute: ToolExecutor` | The principal-bound `graph.mcp_executor`. It is present but is not called by the foundation runner. |
| `rfq_id`, `source_id` | Trusted application ids (`Identifier`) |
| `source_text` | Untrusted customer text. It is hashed (A1's `sha256(text.encode())`) and never stored. |
| `manufacturing_rules`, `pricing_rules`, `approval_rules` | `pinned_rules` results, checked with A4's `_trusted` (mfg-v1.0, price-v1.0) and A5's `_approval_trusted` (approval-v1.2) |
| `timeout_seconds` | ≤ `agent_integration.DEFAULT_TIMEOUT` (390 s) |

There is no `customer_id`, role, permission, token or hash field to override. Nothing from
the context is written to state or to the result except the derived ids, hash and rule
fingerprints.

<a id="supervisor-foundation-5-stage-plan-stage_plan"></a>
### 5. Stage plan (`STAGE_PLAN`)

This is a code-controlled, immutable tuple. It is the allow-list that routing and delegation may
choose from, and no model can extend it.

| Stage | Agent | Entry (existing) | Produces | Consumes |
|---|---|---|---|---|
| intake_extraction | A1 | `intake.run_intake` | `IntakeResult` | — |
| intake_validation | A1 | `intake_validation.validate_intake` | `IntakeValidation` | intake_extraction |
| manufacturing | A2 | `evaluate_manufacturing_feasibility` | `ManufacturingAssessment` | intake_validation |
| supply | A3 | `evaluate_supply` | `SupplyAssessment` | manufacturing, intake_extraction |
| pricing | A4 | `evaluate_pricing` | `PricingAssessment` | manufacturing, supply |
| quotation | A5 | `evaluate_quotation_review` | `QuotationReviewAssessment` | manufacturing, supply, pricing |

Tests pin the plan to the specialist workflow baseline. They check the same stage names and agents
(`agent_integration._AGENT`) and the same functions the baseline calls. Each `produces` type
must be exactly the `RFQWorkflowAssessment` field (`key`) it fills. The plan references
functions; it does not copy or call them.

<a id="supervisor-foundation-6-output-boundaries"></a>
### 6. Output boundaries

| Output | Contents |
|---|---|
| `SupervisorFoundationResult` | scope `supervisor_foundation`, status `initialized`/`error`, fixed error code, identity, attempt id, rule fingerprints, pending stages. `evaluated`, `draft_available`, `approved` and `released` are `Literal[False]`; `human_approval_required` is `Literal[True]`. |
| `summary()` | the same as JSON. No amount, evidence, text or credential exists to leak. |
| `RFQWorkflowAssessment`, `summary()`, `customer_view()` | specialist evaluation contracts, reused by execution |

A model validator keeps status and contents consistent. `initialized` requires no error and
the full identity, attempt id, rules and stages. `error` requires a code and carries no
identity, attempt id, rules or stages. The public fields are unchanged.

<a id="supervisor-foundation-7-failure-handling"></a>
### 7. Failure handling

- Fixed codes: `invalid_initial_state`, `untrusted_context`, `unauthenticated`,
  `invalid_identity`, `invalid_source`, `invalid_budget`, `*_rules_untrusted`,
  `ownership_violation`, `invalid_state`, `execution_limit`, `internal_error`.
  Exception text never surfaces.
- There are no retries: each node runs once.
- `asyncio.CancelledError` propagates.
- A graph with a checkpointer is refused (`ValueError`).
- Exceptions raised inside a node reach the caller as the same `SupervisorError` object, with
  a LangGraph note attached (`During task with name ...`, in `__notes__`). The code stays in
  `error.code` and `args == (code,)`. Tests assert those directly, not with a regex over the
  rendered message, which pytest extends with the notes.

<a id="supervisor-foundation-7a-import-boundary"></a>
### 7a. Import boundary

`supervisor.py` takes `ToolExecutor` from `intake_validation`, the same neutral alias specialist workflow
uses (equal to `graph.ToolExecutor`). It does not import the conversational agent graph module, so
`test_conversations.py::test_no_application_entry_point_imports_the_graph` holds unchanged.

<a id="supervisor-foundation-8-checkpoint-compatibility"></a>
### 8. Checkpoint compatibility

There is no checkpointer, and none may be attached through `run_supervisor_foundation`. The
state holds Pydantic objects, which are outside `graph.SAFE_SERDE`'s msgpack allow-list, so
**Foundation state is not checkpoint-safe as written**. The conversational agent’s `Conversations` is untouched.
The [recovery layer](supervisor_langgraph_architecture.md) persists a validated JSON-only metadata
projection. Evidence and costs are never persisted.

<a id="supervisor-foundation-9-design-decisions"></a>
### 9. Design decisions

- **TypedDict + frozen Pydantic values.** LangGraph channels stay plain, as in conversational agent. The
  values are immutable and strictly validated, following the agents' `_STRICT` convention.
  A Pydantic state *schema* was rejected: LangGraph would rebuild it between nodes and coerce
  inputs, which blurs the "no input state" rule.
- **Context, not state, for authority.** This repeats shared workflow: nothing that grants access
  is ever serialisable state.
- **Supervisor-minted attempt id, and refusal of seeded state.** An attempt cannot be reused
  or forged. Resume semantics belong to the recovery layer.
- **Plan over registry objects.** One tuple of `StageSpec` rows describes the specialists.
  There are no adapter classes, no second agent framework and no wrapper around
  `evaluate_rfq_workflow`.
- **Manufacturing rules are manifest-checked at entry.** This is stricter than the specialist workflow
  preflight, which leaves it to the agents. Delegation must pass real `pinned_rules` results.

<a id="supervisor-foundation-10-foundation-boundaries"></a>
### 10. Foundation boundaries

- The executor is a closure, so its binding to `principal` is the caller's responsibility
  (the same as specialist workflow). MCP still re-authenticates every call.
- RFQ-to-customer ownership is not checked here (there is no database access). Each
  customer-scoped MCP tool enforces it when the specialists run.
- Direct graph invocation filters unknown input keys rather than rejecting them, and coerces
  a dict context (§2). It is internal only. The runner refuses dicts, and the principal must
  always be a real `Principal` instance.
- The conversational agent boundary test matches source text. A comment or docstring naming the graph
  module's dotted path would trip it, which is why the module docstring refers to
  `graph.mcp_executor` and not the full path.

<a id="supervisor-foundation-11-supervisor-routing"></a>
### 11. Supervisor routing

Routing builds on this foundation without changing it. See `supervisor_langgraph_architecture.md`. The
foundation graph, `validate_state`, `run_supervisor_foundation` and their contracts are
unchanged. Both graphs share `_foundation(recursion_limit, schema)`, and both runners
share `_settle`.


## Supervisor Routing

The routing-only runner selects a stage without executing it. The complete
[delegation runner](supervisor_langgraph_architecture.md) attaches specialist execution to these same edges.
**Code:** `src/rfq_copilot/supervisor.py` (routing section). **Tests:**
`tests/test_supervisor_routing.py`. **Builds on:** `supervisor_langgraph_architecture.md`.
**Gating authority:** `agent_integration.py`.

<a id="supervisor-routing-1-routing-and-delegation-boundaries"></a>
### 1. Routing and delegation boundaries

`decide(outcomes)` selects the next stage or stop route. `record_outcome(...)` validates
and records typed results; `validate_routing_state(...)` enforces state consistency.
`build_routing_graph()` and `run_supervisor_routing(context)` expose the routing-only
runner, which stops at `await_delegation`. The full workflow attaches a delegation node
through `attach_routing(delegate=...)`, retaining the same routing edges and validation.

<a id="supervisor-routing-2-topology"></a>
### 2. Topology

```
START -> trusted_entry -> initialize_supervisor -> route_supervisor
route_supervisor --(route.action)--> await_delegation        (delegate)  -> END
                                  -> clarification_required  (clarification) -> END
                                  -> review_required         (review)    -> END
                                  -> blocked                 (blocked)   -> END
                                  -> workflow_failed         (failed)    -> END
                                  -> routing_complete        (complete)  -> END
Supervisor delegation: delegate -> <delegation node> -> route_supervisor  (the only back-edge)
```

- `trusted_entry` and `initialize_supervisor` are the Supervisor foundation nodes, built by one shared
  `_foundation(recursion_limit, schema)`. The foundation runner’s `build_supervisor_graph` is unchanged.
- `route_supervisor` has only conditional outgoing edges, through
  `add_conditional_edges(..., DESTINATIONS)` with an explicit map. No unconditional edge sits
  beside them.
- Each terminal node checks that the route really selected it (`invalid_route` otherwise)
  and writes nothing.
- The node names are code constants. Neither the route nor any input string names a node.

<a id="supervisor-routing-3-routing-state-contract-routingstate"></a>
### 3. Routing-state contract (`RoutingState`)

`RoutingState` extends The foundation runner’s `SupervisorState` (TypedDict, `total=False` for the new keys,
`extra="forbid"`):

| Key | Class | Writer |
|---|---|---|
| Supervisor foundation keys | unchanged (trusted identity, attempt, rules and budget are write-once) | unchanged |
| `outcomes: (StageOutcome, ...)` | validated stage outcomes, a prefix of `STAGE_PLAN` | `record_outcome` only |
| `route: RouteDecision` | Supervisor routing metadata | `route_supervisor` only |
| `stages` | derived from `outcomes`: completed / stopped / error / pending | `record_outcome` |
| `clarifications`, `review_reasons` | copied from the route | `route_supervisor` |

- **`StageOutcome`** holds stage, agent, attempt id, the agent's own status, the decision
  (specialist workflow vocabulary), a reason code and clarification/review codes. Each field is bounded
  (codes ≤ 128 characters, ≤ 1024 codes). It holds **no amount, evidence or customer text**.
- **`RouteDecision`** holds action, next stage or stopping stage, outcome, reason and codes.
  `human_approval_required` is `Literal[True]`; `approved` and `released` are `Literal[False]`.

`validate_routing_state` checks:

- the trusted keys re-derived from the context;
- outcomes in plan order and bound to the attempt id;
- every outcome but the last is `continue`;
- `stages` exactly as derived from the outcomes;
- the route either current (`decide(outcomes)`) or one record old;
- the indicators equal the route's.

The foundation runner’s `validate_state` guards initialisation (all stages pending,
no reasons, no indicators).

<a id="supervisor-routing-4-trusted-evidence-how-an-outcome-is-earned"></a>
### 4. Trusted evidence: how an outcome is earned

`record_outcome(state, context, result, prior)`:

1. Validates the routing state.
2. Requires the current route to be `delegate` and not yet answered (`invalid_route`). The
   stage comes from the route, never from the caller, so a stage cannot be recorded out of
   order, twice, or after a stop.
3. Requires `prior` to be exactly the recorded stages' results, each of its exact type
   (`invalid_state`).
4. Derives the outcome from the result:
   - `type(result) is spec.produces`, else `unexpected_result_type`;
   - the status is looked up in specialist workflow's `_NEXT`, else `unexpected_stage_status`;
   - specialist workflow's `_handoff_error`: RFQ/source identity, and for A2 exactly A1's specifications;
   - A1 extraction's `source_sha256` must equal the attempt's hash (`source_mismatch`);
   - the rule version and fingerprint (mfg for A1v/A2, price for A4 when `priced`, approval
     for A5) must equal the attempt's trusted rules (`rule_mismatch`);
   - a `draft_ready_for_review` without a customer view gives `draft_unavailable`;
   - clarification codes, review codes and the reason come from specialist workflow's `_clarifications`,
     `_reviews` and `_reason`.
5. A failed check is recorded as an **`error` outcome**, so the route becomes `failed` at that
   stage. Nothing after it is ever selected.

Domain evidence is still verified by the downstream agents, as in specialist workflow. A4's gate verifies
A3's evidence, and A5 replays A4's. The router does not re-implement those checks.
Fingerprints are consistency checks, **not signatures**.

<a id="supervisor-routing-5-transition-policy"></a>
### 5. Transition policy

- The next stage is `STAGE_PLAN[len(outcomes)]`, and only when every earlier outcome is
  `continue` and its `consumes` stages are satisfied. Anything else is `invalid_state`.
- The stop mapping derives from specialist workflow's `_NEXT`:

| Stage decision | Route |
|---|---|
| `continue` | `delegate` the next stage |
| `clarification_required` | `clarification` |
| `requires_review` | `review` |
| `not_quotable`, `not_evaluated` | `blocked` |
| `error` | `failed` |
| `draft_ready_for_review` (A5) | `complete`: a non-binding draft that awaits human approval |

- specialist workflow's advisory progressions are preserved because they come from `_NEXT`:
  - A3 `constrained` continues to pricing;
  - A4 `requires_review` continues to A5, which returns a no-amount review.
- There are no backward routes and no repeats. Clarification and continuation use the separate escalation and recovery layers.

<a id="supervisor-routing-6-loop-safety-and-budgets"></a>
### 6. Loop safety and budgets

- **Only back-edge:** delegation node → `route_supervisor`. `route_supervisor` raises
  `no_progress` unless this is its first run or a stage was recorded since its last run. A
  delegation node that records nothing therefore stops the run; it is never re-selected.
- **Recursion limit:** the routing graph uses `ROUTING_RECURSION_LIMIT = 18` (16 steps at
  most with six delegations). The foundation keeps 4. `Budget.recursion_limit` is
  `Literal[4, 18]` and matches the graph that admitted the attempt.
- **Deadline:** 390 s (`DEFAULT_TIMEOUT`), the same as the foundation.
- No retries, no LLM call, no MCP call. Cancellation propagates.

<a id="supervisor-routing-7-security-boundaries"></a>
### 7. Security boundaries

- Authority still comes only from `SupervisorContext`. Routing never reads a principal, role
  or permission from state.
- `run_supervisor_routing(context)` takes no state and no graph. `_route(graph, context)` is
  internal: a supplied graph could carry self-consistent outcomes that no delegation node
  produced, and state validation proves consistency, not provenance.
- When the graph is invoked directly, a recognised pre-seeded key (including `outcomes` and
  `route`) is refused with `invalid_initial_state`. Unknown keys such as `next_agent` or
  `approved` are filtered by LangGraph and grant nothing.
- `SupervisorRoutingResult` holds identity, attempt, rule fingerprints, stage statuses and
  the route. It holds no amount, evidence or text. It is not an assessment and has no
  `customer_view`.

<a id="supervisor-routing-8-foundation-and-specialist-workflow-compatibility"></a>
### 8. Foundation and specialist-workflow compatibility

The foundation retains its initialization-only result and recursion limit of four. Both
runners share `_settle`; routing reuses the foundation result validator. Specialist handoff
checks use `agent_integration._handoff_error`, avoiding a second implementation. Supervisor
code does not import the conversational graph module.

<a id="supervisor-routing-9-delegation-responsibilities"></a>
### 9. Delegation responsibilities

- A delegation node that calls `STAGE_PLAN[stage].entry` through the principal-bound
  executor with the per-stage budget, keeps typed results in a per-run store (never in state
  or a checkpoint), and calls `record_outcome`.
- Context additions: `horizon_start` and the intake graph.
- A delegation-enabled public runner and graph.
- Binding A1 validation to the recorded extraction in the same attempt. The router binds the
  extraction hash, but `IntakeValidation` carries no hash (L03).


## Supervisor Delegation

**Code:** `src/rfq_copilot/supervisor.py` (delegation section).
**Tests:** `tests/test_supervisor_delegation.py`.
**Builds on:** `supervisor_langgraph_architecture.md` and `supervisor_langgraph_architecture.md`.

<a id="supervisor-delegation-1-topology"></a>
### 1. Topology

```
START -> trusted_entry -> initialize_supervisor -> route_supervisor
route_supervisor --delegate--> delegate_stage -> route_supervisor      (the only back-edge)
route_supervisor --> clarification_required | review_required | blocked |
                     workflow_failed | routing_complete -> END
```

- **Builder:** `_execution_graph(custody)` is the Supervisor routing graph built with
  `attach_routing(delegate="delegate_stage")`. There is no `await_delegation` node.
- **Public runner:** `run_supervisor_workflow(context) -> SupervisorExecutionResult`. It takes
  only the trusted context: no state, no stage results, no graph.
- **Inspection only:** `build_execution_graph()` returns the same graph shape for topology
  inspection.
- **Unchanged:** `build_supervisor_graph`, `run_supervisor_foundation`, `build_routing_graph`
  and `run_supervisor_routing`.
- **Recursion limit:** stays 18. A full run takes 16 steps: entry, init, route, six
  delegate+route pairs, and a terminal.

<a id="supervisor-delegation-2-trusted-dependencies"></a>
### 2. Trusted dependencies

`SupervisorContext` includes two optional fields used by delegation; foundation and routing callers may omit them.
They come from the trusted caller exactly as specialist workflow receives them:

- `intake_graph: CompiledStateGraph | None`, with `repr=False`;
- `horizon_start: date | None`.

`delegate_stage` requires both (`missing_dependency`) before the first model call. The
foundation and routing graphs never read them.

<a id="supervisor-delegation-3-per-attempt-custody-_custody"></a>
### 3. Per-attempt custody (`_Custody`)

- The runner creates one custody object per run, and builds a fresh compiled graph whose
  `delegate_stage` closes over it. Two concurrent runs therefore never share custody, a
  graph, or a result. There is no module-level result store.
- `bind(state)` binds custody to the attempt id and identity on first delegation and fixes
  the deadline. Afterwards, any other attempt's state raises `invalid_state`.
- `keep(stage, result)` stores the object a specialist returned, only after `record_outcome`
  accepted it (decision ≠ `error`). An accepted result is never replaced.
- `held(outcomes)` returns a copy holding exactly the results of the recorded stages. A
  mismatch raises `invalid_state`.
- The runner's `finally` clears custody, after success, failure, timeout or cancellation.
- Custody is never graph state, never a checkpoint, and never caller input. The typed
  results (A4 costs, raw evidence) live only there.

<a id="supervisor-delegation-4-l03-the-extraction--validation-chain-of-custody"></a>
### 4. L03: the extraction → validation chain of custody

Within one Supervisor attempt:

1. A1 extraction runs on `context.source_text` with the trusted `source_id`, `rfq_id` and
   intake graph. `record_outcome` checks the returned object's type, status, identity and
   `source_sha256 == sha256(source_text)` (computed at entry). Only then is the object kept.
2. A1 validation is invoked with **that kept object** (identity, not an equal copy), the same
   `source_text`, the context's manufacturing rules and the principal-bound executor.
   `validate_intake` re-checks the hash itself. `record_outcome` checks type, readiness,
   identity and manufacturing rule provenance before keeping the result.
3. A2 receives the kept `IntakeValidation`. A3 receives the kept `IntakeResult`, the same
   object that went to validation.
4. Any rejected result is recorded as an `error` outcome and never kept. Routing then fails
   at that stage, and no later agent runs.

**Why this establishes origin, not just equal content.** Results enter custody only as the
return value of this run's own call to the agent function. Nothing else can put them there:

- no runner parameter, no state key (seeded state is refused), no shared store;
- custody is unreachable outside the run's closure.

An object from another attempt therefore cannot be handed to validation or downstream, even
if its ids and source hash are identical. The tests prove this with object identity (`is`),
not hashes.

**Residual boundary.** The agent functions are trusted in-process code, looked up on their
modules at call time as specialist workflow does. If such a function itself returned an object produced
earlier, that is outside this boundary. This is not cryptographic authentication of
independently transmitted evidence; that is L04/L21.

<a id="supervisor-delegation-5-stage-execution-_arguments-_stage_timeouts"></a>
### 5. Stage execution (`_arguments`, `_STAGE_TIMEOUTS`)

| Stage | Call (specialist workflow signature) |
|---|---|
| A1 extraction | `(intake_graph, source_text, source_id, rfq_id)` |
| A1 validation | `(kept extraction, source_text, rules=manufacturing_rules, catalog=execute)` |
| A2 | `(kept validation, execute)` |
| A3 | `(kept manufacturing, execute, horizon_start, extraction=kept extraction)` |
| A4 | `(kept manufacturing, kept supply, execute, pricing_rules)` |
| A5 | `(kept m, s, p, execute, pricing_rules, approval_rules)` |

- **Allowed stages:** the stage comes only from `route.next_stage`, through the code-controlled
  `STAGE_PLAN`.
- **Timeout per stage:** `min(stage default, remaining)` against one deadline, which is the
  attempt budget (≤ 390 s) measured from the first delegation. No stage starts once the
  deadline has passed; that is recorded as a `timeout` outcome with status `not_run`.
- **Hard backstop:** the runner's `asyncio.timeout` gives `execution_limit`.
- **Agent exceptions:** recorded as `internal_error` (status `error`). Their text never
  surfaces, and the agent is not retried.
- **Cancellation:** propagates.

<a id="supervisor-delegation-6-result-supervisorexecutionresult"></a>
### 6. Result (`SupervisorExecutionResult`)

It holds:

- status (`executed` / `error`) and a fixed error code;
- identity, attempt and rule fingerprints;
- stage statuses and `outcomes`: what ran, with each agent's own status;
- the route: `complete`, `review`, `clarification`, `blocked` or `failed`, with reason and
  codes;
- `draft_available`, and A5's `customer_view()` as `draft`.

`draft` is excluded from `summary()`. `human_approval_required` is fixed `True`; `approved`
and `released` are fixed `False`. Nothing is persisted, numbered or sent. A draft exists
exactly when specialist workflow's would: A5 accepted, status in `_DRAFT`, and a customer view present.

<a id="supervisor-delegation-7-specialist-workflow-parity"></a>
### 7. specialist workflow parity

All 11 Supervisor routing scenarios run through `run_supervisor_workflow` and through
`evaluate_rfq_workflow` with fresh identical fakes. They must agree on:

- the route action and stopping stage;
- per-stage statuses;
- the stage invocation sequence, once each;
- the exact executor tool calls;
- clarification codes, review codes and the error reason;
- `draft_available` and `customer_view()`.

The only intentional difference is the shape of the result contract.

<a id="supervisor-delegation-8-parallel-execution-parallel-part-checks"></a>
### 8. parallel execution parallel part checks

Inside the manufacturing stage, A2 may check independent parts concurrently, bounded by the
trusted `SupervisorContext.part_concurrency`. The stage still records one outcome. See
`supervisor_langgraph_architecture.md`.

<a id="supervisor-delegation-9-escalation-dispositions"></a>
### 9. escalation dispositions

Every result from `run_supervisor_workflow` carries a `disposition`. It is derived only from
the settled route (`decide(outcomes)`) and this attempt's custody codes. See
`supervisor_langgraph_architecture.md`.


## Supervisor parallel execution (L01)

Scope: **per-part concurrency inside A2 (Manufacturing Feasibility)**. The six Supervisor
stages (A1 extraction → A1 validation → A2 → A3 → A4 → A5) stay a strict sequential chain.

<a id="supervisor-parallel-1-dependency-audit"></a>
### 1. Dependency audit

| Operation | Inputs / upstream evidence | Shared mutable state | DB / MCP | Ordering | Concurrent-safe | Selected |
|---|---|---|---|---|---|---|
| A1 extraction | source text, intake graph | none | one model call | first | n/a (single call per RFQ, must stay single) | no |
| A1 validation | the extraction from this attempt | none | `search_materials` per material | after extraction | not one work item per part; catalog lookups feed per-part findings that one validator builds | no |
| **A2 part check** | one `PartSpecification` from a ready `IntakeValidation`, pinned `mfg-v1.0` fingerprint | **none**: each call gets its own token, `httpx` client, MCP session and server-side `READ ONLY` snapshot | `check_manufacturing_feasibility`, read-only (`view_catalog`) | after validated intake; **no order between parts** | **yes** | **yes** |
| A3 inventory / capacity / lead time | all of A2's verified parts, horizon, extraction | stock and capacity are **shared by all parts** (one stock line per material and thickness, one capacity plan) | 3 RFQ-level read-only calls | lead time is checked against inventory and capacity snapshots; documented call order | not per part: splitting by part would double-count shared stock and capacity | no |
| A4 costs / price | A2 + A3, trusted pricing rule | quote-level totals, setup, margin | 2 RFQ-level read-only calls | price is checked against the costs | not per part: the quote total and line pricing come from one call under quote-level rules | no |
| A5 quotation | A2 + A3 + A4 | none | 1 call; replays A4 offline | last | single call | no |

Why A2 is independent: `_check(i, spec, execute, intake)` reads only its own part and the
immutable `intake`. Its result (`PartFeasibility`) depends on no other part, and nothing is
written. The RFQ status is a pure fold over all part statuses (`_PRECEDENCE`), and it does
not depend on order. The MCP client builds a new `httpx.AsyncClient` and SDK session per call
(`mcp/client.py`). The integration asks the credential provider per call
(`api/mcp_integration.py`). The server opens a fresh `READ ONLY` snapshot per call
(`mcp/manufacturing.py`). Nothing non-reentrant is shared, and the principal-bound executor
is reused unchanged (no copied credentials).

Duplicate `part_id`s are not a hidden dependency for A2. Each part is still checked alone, as
it is sequentially, and A3's gate still refuses duplicates.

<a id="supervisor-parallel-2-topology"></a>
### 2. Topology

```text
delegate_stage (Supervisor, one node, unchanged)
   └─ a2.evaluate_manufacturing_feasibility(intake, execute, max_concurrency=ctx.part_concurrency)
        ├─ gate: intake ready, every part has a specification, ≤ MAX_PARTS, executor present
        ├─ limit == 1 or one part → the unchanged sequential loop
        └─ else _parallel:
             asyncio.timeout(stage budget)
               └─ TaskGroup ── Semaphore(limit) ── _check(part 0) ─┐
                                                 ── _check(part 1) ─┼─> collected[(position, result)]
                                                 ── _check(part n) ─┘
             _join(intake.parts, collected) → tuple[PartFeasibility] in Intake order
        → ManufacturingAssessment (same model, same status fold)
   → record_outcome (one A2 outcome) → route_supervisor
```

This is a **bounded async orchestration inside the specialist**, not a LangGraph `Send`
fan-out. Reasons:

- The work items are A2-internal tool calls. A2 already owns their evidence checks
  (`_forged`, rule pins) and the result contract.
- `Send` workers would put `PartSpecification`s and raw tool output into graph channels.
  Supervisor delegation keeps typed results out of graph state (custody, L06).
- A graph fan-out would also have needed a second record path for one stage.

The Supervisor still records **exactly one** validated outcome per stage.

<a id="supervisor-parallel-3-trusted-worker-contract"></a>
### 3. Trusted worker contract

- Work-item identity is the part's **position in `intake.parts`**: stable, derived from the
  validated structure, never customer text. `PartValidation.index` is not enforced unique by
  its model, so position is the key, and `index`, `part_id` and `specification` are checked.
- Each worker has only its part's specification, the call's own `intake` (pinned rule
  version and fingerprint) and the caller's principal-bound executor. The tool input
  (`PartCheckInput`) is unchanged and carries no identity, scope or concurrency setting.
- Workers live in the local `collected` list of one `evaluate_manufacturing_feasibility`
  call. There is no global store, and no state, checkpoint or custody write.

<a id="supervisor-parallel-4-concurrency-limits"></a>
### 4. Concurrency limits

| Setting | Where | Value |
|---|---|---|
| `a2.MAX_CONCURRENCY` | code constant | 4 (hard cap; `ValueError` above it) |
| `evaluate_manufacturing_feasibility(max_concurrency=)` | keyword argument, trusted caller | default **1** (specialist workflow and direct callers stay sequential) |
| `SupervisorContext.part_concurrency` | trusted run context, never state or input | default **2**; anything not an `int` in 1..4 → `invalid_budget` before any agent runs |

`bool`, `float` and `str` are refused (`type(x) is int`).

Cross-RFQ load: each worker holds one HTTP connection and one server snapshot session for
one call, so a run adds at most `part_concurrency` connections. There is no process-global
semaphore: it would bind to one event loop, and it is not needed while the per-run cap is
≤ 4. Throttling many RFQs belongs to the deployment layer (L33/L34). A1's single model call
per RFQ is unchanged. No model is called in A2.

<a id="supervisor-parallel-5-fan-in-_join"></a>
### 5. Fan-in (`_join`)

It refuses, by raising `_JoinError`, which makes the stage result
`status="error", error="invalid_evidence", parts=()` with no partial result:

- a missing item, an empty batch, or an unknown position;
- a duplicate position;
- a result that is not a `PartFeasibility`;
- a result whose `(index, part_id, specification)` is not that part's (another part's,
  another attempt's or another RFQ's);
- a status outside the part statuses, or `error` status and error code that disagree.
- an `error` part that carries evidence (`_failed` never attaches any);
- a non-error part without `ToolEvidence`, or one that is not exactly what `_assess` derives
  from its own evidence output, the arguments digest recomputed from this part's
  specification, and this `intake`. A relabelled status, an altered explanation, another
  part's evidence or a foreign digest is refused. No manufacturing rule is re-implemented:
  the join reuses A2's own derivation (`_forged`, rule pins, status map).

These identity and evidence checks are **consistency checks, not cryptographic proof** of
provenance across attempts. A result that is internally consistent and equal to this part's
is accepted whatever object produced it. Attempt isolation rests on the per-call local
collection and per-run custody, and authenticity of evidence is L04/L21.

Then the parts are rebuilt in Intake order, and the unchanged `_PRECEDENCE` fold gives the
RFQ status. Completion order never reaches the result. Per-part rule-provenance checks
(`_forged`, pinned version and fingerprint → `requires_review`) run in each worker exactly as
sequentially. Supply's gate re-checks the joined result (`_bound`, rule pins, duplicate ids).

<a id="supervisor-parallel-6-failure-timeout-and-cancellation"></a>
### 6. Failure, timeout and cancellation

| Event | Behaviour |
|---|---|
| Tool error for one part (`McpCallError`, invalid output, executor fault) | That part is `error` with its code; siblings still run. This is the **established sequential semantics** (A2 always evaluated every part), so it is preserved rather than cancelled. RFQ status `error`, and the Supervisor stops at A2. |
| Unexpected fault (an exception escaping `_check`) | The TaskGroup cancels and **awaits** every sibling, then raises `ExceptionGroup`. The Supervisor records `internal_error` with no text, and nothing runs after A2. |
| Stage timeout (`min(stage default, attempt remaining)`) | Every worker is cancelled and awaited before `TimeoutError`. Finished parts are kept and unfinished parts are `timeout`, exactly as the sequential loop does. |
| Worker swallows cancellation and returns | It is awaited (no orphan). Its result is **dropped** (`Task.cancelling()`), and the part is `timeout`. |
| External cancellation | Propagates after the children are cancelled and awaited. The Supervisor runner clears custody. |
| Retries | None. Each part is called once. |

Boundary: a worker that never yields after cancellation would block the TaskGroup. Real
calls are bounded by the MCP client's own `anyio.fail_after` (≤ 300 s, default 30 s) and the
390 s attempt budget.

<a id="supervisor-parallel-7-custody-l03-and-security"></a>
### 7. Custody (L03) and security

Unchanged: `_Custody` per run, bound to one attempt, keeping original typed results with no
replacement, and cleared on every exit. A2 workers never see custody. The stage receives the
validation object held for this attempt and returns one `ManufacturingAssessment`, which
`record_outcome` validates and `delegate_stage` keeps. A1 validation and A3 still get the
same extraction object.

Workers cannot touch identity, attempt id, source hash, rule fingerprints, outcomes, the
route, or approval and release flags. Those are Supervisor state written only by their
owning nodes. `approved` and `released` stay `Literal[False]`.

<a id="supervisor-parallel-8-parity"></a>
### 8. Parity

The sequential path is the reference: `max_concurrency=1` (A2) and `part_concurrency=1`
(Supervisor), plus specialist workflow `evaluate_rfq_workflow`, which still calls A2 sequentially. Tests
compare the full typed `ManufacturingAssessment` and every kept stage result
(`SupplyAssessment`, `PricingAssessment`, `QuotationReview`) with specialist workflow's. They also
compare the whole `SupervisorExecutionResult` apart from attempt ids, the customer view, and
the multiset of executor calls.


## Supervisor clarification and escalation

Module: `src/rfq_copilot/escalation.py`. Integration: `supervisor._execute` (trusted `build`)
and `SupervisorExecutionResult.disposition` / `customer_outcome()`.

<a id="supervisor-escalation-1-authority-and-data-flow"></a>
### 1. Authority and data flow

```text
A1..A5 results ─> record_outcome ─> StageOutcome ─> decide() ─> RouteDecision
                                                         │
             custody (this attempt only) ─ codes ─┐      │
                                                   v      v
                     build(): esc.for_result(route, None, draft?, A2 findings, A5 unevaluated)
                                                   │
                                     SupervisorDisposition (internal, codes)
                                                   │
                                      customer_view() -> CustomerOutcome (fixed text)
```

- `decide()` stays the only router, and no node was added. The disposition is a pure,
  one-to-one reading of the settled route. `SupervisorExecutionResult` now refuses a `route`
  that is not `decide(outcomes)`, and a `disposition` that is not
  `for_result(route, error, draft_available, its findings, its unevaluated codes)`.
- The only inputs not on the result are two code tuples that `build` reads from this
  attempt's custody:
  - A2's finding codes of its infeasible parts (only for a `not_quotable` stop);
  - A5's `unevaluated_review_conditions` (only when a draft exists).

  Both are checked against the agents' own vocabularies (`a2._KNOWN`; A5's
  `CUSTOMER_CONDITIONS` plus `borderline_bend_radius`).
- **Authenticity (escalation control).** `run_supervisor_workflow` is the authenticated construction
  boundary: it builds `blocking_findings` and `unevaluated_conditions` from this attempt's
  custody, and custody is cleared afterwards. A serialized result is consistency-checked, not
  authenticated. The model independently enforces:
  - the route equals `decide(outcomes)`, and every other disposition field follows from it;
  - a draft carries exactly one of A5's two unevaluated-condition shapes
    (`A5_UNEVALUATED`: account status and credit limit always, plus `borderline_bend_radius`
    optionally), so the unchecked customer conditions can never be removed, reordered or
    replaced;
  - `not_quotable` carries at least one A2 finding, unique, in A2's vocabulary, and never a
    `MISSING_*` code.

  It **cannot** tell, after serialization, *which* allowed A2 finding produced the stop, or
  whether A5 really added the optional bend condition. Those are tested as documented limits.
  Signed evidence is L04/L21/L26.
- `status="executed"` (technical: the run settled) and the category (business) stay
  separate. There is no success flag.

<a id="supervisor-escalation-2-categories"></a>
### 2. Categories

| Route | Route outcome | Category | Customer outcome |
|---|---|---|---|
| `clarification` | `clarification_required` | `clarification_required` | `information_needed` |
| `blocked` | `not_quotable` (A2 infeasible) | `not_quotable` | `not_quotable` |
| `blocked` | `not_evaluated` | `not_evaluated`: could not be evaluated, **not** an impossibility | `unable_to_evaluate` |
| `review` | `requires_review` | `review_required` | `under_review` |
| `complete` | `draft_ready_for_review` | `draft_ready_for_approval` | `draft_pending_approval` |
| `failed` | `error` | `technical_failure` | `technical_issue` |
| (no route: run failed before settling) | — | `technical_failure`, `stage=None` | `technical_issue` |

The flags `clarification_required`, `blocked`, `review_required` and `technical_failure` are
validated against the category. Constant fields: `automatic_continuation=False`,
`human_approval_required=True`, `approved=False`, `released=False`.

<a id="supervisor-escalation-3-clarification"></a>
### 3. Clarification

- Codes are the route's (specialist workflow `_clarifications`): Intake ids `"<part>:<field>:<kind>"`,
  A2 `"<part>:<spec field>:missing"`, and A3
  `"0:requested_delivery_date:requested_delivery_date_unverified"`.
- Questions come from fixed templates per kind:
  - `missing`, `ambiguous`, `contradiction`, `ownership_unverified` and `invalid` combine
    with an allow-listed field label (Intake's `_LABELS`, plus A2 field names; dimensions
    say "in millimetres");
  - `unknown_operation`, the delivery-date code and `no_parts` have fixed sentences.
- A part is named only by its validated index, never by its customer-written label.
- A code that does not fully match the pattern, or has an unknown field or kind, gets
  `GENERIC_QUESTION`.
- Questions are deduplicated, kept in the route's order, and capped at `MAX_QUESTIONS` (25).
- Intake's own `Clarification.question` is **not** reused, because it quotes customer text.
- Nothing runs downstream (the route stopped). Nothing is resumed: answering is a new attempt
  that re-validates everything (recovery owns interruption and resumption).

<a id="supervisor-escalation-4-blocking"></a>
### 4. Blocking

- `not_quotable` exists only at `manufacturing`. `blocking_findings` holds A2's codes,
  internal only. `blockers` holds customer-safe categories: material, finish, thickness,
  part type, tolerance, bend radius, operation, outside supported range, or `other` when
  nothing explainable is present.
- No substitute material, operation or partial fulfilment is suggested.
- `not_evaluated` is blocked but reported as "could not be evaluated". It has no blockers
  and never says the request is impossible.
- A tool error is `technical_failure` (route `failed`), never a blocker.

<a id="supervisor-escalation-5-review-recommendations"></a>
### 5. Review recommendations

| Category | Codes (where they come from) |
|---|---|
| `engineering` | `manufacturing_requires_review` (A2 rule drift), `BORDERLINE_BEND_RADIUS` (A5 policy), `intake_{engineering_review, unsupported_requirement, unknown_operation, catalog_no_match, catalog_ambiguous}` |
| `supply_delivery` | `supply_requires_review` (A3), `supply_constrained` (A4), `DELIVERY_DATE_INFEASIBLE`, `CAPACITY_OUT_OF_HORIZON`, `CAPACITY_DATA_MISSING` (A3/A5 lead-time findings) |
| `commercial` | `pricing_requires_review`, `pricing_rule_drift`, `pricing_contradicts_upstream` (A4/A5), `QUOTE_TOTAL_AT_OR_ABOVE_THRESHOLD` (A5 manager review) |
| `intake` | other `intake_<kind>` review findings (catalog unavailable or not checked, rules not checked, injection suspected) |
| `other` | any unknown code; also a review with no code |

- Constrained supply still continues to pricing (specialist workflow `_NEXT`). Its concern surfaces at
  A5, and that sequence is unchanged.
- Categories are recommendations only. Nobody is assigned, no ticket is created, nothing is
  notified or approved.
- A completed draft keeps any A5 manager-review reasons as categories. The customer sees
  only "awaiting approval".

**Customer status:** no tool or service verifies account standing or credit. A5 lists
`customer_account_status` and `credit_limit` as unevaluated. The disposition therefore always
has `customer_status="not_verified"` and carries A5's codes in `unevaluated_conditions`. It
is never a pass, and never a block on its own. An authoritative account and credit service
is a quotation lifecycle requirement (L08–L17).

<a id="supervisor-escalation-6-draft"></a>
### 6. Draft

`draft_ready_for_approval` requires `draft_available`. A draft may also accompany
`review_required` at `quotation`. The draft itself stays A5's `customer_view()`, unchanged.
The customer outcome says that a non-binding draft awaits approval. It is not a quote, and it
is not released, persisted, numbered or sent.

<a id="supervisor-escalation-7-technical-failure"></a>
### 7. Technical failure

`failed` routes and unsettled runs carry their fixed code in `reason`, which is operator-only:

- handoff and provenance refusals;
- `timeout`, `internal_error`, `unauthenticated` and untrusted rules;
- `execution_limit`.

The customer gets one fixed sentence ("No decision has been made"). There are no questions,
blockers or review notices, and no retry.

<a id="supervisor-escalation-8-projections"></a>
### 8. Projections

- **Internal** (`disposition`, included in `summary()`): category, stage, reason codes,
  route codes, questions and correlation through the result's `attempt_id` and identity.
  There are no amounts or evidence. Raw A4 and A5 results stay in custody and are cleared.
- **Customer** (`customer_outcome()`): outcome name, fixed message, questions, blocker
  sentences, review notices (review only) and `draft_available`. There are no codes,
  amounts, customer text, exception text or rule diagnostics.
- `view_quote` does not open internal costing. Neither projection contains any (L27/L28).


## Supervisor checkpointing and recovery (L05)

Module: `src/rfq_copilot/recovery.py`. Supervisor hooks: `_Custody` (pause, carried
results, unused budget), `_delegation_node` → `_re_record`, and `_foundation(attempt=...)`.

<a id="supervisor-recovery-1-conversational-agent-checkpoint-audit-graphpy-conversations"></a>
### 1. conversational agent checkpoint audit (`graph.py`, `Conversations`)

| Property | conversational agent behaviour | Reusable for the Supervisor? |
|---|---|---|
| Saver | `InMemorySaver(serde=SAFE_SERDE)`, process-local; a durable saver can be injected | Design reused (owner binding, server ids); saver not attached |
| Serializer | `JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)`: primitives and LangChain messages only | **No.** `RoutingState` holds Pydantic models (identity, outcomes, route, budget), outside the allow-list (Supervisor foundation control) |
| What is saved | Only the trimmed `history` of a successful turn (`aupdate_state`); the turn itself runs on a graph with **no** checkpointer | Same principle: run without a checkpointer, persist only approved data |
| Failures | A failed, cancelled or limit-exceeded turn saves nothing | Kept: failures persist no evidence |
| Threads | Server-chosen `secrets.token_urlsafe(24)`, regex-checked | Reused for `recovery_id` |
| Ownership | `metadata.owner = sha256(customer_id\0user_id)` (`_owner`), `hmac.compare_digest` under a per-thread lock | Shared through `rfq_copilot.ownership.principal_owner` (moved verbatim, recovery control); transitions claimed under a `threading.Lock` |
| Retries | None; tools run once | Kept |
| Interrupt / resume | Not used (no `interrupt()`; LangGraph interrupts need a checkpointer and re-run the node) | Not used: a re-run node would replay tool calls |

<a id="supervisor-recovery-2-supervisor-feasibility-audit"></a>
### 2. Supervisor feasibility audit

| Stage | Needs | External effect | Replay changes outcome? | Persistable? |
|---|---|---|---|---|
| A1 extraction | text, intake graph | **paid model call** | yes (model) | no: raw extraction |
| A1 validation | extraction (L03), text, rules | catalog reads | possibly (catalog) | no |
| A2 | validation, executor | read-only MCP per part | possibly (catalog/rules) | no: full evidence |
| A3 | A2, extraction, horizon | read-only stock/capacity | **yes** (live stock) | no |
| A4 | A2, A3, pricing rule | read-only cost/price | yes if upstream differs | no: internal costs |
| A5 | A2, A3, A4, rules | read-only quotation | yes | no: draft and amounts |

**Answer:** a partially completed run **cannot** resume from metadata. A `StageOutcome` is not
a `ManufacturingAssessment`. Continuation is possible only while the original typed results
are still held. Safe boundaries are therefore stage boundaries: after one outcome is recorded
and before the next agent starts. No boundary exists inside a stage, including A2's fan-out.

<a id="supervisor-recovery-3-chosen-design-option-4-hybrid"></a>
### 3. Chosen design: Option 4 (hybrid)

1. **In-process continuation (same attempt).** `start(context, pause_before=stage)` stops
   before `stage`; its `_Custody` stays alive in the registry entry and is never serialized.
   On `resume`, the execution graph re-records each held result through `record_outcome`,
   with no agent call. That re-checks identity, rules, handoffs and provenance under the
   resumed context, and the result must equal the outcome recorded at the pause, else
   `stale_recovery`. Then `decide()` delegates the next stage as usual, so the topology is
   unchanged.
2. **Fresh restart (new attempt).** `restart(context, id)` supersedes the old attempt, clears
   its custody and runs everything again under a new `recovery_id` and `attempt_id`
   (`restarted_from` set). This is the path after a process restart, after clarification
   (the source may change; customer and RFQ may not), or after any failure.
3. **Retrieval.** `retrieve(principal, id)` returns a finished attempt's stored
   `SupervisorExecutionResult` (no costs or evidence) as of its completion. Nothing is
   re-executed or re-validated.
4. **Refusal**, with a fixed code:
   - `recovery_unavailable` (unknown, malformed or foreign id: one answer);
   - `attempt_busy`, `not_resumable`, `expired`, `stale_generation`;
   - `evidence_unavailable`, `identity_mismatch`, `source_changed`, `rules_changed`,
     `policy_changed`, `stale_recovery`;
   - `restart_limit`, `recovery_capacity`, `invalid_record`.

Option 1 alone (metadata checkpoint) cannot continue anything. Attaching a checkpointer
would break `SAFE_SERDE`. Option 3 alone would rename a restart as a resume.

<a id="supervisor-recovery-4-record-contract-recoveryrecord-the-only-persistable-data"></a>
### 4. Record contract (`RecoveryRecord`, the only persistable data)

- **Fields:**
  - `schema_version` (1), `recovery_id`, `owner` (sha256);
  - `identity` (customer from the principal, RFQ, source, source sha256);
  - `attempt_id`, `generation`;
  - `rules` (≤ 3 version/fingerprint pairs), `horizon_start`;
  - `lifecycle`, `next_stage`, `budget_left_seconds`, `expires_at`, `restarts`, `code`.
- **Validation:** `extra="forbid"`, patterns for ids, hashes, fingerprints and codes,
  `StrictInt`, ≤ 4096 bytes, JSON only (no pickle, no object revival), and an unknown
  version is refused.
- **Not stored:** text, prompts, model output, evidence, amounts, credentials, executors or
  callables.
- **Authenticity:** the record is **not** authenticated (L04/L21/L26). Adopting one grants
  only the right to be refused or restarted by its owner.

<a id="supervisor-recovery-5-ownership-and-staleness"></a>
### 5. Ownership and staleness

Every operation re-derives the owner from the verified `Principal` and re-admits the context
(`supervisor._admit`). Resume additionally requires:

- lifecycle `paused` and the current `generation`;
- live custody;
- the same identity, rule fingerprints and horizon;
- unused budget, before the TTL.

Inside the run, custody refuses another attempt's state, and re-recording refuses evidence
that no longer validates. An ID is only a lookup key, never authorization.

<a id="supervisor-recovery-6-replay-retries-concurrency-and-cancellation"></a>
### 6. Replay, retries, concurrency and cancellation

- **One owner of a transition:** each transition is claimed synchronously under the lock,
  with no await in between. Concurrent resumes give one run and `attempt_busy` for the rest.
  Resume or restart during a run gives `attempt_busy`.
- **No duplicated work:** a continued attempt never calls A1–A5 again for held stages. This
  is process-local only, and there is **no exactly-once** guarantee for external tools.
- **Retries:** none automatic. A restart is explicit and limited to `MAX_RESTARTS` (2) per
  lineage, after which it reports `restart_limit`.
- **Budget:** a resumed run gets only the unused budget measured at the pause. The pause
  itself is bounded by a TTL (900 s).
- **Cancellation:**
  - `cancel` on a paused attempt clears its custody;
  - on a running attempt it cancels the task, the attempt is recorded `cancelled`, and A2
    workers settle;
  - a cancelled, failed, expired or superseded attempt is never `completed`.
- **Process termination:** an adopted `running` record becomes `failed`/`interrupted`.

<a id="supervisor-recovery-cancellation-and-retention-recovery-control-recovery-control"></a>
#### Cancellation and retention (recovery control, recovery control)

- **Running attempts:**
  - `cancel()` records `cancel_requested` under the lifecycle lock, then cancels the task on
    its own loop (`loop.call_soon_threadsafe`), so it is safe from any thread;
  - the response (`outcome="cancelled"`, `code="cancel_requested"`) acknowledges the
    request, not a stopped task, because cancellation is cooperative;
  - once requested, the attempt can only end `cancelled`. A late result or late pause (for
    example, nested code that swallowed `CancelledError`) is discarded, nothing is retained,
    and custody is cleared only after the task settles;
  - repeated requests are acknowledged without re-delivering the cancellation;
  - resume or restart while it runs get `attempt_busy`, and afterwards `not_resumable`. A
    restart is a new attempt and never revives the old one.
- **Paused attempts:** `cancel()` cancels immediately and clears custody (`code=None`).
- **Completed attempts:** `cancel()` is refused.
- **Retention:** cleanup is opportunistic, on access and insertion, plus the explicit
  `prune()` (expired entries) and `close()` (shutdown: every entry that is not running is
  dropped, running ones are asked to cancel). There is no timer, so paused custody is
  **not** released at the exact TTL unless the application calls `prune()`. A running entry
  is never pruned.

<a id="supervisor-recovery-7-survives-a-process-restart"></a>
### 7. Survives a process restart?

| Survives (if `export`ed to a store) | Does not survive |
|---|---|
| `RecoveryRecord` metadata: who, what, which attempt, lifecycle, next stage, expiry, restart count | Custody (typed evidence), stored results, locks, running tasks. Continuation becomes `evidence_unavailable`; restart is the recovery |

No durable store is wired. Process-local guarantees do not extend across workers or machines.


## Supervisor observability (L07)

Opt-in, privacy-safe telemetry for the Supervisor, with an optional Langfuse exporter.
Code: `src/rfq_copilot/observability.py`; hooks in `supervisor.py` (delegation node, tool
wrapper, `_execute`) and `recovery.py` (`_reported`). Tests:
`tests/test_supervisor_observability.py`.

<a id="supervisor-observability-1-design"></a>
### 1. Design

```
SupervisorContext.telemetry ──► supervisor._emit ──► observability.emit ──► Event (validated)
SupervisorRecovery(telemetry=) ─► recovery._report ─┘                          │
                                                                  sink.record(event)
                                                       MemorySink | LangfuseSink | None
```

- **One contract.** A sink only ever receives an `Event`: a closed set of fields, each a fixed
  code (`^[a-z0-9_]{1,64}$`), a random 32-hex id, a bounded int, a bool, a stage name or an
  agent id. Each event name permits only its own fields (`_FIELDS`). There is no field that can
  hold free text, an amount, a customer/user id, a prompt, a response, evidence, a credential
  or exception text.
- **Disabled by default.** `telemetry=None` everywhere. `emit` returns at once; the tool
  executor is not wrapped; `langfuse` is never imported.
- **Explicit enablement.** `observability.from_env()` returns a sink only when
  `RFQ_LANGFUSE_ENABLED=true` (case-insensitive, trimmed). Keys alone, or `1`/`yes`/`on`,
  enable nothing. It reads the process environment (or a mapping you pass); it does not load
  `.env`.
- **Write-only.** Events are emitted after a decision is made and never read back. Telemetry
  cannot route, approve, price, retry, resume or change custody. `emit` never raises: invalid
  events are dropped, sink faults are swallowed.
- **No callbacks.** The LangChain `CallbackHandler` is not attached to any graph. It captures
  node inputs and outputs (customer text, typed results with costs), and no masking proves
  that safe. Every exported observation is created explicitly from an `Event`.

<a id="supervisor-observability-2-events"></a>
### 2. Events

| Event | Fields (besides `run`, `attempt`) | Emitted by |
|---|---|---|
| `workflow_started` | `run_kind` (start/resume/restart), `previous_attempt` (restart), `concurrency` | `_execute` |
| `route_decided` | `action`, `next_stage`, `stage` (stopped at), `outcome`, `reason` | delegation node (delegate); `_execute` (terminal) |
| `stage_started` | `stage`, `agent` | delegation node, just before the agent call |
| `tool_call` | `stage`, `tool` (allow-listed, else `unlisted`), `status` (`ok`, MCP `FailureCode`, `error`, `cancelled`), `duration_ms` | `_observed` executor wrapper |
| `a2_fanout` | `part_count`, `concurrency`, `completed`, `failed`, `fan_in` (joined/sequential/refused/not_run) | delegation node after A2 |
| `stage_finished` | `stage`, `agent`, `status` (the agent's own), `decision`, `reason`, `duration_ms`, `carried` (re-recorded held result on resume) | delegation node, from the `StageOutcome` `record_outcome` derived; `cancelled` on cancellation |
| `workflow_finished` | `outcome` (disposition category, `paused` or `cancelled`), `error`, `action`, `stage`, `reason`, `duration_ms` | `_execute` |
| `recovery` | `operation`, `outcome`, `code`, `generation` (+ `attempt`; no `run`) | `SupervisorRecovery` operations; cancellation settlement |

Correlation: `run` is `secrets.token_hex(16)` per run, `attempt` the Supervisor attempt id
(also random). A resumed run has a new `run` and the same `attempt`; a restart has a new `run`,
a new `attempt` and `previous_attempt`. Recovery ids (resumable handles), owner hashes,
customer, user, RFQ and source ids are never exported. Concurrent runs never share a `run`.

`tool_call.status == "ok"` means only that the executor returned; the agent still validates
the result. No retry count is reported because there are no retries.

<a id="supervisor-observability-3-example-safe-trace-baseline-single-part-rfq"></a>
### 3. Example safe trace (baseline single-part RFQ)

```
rfq.supervisor.workflow   {run, run_kind: start, concurrency: 2}
├─ rfq.route_decided      {action: delegate, next_stage: intake_extraction}
├─ rfq.stage.intake_extraction  {stage, agent: A1, status: extracted, decision: continue, duration_ms}
├─ rfq.route_decided      {action: delegate, next_stage: intake_validation}
├─ rfq.stage.intake_validation
│  └─ rfq.tool_call       {tool: search_materials, status: ok, duration_ms}
├─ … manufacturing (tool_call check_manufacturing_feasibility), rfq.a2_fanout {part_count: 1, fan_in: sequential}
├─ … supply, pricing, quotation
├─ rfq.route_decided      {action: complete, outcome: draft_ready_for_review}
└─ (root final metadata)  {attempt, outcome: draft_ready_for_approval, action: complete, duration_ms}
```

Status values above are illustrative; tests assert the exact codes from each run's outcomes.

<a id="supervisor-observability-4-langfuse-mapping-langfusesink"></a>
### 4. Langfuse mapping (`LangfuseSink`)

- SDK: Python `langfuse` v5 (OpenTelemetry based; Python ≥ 3.11; this project uses 3.12). It
  is an optional extra (`pyproject.toml` `[project.optional-dependencies] langfuse`), imported
  only inside `from_env` on the enabled path.
- Client: `Langfuse(public_key, secret_key, base_url, sample_rate, environment,
  tracing_enabled=True, timeout=3, mask=observability.mask,
  should_export_span=langfuse.span_filter.is_langfuse_span)`. `LANGFUSE_BASE_URL` is required
  (https, or http to localhost); no region is assumed.
- Observations: one root `rfq.supervisor.workflow` per run; `rfq.stage.<stage>` children in
  order; short `rfq.<event>` children for routes, tools (under their stage) and A2 fan-out;
  standalone `rfq.supervisor.recovery` observations. Only `update(metadata=...)` is called,
  never `input`/`output`. Open stage spans are closed when the workflow finishes (cancel or
  error). At most `MAX_OPEN_RUNS` (256) roots are open per sink; runs beyond that are not
  traced.
- Masking (defence in depth): `mask` passes allow-listed metadata and code-shaped scalars and
  redacts everything else. It is not the privacy boundary: a code-shaped string passes it.
- Filtering: only the Langfuse SDK's own spans are exported, so no third-party OpenTelemetry
  instrumentation in the process can add prompts or state.
- Sampling: `LANGFUSE_SAMPLE_RATE` (0–1, validated), applied per trace by the SDK.
- Export: the SDK batches and exports on a background thread (bounded batch processor,
  3-second HTTP timeout). Export failures do not reach the caller. Call `sink.flush()` /
  `sink.shutdown()` at application shutdown; neither raises.

<a id="supervisor-observability-5-llm-usage"></a>
### 5. LLM usage

The model abstraction (`llm.py`, A1's intake graph) does not expose token usage or provider
cost safely, so no generation observations are emitted. Prompts and responses are not captured
to obtain usage. Model invocation cost is unrelated to manufacturing cost or selling price;
none of these appear in telemetry.

<a id="supervisor-observability-6-what-is-and-is-not-verified-l07"></a>
### 6. What is and is not verified (L07)

| Level | Status |
|---|---|
| Local instrumentation (events at workflow, stage, route, tool, A2 and recovery boundaries) | Implemented; coverage in the Supervisor observability tests |
| Mocked export behaviour (fake SDK: mapping, metadata-only, bounds, failures) | Offline test coverage; does not establish live delivery |
| Live Langfuse delivery | Requires the gated [live smoke test](../../tests/test_langfuse_live_smoke.py); offline checks do not establish delivery |
| Production monitoring, dashboards, alerting, retention policy | Not implemented (deployment) |

<a id="supervisor-observability-7-limits"></a>
### 7. Limits

- Telemetry is not authority. On a swallowed cancellation a late run may emit a normal
  `workflow_finished`; the following `recovery` event records the attempt as `cancelled` and
  no result is published.
- A code-shaped value is a code by origin, not by shape: callers pass agent `Literal` statuses
  and fixed Supervisor codes. Tests feed code-shaped customer text and exception text through
  real runs and assert neither appears.
- Telemetry is process-local and in-memory until the SDK exports it; there is no durable
  telemetry buffer.
