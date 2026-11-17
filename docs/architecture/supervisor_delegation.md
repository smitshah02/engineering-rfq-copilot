# Supervisor Delegation

**Code:** `src/rfq_copilot/supervisor.py` (delegation section).
**Tests:** `tests/test_supervisor_delegation.py`.
**Builds on:** `supervisor_foundation.md` and `supervisor_routing.md`.

## 1. Topology

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

## 2. Trusted dependencies

`SupervisorContext` includes two optional fields used by delegation; foundation and routing callers may omit them.
They come from the trusted caller exactly as specialist workflow receives them:

- `intake_graph: CompiledStateGraph | None`, with `repr=False`;
- `horizon_start: date | None`.

`delegate_stage` requires both (`missing_dependency`) before the first model call. The
foundation and routing graphs never read them.

## 3. Per-attempt custody (`_Custody`)

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

## 4. L03: the extraction → validation chain of custody

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

## 5. Stage execution (`_arguments`, `_STAGE_TIMEOUTS`)

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

## 6. Result (`SupervisorExecutionResult`)

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

## 7. specialist workflow parity

All 11 Supervisor routing scenarios run through `run_supervisor_workflow` and through
`evaluate_rfq_workflow` with fresh identical fakes. They must agree on:

- the route action and stopping stage;
- per-stage statuses;
- the stage invocation sequence, once each;
- the exact executor tool calls;
- clarification codes, review codes and the error reason;
- `draft_available` and `customer_view()`.

The only intentional difference is the shape of the result contract.

## 8. parallel execution parallel part checks

Inside the manufacturing stage, A2 may check independent parts concurrently, bounded by the
trusted `SupervisorContext.part_concurrency`. The stage still records one outcome. See
`supervisor_parallel.md`.

## 9. escalation dispositions

Every result from `run_supervisor_workflow` carries a `disposition`. It is derived only from
the settled route (`decide(outcomes)`) and this attempt's custody codes. See
`supervisor_escalation.md`.
