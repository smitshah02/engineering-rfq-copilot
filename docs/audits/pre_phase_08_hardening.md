# Pre-Phase 8 hardening — Workstream 1: recovery reliability, Langfuse security, live readiness

Status: **focused verification passed; full regression and independent-audit fixes pending.**
The agent executed no test, Ruff run, Langfuse, Ollama or MCP call and no Git operation.
Phase 8 has not started. Baseline before this work (user run): 4,123 passed, 10 opt-in skips,
0 failures, Ruff and `uv lock --check` passing, `langfuse==5.2.0` (optional).

Verified by the user after the D50-01/D50-02/D50-07 work and the test corrections:
`tests/test_recovery_hardening.py` 14 passed with `-W error`; intake/manufacturing modules 197
passed with `-W error`; recovery and observability 228 passed; Ruff lint and formatting
passed. The final post-hardening full regression is **pending**, and so are the §11 fixes
(D50-10–12), which are written but not yet run.

## 1. Source audit summary

Inspected: `recovery.py`, `supervisor.py`, `observability.py`, `ownership.py`, the Phase 7 final
audit, the Day 44–50 Supervisor suites, all ten opt-in tests and their fixtures
(`test_mcp_live_e2e.live`/`seed_database`, `test_mcp_client.live_host`, `test_auth.trust`),
`api/tokens.py`, the orchestration models, and the installed Langfuse 5.2.0 source
(`_client/client.py`, `_client/span.py`, `_client/span_filter.py`,
`_client/resource_manager.py`, `_client/span_processor.py`).

Trust boundaries (unchanged): authority enters only through `SupervisorContext` (verified
`Principal`, principal-bound executor, pinned rules); outcomes only through `record_outcome`;
evidence only in the attempt's `_Custody`; recovery ownership re-derived from the principal on
every call; telemetry write-only through the `Event` allow-list.

## 2. Findings and corrections

| ID | Severity | Status | Summary |
|---|---|---|---|
| D50-01 | Low | **Fixed; focused tests verified** | Running recovery entries could stay `running` forever |
| D50-02 | Low | **Fixed; focused tests verified** | Cancellation/shutdown raised `RuntimeError` on a closed event loop |
| D50-07 | Low | **Fixed; focused tests verified** (new) | An abrupt teardown left the workflow trace open; 256 such runs would stop all tracing |
| D50-08 | Info | Documented | Langfuse 5.2.0 installs a global OpenTelemetry tracer provider when none is set |
| D50-09 | Info | **Superseded by D50-11** | Agent `not_evaluated_reason` fields are typed `str`; the code-shape check was the only backstop |
| D50-10 | Low | Fixed; tests authored, not run | `LangfuseSink` lost observation handles when an SDK `update()`/`end()` raised (§11) |
| D50-11 | Low | Fixed; tests authored, not run | Code-shaped but unexpected reason/status/code values were exported unchanged (§11) |
| D50-12 | Low | Fixed; tests authored, not run | Terminal timing used the event loop's clock, which raises when no loop is running (§11) |
| T-CSV | Test-only | Fixed (two verified with `-W error`; five more authored) | Import-time CSV reads left file handles open (`ResourceWarning`) |
| D50-03–06 | Info | Unchanged | As in the Phase 7 final audit |

### D50-01 — abandoned running entries (`recovery.py`)

Cause: `_run` settled only on `CancelledError` and `Exception`; a `BaseException` such as
`GeneratorExit` (a suspended coroutine closed, e.g. at loop teardown) left the entry `running`
with its task set. A task on a loop that was closed while it was pending likewise never
settled. `_prune` never touches `running` entries, so `MAX_ENTRIES` orphans would refuse all
new attempts for the life of the process.

Correction:
- `_run` gains a `BaseException` branch: the entry settles `failed`/`interrupted` (or
  `cancelled` if cancellation was requested) before the exception propagates.
- `_reap(entry)` settles a `running` entry **only** when its task can never run again: the task
  is done without having published, or its loop is closed. Then, and only then, custody is
  cleared. A pending task on an open loop is never touched, however long it runs; its own
  run deadline bounds it.
- `_reap` runs on access (`_owned`), on insertion and `prune()` (via `_prune`), and on
  `close()`. A reaped entry has no result, gets `expires_at = now` and is dropped at the next
  prune, so it frees capacity. `MAX_ENTRIES` and the TTL of paused/finished entries are
  unchanged. No background task is added.
- Publication guard: if the entry is no longer `running` when a result arrives, the result is
  dropped and the settled lifecycle is reported. A late result never revives an expired,
  failed or cancelled attempt.
- Telemetry: a `recovery` event with `operation=reap` (no recovery id, owner or identity).

Boundary: a task pending on a loop that is **stopped but never closed** cannot be told apart
from a slow task and keeps its slot until the loop is closed or the process ends. A process
crash loses all in-memory entries (process-local store; see L05).

### D50-02 — cancellation on a closed loop (`recovery.py`)

Cause: `cancel()` and `close()` called `loop.call_soon_threadsafe(task.cancel)` under the lock.
If the task's loop was closed, `RuntimeError` reached the caller.

Correction: `_request_cancel(entry)` records the intent (`cancel_requested = True`) first,
delivers the cancellation once (a repeated request is acknowledged, not resent), catches the
closed-loop `RuntimeError`, then calls `_reap`. If the loop is closed the task can never run
again, so the attempt settles `cancelled` at once (`code=None`); otherwise the answer stays
`cancel_requested` (requested, not settled) and custody is kept for the live task. `cancel()`
looks the entry up without reaping first, so a request on a dead attempt is recorded and the
attempt ends `cancelled`, not `failed`. `close()` uses the same path for every running entry
and then drops whatever settled. "Settled" means the local task cannot continue; a read-only
tool request already sent to the MCP host is not recalled.

### D50-07 — trace not closed on abrupt teardown (`supervisor.py`)

`_execute` emitted `workflow_finished` only for `CancelledError`. It now does so for any
`BaseException` (`outcome` = `cancelled` or `interrupted`), so `LangfuseSink` ends the root and
any open stage spans and releases its bounded slot.

## 3. L05 — recovery and retry policy

| Guarantee | Status | Evidence / dependency |
|---|---|---|
| Pause at a stage boundary; continue the same attempt from its original typed evidence | Implemented (in-process) | Day 49 |
| Explicit fresh restart as a new attempt (bounded `MAX_RESTARTS`) | Implemented | Day 49 |
| Stale, foreign, duplicate, concurrent resume refused; generation tokens | Implemented (process-local) | Day 49 |
| Cancellation: request vs settlement; late results discarded | Implemented | Day 49 + D50-02 |
| Abandoned running attempts settled; capacity not exhausted by orphans | Implemented | D50-01 |
| Durable metadata | Partially implemented | `RecoveryRecord` export/adopt (unauthenticated JSON); no durable store wired |
| Continuation after process restart or on another worker | **Blocked** | Needs authenticated durable evidence (L04/L21/L26 design, §6) and a durable store |
| Cross-worker mutual exclusion | **Blocked** | Needs a shared lock/lease (e.g. PostgreSQL row lock or advisory lock on `orchestration.workflow_runs`) |
| Automatic retry of A1–A5 | Not implemented by design | No stage is retried; a restart is explicit and a new attempt |
| External-tool idempotency / exactly-once | **Blocked** | Tools are read-only today; write tools (Phase 8) need `orchestration.idempotency_records` keys per call |
| Failure handling | Implemented | Fixed codes; no exception text; abrupt teardown settles |

L05 remains **partially mitigated**. No raw financial result or specialist evidence is persisted
to enable mid-stage resume.

## 4. L06/L07 — Langfuse source audit

Checked against the installed Langfuse 5.2.0 source, on normal and exception paths.

| # | Requirement | Judgment | Where |
|---|---|---|---|
| 1 | Disabled by default | Yes | `telemetry=None` defaults; `emit` returns on `None` |
| 2 | Credentials alone cannot enable | Yes | `from_env` requires `RFQ_LANGFUSE_ENABLED=true` |
| 3 | Explicit base URL | Yes | Required; https or local http; no userinfo/query |
| 4–8 | No RFQ text, prompts, responses, amounts, tokens, exception text | Yes, structurally | `Event` has no field able to hold them; values must match `^[a-z0-9_]{1,64}$`, hex ids, stage or agent literals, ints, bools |
| 9 | Stages and codes allow-listed | Yes | `Stage` literal, `TOOL_NAMES == graph.READ_ONLY_TOOLS`, per-event field sets |
| 10 | Parent/child correct | Yes | One root per run; stage spans under root; tool spans under their stage |
| 11 | Concurrent runs isolated | Yes | Random `run` per run; roots keyed by `run`; lock in `LangfuseSink` |
| 12 | No recovery handles or owner hashes | Yes | `recovery` events carry operation, outcome, code, generation, attempt only |
| 13 | Span cleanup after cancel/failure | Yes after D50-07 | `workflow_finished` on every exit; open stage spans ended |
| 14 | Export failure cannot change results | Yes | `emit` swallows; SDK export runs on its batch thread |
| 15 | Disabled path does no SDK work | Yes | No import, no wrapper, no events |
| 16 | Compatible with 5.2.0 | Yes | Constructor accepts every argument passed (`base_url`, `sample_rate`, `environment`, `tracing_enabled`, `timeout`, `mask`, `should_export_span`); `langfuse.span_filter.is_langfuse_span` exists (re-exported); `start_observation(name, as_type="span")`, `update(metadata=)`, `end()` exist on client and spans |
| 17 | Masking is defence in depth | Yes | 5.2.0 calls `mask(data=<whole metadata dict>)` for metadata; our mask passes only allow-listed keys with safe scalars |
| 18 | Only approved OTel spans leave | Yes | `should_export_span=is_langfuse_span` (scope `langfuse-sdk`); nothing in `src/` other than `observability.py` creates Langfuse spans; no LangChain callback |

D50-08: with no configured provider, 5.2.0 calls `opentelemetry.trace.set_tracer_provider`.
Other OpenTelemetry instrumentation in the process would then record real spans, but the
Langfuse processor drops every non-Langfuse span, and no other exporter is configured. If an
OTLP exporter is added at deployment, review it separately.

D50-09: A2–A5 `not_evaluated_reason` is `str`; every assignment found is a code literal or
`f"supply_{status}"`. A value outside the code pattern is dropped, never exported.

**Judgment (source level):** the default integration cannot export RFQ text, identities,
amounts, credentials or exception text. Live delivery is not verified.

## 5. L04/L21/L26 — evidence provenance

What exists today:

| Property | Today | Established by |
|---|---|---|
| In-process custody | Yes | `_Custody` holds the objects this attempt's agents returned |
| Rule fingerprinting | Yes (integrity against a code-pinned manifest) | `rule_manifest`, `canonical.fingerprint`, `pinned_rules` |
| Source identity hashing | Binding only | `source_sha256` computed from trusted text at admission |
| Origin authentication of evidence | **No** | MCP results are authenticated in transit only by the session to our own host |
| Tamper resistance of stored evidence | **No** | `RecoveryRecord` and serialized results are plain JSON |
| Durable evidence provenance | **No** | Nothing durable is written |
| Trusted signature verification | **No** | The app verifies JWTs (public key, `api/tokens.py`) but holds no signing key |
| Key lifecycle and rotation | **No** | — |

A sha256 is a binding, not authentication: anyone who can write the record can recompute it.

Proposed design (next focused component, not implemented here):

1. **Keep evidence server-side.** Store typed stage results in a new table
   (`orchestration.stage_evidence`, new Alembic revision, needs approval) keyed by
   `(attempt_id, stage)`, with `customer_id`, `source_sha256`, rule fingerprints and a canonical
   JSON body. Financial evidence stays internal: never in telemetry, customer views or records.
2. **Sign an envelope, not the row.** `{kid, attempt_id, stage, customer_id, source_sha256,
   rules, body_sha256, created_at}` canonicalised with the existing `canonical.fingerprint`
   rules, signed with Ed25519 or ECDSA P-256 via `cryptography` (already installed through
   `pyjwt[crypto]`; no new dependency).
3. **Keys from deployment.** The private key lives in a KMS/HSM or a secrets manager; the app
   reads it from a file path (as `RFQ_MCP_JWT_PUBLIC_KEY_FILE` is read today), never from code
   or the repository. Public verification keys are pinned by `kid` in a manifest, as rule
   versions are, so rotation means adding a `kid` and later retiring the old one.
4. **Verify on every load.** Resume after restart, retrieval and any cross-worker handoff
   verify the signature, the `kid`, the attempt and customer binding and the body hash, then
   re-run `record_outcome` exactly as `_re_record` does. Failure is `stale_recovery`.
5. **Tests.** Tampered body, swapped stage, another attempt, another customer, unknown or
   retired `kid`, wrong algorithm, truncated signature, replay into a new attempt.

Implementation prerequisites: approval of the schema change; a decision on key custody
(KMS vs file-mounted secret); a rotation policy. Until then L04/L21/L26 stay **deferred** and
nothing is claimed as authenticated.

## 6. Integration readiness (ten opt-in tests)

Common to every live MCP test: `.env` with `DATABASE_URL`, `TEST_DATABASE_URL`,
`TEST_DATABASE_NAME=rfq_copilot_test`; PostgreSQL running; the test database must be **empty**
(the `seed_database` fixture asserts no relations, migrates to head, takes advisory lock
461441/505, and in teardown asserts the tables are empty, downgrades to base and checks the
original objects are back). The `live` fixture **commits** a synthetic dataset to
`rfq_copilot_test` and truncates it in teardown. `live_host` starts a separate uvicorn MCP host
on a free loopback port (test JWT key generated per session) and checks its log for `.env`
values after SIGTERM. Network: loopback only. No model, no hosted provider.

| Test | Gate | Services | Model | DB | Writes | Network | Command |
|---|---|---|---|---|---|---|---|
| `test_agent_tools.py::test_agent_executes_read_only_tools_through_the_live_mcp_host` | `RFQ_AGENT_LIVE_MCP=1` | PostgreSQL, uvicorn MCP host | scripted chat model | test DB | test DB seed + truncate | loopback | `RFQ_AGENT_LIVE_MCP=1 .venv/bin/python -m pytest -q -rs tests/test_agent_tools.py -k live_mcp_host` |
| `test_intake_live_mcp.py` | `RFQ_AGENT_LIVE_MCP=1` | same | none | test DB | seed + truncate | loopback | `RFQ_AGENT_LIVE_MCP=1 .venv/bin/python -m pytest -q -rs tests/test_intake_live_mcp.py` |
| `test_manufacturing_agent_live_mcp.py` | `RFQ_AGENT_LIVE_MCP=1` | same | none | test DB | seed + truncate | loopback | `… tests/test_manufacturing_agent_live_mcp.py` |
| `test_supply_agent_live_mcp.py` | `RFQ_AGENT_LIVE_MCP=1` | same | none | test DB | seed + truncate | loopback | `… tests/test_supply_agent_live_mcp.py` |
| `test_pricing_agent_live_mcp.py` | `RFQ_AGENT_LIVE_MCP=1` | same | none | test DB | seed + truncate | loopback | `… tests/test_pricing_agent_live_mcp.py` |
| `test_quotation_agent_live_mcp.py` | `RFQ_AGENT_LIVE_MCP=1` | same | none | test DB | seed + `register_rule_version(approval-v1.2)` (idempotent) + truncate | loopback | `… tests/test_quotation_agent_live_mcp.py` |
| `test_specialized_agent_integration_live_mcp.py` | `RFQ_AGENT_LIVE_MCP=1` | same | Day 36 fake chat model | test DB | as above; the workflow writes nothing | loopback | `… tests/test_specialized_agent_integration_live_mcp.py` |
| `test_intake_ollama.py` | `RFQ_OLLAMA_SMOKE=1` | `ollama serve` | `qwen3:4b-instruct-2507-q4_K_M` | none | none | localhost:11434 | `RFQ_OLLAMA_SMOKE=1 .venv/bin/python -m pytest -q -rs tests/test_intake_ollama.py` |
| `test_ollama_smoke.py` | `RFQ_OLLAMA_SMOKE=1` | `ollama serve`, PostgreSQL | same model | **development DB, read-only** (`default_transaction_read_only=on`) | none (customer binding simulated in-process) | localhost | `RFQ_OLLAMA_SMOKE=1 .venv/bin/python -m pytest -q -rs -s tests/test_ollama_smoke.py` |
| `test_langfuse_live_smoke.py` | `RFQ_LANGFUSE_SMOKE=1` + `RFQ_LANGFUSE_ENABLED=true` + Langfuse vars | Langfuse (cloud or self-hosted) | none | none | none locally; 2 synthetic traces in Langfuse | HTTPS to `LANGFUSE_BASE_URL` | `RFQ_LANGFUSE_SMOKE=1 .venv/bin/python -m pytest -q -s tests/test_langfuse_live_smoke.py` |

Safety notes:
- Run the live MCP modules sequentially (one pytest process); each module migrates up and
  down the test database. Do not run them while another process uses `rfq_copilot_test`.
- If a live MCP run is interrupted mid-teardown, the next run fails its "empty test database"
  assertion by design; restore with the project's guarded test-DB procedure rather than by
  hand.
- `test_ollama_smoke.py` needs the seeded development database and reads it only.
- `live_host` passes `.env` values to the host subprocess environment (as before). Keep the
  Langfuse keys out of `.env` if you do not want them in that process.

## 7. Updated limitation statuses (original identifiers and meanings preserved)

| ID | Status after this workstream |
|---|---|
| L01 | Resolved within a defined scope (unchanged) |
| L02 | Resolved (development) (unchanged) |
| L03 | Resolved within a defined scope, in-process (unchanged) |
| L04 / L21 / L26 | Deferred; design in §5 |
| L05 | Partially mitigated; D50-01/D50-02 fixed; durable and cross-worker parts blocked (§3) |
| L06 | Partially mitigated; telemetry path confirmed free of financial data at source level; custody still holds costs in memory while running or paused |
| L07 | Partially mitigated; instrumentation and mocked export implemented; live delivery pending the smoke test; production monitoring/alerting not implemented |
| L08–L17 | Deferred to Phase 8 (unchanged) |
| L18–L25 | Carried forward (unchanged) |
| L27–L34 | Partially mitigated (development); deployment remainder unchanged |

## 8. Manual commands (in order)

```bash
cd ~/RFQ_CoPilot

# 1. Focused recovery reliability
.venv/bin/python -m pytest -q tests/test_recovery_hardening.py

# 2. Focused observability privacy
.venv/bin/python -m pytest -q tests/test_supervisor_observability.py

# 3. Affected existing tests
.venv/bin/python -m pytest -q tests/test_supervisor_recovery.py tests/test_supervisor_delegation.py \
  tests/test_supervisor_parallel.py tests/test_supervisor_escalation.py tests/test_supervisor_end_to_end.py \
  tests/test_supervisor.py tests/test_supervisor_routing.py tests/test_graph.py tests/test_conversations.py

# 4. Ruff
.venv/bin/ruff check .
.venv/bin/ruff format --check src tests alembic

# 5. Langfuse configuration preflight (prints only a fixed reason; builds the client, sends no span)
#    Export the variables in this shell first (do not echo them):
#    export RFQ_LANGFUSE_ENABLED=true LANGFUSE_BASE_URL=https://... LANGFUSE_TRACING_ENVIRONMENT=prephase8-smoke
#    export LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=...
.venv/bin/python -c "from rfq_copilot import observability as o; s, r = o.from_env(); print(r); s and s.shutdown()"

# 6. Gated synthetic Langfuse smoke (two synthetic traces)
RFQ_LANGFUSE_SMOKE=1 .venv/bin/python -m pytest -q -rs -s tests/test_langfuse_live_smoke.py

# 7. Gated Ollama (ollama serve; model pulled; development DB seeded for the second file)
RFQ_OLLAMA_SMOKE=1 .venv/bin/python -m pytest -q -rs -s tests/test_intake_ollama.py tests/test_ollama_smoke.py

# 8. Gated authenticated live MCP (empty rfq_copilot_test; nothing else using it)
RFQ_AGENT_LIVE_MCP=1 .venv/bin/python -m pytest -q -rs tests/test_agent_tools.py tests/test_intake_live_mcp.py \
  tests/test_manufacturing_agent_live_mcp.py tests/test_supply_agent_live_mcp.py tests/test_pricing_agent_live_mcp.py \
  tests/test_quotation_agent_live_mcp.py tests/test_specialized_agent_integration_live_mcp.py

# 9. Final full regression with every authorized flag (Langfuse variables still exported)
RFQ_AGENT_LIVE_MCP=1 RFQ_OLLAMA_SMOKE=1 RFQ_LANGFUSE_SMOKE=1 .venv/bin/python -m pytest -q -rs

# Afterwards: disable telemetry again
unset RFQ_LANGFUSE_ENABLED LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY
```

Expected for step 9: 0 skipped and 0 failed if every service is available. Any skip or failure
is reported as such; a skip is not a pass.

## 9. Outstanding deployment dependencies

Signed durable evidence and key custody (L04/L21/L26); a durable recovery store and
cross-worker leases (L05); idempotency keys for future write tools; production telemetry
retention, dashboards and alerting (L07); review of any additional OpenTelemetry exporter
(D50-08); multi-worker isolation, secrets management and network policy (L27–L34); the Phase 8
approval/persistence/release workflow (L08–L17).

## 10. Files changed in this workstream

- `src/rfq_copilot/recovery.py` — `_reap`, `_request_cancel`, `_owned(reap=)`, `BaseException`
  settlement and publication guard in `_run`, `cancel()`/`close()`/`_prune` use them.
- `src/rfq_copilot/supervisor.py` — `_execute` closes the trace on any `BaseException` (D50-07).
- `tests/test_recovery_hardening.py` (new).
- `docs/audits/pre_phase_08_hardening.md` (new).

## 11. Independent-audit remediation (D50-10 – D50-12, CSV handles)

Written after the focused verification above; **not yet run**.

- **D50-10 — Langfuse partial failures (`observability.LangfuseSink`).** `_child` now ends a
  newly opened observation if labelling it fails and re-raises (`emit` contains it), so no
  unlabelled handle is stored. Stage and workflow finishes release their references *before*
  any SDK call, then label and end through `_finish` (end attempted even if the label fails);
  each `end()` is best effort (`_end`), so one failing observation never blocks the others.
  A failed root is never stored, so the run's later events are dropped and a later trace starts
  normally. Event names, parent-child structure, metadata and `MAX_OPEN_RUNS` are unchanged.
  An SDK failure means the observation may never reach Langfuse: nothing claims otherwise.
- **D50-11 — approved telemetry vocabularies.** `StageOutcome.reason` (from the free-`str`
  `not_evaluated_reason` of A2–A5), `McpCallError.code` (any string at runtime) and
  `RecoveryResult.code` were not statically fixed codes. Every Supervisor event now passes
  through `supervisor.telemetry_code` and every recovery event through
  `SupervisorRecovery._event`. Values outside the approved vocabularies are exported as
  `"unlisted"`. The vocabularies are derived from the agents' own `Literal` types, the
  `<stage>_<status>` gate codes, the Supervisor `ErrorCode`, `Decision`, `RouteAction` and
  disposition categories, MCP `FailureCode`, and the fixed codes enumerated from source.
  Business results are unchanged; the internal codes stay available to operators.
- **D50-12 — terminal timing without an event loop.** `_elapsed_ms` and every telemetry
  duration now use `time.monotonic()` (clamped to the event bound), so the terminal
  `workflow_finished` can be built while a coroutine is closed outside any running loop. The
  business deadline logic (`_Custody.deadline`, `_remaining`) still uses the loop clock.
- **CSV handles (tests only).** `test_day07_materials.py` (2 reads), `test_day08_manufacturing.py`,
  `test_day09_inventory.py`, `test_day10_capacity.py` and `test_grounding.py` now read with
  `with`; values, paths and assertions are unchanged.

Tests: `tests/test_telemetry_hardening.py` (new; partial failures at creation, new-child
update, stage-finish update, workflow-finish update and termination; later traces start;
adversarial code-shaped values through typed A2/A3 results, a forged MCP failure code and the
recovery event; every real Day 45 scenario exports its codes unchanged; loop-free terminal
event). `tests/test_supervisor_observability.py`: the fake SDK gains `fail_start`,
`fail_update` and `fail_end`.

Commands for this remediation:

```bash
cd ~/RFQ_CoPilot
.venv/bin/python -m pytest -q -W error tests/test_telemetry_hardening.py
.venv/bin/python -m pytest -q -W error tests/test_recovery_hardening.py tests/test_supervisor_observability.py
.venv/bin/python -m pytest -q -W error tests/test_day07_materials.py tests/test_day08_manufacturing.py \
  tests/test_day09_inventory.py tests/test_day10_capacity.py tests/test_grounding.py
.venv/bin/python -m pytest -q tests/test_supervisor_recovery.py tests/test_supervisor_delegation.py \
  tests/test_supervisor_parallel.py tests/test_supervisor_escalation.py tests/test_supervisor_end_to_end.py \
  tests/test_supervisor.py tests/test_supervisor_routing.py
.venv/bin/ruff check .
.venv/bin/ruff format --check src tests alembic
.venv/bin/python -m pytest -q -rs
```

The day-7 to day-10 suites are database integration tests: the strict run needs the guarded
test database configured, as the default suite does.
