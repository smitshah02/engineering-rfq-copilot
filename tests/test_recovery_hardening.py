"""Pre-recovery hardening recovery hardening: telemetry control (abandoned running entries), telemetry control (cancellation on
a closed event loop) and telemetry control (telemetry closed on abrupt teardown). Offline: the specialist workflow
harness, no network, database or model.

Abandonment is driven with synthetic task objects on genuinely closed (or closing) asyncio
event loops: the boundary under test is the recovery store's own bookkeeping. The one real
Supervisor attempt is cancelled and awaited to settlement before its own loop is closed, so no
pending LangGraph or worker task is ever left for garbage collection.

Sections (-k): test_abandon_, test_closed_loop_, test_teardown_."""

import asyncio

import pytest
from test_intake_validation import RULES
from test_specialized_agent_integration import TWO, Router, two_args
from test_supervisor_delegation import ALICE, context
from test_supervisor_observability import IDENTIFIERS, FakeLangfuse, assert_safe
from test_supervisor_parallel import Gate, until
from test_supervisor_recovery import Clock

from rfq_copilot import observability as obs
from rfq_copilot import recovery as rc
from rfq_copilot import supervisor as sv
from rfq_copilot.mcp.client import McpToolClient

A = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def offline_harness(monkeypatch):
    """As delegation: no direct MCP; only the RULES fixture is admitted as the manufacturing rule."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("the Supervisor must use only the injected executor")

    monkeypatch.setattr(McpToolClient, "call", forbidden)
    original = sv._manufacturing_trusted
    monkeypatch.setattr(sv, "_manufacturing_trusted", lambda r: r is RULES or original(r))


class FakeTask:
    def __init__(self, done=False):
        self._done, self.cancels = done, 0

    def done(self):
        return self._done

    def cancel(self):
        self.cancels += 1


def closed_loop():
    loop = asyncio.new_event_loop()
    loop.close()
    return loop


class ClosingLoop:
    """A loop that closes while the cancellation is being delivered (the race in telemetry control)."""

    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed

    def call_soon_threadsafe(self, callback):
        self.closed = True
        raise RuntimeError("Event loop is closed")


def running(recovery, task, loop):
    """A running entry as ``start`` inserts it, with its task and loop set exactly."""
    ctx = context()
    owner, admitted = recovery._admit(ctx)
    entry = recovery._new(owner, admitted, ctx)
    with recovery._lock:
        assert recovery._insert(entry)
    entry.task, entry.loop = task, loop
    entry.custody.carried["held"] = object()  # stands in for held evidence
    return entry


def assert_released(entry, custody, lifecycle, code):
    assert (entry.record.lifecycle, entry.record.code) == (lifecycle, code)
    assert entry.custody is None and entry.task is None and entry.loop is None
    assert custody.carried == {} and entry.result is None


# --- telemetry control: abandoned running entries ------------------------------------------------------


@A
async def test_abandon_task_ended_without_settling_is_reaped_and_frees_capacity():
    recovery = rc.SupervisorRecovery(clock=Clock(), max_entries=1)
    entry = running(recovery, FakeTask(done=True), asyncio.get_running_loop())
    custody = entry.custody
    result = await recovery.start(context())  # insertion prunes: the orphan no longer blocks
    assert result.outcome == "completed"
    assert entry.record.recovery_id not in recovery._entries
    assert_released(entry, custody, "failed", "interrupted")


@A
async def test_abandon_closed_loop_is_reaped_reported_and_never_completes():
    events = obs.MemorySink()
    recovery = rc.SupervisorRecovery(clock=Clock(), telemetry=events)
    entry = running(recovery, FakeTask(), closed_loop())
    custody, rid = entry.custody, entry.record.recovery_id
    assert recovery.prune() == 1
    assert_released(entry, custody, "failed", "interrupted")
    assert recovery.retrieve(ALICE, rid).code == "recovery_unavailable"  # dropped
    reap, refusal = events.events  # the reap, then the retrieve's own refusal event
    assert (reap.operation, reap.outcome, reap.code) == ("reap", "failed", "interrupted")
    assert (refusal.operation, refusal.code) == ("retrieve", "recovery_unavailable")
    assert_safe(events, rid, *IDENTIFIERS)


@A
async def test_abandon_a_live_task_is_never_reaped_even_past_its_ttl():
    clock = Clock()
    recovery = rc.SupervisorRecovery(clock=clock, max_entries=1)
    task = FakeTask()  # pending on an open loop: it may still use its custody
    entry = running(recovery, task, asyncio.get_running_loop())
    custody = entry.custody
    clock.now += rc.DEFAULT_TTL * 10
    assert recovery.prune() == 0
    assert entry.record.lifecycle == "running" and entry.custody is custody
    assert custody.carried  # untouched
    refused = await recovery.start(context())
    assert refused.code == "recovery_capacity"  # the bound holds; nothing is evicted
    busy = await recovery.resume(context(), entry.record.recovery_id, entry.record.generation)
    assert busy.code == "attempt_busy" and entry.custody is custody


@A
async def test_abandon_access_reaps_before_answering():
    recovery = rc.SupervisorRecovery(clock=Clock())
    entry = running(recovery, FakeTask(done=True), asyncio.get_running_loop())
    refused = await recovery.resume(context(), entry.record.recovery_id, entry.record.generation)
    assert refused.outcome == "recovery_refused" and refused.code != "attempt_busy"
    assert entry.record.lifecycle in ("failed", "expired") and entry.custody is None


@A
async def test_abandon_late_result_after_reaping_is_dropped(monkeypatch):
    recovery = rc.SupervisorRecovery(clock=Clock())
    real = sv._execute

    async def execute(graph, context, custody):
        result = await real(graph, context, custody)
        (entry,) = recovery._entries.values()
        entry.task = FakeTask(done=True)  # as if its task had ended: reaped meanwhile
        assert recovery.prune() == 1
        return result  # a valid completed result arrives after the entry was settled

    monkeypatch.setattr(sv, "_execute", execute)
    result = await recovery.start(context())
    assert (result.outcome, result.code, result.result) == ("failed", "interrupted", None)
    assert recovery._entries == {}  # never revived as completed


@A
async def test_abandon_abrupt_teardown_settles_instead_of_staying_running(monkeypatch):
    recovery = rc.SupervisorRecovery(clock=Clock())

    async def torn_down(graph, context, custody):
        raise GeneratorExit  # e.g. the coroutine closed while suspended

    monkeypatch.setattr(sv, "_execute", torn_down)
    with pytest.raises(GeneratorExit):
        await recovery.start(context())
    (entry,) = recovery._entries.values()
    assert (entry.record.lifecycle, entry.record.code) == ("failed", "interrupted")
    assert entry.custody is None and entry.task is None
    assert recovery.prune() == 1  # no result to keep: dropped at once


@A
async def test_abandon_loop_closed_after_insertion_frees_capacity_for_a_new_attempt():
    """The entry is inserted while its loop is open (a live attempt), then that loop closes
    with the task still pending: the slot is recovered, custody released, nothing completed."""
    recovery = rc.SupervisorRecovery(clock=Clock(), max_entries=1)
    loop, task = asyncio.new_event_loop(), FakeTask()
    entry = running(recovery, task, loop)
    custody = entry.custody
    assert recovery.prune() == 0 and entry.custody is custody  # open loop: still live
    loop.close()  # a genuinely closed asyncio loop: the task can never run again
    result = await recovery.start(context())  # insertion reaps and prunes the orphan
    assert result.outcome == "completed"
    assert entry.record.recovery_id not in recovery._entries
    assert_released(entry, custody, "failed", "interrupted")
    assert task.cancels == 0 and not task.done()  # abandoned, never claimed as cancelled


def test_abandon_real_attempt_is_cancelled_and_settled_before_its_loop_closes():
    """A real Supervisor attempt (A2 parts in flight) on its own loop: cancelled, awaited to
    settlement, every task finished, and only then the loop closed. No pending task is left
    for garbage collection; the closed loop leaves nothing for _reap to do."""
    clock = Clock()
    recovery = rc.SupervisorRecovery(clock=clock, max_entries=1)
    loop, gate = asyncio.new_event_loop(), Gate()
    try:
        task = loop.create_task(recovery.start(context(TWO, two_args(), router=Router(manufacturing=gate))))  # fmt: skip
        loop.run_until_complete(until(lambda: gate.active == 2))
        (rid,) = recovery._entries
        entry = recovery._entries[rid]
        custody = entry.custody
        assert entry.record.lifecycle == "running" and entry.task is task
        assert recovery.prune() == 0 and custody.get("intake_validation") is not None  # in use
        ack = recovery.cancel(ALICE, rid)
        assert (ack.outcome, ack.code) == ("cancelled", "cancel_requested")  # requested only
        assert entry.custody is custody  # not cleared while the task still runs
        (outcome,) = loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        assert isinstance(outcome, asyncio.CancelledError)  # cancellation propagated
        assert asyncio.all_tasks(loop) == set()  # workers and graph tasks all finished
        assert gate.active == 0
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()
    assert (entry.record.lifecycle, entry.record.code) == ("cancelled", "cancelled")
    assert entry.custody is None and entry.result is None and entry.task is None
    assert custody.get("intake_validation") is None
    assert recovery.prune() == 0  # settled by the task itself; kept until its TTL
    assert recovery.retrieve(ALICE, rid).code == "not_resumable"  # no late result published
    clock.now += rc.DEFAULT_TTL + 1
    assert recovery.prune() == 1  # capacity returns on the normal TTL


# --- telemetry control: cancellation when the task's loop is closed --------------------------------------


@A
@pytest.mark.parametrize("loop_of", [closed_loop, ClosingLoop], ids=["closed", "closing"])
async def test_closed_loop_cancel_settles_without_raising(loop_of):
    recovery = rc.SupervisorRecovery(clock=Clock())
    task = FakeTask()
    entry = running(recovery, task, loop_of())
    custody, rid = entry.custody, entry.record.recovery_id
    result = recovery.cancel(ALICE, rid)  # no RuntimeError
    assert (result.outcome, result.code) == ("cancelled", None)  # settled, not just requested
    assert entry.cancel_requested is True and task.cancels == 0  # could not be delivered
    assert_released(entry, custody, "cancelled", "cancelled")
    again = recovery.cancel(ALICE, rid)
    assert (again.outcome, again.code) == ("recovery_refused", "not_resumable")
    assert recovery.retrieve(ALICE, rid).code == "not_resumable"


@A
async def test_closed_loop_close_settles_dead_entries_and_only_signals_live_ones():
    recovery = rc.SupervisorRecovery(clock=Clock())
    dead_loop, dead_task = asyncio.new_event_loop(), FakeTask()
    dead = running(recovery, dead_task, dead_loop)  # inserted while its loop is open
    live_task = FakeTask()
    live = running(recovery, live_task, asyncio.get_running_loop())  # this insert prunes
    dead_custody, live_custody = dead.custody, live.custody
    dead_loop.close()  # only now does the first entry's task become unrunnable
    assert set(recovery._entries) == {dead.record.recovery_id, live.record.recovery_id}
    assert dead_custody.carried and live_custody.carried  # nothing cleared yet
    assert recovery.close() == 1  # exactly the dead entry, settled and dropped
    assert (dead.record.lifecycle, dead.record.code) == ("cancelled", "cancelled")
    assert dead.custody is None and dead_custody.carried == {} and dead_task.cancels == 0
    assert live.record.lifecycle == "running" and live.custody is live_custody  # still in use
    assert live_custody.carried and live.cancel_requested
    assert set(recovery._entries) == {live.record.recovery_id}
    await until(lambda: live_task.cancels == 1)  # delivered on the task's own loop
    assert recovery.close() == 0 and live_task.cancels == 1  # idempotent: not resent
    assert recovery.close() == 0 and live.custody is live_custody


@A
async def test_closed_loop_cancel_of_a_live_task_is_only_a_request():
    recovery = rc.SupervisorRecovery(clock=Clock())
    task = FakeTask()
    entry = running(recovery, task, asyncio.get_running_loop())
    first = recovery.cancel(ALICE, entry.record.recovery_id)
    second = recovery.cancel(ALICE, entry.record.recovery_id)
    assert {first.code, second.code} == {"cancel_requested"}  # acknowledgement, not settlement
    await until(lambda: task.cancels == 1)
    await asyncio.sleep(0)
    assert task.cancels == 1 and entry.custody is not None  # sent once; custody still in use


@A
async def test_closed_loop_concurrent_cancel_and_close_from_threads_settle_once():
    recovery = rc.SupervisorRecovery(clock=Clock())
    gate = Gate()
    task = asyncio.create_task(recovery.start(context(TWO, two_args(), router=Router(manufacturing=gate))))  # fmt: skip
    await until(lambda: gate.active == 2)
    (rid,) = recovery._entries
    entry = recovery._entries[rid]
    ack, dropped = await asyncio.gather(
        asyncio.to_thread(recovery.cancel, ALICE, rid), asyncio.to_thread(recovery.close)
    )
    assert dropped == 0 and ack.outcome in ("cancelled", "recovery_refused")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (entry.record.lifecycle, entry.record.code) == ("cancelled", "cancelled")
    assert entry.custody is None and entry.result is None and gate.active == 0


# --- telemetry control: telemetry after abrupt teardown ------------------------------------------------


@A
async def test_teardown_abrupt_exit_still_closes_the_trace(monkeypatch):
    async def torn_down(graph, context, custody):
        raise GeneratorExit

    monkeypatch.setattr(sv, "_run_execute", torn_down)
    sink, client = obs.MemorySink(), FakeLangfuse()
    langfuse = obs.LangfuseSink(client)
    for target in (sink, langfuse):
        with pytest.raises(GeneratorExit):
            await sv.run_supervisor_workflow(context(telemetry=target))
    assert [e.name for e in sink.events] == ["workflow_started", "workflow_finished"]
    assert sink.events[-1].outcome == "interrupted"
    assert langfuse._roots == {} and all(o.ended for o in client.observations)
