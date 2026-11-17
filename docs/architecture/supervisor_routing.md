# Supervisor Routing

The routing-only runner selects a stage without executing it. The complete
[delegation runner](supervisor_delegation.md) attaches specialist execution to these same edges.
**Code:** `src/rfq_copilot/supervisor.py` (routing section). **Tests:**
`tests/test_supervisor_routing.py`. **Builds on:** `supervisor_foundation.md`.
**Gating authority:** `agent_integration.py`.

## 1. Routing and delegation boundaries

`decide(outcomes)` selects the next stage or stop route. `record_outcome(...)` validates
and records typed results; `validate_routing_state(...)` enforces state consistency.
`build_routing_graph()` and `run_supervisor_routing(context)` expose the routing-only
runner, which stops at `await_delegation`. The full workflow attaches a delegation node
through `attach_routing(delegate=...)`, retaining the same routing edges and validation.

## 2. Topology

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

## 3. Routing-state contract (`RoutingState`)

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

## 4. Trusted evidence: how an outcome is earned

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

## 5. Transition policy

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

## 6. Loop safety and budgets

- **Only back-edge:** delegation node → `route_supervisor`. `route_supervisor` raises
  `no_progress` unless this is its first run or a stage was recorded since its last run. A
  delegation node that records nothing therefore stops the run; it is never re-selected.
- **Recursion limit:** the routing graph uses `ROUTING_RECURSION_LIMIT = 18` (16 steps at
  most with six delegations). The foundation keeps 4. `Budget.recursion_limit` is
  `Literal[4, 18]` and matches the graph that admitted the attempt.
- **Deadline:** 390 s (`DEFAULT_TIMEOUT`), the same as the foundation.
- No retries, no LLM call, no MCP call. Cancellation propagates.

## 7. Security boundaries

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

## 8. Foundation and specialist-workflow compatibility

The foundation retains its initialization-only result and recursion limit of four. Both
runners share `_settle`; routing reuses the foundation result validator. Specialist handoff
checks use `agent_integration._handoff_error`, avoiding a second implementation. Supervisor
code does not import the conversational graph module.

## 9. Delegation responsibilities

- A delegation node that calls `STAGE_PLAN[stage].entry` through the principal-bound
  executor with the per-stage budget, keeps typed results in a per-run store (never in state
  or a checkpoint), and calls `record_outcome`.
- Context additions: `horizon_start` and the intake graph.
- A delegation-enabled public runner and graph.
- Binding A1 validation to the recorded extraction in the same attempt. The router binds the
  extraction hash, but `IntakeValidation` carries no hash (L03).
