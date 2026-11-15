"""Day 49 checkpointing and recovery (L05): stage-boundary pause, in-process continuation from
the original evidence, explicit restart as a new attempt, retrieval, cancellation, refusal.

Offline: the Phase 6 harness through the Day 46 runner. No network, database or model.

Sections (-k): test_schema_, test_pause_, test_identity_, test_stale_, test_replay_,
test_failure_, test_security_, test_compat_."""

import asyncio
import dataclasses
import hashlib
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_hardening_services import APPROVAL_V12
from test_intake_validation import COMPLETE, RULES
from test_pricing_agent import TRUSTED
from test_specialized_agent_integration import TWO, Router, two_args
from test_supervisor import TOKEN
from test_supervisor_delegation import ALICE, BOB, STAGES, Spy, context, stall
from test_supervisor_escalation import prepared
from test_supervisor_parallel import FailOne, Gate, business, until
from test_supply_agent import MONDAY

from rfq_copilot import ownership
from rfq_copilot import recovery as rc
from rfq_copilot import supervisor as sv
from rfq_copilot.api.auth import Principal
from rfq_copilot.mcp.client import McpToolClient

A = pytest.mark.asyncio
SECRETS = (COMPLETE, TOKEN, "_usd", "cost", "margin", "evidence", "draft")


def assert_metadata_only(text: str):
    """No customer text, credential, amount or evidence; the principal only as its hash."""
    assert not any(s in text for s in SECRETS)
    values = json.loads(text)
    assert ALICE.user_id not in values.values() and ALICE.subject not in values.values()


# The same customer as ALICE, another user: threads and attempts are owned per user.
CAROL = Principal("CUST-008", frozenset({"view_quote"}), user_id="U-2", subject="sub-2")


@pytest.fixture(autouse=True)
def offline_harness(monkeypatch):
    """As Day 46: no direct MCP; only the RULES fixture is admitted as the manufacturing rule."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("the Supervisor must use only the injected executor")

    monkeypatch.setattr(McpToolClient, "call", forbidden)
    original = sv._manufacturing_trusted
    monkeypatch.setattr(sv, "_manufacturing_trusted", lambda r: r is RULES or original(r))


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def recovery(clock):
    return rc.SupervisorRecovery(clock=clock)


async def paused_at(recovery, stage, router=None, **over):
    router = router or Router()
    result = await recovery.start(context(router=router, **over), pause_before=stage)
    assert (result.outcome, result.next_stage) == ("paused", stage), result
    return result, router


def entry(recovery, result):
    return recovery._entries[result.recovery_id]  # test introspection only


# --- A. checkpoint schema -------------------------------------------------------------------


@A
async def test_schema_paused_record_round_trips_and_holds_only_metadata(recovery):
    paused, _ = await paused_at(recovery, "supply")
    text = recovery.export(ALICE, paused.recovery_id)
    record = rc.RecoveryRecord.from_json(text)
    assert record == entry(recovery, paused).record and record.to_json() == text
    assert set(json.loads(text)) == set(rc.RecoveryRecord.model_fields)
    assert (record.lifecycle, record.next_stage, record.generation) == ("paused", "supply", 1)
    assert record.attempt_id == paused.attempt_id and record.budget_left_seconds > 0
    assert_metadata_only(text)
    assert len(text.encode()) <= rc.MAX_RECORD_BYTES


def valid_record(**over) -> dict:
    identity = {"customer_id": "CUST-008", "rfq_id": "RFQ-1", "source_id": "src-1", "source_sha256": "a" * 64}  # fmt: skip
    return {
        "schema_version": 1,
        "recovery_id": "A" * 32,
        "owner": "b" * 64,
        "identity": identity,
        "attempt_id": "c" * 32,
        "generation": 1,
        "rules": [["mfg-v1.0", "test"], ["price-v1.0", "d" * 64]],
        "horizon_start": "2026-11-09",
        "lifecycle": "paused",
        "next_stage": "supply",
        "budget_left_seconds": 100.0,
        "expires_at": 2_000_000.0,
        "restarts": 0,
        "code": None,
    } | over


def test_schema_valid_record_is_accepted():
    assert rc.RecoveryRecord.from_json(json.dumps(valid_record())).next_stage == "supply"


# fmt: off
BAD_RECORDS = [
    pytest.param(json.dumps(valid_record(schema_version=2)), id="unsupported_version"),
    pytest.param(json.dumps(valid_record(extra="x")), id="unexpected_field"),
    pytest.param("{not json", id="malformed"),
    pytest.param(json.dumps(valid_record(code="x" * 10_000)), id="oversized"),
    pytest.param(json.dumps(valid_record(attempt_id="zz")), id="invalid_attempt"),
    pytest.param(json.dumps(valid_record(identity=valid_record()["identity"] | {"source_sha256": "x"})), id="invalid_source_digest"),
    pytest.param(json.dumps(valid_record(rules=[["mfg-v1.0", "bad fingerprint\\n"]])), id="invalid_fingerprint"),
    pytest.param(json.dumps(valid_record(generation="1")), id="string_generation"),
    pytest.param(json.dumps(valid_record(identity={"__class__": "os.system", "args": ["rm"]})), id="object_revival"),
    pytest.param(json.dumps(valid_record(lifecycle="approved")), id="unknown_lifecycle"),
    pytest.param(json.dumps(valid_record(next_stage=None)), id="paused_without_boundary"),
    pytest.param(json.dumps(valid_record(lifecycle="completed")), id="boundary_without_pause"),
    pytest.param(json.dumps(valid_record(recovery_id="short")), id="invalid_recovery_id"),
    pytest.param(json.dumps(valid_record(code="Rule Drift!")), id="free_text_code"),
    pytest.param(json.dumps(valid_record(next_stage="preflight")), id="unplanned_stage"),
    pytest.param(b"\x80\x04\x95pickle", id="pickle_bytes"),
]
# fmt: on


@pytest.mark.parametrize("data", BAD_RECORDS)
def test_schema_invalid_records_fail_closed(data):
    with pytest.raises(ValueError):
        rc.RecoveryRecord.from_json(data)


# --- B. pause and continuation --------------------------------------------------------------


@A
async def test_pause_before_any_work_runs_nothing(recovery, monkeypatch):
    spy = Spy(monkeypatch)
    paused, router = await paused_at(recovery, "intake_extraction")
    assert spy.calls == [] and router.calls == []  # no model call, no tool call
    assert paused.result is None and paused.customer_outcome() is None  # nothing concluded


@A
async def test_pause_continuation_runs_only_the_remaining_stages(recovery, monkeypatch):
    spy = Spy(monkeypatch)
    paused, router = await paused_at(recovery, "supply")
    assert spy.stages() == ["intake_extraction", "intake_validation", "manufacturing"]
    resumed = await recovery.resume(context(router=router), paused.recovery_id, paused.generation)
    assert resumed.outcome == "completed" and resumed.operation == "resume"
    assert spy.stages() == STAGES  # A1 (one model call) and A2 ran once; nothing repeated
    assert router.calls.count("check_manufacturing_feasibility") == 1
    assert resumed.attempt_id == paused.attempt_id == resumed.result.attempt_id  # same attempt
    assert {o.attempt_id for o in resumed.result.outcomes} == {paused.attempt_id}
    # L03 across the pause: A3 got the very extraction A1 returned before it.
    assert spy.one("supply")[2]["extraction"] is spy.one("intake_extraction")[3]
    assert spy.one("supply")[1][0] is spy.one("manufacturing")[3]


@A
@pytest.mark.parametrize("stage", STAGES)
async def test_pause_at_every_boundary_matches_the_uninterrupted_run(recovery, stage):
    paused, router = await paused_at(recovery, stage)
    resumed = await recovery.resume(context(router=router), paused.recovery_id, paused.generation)
    baseline = await sv.run_supervisor_workflow(context())
    assert business(resumed.result) == business(baseline)
    assert resumed.result.disposition == baseline.disposition
    assert resumed.customer_outcome() == baseline.customer_outcome()


@A
async def test_pause_lifecycle_generations_and_retrieval(recovery, monkeypatch):
    paused, router = await paused_at(recovery, "pricing")
    resumed = await recovery.resume(context(router=router), paused.recovery_id, 1, pause_before="quotation")  # fmt: skip
    assert (resumed.outcome, resumed.next_stage, resumed.generation) == ("paused", "quotation", 3)
    done = await recovery.resume(context(router=router), paused.recovery_id, 3)
    assert (done.outcome, done.generation) == ("completed", 5)
    spy = Spy(monkeypatch)
    got = recovery.retrieve(ALICE, paused.recovery_id)
    assert (got.outcome, got.result) == ("completed", done.result) and spy.calls == []


@A
async def test_pause_a_clarification_attempt_finishes_with_its_disposition(recovery, monkeypatch):
    ctx = await prepared("missing_thickness", monkeypatch)
    paused = await recovery.start(ctx, pause_before="intake_validation")
    resumed = await recovery.resume(ctx, paused.recovery_id, paused.generation)
    assert resumed.outcome == "completed"  # the attempt finished; the RFQ needs clarification
    assert resumed.result.disposition.category == "clarification_required"


# --- C. identity and ownership --------------------------------------------------------------


@A
@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"principal": BOB}, "recovery_unavailable"),  # another customer: one opaque answer
        ({"principal": CAROL}, "recovery_unavailable"),  # same customer, another user
        ({"principal": None}, "recovery_unavailable"),
        ({"rfq_id": "RFQ-2"}, "identity_mismatch"),
        ({"source_id": "src-2"}, "identity_mismatch"),
        ({"text": COMPLETE + " Also 5 more."}, "source_changed"),
        ({"horizon_start": MONDAY + timedelta(weeks=1)}, "policy_changed"),
    ],
)
async def test_identity_resume_requires_the_same_owner_and_identity(recovery, over, code):
    paused, router = await paused_at(recovery, "supply")
    refused = await recovery.resume(context(router=router, **over), paused.recovery_id, paused.generation)  # fmt: skip
    assert (refused.outcome, refused.code, refused.result) == ("recovery_refused", code, None)
    ok = await recovery.resume(context(router=router), paused.recovery_id, paused.generation)
    assert ok.outcome == "completed"  # a refusal left the rightful attempt resumable


@A
@pytest.mark.parametrize("bad", ["A" * 32, "../../etc", None, 7, ""])
async def test_identity_unknown_or_malformed_ids_are_one_opaque_refusal(recovery, bad):
    await paused_at(recovery, "supply")
    refused = await recovery.resume(context(), bad, 1)
    assert refused.code == "recovery_unavailable"


@A
@pytest.mark.parametrize("generation", [0, 2, 99, "1", True, None])
async def test_identity_wrong_generation_is_refused(recovery, generation):
    paused, router = await paused_at(recovery, "supply")
    refused = await recovery.resume(context(router=router), paused.recovery_id, generation)
    assert refused.code == "stale_generation"


@A
async def test_identity_other_principals_cannot_read_export_retrieve_or_cancel(recovery):
    paused, router = await paused_at(recovery, "supply")
    rid = paused.recovery_id
    assert recovery.export(BOB, rid) is None and recovery.export(CAROL, rid) is None
    assert recovery.retrieve(BOB, rid).code == "recovery_unavailable"
    assert recovery.cancel(BOB, rid).code == "recovery_unavailable"
    assert (await recovery.restart(context(principal=BOB), rid)).code == "recovery_unavailable"
    assert entry(recovery, paused).record.lifecycle == "paused"  # untouched


@A
async def test_identity_identical_rfqs_keep_separate_attempts(recovery, monkeypatch):
    spy = Spy(monkeypatch)
    one, router_one = await paused_at(recovery, "supply")
    two, router_two = await paused_at(recovery, "supply")
    assert one.recovery_id != two.recovery_id and one.attempt_id != two.attempt_id
    first = await recovery.resume(context(router=router_one), one.recovery_id, 1)
    second = await recovery.resume(context(router=router_two), two.recovery_id, 1)
    extractions = [c[3] for c in spy.calls if c[0] == "intake_extraction"]
    supplies = [c[2]["extraction"] for c in spy.calls if c[0] == "supply"]
    assert supplies[0] is extractions[0] and supplies[1] is extractions[1]  # never crossed
    assert business(first.result) == business(second.result)


# --- D. stale state -------------------------------------------------------------------------


@A
@pytest.mark.parametrize("kind", ["manufacturing", "pricing", "approval"])
async def test_stale_rule_drift_refuses_continuation(recovery, monkeypatch, kind):
    paused, router = await paused_at(recovery, "supply")
    base = {"manufacturing": RULES, "pricing": TRUSTED, "approval": APPROVAL_V12}[kind]
    drifted = dataclasses.replace(base, fingerprint="f" * 64)  # trusted, but not the pinned one
    target, name = {"manufacturing": (sv, "_manufacturing_trusted"), "pricing": (sv.a4, "_trusted"), "approval": (sv.a5, "_approval_trusted")}[kind]  # fmt: skip
    original = getattr(target, name)
    monkeypatch.setattr(target, name, lambda r, *a: r is drifted or original(r, *a))
    spy = Spy(monkeypatch)
    refused = await recovery.resume(context(router=router, **{f"{kind}_rules": drifted}), paused.recovery_id, 1)  # fmt: skip
    assert (refused.outcome, refused.code) == ("recovery_refused", "rules_changed")
    assert spy.calls == []


@A
async def test_stale_untrusted_rules_are_refused_by_admission(recovery):
    paused, router = await paused_at(recovery, "supply")
    refused = await recovery.resume(context(router=router, pricing_rules=None), paused.recovery_id, 1)  # fmt: skip
    assert refused.outcome == "recovery_refused" and refused.code is not None


@A
async def test_stale_expired_pause_clears_its_evidence(recovery, clock):
    paused, router = await paused_at(recovery, "supply")
    custody = entry(recovery, paused).custody
    clock.now += rc.DEFAULT_TTL + 1
    refused = await recovery.resume(context(router=router), paused.recovery_id, 1)
    assert refused.code == "expired"
    assert custody.get("manufacturing") is None and entry(recovery, paused).custody is None


@A
async def test_stale_superseded_attempt_cannot_resume(recovery):
    paused, router = await paused_at(recovery, "supply")
    restarted = await recovery.restart(context(router=Router()), paused.recovery_id)
    assert restarted.outcome == "completed"
    refused = await recovery.resume(context(router=router), paused.recovery_id, 1)
    assert refused.code == "not_resumable"


@A
async def test_stale_missing_evidence_after_restart_of_the_process(recovery, clock):
    paused, _ = await paused_at(recovery, "supply")
    exported = recovery.export(ALICE, paused.recovery_id)
    fresh = rc.SupervisorRecovery(clock=clock)  # a new process: metadata only, no custody
    assert fresh.adopt(exported) is None
    refused = await fresh.resume(context(), paused.recovery_id, 1)
    assert refused.code == "evidence_unavailable"
    restarted = await fresh.restart(context(), paused.recovery_id)  # explicit new evaluation
    assert restarted.outcome == "completed" and restarted.attempt_id != paused.attempt_id


@A
@pytest.mark.parametrize("forgery", ["changed_result", "dropped_result", "forged_outcome"])
async def test_stale_forged_held_evidence_is_refused_before_any_agent(recovery, monkeypatch, forgery):  # fmt: skip
    paused, router = await paused_at(recovery, "supply")
    custody = entry(recovery, paused).custody
    if forgery == "changed_result":  # a "completed" stage whose evidence no longer says so
        custody._results["manufacturing"] = custody._results["manufacturing"].model_copy(update={"status": "infeasible"})  # fmt: skip
    elif forgery == "dropped_result":
        del custody._results["intake_validation"]
    else:  # a completed-stage claim with no matching evidence
        custody.expected = (*custody.expected[:-1], custody.expected[-1].model_copy(update={"status": "requires_review"}))  # fmt: skip
    spy = Spy(monkeypatch)
    refused = await recovery.resume(context(router=router), paused.recovery_id, 1)
    assert (refused.outcome, refused.code, refused.result) == ("recovery_refused", "stale_recovery", None)  # fmt: skip
    assert spy.calls == []  # no stage ran on forged evidence
    assert entry(recovery, paused).custody is None  # and the evidence was discarded


# --- E. replay and duplicate work -----------------------------------------------------------


@A
async def test_replay_duplicate_resume_is_refused(recovery):
    paused, router = await paused_at(recovery, "supply")
    assert (await recovery.resume(context(router=router), paused.recovery_id, 1)).outcome == "completed"  # fmt: skip
    again = await recovery.resume(context(router=router), paused.recovery_id, 1)
    assert again.code == "not_resumable" and router.calls.count("evaluate_quotation") == 1


@A
async def test_replay_concurrent_resumes_run_once(recovery, monkeypatch):
    paused, router = await paused_at(recovery, "supply")
    spy = Spy(monkeypatch)
    results = await asyncio.gather(*(recovery.resume(context(router=router), paused.recovery_id, 1) for _ in range(3)))  # fmt: skip
    assert sorted(r.outcome for r in results) == ["completed", "recovery_refused", "recovery_refused"]  # fmt: skip
    assert {r.code for r in results if r.outcome == "recovery_refused"} == {"attempt_busy"}
    assert spy.stages() == ["supply", "pricing", "quotation"]  # each once


@A
async def test_replay_recovery_and_cancellation_while_running(recovery):
    gate = Gate()
    task = asyncio.create_task(recovery.start(context(TWO, two_args(), Router(manufacturing=gate))))
    await until(lambda: gate.active == 2)
    (rid,) = recovery._entries
    assert (await recovery.resume(context(), rid, 0)).code == "attempt_busy"
    assert (await recovery.restart(context(), rid)).code == "attempt_busy"
    assert recovery.cancel(ALICE, rid).outcome == "cancelled"
    with pytest.raises(asyncio.CancelledError):
        await task
    await until(lambda: gate.active == 0)  # A2 workers settled; no late result kept
    record = recovery._entries[rid].record
    assert (record.lifecycle, record.code) == ("cancelled", "cancelled")
    assert recovery._entries[rid].custody is None and recovery._entries[rid].result is None
    assert (await recovery.resume(context(), rid, record.generation)).code == "not_resumable"
    assert recovery.retrieve(ALICE, rid).code == "not_resumable"  # never completed


@A
async def test_replay_restart_is_a_new_evaluation_not_a_resume(recovery, monkeypatch):
    paused, _ = await paused_at(recovery, "pricing")
    spy = Spy(monkeypatch)
    restarted = await recovery.restart(context(), paused.recovery_id)
    assert (restarted.operation, restarted.restarted_from) == ("restart", paused.recovery_id)
    assert restarted.recovery_id != paused.recovery_id and restarted.attempt_id != paused.attempt_id  # fmt: skip
    assert spy.stages() == STAGES  # everything re-evaluated, A1 included, from fresh context
    assert entry(recovery, paused).record.lifecycle == "superseded"


@A
async def test_replay_restart_after_clarification_takes_the_corrected_source(recovery, monkeypatch):  # fmt: skip
    ctx = await prepared("missing_thickness", monkeypatch)
    first = await recovery.start(ctx)
    assert first.result.disposition.category == "clarification_required"
    corrected = await recovery.restart(context(), first.recovery_id)  # the full, new text
    assert corrected.result.disposition.category == "draft_ready_for_approval"
    assert corrected.result.identity.source_sha256 != first.result.identity.source_sha256


@A
async def test_replay_restart_budget_is_bounded(recovery):
    current = await recovery.start(context())
    for _ in range(rc.MAX_RESTARTS):
        current = await recovery.restart(context(), current.recovery_id)
        assert current.outcome == "completed"
    exhausted = await recovery.restart(context(), current.recovery_id)
    assert exhausted.code == "restart_limit"


# --- F. failure handling --------------------------------------------------------------------


@A
async def test_failure_tool_unavailable_is_a_failed_attempt_never_completed(recovery):
    result = await recovery.start(context(TWO, two_args(), Router(manufacturing=FailOne("BRK-201"))))  # fmt: skip
    assert (result.outcome, result.code) == ("failed", "unavailable")
    assert result.result.disposition.category == "technical_failure"


@A
async def test_failure_attempt_timeout(recovery, monkeypatch):
    monkeypatch.setattr(sv.a1, "run_intake", stall(5))
    result = await recovery.start(context(timeout_seconds=0.05))
    assert (result.outcome, result.code) == ("failed", "execution_limit")


@A
async def test_failure_no_budget_left_after_a_pause_expires_the_attempt(recovery):
    paused, router = await paused_at(recovery, "supply")
    e = entry(recovery, paused)
    e.record = e.record.model_copy(update={"budget_left_seconds": 0.0})
    refused = await recovery.resume(context(router=router), paused.recovery_id, 1)
    assert refused.code == "expired" and e.custody is None


@A
async def test_failure_cancel_a_paused_attempt(recovery):
    paused, router = await paused_at(recovery, "supply")
    custody = entry(recovery, paused).custody
    assert recovery.cancel(ALICE, paused.recovery_id).outcome == "cancelled"
    assert custody.get("manufacturing") is None
    assert (await recovery.resume(context(router=router), paused.recovery_id, 2)).code == "not_resumable"  # fmt: skip
    assert recovery.cancel(ALICE, paused.recovery_id).code == "not_resumable"


@A
async def test_failure_interrupted_running_record_is_never_resumed(recovery, clock):
    record = rc.RecoveryRecord.from_json(json.dumps(valid_record(lifecycle="running", next_stage=None, budget_left_seconds=None)))  # fmt: skip
    assert recovery.adopt(record.to_json()) is None
    adopted = recovery._entries[record.recovery_id].record
    assert (adopted.lifecycle, adopted.code) == ("failed", "interrupted")


@pytest.mark.parametrize("data", ["{", "x" * (rc.MAX_RECORD_BYTES + 1), json.dumps(valid_record(schema_version=9))])  # fmt: skip
def test_failure_malformed_records_are_not_adopted(recovery, data):
    assert recovery.adopt(data) == "invalid_record" and recovery._entries == {}


def test_failure_duplicate_adoption_is_refused(recovery):
    data = json.dumps(valid_record())
    assert recovery.adopt(data) is None and recovery.adopt(data) == "invalid_record"


@A
async def test_failure_store_capacity_is_bounded(clock):
    small = rc.SupervisorRecovery(clock=clock, max_entries=1)
    await small.start(context(), pause_before="supply")
    assert (await small.start(context())).code == "recovery_capacity"
    clock.now += rc.DEFAULT_TTL + 1  # lapsed entries are dropped and their custody cleared
    assert (await small.start(context())).outcome == "completed"


@A
async def test_failure_internal_error_is_a_fixed_code(recovery, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError(f"secret {TOKEN}")

    monkeypatch.setattr(sv, "_execute", broken)
    result = await recovery.start(context())
    assert (result.outcome, result.code, result.result) == ("failed", "internal_error", None)
    assert TOKEN not in result.model_dump_json()


@A
async def test_failure_unauthorized_start_is_refused(recovery):
    assert (await recovery.start(context(execute=None))).code == "unauthenticated"
    assert (await recovery.start(context(principal=None))).code == "recovery_unavailable"
    assert recovery._entries == {}


# --- G. customer and security boundaries ----------------------------------------------------


@A
async def test_security_no_secrets_in_any_exported_record(recovery):
    for stage in (*STAGES, None):
        result = await (recovery.start(context(), pause_before=stage) if stage else recovery.start(context()))  # fmt: skip
        assert_metadata_only(recovery.export(ALICE, result.recovery_id))


def test_security_recovery_results_never_approve_or_release():
    for claim in ({"approved": True}, {"released": True}, {"human_approval_required": False}):
        with pytest.raises(ValueError):
            rc.RecoveryResult(operation="start", outcome="cancelled", **claim)
    for fields in (
        {"operation": "resume", "outcome": "completed"},  # a completion without a result
        {"operation": "start", "outcome": "paused"},  # a pause without its boundary
        {"operation": "resume", "outcome": "recovery_refused"},  # a refusal without a code
        {"operation": "start", "outcome": "cancelled", "next_stage": "supply"},
    ):
        with pytest.raises(ValueError):
            rc.RecoveryResult(**fields)


@A
async def test_security_completed_result_keeps_day48_semantics(recovery):
    done = await recovery.start(context())
    d = done.result.disposition
    assert (d.category, d.approved, d.released, d.customer_status) == ("draft_ready_for_approval", False, False, "not_verified")  # fmt: skip
    assert done.customer_outcome() == done.result.customer_outcome()


# --- H. compatibility -----------------------------------------------------------------------


@A
@pytest.mark.parametrize(
    ("name", "outcome"),
    [
        ("baseline", "completed"),
        ("missing_thickness", "completed"),
        ("unsupported_thickness", "completed"),
        ("insufficient_stock", "completed"),
        ("pricing_rule_drift", "completed"),
        ("manufacturing_error", "failed"),
    ],
)
async def test_compat_uninterrupted_recovery_runs_equal_the_plain_runner(recovery, monkeypatch, name, outcome):  # fmt: skip
    via_recovery = await recovery.start(await prepared(name, monkeypatch))
    with monkeypatch.context() as patch:
        plain = await sv.run_supervisor_workflow(await prepared(name, patch))
    assert via_recovery.outcome == outcome
    assert business(via_recovery.result) == business(plain)


def test_compat_supervisor_graphs_still_refuse_checkpointers():
    assert sv.build_execution_graph().checkpointer is None
    assert sv.build_routing_graph().checkpointer is None


# --- D49-01. cancellation lifecycle ---------------------------------------------------------


def swallowing(monkeypatch) -> asyncio.Event:
    """Nested code that suppresses the cancellation, then returns an otherwise valid (late)
    Supervisor result: the real run, started only after the cancel was swallowed."""
    real, started = sv._execute, asyncio.Event()

    async def execute(graph, context, custody):
        started.set()
        try:
            await asyncio.Event().wait()  # blocked until cancelled
        except asyncio.CancelledError:
            pass  # swallowed: a CancelledError never reaches the recovery layer
        return await real(graph, context, custody)

    monkeypatch.setattr(sv, "_execute", execute)
    return started


def assert_cancelled(recovery, rid):
    e = recovery._entries[rid]
    assert (e.record.lifecycle, e.record.code) == ("cancelled", "cancelled")
    assert e.custody is None and e.result is None and e.task is None  # nothing retained
    assert recovery.retrieve(ALICE, rid).code == "not_resumable"


@A
async def test_cancel_cooperative_running_task_acknowledges_then_settles(recovery):
    gate = Gate()
    task = asyncio.create_task(recovery.start(context(TWO, two_args(), Router(manufacturing=gate))))
    await until(lambda: gate.active == 2)
    (rid,) = recovery._entries
    ack = recovery.cancel(ALICE, rid)
    assert (ack.outcome, ack.code) == ("cancelled", "cancel_requested")  # a request, not a stop
    assert recovery._entries[rid].custody is not None  # still in use: not cleared yet
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_cancelled(recovery, rid)
    assert gate.active == 0


@A
@pytest.mark.parametrize("pause_before", [None, "supply"], ids=["late_result", "late_pause"])
async def test_cancel_a_swallowed_cancellation_never_publishes_the_late_outcome(recovery, monkeypatch, pause_before):  # fmt: skip
    started = swallowing(monkeypatch)
    spy = Spy(monkeypatch)
    task = asyncio.create_task(recovery.start(context(), pause_before=pause_before))
    await started.wait()
    (rid,) = recovery._entries
    assert recovery.cancel(ALICE, rid).code == "cancel_requested"
    result = await task  # the task returned normally: the cancellation was swallowed
    assert spy.calls  # the late run really happened (and produced a valid result or pause)
    assert (result.outcome, result.code, result.result, result.next_stage) == ("cancelled", "cancelled", None, None)  # fmt: skip
    assert_cancelled(recovery, rid)


@A
async def test_cancel_racing_with_completion_lands_before_publication(recovery, monkeypatch):
    real = sv._execute

    async def execute(graph, context, custody):
        result = await real(graph, context, custody)
        (rid,) = recovery._entries
        assert recovery.cancel(ALICE, rid).code == "cancel_requested"  # between result and publish
        return result

    monkeypatch.setattr(sv, "_execute", execute)
    result = await asyncio.create_task(recovery.start(context()))  # its own task is cancelled
    assert (result.outcome, result.result) == ("cancelled", None)
    assert_cancelled(recovery, result.recovery_id)


@A
async def test_cancel_after_completion_is_refused(recovery):
    done = await recovery.start(context())
    assert recovery.cancel(ALICE, done.recovery_id).code == "not_resumable"
    assert recovery.retrieve(ALICE, done.recovery_id).outcome == "completed"


@A
async def test_cancel_duplicate_requests_are_acknowledged_once(recovery, monkeypatch):
    started = swallowing(monkeypatch)  # a second delivered cancel would interrupt the late run
    task = asyncio.create_task(recovery.start(context()))
    await started.wait()
    (rid,) = recovery._entries
    acks = [recovery.cancel(ALICE, rid) for _ in range(3)]
    assert {(a.outcome, a.code) for a in acks} == {("cancelled", "cancel_requested")}
    result = await task  # returned normally: exactly one cancellation was delivered
    assert result.outcome == "cancelled"
    assert recovery.cancel(ALICE, rid).code == "not_resumable"  # settled: refused from now on


@A
async def test_cancel_requested_attempt_is_busy_then_never_revived(recovery, monkeypatch):
    started = swallowing(monkeypatch)
    task = asyncio.create_task(recovery.start(context(), pause_before="supply"))
    await started.wait()
    (rid,) = recovery._entries
    recovery.cancel(ALICE, rid)
    assert (await recovery.resume(context(), rid, 0)).code == "attempt_busy"
    assert (await recovery.restart(context(), rid)).code == "attempt_busy"
    await task
    generation = recovery._entries[rid].record.generation
    assert (await recovery.resume(context(), rid, generation)).code == "not_resumable"
    assert recovery.cancel(ALICE, rid).code == "not_resumable"


@A
async def test_cancel_restart_after_cancellation_is_a_new_attempt(recovery):
    paused, _ = await paused_at(recovery, "supply")
    recovery.cancel(ALICE, paused.recovery_id)
    restarted = await recovery.restart(context(), paused.recovery_id)
    assert restarted.outcome == "completed" and restarted.recovery_id != paused.recovery_id
    assert recovery._entries[paused.recovery_id].record.lifecycle == "superseded"
    assert (await recovery.resume(context(), paused.recovery_id, 3)).code == "not_resumable"


@A
async def test_cancel_requested_before_the_task_began_runs_nothing(recovery, monkeypatch):
    insert = recovery._insert

    def insert_then_cancel(entry):  # another thread's request lands before _run starts
        ok = insert(entry)
        entry.cancel_requested = True
        return ok

    monkeypatch.setattr(recovery, "_insert", insert_then_cancel)
    spy = Spy(monkeypatch)
    result = await recovery.start(context())
    assert (result.outcome, result.code) == ("cancelled", "cancelled") and spy.calls == []
    assert_cancelled(recovery, result.recovery_id)


@A
async def test_cancel_from_another_thread_runs_on_the_tasks_loop(recovery):
    gate = Gate()
    task = asyncio.create_task(recovery.start(context(TWO, two_args(), Router(manufacturing=gate))))
    await until(lambda: gate.active == 2)
    (rid,) = recovery._entries
    ack = await asyncio.to_thread(recovery.cancel, ALICE, rid)  # a worker thread's request
    assert ack.code == "cancel_requested"
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_cancelled(recovery, rid)


# --- D49-02. retention ----------------------------------------------------------------------


@A
async def test_retention_prune_drops_expired_entries_and_clears_their_custody(recovery, clock):
    paused, _ = await paused_at(recovery, "supply")
    done = await recovery.start(context())
    custody = entry(recovery, paused).custody
    assert recovery.prune() == 0  # nothing has lapsed yet
    clock.now += rc.DEFAULT_TTL + 1
    assert recovery.prune() == 2 and recovery._entries == {}
    assert custody.get("manufacturing") is None and custody.carried == {}  # memory released
    assert recovery.retrieve(ALICE, done.recovery_id).code == "recovery_unavailable"


@A
async def test_retention_prune_and_close_never_clear_a_running_attempt(recovery, clock):
    gate = Gate()
    task = asyncio.create_task(recovery.start(context(TWO, two_args(), Router(manufacturing=gate))))
    await until(lambda: gate.active == 2)
    (rid,) = recovery._entries
    clock.now += rc.DEFAULT_TTL + 1  # lapsed by time, but still running
    assert recovery.prune() == 0 and recovery._entries[rid].custody is not None
    gate.release()
    assert (await task).outcome == "completed"
    assert recovery.prune() == 0  # the finished result now has its own retention window


@A
async def test_retention_close_releases_everything_and_cancels_running_work(recovery):
    paused, _ = await paused_at(recovery, "supply")
    custody = entry(recovery, paused).custody
    gate = Gate()
    task = asyncio.create_task(recovery.start(context(TWO, two_args(), Router(manufacturing=gate))))
    await until(lambda: gate.active == 2)
    running = next(k for k, e in recovery._entries.items() if e.record.lifecycle == "running")
    assert recovery.close() == 1  # the paused entry; the running one is only asked to stop
    assert custody.get("manufacturing") is None
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_cancelled(recovery, running)


# --- D49-03. shared ownership, no graph dependency ------------------------------------------


def test_ownership_owner_key_is_the_phase5_derivation_unchanged():
    expected = hashlib.sha256(b"CUST-008\x00U-1").hexdigest()
    assert ownership.principal_owner(ALICE) == expected
    assert ownership.principal_owner(CAROL) != expected  # same customer, another user
    assert ownership.principal_owner(BOB) != expected


def test_ownership_conversations_and_recovery_share_one_implementation():
    from rfq_copilot import graph as g  # the test may import the graph; application code may not

    assert g._owner is ownership.principal_owner is rc._owner
    assert g.ThreadAccessError is ownership.ThreadAccessError is rc.ThreadAccessError
    root = Path(rc.__file__).parent
    implementations = [p.name for p in root.rglob("*.py") if "principal.customer_id}\\x00" in p.read_text()]  # fmt: skip
    assert implementations == ["ownership.py"]


# Factories, called in the test body: nothing is constructed at collection time.
# fmt: off
UNOWNED = [
    pytest.param(lambda: None, id="no_principal"),
    pytest.param(lambda: "CUST-008", id="a_string"),
    pytest.param(lambda: Principal("CUST-008", frozenset(), user_id=None, subject="s"), id="no_user"),
    pytest.param(lambda: Principal("CUST-008", frozenset(), user_id="", subject="s"), id="empty_user"),
    pytest.param(lambda: SimpleNamespace(customer_id="", user_id="U-1"), id="look_alike_without_customer"),
    pytest.param(lambda: SimpleNamespace(customer_id="CUST-008", user_id="U-1"), id="look_alike_with_valid_fields"),
]
# fmt: on


@pytest.mark.parametrize("make", UNOWNED)
def test_ownership_unverified_principals_are_refused(make):
    with pytest.raises(ownership.ThreadAccessError) as error:
        ownership.principal_owner(make())
    assert str(error.value) == "thread_unavailable"  # the same opaque answer as Phase 5


# fmt: off
UNCONSTRUCTIBLE = [
    pytest.param(lambda: Principal("", frozenset(), user_id="U-1"), id="empty_customer"),
    pytest.param(lambda: Principal("   ", frozenset(), user_id="U-1"), id="blank_customer"),
    pytest.param(lambda: Principal("CUST\x00008", frozenset(), user_id="U-1"), id="nul_in_customer"),
    pytest.param(lambda: Principal("C" * 65, frozenset(), user_id="U-1"), id="customer_too_long"),
    pytest.param(lambda: Principal(None, frozenset(), user_id="U-1"), id="no_customer"),
    pytest.param(lambda: Principal("CUST-008", {"view_quote"}, user_id="U-1"), id="permissions_not_frozen"),
]
# fmt: on


@pytest.mark.parametrize("make", UNCONSTRUCTIBLE)
def test_ownership_invalid_principals_cannot_be_constructed(make):
    """Principal's own validation: such an identity never reaches principal_owner at all."""
    with pytest.raises(ValueError):
        make()


@A
async def test_ownership_recovery_records_bind_the_shared_owner_key(recovery):
    paused, router = await paused_at(recovery, "supply")
    assert entry(recovery, paused).record.owner == ownership.principal_owner(ALICE)
    for other in (BOB, CAROL):
        refused = await recovery.resume(context(router=router, principal=other), paused.recovery_id, 1)  # fmt: skip
        assert refused.code == "recovery_unavailable"
    assert (await recovery.resume(context(router=router), paused.recovery_id, 1)).outcome == "completed"  # fmt: skip


def test_ownership_recovery_never_imports_the_graph():
    for module in (rc, ownership):
        text = Path(module.__file__).read_text()
        assert "rfq_copilot.graph" not in text and "import graph" not in text


def test_ownership_modules_import_cleanly_in_a_fresh_interpreter():
    """No circular import, in either order (a fresh process: nothing cached)."""
    for order in ("ownership, recovery, graph", "graph, ownership, recovery", "recovery, graph"):
        code = "import " + ", ".join(f"rfq_copilot.{name}" for name in order.split(", "))
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=120)
        assert done.returncode == 0, (order, done.stderr.decode()[-500:])
