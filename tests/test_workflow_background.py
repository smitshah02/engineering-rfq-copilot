"""Offline process-lifecycle tests, separately parameterized for worker and drafter.

These exercise scheduling and cleanup with injected queue operations. Real PostgreSQL
claim exclusion, fencing and quotation idempotency remain in the *_database selections.
"""

import asyncio
import signal
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import pytest

from rfq_copilot import background
from rfq_copilot import workflow_drafter as wd
from rfq_copilot import workflow_worker as ww
from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database, DatabaseError, PrivilegedRuntimeRole


@pytest.fixture(params=["worker", "drafter"])
def consumer(request):
    if request.param == "worker":
        module, settings_type = ww, ww.WorkerSettings
        service = ww.Worker(None, settings_type(poll_seconds=0.05), None)
        run = ww.Claim("run", "rfq", "customer", 1, 1)
    else:
        module, settings_type = wd, wd.DrafterSettings
        service = wd.Drafter(None, settings_type(poll_seconds=0.05))
        run = wd.DraftClaim("run", "rfq", "customer", 1, 1)
    return SimpleNamespace(module=module, settings_type=settings_type, service=service, run=run)


@pytest.mark.parametrize(
    "over",
    [
        {"poll_seconds": 0},
        {"poll_seconds": 61},
        {"poll_seconds": float("nan")},
        {"error_backoff_max_seconds": float("inf")},
        {"error_backoff_max_seconds": 4},
        {"shutdown_grace_seconds": -1},
        {"shutdown_grace_seconds": float("nan")},
        {"shutdown_grace_seconds": 301},
    ],
)
def test_poll_backoff_and_shutdown_bounds(consumer, over):
    with pytest.raises(ValueError):
        consumer.settings_type(**over)


@pytest.mark.asyncio
async def test_empty_queue_waits_between_polls_and_stops(consumer, monkeypatch):
    polls, waits = [], []

    async def claim(*args):
        polls.append(1)
        return None

    async def pause(stop, seconds):
        waits.append(seconds)
        if len(waits) == 3:
            stop.set()

    monkeypatch.setattr(consumer.module, "claim", claim)
    monkeypatch.setattr(background, "pause", pause)
    await consumer.service.serve()
    assert len(polls) == 3 and waits == [0.05] * 3
    assert consumer.service._running == {}


@pytest.mark.asyncio
async def test_continuous_mode_discovers_and_processes_work(consumer, monkeypatch):
    claimed, processed = [], []

    async def claim(*args):
        if not claimed:
            claimed.append(consumer.run)
            return consumer.run
        return None

    async def process(*args):
        processed.append(args[-1])
        consumer.service.stopping.set()
        return "completed"

    monkeypatch.setattr(consumer.module, "claim", claim)
    monkeypatch.setattr(consumer.module, "process", process)
    await asyncio.wait_for(consumer.service.serve(), 1)
    assert claimed == processed == [consumer.run]
    assert consumer.service._running == {}


@pytest.mark.asyncio
async def test_database_claim_outage_backs_off_to_cap_then_recovers(consumer, monkeypatch, caplog):
    polls, waits = [], []
    consumer.service.settings = consumer.settings_type(poll_seconds=1, error_backoff_max_seconds=4)

    async def claim(*args):
        polls.append(1)
        if len(polls) <= 4:
            raise DatabaseError("secret database password")
        return consumer.run

    async def pause(stop, seconds):
        waits.append(seconds)
        await asyncio.sleep(0)

    async def process(*args):
        consumer.service.stopping.set()
        return "completed"

    monkeypatch.setattr(consumer.module, "claim", claim)
    monkeypatch.setattr(consumer.module, "process", process)
    monkeypatch.setattr(background, "pause", pause)
    await asyncio.wait_for(consumer.service.serve(), 1)
    assert waits[:5] == [1, 2, 4, 4, 1]
    assert "queue_unavailable" in caplog.text and "secret" not in caplog.text


@pytest.mark.asyncio
async def test_unsafe_role_stops_instead_of_retrying(consumer, monkeypatch):
    async def claim(*args):
        raise PrivilegedRuntimeRole("unsafe role")

    monkeypatch.setattr(consumer.module, "claim", claim)
    with pytest.raises(PrivilegedRuntimeRole):
        await consumer.service.serve()
    assert consumer.service.stopping.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("release_outage", [False, True])
@pytest.mark.parametrize("once", [False, True])
async def test_signals_cancel_then_release_or_leave_lease_for_recovery(
    consumer, monkeypatch, caplog, sig, release_outage, once
):
    callbacks, removed, events = {}, [], []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: callbacks.update({sig: cb}))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))
    consumer.service.settings = consumer.settings_type(poll_seconds=0.05, shutdown_grace_seconds=0)

    async def claim(*args):
        return consumer.run

    async def process(*args):
        events.append("started")
        callbacks[sig]()  # invoke the registered handler without signaling the test runner
        try:
            await asyncio.Event().wait()
        finally:
            events.append("process_closed")

    async def release(*args):
        events.append("release")
        if release_outage:
            raise DatabaseError("secret credentials")
        return True

    monkeypatch.setattr(consumer.module, "claim", claim)
    monkeypatch.setattr(consumer.module, "process", process)
    monkeypatch.setattr(consumer.module, "release", release)
    await asyncio.wait_for(background.run(consumer.service, once=once), 1)
    assert events == ["started", "process_closed", "release"]
    assert removed == [signal.SIGINT, signal.SIGTERM]
    assert consumer.service._running == {}
    assert "secret" not in caplog.text
    if release_outage:
        assert "recovery=lease" in caplog.text


@pytest.mark.asyncio
async def test_failed_settlement_is_observed_and_polling_continues(consumer, monkeypatch, caplog):
    calls = []

    async def claim(*args):
        if not calls:
            calls.append("claim")
            return consumer.run
        return None

    async def process(*args):
        raise DatabaseError("secret database URL")

    async def failed_settlement(*args):
        raise DatabaseError("secret database URL")

    async def pause(stop, seconds):
        await asyncio.sleep(0)
        if "settlement_unavailable" in caplog.text:
            stop.set()

    monkeypatch.setattr(consumer.module, "claim", claim)
    monkeypatch.setattr(consumer.module, "process", process)
    if consumer.module is ww:
        monkeypatch.setattr(ww, "_settle_failure", failed_settlement)
    monkeypatch.setattr(background, "pause", pause)
    await asyncio.wait_for(consumer.service.serve(), 1)
    assert "settlement_unavailable recovery=lease" in caplog.text
    assert "secret" not in caplog.text and consumer.service._running == {}


@pytest.mark.asyncio
async def test_once_keeps_one_batch_and_handles_a_later_claim_failure(consumer, monkeypatch):
    consumer.service.settings = consumer.settings_type(concurrency=2)
    claimed, processed = [], []

    async def claim(*args):
        claimed.append(1)
        if len(claimed) == 2:
            raise DatabaseError("offline")
        return consumer.run

    async def process(*args):
        processed.append(args[-1])
        return "completed"

    monkeypatch.setattr(consumer.module, "claim", claim)
    monkeypatch.setattr(consumer.module, "process", process)
    with pytest.raises(DatabaseError):
        await consumer.service.run_once()
    assert processed == [consumer.run] and consumer.service._running == {}


@pytest.mark.asyncio
async def test_once_does_not_poll_or_apply_continuous_backoff(consumer, monkeypatch):
    calls = []

    async def claim(*args):
        calls.append("claim")
        return consumer.run

    async def process(*args):
        calls.append("process")
        return "completed"

    async def forbidden(*args):
        raise AssertionError("--once must not enter continuous polling")

    monkeypatch.setattr(consumer.module, "claim", claim)
    monkeypatch.setattr(consumer.module, "process", process)
    monkeypatch.setattr(background, "pause", forbidden)
    assert await consumer.service.run_once() == 1
    assert calls == ["claim", "process"]


@pytest.mark.asyncio
async def test_once_collects_the_batch_before_processing_a_fast_retry(consumer, monkeypatch):
    consumer.service.settings = consumer.settings_type(concurrency=2)
    events, ready = [], [consumer.run]

    async def claim(*args):
        await asyncio.sleep(0)  # a real claim yields during its database I/O
        events.append("claim")
        return ready.pop() if ready else None

    async def process(*args):
        events.append("process")
        ready.append(consumer.run)  # an immediately retryable technical settlement
        return "queued" if consumer.module is ww else "completed"

    monkeypatch.setattr(consumer.module, "claim", claim)
    monkeypatch.setattr(consumer.module, "process", process)
    assert await consumer.service.run_once() == 1
    assert events == ["claim", "claim", "process"]


@pytest.mark.parametrize("argv", [["--continuous"], [], ["--once", "--continuous"]])
def test_continuous_cli_requires_target_and_modes_are_exclusive(consumer, argv):
    with pytest.raises(SystemExit) as exc:
        consumer.module.main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("value", ["0", "0.05", "61", "nan", "inf", "secret"])
def test_cli_rejects_bad_poll_before_database_or_model_construction(consumer, monkeypatch, value):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid settings reached database configuration")

    monkeypatch.setattr(DatabaseConfig, "from_serving_env", forbidden)
    with pytest.raises(SystemExit) as exc:
        consumer.module.main(["--target", "test", "--continuous", "--poll-seconds", value])
    assert exc.value.code == 2


@pytest.mark.parametrize("fail", [False, True])
def test_cli_uses_selected_role_and_closes_database(consumer, monkeypatch, fail):
    events, settings = [], []
    prefix = "RFQ_WORKFLOW" if consumer.module is ww else "RFQ_DRAFTER"
    monkeypatch.setenv(f"{prefix}_POLL_SECONDS", "7")
    monkeypatch.setenv(f"{prefix}_ERROR_BACKOFF_MAX_SECONDS", "28")
    monkeypatch.setenv(f"{prefix}_SHUTDOWN_GRACE_SECONDS", "11")

    def config(target, **flags):
        events.append((target, flags))
        return object()

    class FakeDatabase:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            events.append("open")
            return self

        async def __aexit__(self, *args):
            events.append("close")

    async def serve(service):
        settings.append(service.settings)
        events.append("serve")
        if fail:
            raise PrivilegedRuntimeRole("unsafe configuration")

    monkeypatch.setattr(DatabaseConfig, "from_serving_env", config)
    import rfq_copilot.database as db_module

    monkeypatch.setattr(db_module, "Database", FakeDatabase)
    monkeypatch.setattr(type(consumer.service), "serve", serve)
    if consumer.module is ww:
        monkeypatch.setattr(ww, "_intake_graph_from_env", lambda *a, **kw: object())
        monkeypatch.setattr(ww, "_mcp_from_env", lambda *a: object())
    result = consumer.module.main(["--target", "test", "--continuous", "--poll-seconds", "3"])
    assert result == (1 if fail else 0)
    flag = "workflow" if consumer.module is ww else "drafter"
    assert events == [("test", {flag: True}), "open", "serve", "close"]
    assert settings[0].poll_seconds == 3
    assert settings[0].error_backoff_max_seconds == 28
    assert settings[0].shutdown_grace_seconds == 11


@pytest.mark.asyncio
async def test_database_session_context_rolls_back_and_closes_without_a_connection():
    events = []

    class Session:
        async def rollback(self):
            events.append("rollback")

    @asynccontextmanager
    async def session_factory():
        events.append("open")
        try:
            yield Session()
        finally:
            events.append("close")

    # Exercise the real context manager with a fake factory, never build an engine.
    database = object.__new__(Database)
    database._disposed = False
    database.config = SimpleNamespace(runtime=False, workflow=False, drafter=False, decision=False)
    database._session_factory = session_factory
    with pytest.raises(DatabaseError):
        async with database.session():
            raise OSError("secret connection detail")
    assert events == ["open", "rollback", "close"]


@pytest.mark.asyncio
async def test_worker_awaits_supervisor_and_heartbeat_before_returning_claim(monkeypatch):
    rules = {"manufacturing": object(), "pricing": object(), "approval": object()}
    prepared = ww.Prepared(SimpleNamespace(user_id="service"), (), "source", rules)
    started, closed = asyncio.Event(), asyncio.Event()

    async def prepare(*args):
        return prepared

    async def supervisor(context):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed.set()

    monkeypatch.setattr(ww, "prepare", prepare)
    monkeypatch.setattr(ww.sv, "run_supervisor_workflow", supervisor)
    deps = ww.Dependencies(object(), lambda principal: object())
    run = ww.Claim("run", "rfq", "customer", 1, 1)
    task = asyncio.create_task(ww.process(None, ww.WorkerSettings(), deps, run))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_worker_owns_and_closes_both_ollama_clients_on_configuration_failure(monkeypatch):
    import langchain_ollama

    from rfq_copilot import intake

    events = []

    async def close_async():
        events.append("async_closed")

    model = SimpleNamespace(
        _client=SimpleNamespace(close=lambda: events.append("sync_closed")),
        _async_client=SimpleNamespace(close=close_async),
    )
    monkeypatch.setattr(langchain_ollama, "ChatOllama", lambda **kw: model)
    monkeypatch.setattr(intake, "build_intake_graph", lambda model: object())
    with pytest.raises(ValueError):
        async with AsyncExitStack() as resources:
            ww._intake_graph_from_env(
                {"RFQ_WORKFLOW_LLM": "ollama", "RFQ_WORKFLOW_OLLAMA_MODEL": "offline"},
                resources=resources,
            )
            raise ValueError("later configuration refusal")
    assert events == ["async_closed", "sync_closed"]
