# Supervisor parallel execution (Day 47, L01)

Scope: **per-part concurrency inside A2 (Manufacturing Feasibility)**. The six Supervisor
stages (A1 extraction → A1 validation → A2 → A3 → A4 → A5) stay a strict sequential chain.

## 1. Dependency audit

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

## 2. Topology

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
  Day 46 keeps typed results out of graph state (custody, L06).
- A graph fan-out would also have needed a second record path for one stage.

The Supervisor still records **exactly one** validated outcome per stage.

## 3. Trusted worker contract

- Work-item identity is the part's **position in `intake.parts`**: stable, derived from the
  validated structure, never customer text. `PartValidation.index` is not enforced unique by
  its model, so position is the key, and `index`, `part_id` and `specification` are checked.
- Each worker has only its part's specification, the call's own `intake` (pinned rule
  version and fingerprint) and the caller's principal-bound executor. The tool input
  (`PartCheckInput`) is unchanged and carries no identity, scope or concurrency setting.
- Workers live in the local `collected` list of one `evaluate_manufacturing_feasibility`
  call. There is no global store, and no state, checkpoint or custody write.

## 4. Concurrency limits

| Setting | Where | Value |
|---|---|---|
| `a2.MAX_CONCURRENCY` | code constant | 4 (hard cap; `ValueError` above it) |
| `evaluate_manufacturing_feasibility(max_concurrency=)` | keyword argument, trusted caller | default **1** (Phase 6 and direct callers stay sequential) |
| `SupervisorContext.part_concurrency` | trusted run context, never state or input | default **2**; anything not an `int` in 1..4 → `invalid_budget` before any agent runs |

`bool`, `float` and `str` are refused (`type(x) is int`).

Cross-RFQ load: each worker holds one HTTP connection and one server snapshot session for
one call, so a run adds at most `part_concurrency` connections. There is no process-global
semaphore: it would bind to one event loop, and it is not needed while the per-run cap is
≤ 4. Throttling many RFQs belongs to the deployment layer (L33/L34). A1's single model call
per RFQ is unchanged. No model is called in A2.

## 5. Fan-in (`_join`)

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

## 6. Failure, timeout and cancellation

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

## 7. Custody (L03) and security

Unchanged: `_Custody` per run, bound to one attempt, keeping original typed results with no
replacement, and cleared on every exit. A2 workers never see custody. The stage receives the
validation object held for this attempt and returns one `ManufacturingAssessment`, which
`record_outcome` validates and `delegate_stage` keeps. A1 validation and A3 still get the
same extraction object.

Workers cannot touch identity, attempt id, source hash, rule fingerprints, outcomes, the
route, or approval and release flags. Those are Supervisor state written only by their
owning nodes. `approved` and `released` stay `Literal[False]`.

## 8. Parity

The sequential path is the reference: `max_concurrency=1` (A2) and `part_concurrency=1`
(Supervisor), plus Phase 6 `evaluate_rfq_workflow`, which still calls A2 sequentially. Tests
compare the full typed `ManufacturingAssessment` and every kept stage result
(`SupplyAssessment`, `PricingAssessment`, `QuotationReview`) with Phase 6's. They also
compare the whole `SupervisorExecutionResult` apart from attempt ids, the customer view, and
the multiset of executor calls.
