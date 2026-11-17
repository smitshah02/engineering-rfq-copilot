# Supervisor checkpointing and recovery (L05)

Module: `src/rfq_copilot/recovery.py`. Supervisor hooks: `_Custody` (pause, carried
results, unused budget), `_delegation_node` → `_re_record`, and `_foundation(attempt=...)`.

## 1. conversational agent checkpoint audit (`graph.py`, `Conversations`)

| Property | conversational agent behaviour | Reusable for the Supervisor? |
|---|---|---|
| Saver | `InMemorySaver(serde=SAFE_SERDE)`, process-local; a durable saver can be injected | Design reused (owner binding, server ids); saver not attached |
| Serializer | `JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)`: primitives and LangChain messages only | **No.** `RoutingState` holds Pydantic models (identity, outcomes, route, budget), outside the allow-list (D44-d) |
| What is saved | Only the trimmed `history` of a successful turn (`aupdate_state`); the turn itself runs on a graph with **no** checkpointer | Same principle: run without a checkpointer, persist only approved data |
| Failures | A failed, cancelled or limit-exceeded turn saves nothing | Kept: failures persist no evidence |
| Threads | Server-chosen `secrets.token_urlsafe(24)`, regex-checked | Reused for `recovery_id` |
| Ownership | `metadata.owner = sha256(customer_id\0user_id)` (`_owner`), `hmac.compare_digest` under a per-thread lock | Shared through `rfq_copilot.ownership.principal_owner` (moved verbatim, D49-03); transitions claimed under a `threading.Lock` |
| Retries | None; tools run once | Kept |
| Interrupt / resume | Not used (no `interrupt()`; LangGraph interrupts need a checkpointer and re-run the node) | Not used: a re-run node would replay tool calls |

## 2. Supervisor feasibility audit

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

## 3. Chosen design: Option 4 (hybrid)

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

## 4. Record contract (`RecoveryRecord`, the only persistable data)

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

## 5. Ownership and staleness

Every operation re-derives the owner from the verified `Principal` and re-admits the context
(`supervisor._admit`). Resume additionally requires:

- lifecycle `paused` and the current `generation`;
- live custody;
- the same identity, rule fingerprints and horizon;
- unused budget, before the TTL.

Inside the run, custody refuses another attempt's state, and re-recording refuses evidence
that no longer validates. An ID is only a lookup key, never authorization.

## 6. Replay, retries, concurrency and cancellation

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

### Cancellation and retention (D49-01, D49-02)

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

## 7. Survives a process restart?

| Survives (if `export`ed to a store) | Does not survive |
|---|---|
| `RecoveryRecord` metadata: who, what, which attempt, lifecycle, next stage, expiry, restart count | Custody (typed evidence), stored results, locks, running tasks. Continuation becomes `evidence_unavailable`; restart is the recovery |

No durable store is wired. Process-local guarantees do not extend across workers or machines.
