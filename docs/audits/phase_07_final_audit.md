# Phase 7 final audit — Supervisor (Days 44–50)

Status: **not closed.** Sections 1–16 are the Day 50 audit as written before verification;
their "authored"/"pending" wording is kept as the historical record. Verification results
reported by the user since then, and the pre-Phase 8 hardening status, are in §17. The final
post-hardening full regression is still pending, and Phase 7 is not formally closed.

## 1. Executive summary

The Supervisor runs the six Phase 6 stages (A1 extraction, A1 validation, A2 manufacturing, A3
supply, A4 pricing, A5 quotation review) as a strict chain in one LangGraph graph. A
deterministic `decide()` picks the next stage from validated outcomes; a single delegation node
calls the existing agent once and records its outcome through `record_outcome`, which repeats
the Phase 6 status, handoff, identity, source and rule checks. Typed results stay in an
attempt-scoped `_Custody` object, never in graph state. A2 checks parts concurrently within a
trusted bound and joins them deterministically. Day 48 turns the settled route into a
disposition; Day 49 adds in-process pause, resume, restart and cancellation. Day 50 adds
opt-in, allow-listed telemetry with a Langfuse exporter, end-to-end and adversarial tests, and
this audit.

The source review found **no confirmed critical or high defect** in development scope. Two low
findings and four informational items are recorded (§13). Production readiness depends on
deferred items: durable signed evidence (L04/L21/L26), durable recovery (L05), deployment
security and monitoring (L27–L34, L07 production part) and the Phase 8 business workflow
(L08–L17).

## 2. Architecture and integration

| Concern | Where | Notes |
|---|---|---|
| Entry point | `supervisor.run_supervisor_workflow(context)`; recovery via `recovery.SupervisorRecovery` | Takes only a `SupervisorContext`; no state, results or graph |
| Trusted inputs | `SupervisorContext` (LangGraph run context) | Principal, principal-bound executor, ids, text, pinned rules, horizon, concurrency, telemetry; never in state |
| State ownership | `owned_update`, `_WRITES` | Trusted keys written once by `trusted_entry`; outcomes only by `record_outcome` |
| Routing | `decide`, `attach_routing` | Prefix of `STAGE_PLAN`, prerequisites from `consumes`; terminal nodes reachable only through their edge |
| Handoffs | `_arguments`, `_Custody.held` | Each stage receives exactly this attempt's kept upstream objects |
| Output | `SupervisorExecutionResult` | Codes, route, disposition; draft only via `customer_view()`; `approved`/`released` are `Literal[False]` |
| Checkpointing | none on the graph (`_settle` refuses one); `RecoveryRecord` metadata only | `graph.SAFE_SERDE` unchanged |
| Observability | `observability.py`; `SupervisorContext.telemetry`; `SupervisorRecovery(telemetry=)` | Day 50, see `docs/architecture/supervisor_observability.md` |

## 3. A1–A5 execution verification

- Order and single execution: `test_delegation_runs_a1_to_a5_once_each_in_order` (Day 46);
  `test_e2e_invariant_every_scenario_keeps_the_business_contract` (Day 50, all 29 offline
  scenarios: 11 Day 45, 8 two-part Day 47, 10 new Day 50).
- New Day 50 scenarios: missing material, missing length, missing width, grade not in catalog,
  material outside the catalog categories, catalog outage, feasibility-tool timeout, forbidden
  inventory tool, pricing-tool outage, quotation-tool invalid output. Their expected outcomes
  are not hand-written: each is compared with the Phase 6 baseline
  (`test_e2e_parity_new_scenarios_match_the_sequential_baseline`), plus rule-derived
  invariants (incomplete or unknown material stops at A1 validation; a tool failure is never a
  business conclusion and is never retried).

## 4. Routing and escalation

`decide()` is unchanged since Day 45. Telemetry adds `route_decided` events but nothing reads
them. Clarification, blocking, review, draft and failure dispositions come from
`escalation.for_result` (Day 48). Day 50 tests check, for every scenario, that the emitted
stage and route codes equal the run's own outcomes and route
(`test_trace_every_scenario_reports_its_real_route_and_disposition`).

## 5. Business parity with Phase 6

Parity tests compare stage statuses, invoked stages, tool calls, clarifications, review
reasons, failure reason, draft availability and the customer view with
`agent_integration.evaluate_rfq_workflow` (Day 45 routing, Day 46 delegation, Day 47 two-part,
Day 50 new scenarios). Known, documented differences only: A2 concurrency changes the order of
concurrent tool calls, not the results (Day 47); the Supervisor fails closed with fixed codes
where Phase 6 receives untrusted inputs (admission errors). No live-data timing difference
applies offline.

Business invariants asserted over every scenario (Day 50): a draft exists only from this
attempt's accepted A5 result with a draft status (`wf._DRAFT`: `draft_ready_for_review` or
`requires_review`) and exactly when the Phase 6 baseline has one; a completed route always has
a draft; an A5 review route may carry a **conditional advisory draft** that still requires
human review (e.g. constrained supply, unachievable delivery, borderline bend); a route that
stops before A5, or a clarification, blocked or failed route, never has one (D50-V02);
a draft implies A2 `feasible`; `not_quotable` only from A2 `infeasible`; constrained
supply continues to pricing; the draft is conditional while account and credit checks are
unevaluated (`customer_status: not_verified`); its notice says it is not a quote or commitment;
the delivery date is `estimated_ready_date`; customer-facing text contains no cost, margin,
evidence, fingerprint, "reserved" or "guarantee"; nothing is approved, released or dispatched;
no stage runs after a stop.

## 6. Principal and MCP security

- Identity: the customer id comes only from the verified `Principal`
  (`_admit`); customer text naming another customer, role or approval changes nothing
  (`test_e2e_security_customer_identity_comes_only_from_the_principal`).
- MCP: the Supervisor calls only the injected principal-bound executor (`graph.mcp_executor`;
  tests forbid `McpToolClient.call`). Agent tool allow-lists (`ALLOWED_TOOLS`) are subsets of
  `graph.READ_ONLY_TOOLS` (Phase 6 tests). Day 50 adds no tool, no gateway bypass and no new
  JWT/RBAC code. The telemetry wrapper passes the same arguments to the same executor and
  re-raises the same exception.
- Rules: pricing, approval and manufacturing manifests are checked at admission; a drifted
  fingerprint or the wrong manifest runs no agent
  (`test_e2e_security_untrusted_rule_manifest_runs_no_agent`).
- Recovery: owner derived from the principal on every call; foreign ids get one opaque refusal
  (Day 49; telemetry tests confirm the refusal event carries no handle or owner).
- Financial confidentiality: `summary()` excludes the draft and holds no amount; the customer
  view has selling prices only; telemetry has no field for amounts.

## 7. Evidence provenance

Chain of custody is tested end to end
(`test_e2e_evidence_each_stage_receives_this_attempts_upstream_objects`): validation receives
the extraction object A1 returned in this attempt; A2 the validation; A3 the A2 result and the
same extraction; A4 the A2 and A3 results; A5 all three; A2 parts keep their index and
specification; the extraction's source sha256 equals the trusted identity; recorded rule
fingerprints equal the pinned ones. A result of the wrong type stops the workflow
(`test_e2e_evidence_wrong_result_type_fails_closed_and_stops`). These are in-process
consistency checks: evidence is not signed and serialized results are not authenticated
(L04/L21/L26 stay open).

## 8. Concurrency and fan-in

A2 uses a semaphore-bounded `TaskGroup` (Day 47) over independent read-only checks; `_join`
requires exactly one result per expected part, in Intake order, each re-derivable from its own
evidence. Cancellation cancels and awaits every worker. Day 50 telemetry reports part count,
bound, completed and failed counts and the join outcome (`a2_fanout`), tested for parallel,
sequential, a failing part, a refused join and cancellation. No new parallelism was added: the
six stages depend on each other.

## 9. Recovery and cancellation

Day 49 behaviour is unchanged. Day 50 adds telemetry only: `recovery` events per operation,
`run_kind` resume/restart, `previous_attempt` on restart, `carried` on re-recorded stages,
acknowledgement (`cancel_requested`) distinct from settlement (`cancelled`). Process-local
limits remain (no cross-process continuation, no exactly-once tools).

## 10. Langfuse integration

Opt-in exporter behind `RFQ_LANGFUSE_ENABLED=true`; Langfuse SDK v5 as an optional extra;
explicit observations with metadata only; no LangChain callback; only the SDK's own spans
exported; bounded open runs; failures contained. Live export has **not** been run.

## 11. Telemetry confidentiality

Structural: the `Event` allow-list cannot express text, identifiers, amounts or secrets.
Tested adversarially (Day 50): code-shaped customer tokens, prompt injection, exception text
containing a database URL and a key, executor errors, unlisted tool names, all eleven Day 45
scenarios checked for ids, customer text, the source hash, rule fingerprints, pricing amounts
and draft amounts; recovery events checked for recovery ids and owner hashes.

## 12. Test matrix

| Group | File | Status |
|---|---|---|
| A Full workflow | `test_supervisor_end_to_end.py`, `test_supervisor_delegation.py` | Day 46 verified; Day 50 authored |
| B Routing and parity | `test_supervisor_routing.py`, `test_supervisor_end_to_end.py` | Day 45 verified; Day 50 authored |
| C Security and authorization | `test_supervisor_end_to_end.py` (`test_e2e_security_*`), Day 44–49 suites | Earlier verified; Day 50 authored |
| D Evidence and provenance | `test_e2e_evidence_*`, Day 46 `test_l03_*` | As above |
| E A2 concurrency | `test_supervisor_parallel.py`, `test_trace_a2_*` | Day 47 verified; Day 50 authored |
| F Recovery and cancellation | `test_supervisor_recovery.py`, `test_recovery_*` (observability) | Day 49 verified; Day 50 authored |
| G Langfuse metadata and privacy | `test_supervisor_observability.py` (`test_contract_*`, `test_privacy_*`, `test_langfuse_*`) | Authored |
| H Langfuse failure and disabled | `test_config_*`, `test_failure_*` | Authored |
| I Adversarial | spread across the above | Authored |
| J Phase 5/6 compatibility | existing suites; Day 50 changes no Phase 5/6 module | To be re-run |

Results: **pending** (user to run the commands in `docs/dev_log/day_50.md`).

### Opt-in skips

Nine existing opt-in tests are skipped by default; Day 50 adds one (a tenth).

| File | Gate | Disposition |
|---|---|---|
| `test_intake_ollama.py` | `RFQ_OLLAMA_SMOKE=1` | Local model, optional |
| `test_ollama_smoke.py` | `RFQ_OLLAMA_SMOKE=1` | Local model + database, optional |
| `test_intake_live_mcp.py` | `RFQ_AGENT_LIVE_MCP=1` | Live authenticated MCP |
| `test_manufacturing_agent_live_mcp.py` | `RFQ_AGENT_LIVE_MCP=1` | Live authenticated MCP |
| `test_supply_agent_live_mcp.py` | `RFQ_AGENT_LIVE_MCP=1` | Live authenticated MCP |
| `test_pricing_agent_live_mcp.py` | `RFQ_AGENT_LIVE_MCP=1` | Live authenticated MCP |
| `test_quotation_agent_live_mcp.py` | `RFQ_AGENT_LIVE_MCP=1` | Live authenticated MCP |
| `test_specialized_agent_integration_live_mcp.py` | `RFQ_AGENT_LIVE_MCP=1` | Live authenticated MCP |
| `test_agent_tools.py` (one gated test, line 407) | `RFQ_AGENT_LIVE_MCP=1` | Live authenticated MCP |
| `test_langfuse_live_smoke.py` (new) | `RFQ_LANGFUSE_SMOKE=1` + enabled config | Live Langfuse, never default |

Each of the nine gated files holds one test (from the source), which matches the nine
baseline skips; confirm against the `-rs` output. A skipped test is unverified, not passing. None of the live suites were
re-run for Day 50; Day 50 does not change the agents or the MCP path they exercise.

## 13. Findings register

| ID | Severity | Location | Finding | Status |
|---|---|---|---|---|
| D50-01 | Low (plausible) | `recovery.py:249` `_prune`, `:227` `_owned` | A `running` entry never expires and is never pruned. If its task disappears without settling (event loop closed with the task pending), the slot is held for the process lifetime; `MAX_ENTRIES` such entries refuse new starts (`recovery_capacity`). Impact: availability only, process-local; no data exposure. Fix (deferred): a running deadline (budget + margin) after which the entry is marked failed. | Fixed in pre-Phase 8 hardening (`_reap`, `BaseException` settlement; see §17) |
| D50-02 | Low | `recovery.py:467`, `:487` | `cancel()` / `close()` call `loop.call_soon_threadsafe` on the task's loop; if that loop is already closed, `RuntimeError` reaches the caller after `cancel_requested` is set. The attempt still cannot complete. Fix (deferred): catch `RuntimeError` and treat the entry as settled-cancelled. | Fixed in pre-Phase 8 hardening (`_request_cancel`; see §17) |
| D50-03 | Info | `supervisor.py:1116` `_execute`; `recovery.py` `_run` | With a swallowed cancellation the late run emits a normal `workflow_finished`; the following `recovery` event says `cancelled`. Telemetry is not authority. | Tested (`test_recovery_swallowed_cancel_*`) |
| D50-04 | Info | `llm.py`, intake graph | No safe token-usage or provider-cost data is exposed, so no generation observations are emitted. | Documented |
| D50-05 | Info | `observability.from_env` | Reads the process environment, not `.env`; the application must export the variables (deliberate: nothing enables implicitly). | Documented |
| D50-06 | Info | `recovery.py:500` `adopt` | Unauthenticated records can consume capacity until they expire (part of L04/L21/L26). | Carried |

Day 50 self-review fix before hand-off: the `mask` scalar pattern initially rejected agent ids
(`A1`–`A5`), which would have redacted all stage metadata in Langfuse; corrected and covered by
`test_langfuse_maps_one_root_and_ordered_stage_children_with_metadata_only`.

## 14. Limitations L01–L34

Only L01–L07 have individual descriptions in the Phase 6 handoff; L08–L34 were handed over as
named groups (`docs/dev_log/day_44.md`; `specialized_agent_integration.md` §16). They are kept
as groups here rather than invented per number.

| ID | Original scope | Status | Implementation evidence | Test evidence | Remaining risk | Owner |
|---|---|---|---|---|---|---|
| L01 | Sequential A1→A5 only | Resolved within a defined scope | Delegation (Day 46); bounded A2 part concurrency (Day 47) | `test_supervisor_parallel.py` (verified Day 47); Day 50 `test_trace_a2_*` (authored) | Stages remain sequential by dependency (intended) | — |
| L02 | No dynamic routing | Resolved and verified (development) | `decide`, conditional edges (Day 45) | `test_supervisor_routing.py`, parity suites | — | — |
| L03 | A1 extraction/validation pairing not authenticated | Resolved within a defined scope (in-process) | `_Custody` (Day 46) | `test_l03_*`; Day 50 evidence test | Not cryptographic; serialized pairs unauthenticated (L04) | Phase 8 / production |
| L04 | Evidence not signed | Deferred | None | Schema-only checks | Forged durable evidence undetectable | Production |
| L05 | No recovery policy | Partially mitigated | In-process pause/resume/restart/cancel (Day 49) | `test_supervisor_recovery.py` (verified Day 49) | No durable or cross-process continuation; no exactly-once tools; D50-01/02 | Phase 8 / deployment |
| L06 | Internal state may hold financial evidence | Partially mitigated | No evidence in state, results, records or telemetry | Day 46–50 security and privacy tests | Custody holds costs in memory while running or paused (TTL-bounded) | Production |
| L07 | Incomplete workflow observability | Partially mitigated | Allow-listed telemetry + Langfuse exporter (Day 50) | Offline/mocked tests authored; live not run | Live delivery unverified; no production monitoring or alerting | Day 50 smoke test; deployment |
| L08–L17 | Deferred business workflow (quotation persistence, approvals, release, …) | Deferred to Phase 8 | Supervisor never approves, persists, numbers or releases | `Literal[False]` fields; Day 50 invariants | Human approval workflow absent | Phase 8 |
| L18–L25 | Snapshot, evidence, planning and reservation limitations | Carried forward unchanged | No business-rule change; restarts re-read live data | Day 49 replay tests | Snapshot consistency across stages not atomic | Phase 8 / data layer |
| L21 / L26 | Evidence authenticity | Deferred | Consistency checks only | Day 46–48 forgery tests | As L04 | Production |
| L27–L34 | Authorization, confidential diagnostics, deployment | Partially mitigated (development); deployment-only remainder | Principal-bound executor, fixed codes, context-only authority, telemetry allow-list | Day 44–50 security tests | Multi-worker isolation, secrets management, network policy, monitoring | Deployment |

## 15. Development versus production readiness

Development: the orchestration, routing, custody, concurrency, escalation, recovery and
telemetry contracts are implemented and (through Day 49) verified offline. Production needs:
signed durable evidence; a durable, multi-worker recovery store; exactly-once or idempotent
tool policy; the Phase 8 approval/persistence/release workflow; secrets management; deployment
monitoring and alerting; a verified live Langfuse export and a data-retention decision.

## 16. Remaining Phase 8 / deployment dependencies

L04/L21/L26 signatures; L05 durable recovery and D50-01/02; L07 live export, dashboards,
alerting; L08–L17 business workflow; L18–L25 snapshot consistency; L27–L34 deployment security.

## 17. Post-audit verification and pre-Phase 8 hardening status

Results reported by the user (not re-run by the agent):

| Run | Result |
|---|---|
| Phase 7 full regression after the Day 50 remediation | 4,123 passed, 10 opt-in skips, 0 failures; Ruff passed; `uv lock --check` passed (`langfuse==5.2.0`, optional extra) |
| `tests/test_recovery_hardening.py` with `-W error` | 14 passed |
| Intake / manufacturing modules with `-W error` | 197 passed |
| Recovery and observability suites | 228 passed |
| Ruff lint and formatting | passed |
| Final post-hardening full regression | **pending** |

Findings status (history in §13 preserved):

| ID | Status |
|---|---|
| D50-01 | Fixed (abandoned running entries settled by `_reap`; abrupt teardown settles in `_run`); focused tests verified (14 passed, `-W error`) |
| D50-02 | Fixed (closed-loop cancellation contained by `_request_cancel`); focused tests verified |
| D50-07 | Fixed (trace closed on any `BaseException`); focused tests verified |
| D50-10, D50-11, D50-12 | Fixed in the independent-audit remediation (Langfuse partial-failure cleanup, telemetry code vocabularies, loop-free terminal timing); tests authored, **not yet run** |

Limitations: L04/L21/L26 remain deferred (no authenticated durable evidence). L05 remains
partially mitigated: in-process recovery, abandonment and cancellation are implemented; durable
continuation, cross-worker exclusion and tool idempotency remain open. L07 live Langfuse
delivery is not verified. Details: `docs/audits/pre_phase_08_hardening.md`.
