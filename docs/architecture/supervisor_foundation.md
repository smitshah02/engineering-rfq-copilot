# Supervisor Foundation

This document describes the initialization-only foundation runner. The complete Supervisor
adds [routing](supervisor_routing.md), [delegation](supervisor_delegation.md),
[escalation](supervisor_escalation.md), and [recovery](supervisor_recovery.md).
**Code:** `src/rfq_copilot/supervisor.py`. **Tests:** `tests/test_supervisor.py`.
**Related:** `supervisor_langgraph_architecture.md` (conceptual design),
`langgraph_foundation.md`, `specialized_agent_integration.md` (specialist workflow).

## 1. Responsibility

The Supervisor (A0) coordinates A1–A5 through explicit LangGraph nodes. Its foundation runner:

- admits one workflow attempt from a trusted run context;
- fixes its identity, source provenance, rule provenance and budget;
- lays out the six specialist stages as `pending`;
- returns a result that says only "initialised".

It owns the workflow lifecycle, attempt identity, stage transitions, state consistency,
failure containment and budgets. It never owns business results: those stay with the
deterministic services behind A1–A5 and are produced only when an agent actually runs.

## 2. Topology

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

### Direct graph invocation (internal interface)

The compiled graph is not an independently authorised entry point. When it is called
directly:

- Any input key that is a `SupervisorState` key fails with `invalid_initial_state`.
- LangGraph drops input keys that are not state keys before any node runs; they are
  *filtered, not rejected*. They never become state and grant nothing: identity comes only
  from the context. `test_security_unknown_input_keys_never_become_state` pins this.
- A dict `context` is coerced into `SupervisorContext` (LangGraph `_coerce_context`); the
  runner refuses dicts.

## 3. State (`SupervisorState`, a TypedDict like conversational agent's `GraphState`)

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

### Ownership (`owned_update`, `validate_state`)

- Each node may write only the keys listed in `_WRITES`. Trusted keys can be written once
  only. Any other write raises `ownership_violation`. Specialists never write foundation state directly.
- `validate_state(state, context)` re-derives identity, rules and budget from the context
  and compares them. It requires the stage plan in order. It enforces the **foundation
  state**: every stage `pending` with `reason=None`, and empty `clarifications` and
  `review_reasons`. No validated evidence exists, so a status, reason or indicator would be
  a forgery. The execution layer records only stages that ran and produced validated
  results, each with its own evidence check.

## 4. Trusted runtime context (`SupervisorContext`)

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

## 5. Stage plan (`STAGE_PLAN`)

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

## 6. Output boundaries

| Output | Contents |
|---|---|
| `SupervisorFoundationResult` | scope `supervisor_foundation`, status `initialized`/`error`, fixed error code, identity, attempt id, rule fingerprints, pending stages. `evaluated`, `draft_available`, `approved` and `released` are `Literal[False]`; `human_approval_required` is `Literal[True]`. |
| `summary()` | the same as JSON. No amount, evidence, text or credential exists to leak. |
| `RFQWorkflowAssessment`, `summary()`, `customer_view()` | specialist evaluation contracts, reused by execution |

A model validator keeps status and contents consistent. `initialized` requires no error and
the full identity, attempt id, rules and stages. `error` requires a code and carries no
identity, attempt id, rules or stages. The public fields are unchanged.

## 7. Failure handling

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

## 7a. Import boundary

`supervisor.py` takes `ToolExecutor` from `intake_validation`, the same neutral alias specialist workflow
uses (equal to `graph.ToolExecutor`). It does not import the conversational agent graph module, so
`test_conversations.py::test_no_application_entry_point_imports_the_graph` holds unchanged.

## 8. Checkpoint compatibility

There is no checkpointer, and none may be attached through `run_supervisor_foundation`. The
state holds Pydantic objects, which are outside `graph.SAFE_SERDE`'s msgpack allow-list, so
**Foundation state is not checkpoint-safe as written**. The conversational agent’s `Conversations` is untouched.
The [recovery layer](supervisor_recovery.md) persists a validated JSON-only metadata
projection. Evidence and costs are never persisted.

## 9. Design decisions

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

## 10. Foundation boundaries

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

## 11. Supervisor routing

Routing builds on this foundation without changing it. See `supervisor_routing.md`. The
foundation graph, `validate_state`, `run_supervisor_foundation` and their contracts are
unchanged. Both graphs share `_foundation(recursion_limit, schema)`, and both runners
share `_settle`.
