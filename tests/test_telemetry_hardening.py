"""Pre-recovery hardening telemetry hardening: Langfuse partial-failure cleanup (Task 1), approved code
vocabularies at the telemetry boundary (Task 2) and terminal events without a running event
loop (Task 3). Offline: the specialist workflow harness, an in-memory sink and a fake Langfuse SDK.

Sections (-k): test_partial_, test_vocabulary_, test_terminal_."""

import dataclasses
import json
import time

import pytest
from test_intake_validation import RULES
from test_specialized_agent_integration import Router
from test_supervisor_delegation import STAGES, context, replace_result
from test_supervisor_escalation import prepared
from test_supervisor_observability import FakeLangfuse
from test_supervisor_parallel import business
from test_supervisor_recovery import Clock
from test_supervisor_routing import SCENARIOS

from rfq_copilot import manufacturing_agent as a2
from rfq_copilot import observability as obs
from rfq_copilot import recovery as rc
from rfq_copilot import supervisor as sv
from rfq_copilot.mcp.client import McpCallError, McpToolClient

A = pytest.mark.asyncio
RUN, OTHER = "a" * 32, "b" * 32


@pytest.fixture(autouse=True)
def offline_harness(monkeypatch):
    """As delegation: no direct MCP; only the RULES fixture is admitted as the manufacturing rule."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("the Supervisor must use only the injected executor")

    monkeypatch.setattr(McpToolClient, "call", forbidden)
    original = sv._manufacturing_trusted
    monkeypatch.setattr(sv, "_manufacturing_trusted", lambda r: r is RULES or original(r))


def send(sink, name, run=RUN, **fields):
    obs.emit(sink, name, run=run, **fields)  # emit contains every SDK failure


def started(sink, run=RUN):
    send(sink, "workflow_started", run, run_kind="start")
    send(sink, "stage_started", run, stage="pricing", agent="A4")


def finished(sink, run=RUN):
    send(sink, "workflow_finished", run, outcome="technical_failure", duration_ms=5)


def named(client, prefix):
    return [o for o in client.observations if o.name.startswith(prefix)]


def assert_clean(sink):
    assert sink._roots == {} and sink._stages == {}  # every local reference released


# --- Task 1: partial SDK failures -----------------------------------------------------------


def test_partial_root_creation_failure_keeps_nothing_and_drops_the_run():
    client = FakeLangfuse(fail_start=("rfq.supervisor.workflow",))
    sink = obs.LangfuseSink(client)
    started(sink)
    send(sink, "route_decided", action="delegate", next_stage="pricing")
    finished(sink)
    assert client.observations == [] and sink._roots == {}
    client.fail_start = ()  # the SDK recovers: the next trace starts normally
    started(sink, OTHER)
    finished(sink, OTHER)
    assert [o.name for o in client.observations] == ["rfq.supervisor.workflow", "rfq.stage.pricing"]  # fmt: skip
    assert all(o.ended for o in client.observations)
    assert_clean(sink)


def test_partial_stage_creation_failure_is_contained_and_the_root_still_ends():
    client = FakeLangfuse(fail_start=("rfq.stage.",))
    sink = obs.LangfuseSink(client)
    started(sink)
    send(sink, "stage_finished", stage="pricing", agent="A4", status="priced", decision="continue")  # fmt: skip
    finished(sink)
    (root,) = client.observations
    assert root.name == "rfq.supervisor.workflow" and root.ended
    assert_clean(sink)


@pytest.mark.parametrize("prefix", ["rfq.supervisor.workflow", "rfq.stage."])
def test_partial_update_failure_on_a_new_child_ends_it_and_keeps_no_handle(prefix):
    client = FakeLangfuse(fail_update=(prefix,))
    sink = obs.LangfuseSink(client)
    started(sink)
    (opened, *_) = named(client, prefix)
    assert opened.ended and opened.metadata == []  # ended at once, never labelled
    finished(sink)
    assert all(o.ended for o in client.observations)
    assert_clean(sink)


def test_partial_stage_finish_update_failure_still_ends_the_stage():
    client = FakeLangfuse()
    sink = obs.LangfuseSink(client)
    started(sink)
    client.fail_update = ("rfq.stage.",)
    send(sink, "stage_finished", stage="pricing", agent="A4", status="priced", decision="continue")  # fmt: skip
    (stage,) = named(client, "rfq.stage.")
    assert stage.ended and sink._stages == {}
    client.fail_update = ()
    finished(sink)
    assert all(o.ended for o in client.observations)
    assert_clean(sink)


def test_partial_workflow_finish_update_failure_still_ends_root_and_stages():
    client = FakeLangfuse()
    sink = obs.LangfuseSink(client)
    started(sink)
    client.fail_update = ("rfq.supervisor.workflow",)
    finished(sink)
    root, stage = client.observations
    assert root.ended and stage.ended and len(root.metadata) == 1  # final label not applied
    assert_clean(sink)


@pytest.mark.parametrize("prefix", ["rfq.stage.", "rfq.supervisor.workflow", "rfq.route_decided"])  # fmt: skip
def test_partial_end_failure_never_blocks_the_other_observations(prefix):
    client = FakeLangfuse(fail_end=(prefix,))
    sink = obs.LangfuseSink(client)
    started(sink)
    send(sink, "route_decided", action="delegate", next_stage="pricing")
    finished(sink)
    for o in client.observations:
        if o.name.startswith(prefix):
            assert o.end_attempts == 1 and not o.ended  # attempted, never claimed delivered
        else:
            assert o.ended
    assert_clean(sink)


@A
async def test_partial_failures_never_change_results_and_later_traces_start():
    plain = await sv.run_supervisor_workflow(context(router=Router()))
    client = FakeLangfuse(fail_update=("rfq.",), fail_end=("rfq.stage.",))
    sink = obs.LangfuseSink(client)
    failing = await sv.run_supervisor_workflow(context(router=Router(), telemetry=sink))
    assert business(failing) == business(plain)
    assert_clean(sink)
    client.fail_update, client.fail_end = (), ()
    healthy = await sv.run_supervisor_workflow(context(router=Router(), telemetry=sink))
    assert business(healthy) == business(plain)
    roots = [o for o in client.observations if o.parent is None and o.metadata]
    assert len(roots) == 1 and roots[0].ended  # the healthy run's trace, labelled and ended
    stages = [o.name for o in client.observations if o.parent is roots[0] and o.name.startswith("rfq.stage.")]  # fmt: skip
    assert stages == [f"rfq.stage.{s}" for s in STAGES]
    assert_clean(sink)


# --- Task 2: approved vocabularies ----------------------------------------------------------


def events_of(sink, name):
    return [e for e in sink.events if e.name == name]


@A
async def test_vocabulary_code_shaped_reason_in_a_typed_result_is_not_exported(monkeypatch):
    token = "acme_secret_project"
    replace_result(monkeypatch, "supply", lambda r: r.model_copy(update={"status": "not_evaluated", "not_evaluated_reason": token}))  # fmt: skip
    sink = obs.MemorySink()
    result = await sv.run_supervisor_workflow(context(telemetry=sink))
    assert (result.route.stopped_at, result.route.reason) == ("supply", token)  # business intact
    (supply,) = [e for e in events_of(sink, "stage_finished") if e.stage == "supply"]
    assert (supply.status, supply.decision, supply.reason) == ("not_evaluated", "not_evaluated", "unlisted")  # fmt: skip
    assert events_of(sink, "route_decided")[-1].reason == "unlisted"
    assert events_of(sink, "workflow_finished")[-1].reason == "unlisted"
    assert token not in json.dumps([e.metadata() for e in sink.events])


@A
async def test_vocabulary_code_shaped_error_reaches_neither_stage_nor_recovery_events(monkeypatch):  # fmt: skip
    token = "cust_008_leak"
    replace_result(monkeypatch, "manufacturing", lambda r: r.model_copy(update={"status": "error", "error": token}))  # fmt: skip
    sink, events = obs.MemorySink(), obs.MemorySink()
    recovery = rc.SupervisorRecovery(clock=Clock(), telemetry=events)
    done = await recovery.start(context(telemetry=sink))
    assert (done.outcome, done.code) == ("failed", token)  # the operator code is kept internally
    (a2_end,) = [e for e in events_of(sink, "stage_finished") if e.stage == "manufacturing"]
    assert a2_end.reason == "unlisted"
    assert events.events[-1].code == "unlisted"
    blob = json.dumps([e.metadata() for e in (*sink.events, *events.events)])
    assert token not in blob


@A
async def test_vocabulary_unexpected_tool_failure_code_is_not_exported():
    class Forged(Router):
        async def __call__(self, tool, request):
            if tool == a2.TOOL:
                raise McpCallError("cust_secret_code")  # not a FailureCode
            return await super().__call__(tool, request)

    sink = obs.MemorySink()
    await sv.run_supervisor_workflow(context(router=Forged(), telemetry=sink))
    statuses = [e.status for e in events_of(sink, "tool_call") if e.tool == a2.TOOL]
    assert statuses == ["unlisted"]
    assert "cust_secret_code" not in json.dumps([e.metadata() for e in sink.events])


@A
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_vocabulary_every_real_scenario_code_is_exported_unchanged(monkeypatch, name):
    """The vocabularies keep trace usefulness: no code a real scenario produces is lost."""
    sink = obs.MemorySink()
    ctx = dataclasses.replace(await prepared(name, monkeypatch), telemetry=sink)
    result = await sv.run_supervisor_workflow(ctx)
    finished_ = events_of(sink, "stage_finished")
    assert [(e.status, e.decision, e.reason) for e in finished_] == [(o.status, o.decision, o.reason) for o in result.outcomes]  # fmt: skip
    terminal = events_of(sink, "route_decided")[-1]
    assert (terminal.action, terminal.outcome, terminal.reason) == (result.route.action, result.route.outcome, result.route.reason)  # fmt: skip
    assert "unlisted" not in json.dumps([e.metadata() for e in sink.events])


def test_vocabulary_normalization_is_per_field_and_never_coerces_unknown_fields():
    assert sv.telemetry_code("status", "priced") == "priced"
    assert sv.telemetry_code("status", "ok", "tool_call") == "ok"
    assert sv.telemetry_code("status", "ok") == "unlisted"  # not an agent status
    assert sv.telemetry_code("status", "priced", "tool_call") == "unlisted"  # not a tool status
    assert sv.telemetry_code("reason", "acme_internal_42") == "unlisted"
    assert sv.telemetry_code("reason", None) is None
    assert sv.telemetry_code("stage", "pricing") == "pricing"  # validated by the Event contract


# --- Task 3: terminal events without an event loop ------------------------------------------


def test_terminal_elapsed_ms_needs_no_loop_and_is_bounded():
    now = time.monotonic()
    assert 0 <= sv._elapsed_ms(now) < 1000
    assert sv._elapsed_ms(now - 10**8) == sv._MAX_MS == 86_400_000  # clamped to the event bound
    assert sv._elapsed_ms(now + 100) == 0  # never negative


class Suspend:
    """Suspends a coroutine once without any event loop (``yield`` inside ``__await__``)."""

    def __await__(self):
        yield


def test_terminal_event_is_emitted_when_a_coroutine_is_closed_outside_any_loop(monkeypatch):
    async def suspended(graph, context, custody):
        await Suspend()

    monkeypatch.setattr(sv, "_run_execute", suspended)
    sink = obs.MemorySink()
    coroutine = sv._execute(None, context(telemetry=sink), sv._Custody())
    coroutine.send(None)  # runs to the suspension: no event loop exists in this test
    assert [e.name for e in sink.events] == ["workflow_started"]
    coroutine.close()  # GeneratorExit at the suspension point, outside any running loop
    end = sink.events[-1]
    assert (end.name, end.outcome) == ("workflow_finished", "interrupted")
    assert isinstance(end.duration_ms, int) and 0 <= end.duration_ms <= sv._MAX_MS
