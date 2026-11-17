# Supervisor observability (L07)

Opt-in, privacy-safe telemetry for the Supervisor, with an optional Langfuse exporter.
Code: `src/rfq_copilot/observability.py`; hooks in `supervisor.py` (delegation node, tool
wrapper, `_execute`) and `recovery.py` (`_reported`). Tests:
`tests/test_supervisor_observability.py`.

## 1. Design

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

## 2. Events

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

## 3. Example safe trace (baseline single-part RFQ)

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

## 4. Langfuse mapping (`LangfuseSink`)

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

## 5. LLM usage

The model abstraction (`llm.py`, A1's intake graph) does not expose token usage or provider
cost safely, so no generation observations are emitted. Prompts and responses are not captured
to obtain usage. Model invocation cost is unrelated to manufacturing cost or selling price;
none of these appear in telemetry.

## 6. What is and is not verified (L07)

| Level | Status |
|---|---|
| Local instrumentation (events at workflow, stage, route, tool, A2 and recovery boundaries) | Implemented; coverage in the Supervisor observability tests |
| Mocked export behaviour (fake SDK: mapping, metadata-only, bounds, failures) | Offline test coverage; does not establish live delivery |
| Live Langfuse delivery | Requires the gated [live smoke test](../../tests/test_langfuse_live_smoke.py); offline checks do not establish delivery |
| Production monitoring, dashboards, alerting, retention policy | Not implemented (deployment) |

## 7. Limits

- Telemetry is not authority. On a swallowed cancellation a late run may emit a normal
  `workflow_finished`; the following `recovery` event records the attempt as `cancelled` and
  no result is published.
- A code-shaped value is a code by origin, not by shape: callers pass agent `Literal` statuses
  and fixed Supervisor codes. Tests feed code-shaped customer text and exception text through
  real runs and assert neither appears.
- Telemetry is process-local and in-memory until the SDK exports it; there is no durable
  telemetry buffer.
