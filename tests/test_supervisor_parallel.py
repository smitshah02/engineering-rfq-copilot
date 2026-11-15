"""Day 47 safe parallel execution (L01): A2's per-part feasibility checks run as bounded,
independent work items inside one RFQ; the six A1 -> A5 stages stay a strict chain.

Overlap is proven with asyncio.Event barriers, never timing. Offline: the Phase 6 harness
(fake intake model, fake MCP executor running the real pure services), no network, no DB.

Sections (-k): test_dependency_, test_concurrency_, test_fanin_, test_parity_, test_security_,
test_safety_, test_compat_."""

import asyncio
import inspect
import json
from decimal import Decimal

import pytest
from test_intake import ev
from test_intake_validation import RULES
from test_manufacturing_agent import Tool, ready, service_result, spec
from test_specialized_agent_integration import SHORT, TWO, Router, drifted, run, two_args
from test_supervisor import TOKEN
from test_supervisor_delegation import ALICE, BOB, STAGES, Spy, context, track_custody
from test_supervisor_routing import EXPECTED, LATE, pricing_drift, results_of

from rfq_copilot import manufacturing_agent as a2
from rfq_copilot import supervisor as sv
from rfq_copilot import supply_agent as a3
from rfq_copilot.mcp.client import McpCallError, McpToolClient

A = pytest.mark.asyncio
DOWNSTREAM = {*a3.TOOLS, "estimate_costs", "calculate_price", "evaluate_quotation"}


@pytest.fixture(autouse=True)
def offline_harness(monkeypatch):
    """As Day 46: no direct MCP; only the RULES fixture is admitted as the manufacturing rule."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("the Supervisor must use only the injected executor")

    monkeypatch.setattr(McpToolClient, "call", forbidden)
    original = sv._manufacturing_trusted
    monkeypatch.setattr(sv, "_manufacturing_trusted", lambda r: r is RULES or original(r))


class Gate:
    """Executor wrapper: each manufacturing call blocks on its part's Event until released and
    is counted, so overlap and the concurrency bound are observed, not inferred from timing."""

    def __init__(self, inner=None, released=False):
        self.inner = inner or Tool()  # the real services, as the MCP server maps them
        self.open = released
        self.events: dict[str, asyncio.Event] = {}
        self.active = self.peak = 0
        self.started, self.finished = [], []

    def _event(self, part_id) -> asyncio.Event:
        event = self.events.setdefault(part_id, asyncio.Event())
        if self.open:
            event.set()
        return event

    def release(self, part_id=None):
        if part_id is not None:
            return self._event(part_id).set()
        self.open = True
        for event in self.events.values():
            event.set()

    async def __call__(self, tool, request):
        if tool != a2.TOOL:
            return await self.inner(tool, request)
        part_id = request.part.part_id
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.append(part_id)
        try:
            await self._event(part_id).wait()
            return await self.inner(tool, request)
        finally:
            self.active -= 1
            self.finished.append(part_id)


async def until(condition, steps=2000):
    """Yield to the loop until ``condition()`` holds; a bounded wait, not a sleep-based timer."""
    for _ in range(steps):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


async def settle(steps=50):
    for _ in range(steps):
        await asyncio.sleep(0)


def new_tasks(before):
    return {t for t in asyncio.all_tasks() - before if not t.done()}


def parts(*ids):
    return [spec(part_id=i) for i in ids]


async def evaluate(specs, execute, limit, **over):
    return await a2.evaluate_manufacturing_feasibility(ready(*specs), execute, max_concurrency=limit, **over)  # fmt: skip


def two_part(text=TWO, args=None, router=None, **over):
    return context(text, args or two_args(), router or Router(), **over)


def business(result: sv.SupervisorExecutionResult) -> dict:
    """The execution result without the per-run attempt id (top level and per outcome)."""
    return result.model_dump(exclude={"attempt_id": True, "outcomes": {"__all__": {"attempt_id"}}})  # fmt: skip


# --- A. dependency eligibility --------------------------------------------------------------


@A
async def test_dependency_parallel_parts_start_only_after_validated_intake(monkeypatch):
    spy = Spy(monkeypatch)
    gate = Gate()
    seen = []

    async def watch(tool, request):
        if not seen:  # the first A2 worker to start sees what has already run
            validation = spy.one("intake_validation")[3]
            seen.append((spy.stages(), validation.readiness if validation else None))
        return await gate(tool, request)

    router = Router(manufacturing=watch)
    task = asyncio.create_task(sv.run_supervisor_workflow(two_part(router=router)))
    await until(lambda: len(gate.started) == 2)
    assert seen == [(["intake_extraction", "intake_validation", "manufacturing"], a2.READY)]
    assert not DOWNSTREAM & set(router.calls)  # A3 has not started while A2 parts are open
    gate.release()
    result = await task
    assert result.status == "executed" and spy.stages() == STAGES


@A
async def test_dependency_downstream_stages_wait_for_the_joined_a2_result(monkeypatch):
    spy = Spy(monkeypatch)
    gate = Gate()
    router = Router(manufacturing=gate)
    task = asyncio.create_task(sv.run_supervisor_workflow(two_part(router=router)))
    await until(lambda: len(gate.started) == 2)
    gate.release("BRK-201")  # the second part finishes first
    await until(lambda: gate.finished == ["BRK-201"])
    await settle()
    assert spy.stages()[-1] == "manufacturing" and not DOWNSTREAM & set(router.calls)
    gate.release("BRK-200")
    await task
    supply_input = spy.one("supply")[1][0]
    assert supply_input is spy.one("manufacturing")[3]  # A3 gets the one joined assessment
    assert [p.part_id for p in supply_input.parts] == ["BRK-200", "BRK-201"]
    later = router.calls[router.calls.index("check_inventory") :]
    assert a2.TOOL not in later and sorted(later) == sorted(DOWNSTREAM)  # each once, after A2


@A
@pytest.mark.parametrize("readiness", ["needs_clarification", "requires_review", "not_evaluated"])
async def test_dependency_not_ready_intake_starts_no_worker_at_any_limit(readiness):
    tool = Tool()
    intake = ready(*parts("P-1", "P-2"), readiness=readiness)
    a = await a2.evaluate_manufacturing_feasibility(intake, tool, max_concurrency=4)
    assert (a.status, a.parts, tool.calls) == ("not_evaluated", (), [])


def test_dependency_only_a2_receives_a_concurrency_setting():
    """Supply, pricing and quotation make RFQ-level calls over shared stock, capacity and quote
    totals (the audit): they are never handed a concurrency setting."""
    held = {s: f"<{s}>" for s in STAGES}
    ctx = context(part_concurrency=3)
    for stage in STAGES:
        _, kwargs = sv._arguments(stage, ctx, held)
        assert ("max_concurrency" in kwargs) == (stage == "manufacturing"), stage
    assert sv._arguments("manufacturing", ctx, held)[1] == {"max_concurrency": 3}


# --- B. real concurrency --------------------------------------------------------------------


@A
async def test_concurrency_two_parts_of_one_rfq_overlap_in_the_supervisor_workflow():
    gate = Gate()
    router = Router(manufacturing=gate)
    task = asyncio.create_task(sv.run_supervisor_workflow(two_part(router=router)))
    await until(lambda: len(gate.started) == 2)
    # Both parts of this one RFQ are inside the tool at once; neither released nor finished.
    assert (sorted(gate.started), gate.finished, gate.active) == (["BRK-200", "BRK-201"], [], 2)
    gate.release()
    result = await task
    assert result.status == "executed" and gate.peak == 2
    assert [o.stage for o in result.outcomes] == STAGES  # still one recorded outcome per stage


@A
@pytest.mark.parametrize(("limit", "count"), [(2, 4), (3, 4), (4, 5)])
async def test_concurrency_never_exceeds_the_configured_limit(limit, count):
    gate = Gate()
    ids = [f"P-{i}" for i in range(1, count + 1)]
    task = asyncio.create_task(evaluate(parts(*ids), gate, limit))
    await until(lambda: len(gate.started) == limit)
    await settle()
    assert len(gate.started) == limit == gate.active  # the rest wait on the semaphore
    assert gate.started == ids[:limit]  # admitted in Intake order
    gate.release()
    a = await task
    assert gate.peak == limit and [p.part_id for p in a.parts] == ids
    assert a.status == "feasible"


@A
async def test_concurrency_reverse_completion_keeps_intake_order():
    gate = Gate()
    ids = ["P-1", "P-2", "P-3"]
    task = asyncio.create_task(evaluate(parts(*ids), gate, 3))
    await until(lambda: len(gate.started) == 3)
    for part_id in reversed(ids):
        gate.release(part_id)
        await until(lambda p=part_id: p in gate.finished)
    a = await task
    assert gate.finished == list(reversed(ids))
    assert [(p.index, p.part_id) for p in a.parts] == [(1, "P-1"), (2, "P-2"), (3, "P-3")]
    assert a == await evaluate(parts(*ids), Tool(), 1)  # identical to the sequential result


@A
@pytest.mark.parametrize(("limit", "count"), [(1, 3), (4, 1)])
async def test_concurrency_sequential_path_for_limit_one_or_a_single_part(monkeypatch, limit, count):  # fmt: skip
    async def never(*args, **kwargs):
        raise AssertionError("no fan-out for one part or limit 1")

    monkeypatch.setattr(a2, "_parallel", never)
    gate = Gate(released=True)
    a = await evaluate(parts(*[f"P-{i}" for i in range(1, count + 1)]), gate, limit)
    assert gate.peak == 1 and len(a.parts) == count and a.status == "feasible"


@A
async def test_concurrency_supervisor_sequential_mode_never_overlaps():
    gate = Gate(released=True)
    ctx = two_part(router=Router(manufacturing=gate), part_concurrency=1)
    result = await sv.run_supervisor_workflow(ctx)
    assert result.status == "executed" and gate.peak == 1
    assert gate.started == ["BRK-200", "BRK-201"]


@A
@pytest.mark.parametrize("limit", [0, 5, -1, True, 2.0, "2", None])
async def test_concurrency_limit_outside_the_trusted_range_is_refused(limit):
    tool = Tool()
    with pytest.raises(ValueError):
        await evaluate(parts("P-1", "P-2"), tool, limit)
    assert tool.calls == []


# --- C. fan-in ------------------------------------------------------------------------------


def expected(*ids):
    return ready(*parts(*ids))


def results(*ids):
    return [a2._failed(p.index, p.specification, "timeout") for p in expected(*ids).parts]


def altered(r, **update):
    """The second result changed without validation (model_copy), as a faulty worker might."""
    return [(0, r[0]), (1, r[1].model_copy(update=update))]


def test_fanin_rebuilds_intake_order_from_any_completion_order():
    one, two, three = results("P-1", "P-2", "P-3")
    joined = a2._join(expected("P-1", "P-2", "P-3"), [(2, three), (0, one), (1, two)])
    assert joined == (one, two, three)


@pytest.mark.parametrize(
    "collected",
    [
        pytest.param(lambda r: [(0, r[0])], id="missing"),
        pytest.param(lambda r: [], id="empty"),
        pytest.param(lambda r: [(0, r[0]), (0, r[0]), (1, r[1])], id="duplicate"),
        pytest.param(lambda r: [(0, r[0]), (1, r[1]), (2, r[1])], id="unknown_item"),
        pytest.param(lambda r: [(0, r[1]), (1, r[0])], id="another_parts_result"),
        pytest.param(lambda r: [(0, r[0]), (1, r[1].model_dump())], id="wrong_type"),
        pytest.param(lambda r: [(0, r[0]), (1, "feasible")], id="not_a_result"),
        pytest.param(lambda r: altered(r, status="not_evaluated"), id="invalid_status"),
        pytest.param(lambda r: altered(r, error=None), id="error_without_code"),
        pytest.param(lambda r: altered(r, status="feasible"), id="success_with_error"),
    ],
)
def test_fanin_refuses_incomplete_or_mismatched_batches(collected):
    with pytest.raises(a2._JoinError):
        a2._join(expected("P-1", "P-2"), collected(results("P-1", "P-2")))


def test_fanin_refuses_a_result_from_another_evaluation_of_the_same_part():
    """Same index and part id, different specification: another attempt's or RFQ's result."""
    own = results("P-1", "P-2")
    other = ready(spec(part_id="P-1"), spec(part_id="P-2", quantity=99)).parts[1]
    foreign = a2._failed(other.index, other.specification, "timeout")
    with pytest.raises(a2._JoinError):
        a2._join(expected("P-1", "P-2"), [(0, own[0]), (1, foreign)])


async def evaluated(specs):
    """The intake and its real sequential part results (with evidence)."""
    intake = ready(*specs)
    return intake, (await a2.evaluate_manufacturing_feasibility(intake, Tool())).parts


def foreign_digest(part):
    return {"evidence": part.evidence.model_copy(update={"arguments_sha256": "0" * 64})}


@A
async def test_fanin_refuses_a_conclusion_without_evidence():
    intake, real = await evaluated(parts("P-1", "P-2"))
    assert real[1].status == "feasible"
    with pytest.raises(a2._JoinError):
        a2._join(intake, altered(real, evidence=None))


@A
@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda real: {"evidence": real[0].evidence}, id="another_parts_evidence"),
        pytest.param(lambda real: {"status": "feasible"}, id="infeasible_relabelled_feasible"),
        pytest.param(lambda real: {"status": "requires_review"}, id="unsupported_review_claim"),
        pytest.param(lambda real: {"explanation": "Part 2: manufacturable."}, id="explanation"),
        pytest.param(lambda real: foreign_digest(real[1]), id="digest_not_this_part"),
    ],
)
async def test_fanin_refuses_evidence_that_does_not_support_the_result(tamper):
    intake, real = await evaluated([spec(part_id="P-1"), spec(part_id="P-2", thickness_mm=Decimal("5"))])  # fmt: skip
    assert [p.status for p in real] == ["feasible", "infeasible"]
    with pytest.raises(a2._JoinError):
        a2._join(intake, altered(real, **tamper(real)))


@A
async def test_fanin_refuses_an_error_carrying_successful_evidence():
    intake, real = await evaluated(parts("P-1", "P-2"))
    failed = a2._failed(intake.parts[1].index, intake.parts[1].specification, "timeout")
    with pytest.raises(a2._JoinError):
        a2._join(intake, [(0, real[0]), (1, failed.model_copy(update={"evidence": real[1].evidence}))])  # fmt: skip


@A
@pytest.mark.parametrize("rewrite", [None, drifted], ids=["pinned", "rule_drift"])
async def test_fanin_accepts_every_genuine_outcome_unchanged(rewrite):
    """Feasible, infeasible, needs_clarification, requires_review and error parts pass the
    evidence check, and parallel equals sequential."""
    intake = ready(*MIXED, spec(part_id="P-5"))
    failing = FailOne("P-5")
    failing.inner = Tool(rewrite=rewrite)
    serial = await a2.evaluate_manufacturing_feasibility(intake, failing)
    statuses = {p.status for p in serial.parts}
    assert "error" in statuses and statuses - {"error"}  # both kinds present
    collected = list(enumerate(serial.parts))[::-1]
    assert a2._join(intake, collected) == serial.parts
    parallel = FailOne("P-5")
    parallel.inner = Tool(rewrite=rewrite)
    assert await a2.evaluate_manufacturing_feasibility(intake, parallel, max_concurrency=3) == serial  # fmt: skip


@A
async def test_fanin_refusal_fails_the_whole_stage_closed_with_no_partial_parts(monkeypatch):
    original = a2._check

    async def misbound(i, specification, execute, intake):  # returns the other part's result
        other = intake.parts[0] if i == intake.parts[1].index else intake.parts[1]
        return await original(other.index, other.specification, execute, intake)

    monkeypatch.setattr(a2, "_check", misbound)
    a = await evaluate(parts("P-1", "P-2"), Tool(), 2)
    assert (a.status, a.error, a.parts) == ("error", "invalid_evidence", ())


@A
async def test_fanin_rule_drift_is_kept_per_part_as_review_in_both_modes():
    serial = await evaluate(parts("P-1", "P-2"), Tool(rewrite=drifted), 1)
    parallel = await evaluate(parts("P-1", "P-2"), Tool(rewrite=drifted), 2)
    assert parallel == serial and parallel.status == "requires_review"
    assert {p.status for p in parallel.parts} == {"requires_review"}


# --- D. business parity ---------------------------------------------------------------------


class FailOne:
    """The real fake, except one part's tool call fails; records every call (no retries)."""

    def __init__(self, part_id, code="unavailable"):
        self.inner, self.part_id, self.code, self.calls = Tool(), part_id, code, []

    async def __call__(self, tool, request):
        self.calls.append(request.part.part_id)
        if request.part.part_id == self.part_id:
            raise McpCallError(self.code)
        return await self.inner(tool, request)


MIXED = [
    spec(part_id="P-1"),
    spec(part_id="P-2", thickness_mm=Decimal("5")),  # the Day 45/46 unsupported thickness
    spec(part_id="P-3", part_type="l_bracket"),  # mfg-v1.0 needs height_mm (Day 38 test)
    spec(part_id="P-4", material_id="MAT-999"),  # not in the catalog (Day 38 test)
]


@A
@pytest.mark.parametrize("limit", [2, 3, 4])
async def test_parity_mixed_part_outcomes_match_the_sequential_reference(limit):
    serial, parallel = Tool(), Tool()
    a = await evaluate(MIXED, serial, 1)
    b = await evaluate(MIXED, parallel, limit)
    assert b == a  # the full typed assessment: statuses, findings, evidence, explanations
    assert sorted(r.part.part_id for _, r in parallel.calls) == ["P-1", "P-2", "P-3", "P-4"]


@A
async def test_parity_one_failing_part_is_that_parts_error_exactly_as_sequentially():
    serial, parallel = FailOne("P-2"), FailOne("P-2")
    a = await evaluate(parts("P-1", "P-2", "P-3"), serial, 1)
    b = await evaluate(parts("P-1", "P-2", "P-3"), parallel, 3)
    assert b == a and (b.status, b.error) == ("error", "unavailable")
    assert [p.status for p in b.parts] == ["feasible", "error", "feasible"]
    assert sorted(parallel.calls) == ["P-1", "P-2", "P-3"]  # each once: no retry


def with_part(args, i, **fields):
    args["parts"][i] |= fields
    return args


# name -> (text, intake args, router factory): two-part variants of the Day 45 scenarios
# fmt: off
MULTI = {
    "two_part_baseline": (TWO, two_args(), Router),
    "two_part_missing_thickness": (TWO.replace("3 mm thick, ", ""), with_part(two_args(), 1, thickness=None), Router),
    "two_part_unsupported_tolerance": (TWO.replace("±0.2 mm, brushed", "±0.1 mm, brushed"), with_part(two_args(), 1, tolerance=ev("±0.1 mm", "tolerance ±0.1 mm")), Router),
    "two_part_manufacturing_rule_drift": (TWO, two_args(), lambda: Router(manufacturing=Tool(rewrite=drifted))),
    "two_part_insufficient_stock": (TWO, two_args(), lambda: Router(available=SHORT)),
    "two_part_unachievable_delivery": (TWO + LATE, two_args() | {"requested_delivery_date": ev("2026-11-12", LATE.strip(" ."))}, Router),
    "two_part_pricing_rule_drift": (TWO, two_args(), pricing_drift),
    "two_part_tool_failure": (TWO, two_args(), lambda: Router(manufacturing=FailOne("BRK-201"))),
}
# fmt: on


@A
@pytest.mark.parametrize("name", sorted(MULTI))
async def test_parity_parallel_supervisor_matches_sequential_supervisor_and_phase6(monkeypatch, name):  # fmt: skip
    text, args, router = MULTI[name]
    a, baseline_router = await run(text, args, router())  # Phase 6: A2 sequential
    made = track_custody(monkeypatch)
    runs = []
    for limit in (1, 2):
        r = router()
        runs.append((await sv.run_supervisor_workflow(context(text, args, r, part_concurrency=limit)), r))  # fmt: skip
    (serial, serial_router), (parallel, parallel_router) = runs
    for result in (serial, parallel):
        assert result.status == "executed", result.summary()
        assert [(o.stage, o.status) for o in result.outcomes] == [(s.stage, s.status) for s in a.stages]  # fmt: skip
        assert (result.route.clarifications, result.route.review_reasons) == (a.clarifications, a.review_reasons)  # fmt: skip
        assert result.customer_view() == a.customer_view()
    assert business(parallel) == business(serial)
    # Full typed stage results (amounts, evidence, explanations) equal Phase 6's, stage by stage.
    reference = results_of(a)
    assert len(made) == 2
    for custody in made:
        assert custody.kept == {s: reference[s] for s in custody.kept}, name
    # The same tools, each once; only the order of concurrent A2 calls may differ.
    assert sorted(parallel_router.calls) == sorted(serial_router.calls) == sorted(baseline_router.calls)  # fmt: skip


@A
async def test_parity_documented_two_part_outcomes_in_parallel_mode():
    """Outcomes the fixtures document: BRK-201 at ±0.1 mm is TOLERANCE_UNSUPPORTED (D43-01);
    constrained supply is priced and goes to review at A5 (Day 45 EXPECTED)."""
    text, args, router = MULTI["two_part_unsupported_tolerance"]
    a, _ = await run(text, args, router())
    assert [p.status for p in a.manufacturing.parts] == ["feasible", "infeasible"]
    result = await sv.run_supervisor_workflow(context(text, args, router()))
    assert (result.route.action, result.route.stopped_at) == ("blocked", "manufacturing")
    result = await sv.run_supervisor_workflow(two_part(router=Router(available=SHORT)))
    assert (result.route.action, result.route.stopped_at) == EXPECTED["insufficient_stock"]
    assert not (result.approved or result.released)


# --- E. L03 custody and security ------------------------------------------------------------


@A
@pytest.mark.parametrize("second", [ALICE, BOB], ids=["same_customer", "other_customer"])
async def test_security_concurrent_identical_two_part_rfqs_stay_isolated(monkeypatch, second):
    made = track_custody(monkeypatch)
    spy = Spy(monkeypatch)
    gates = Gate(), Gate()
    routers = Router(manufacturing=gates[0]), Router(manufacturing=gates[1])
    contexts = two_part(router=routers[0]), two_part(router=routers[1], principal=second)
    tasks = [asyncio.create_task(sv.run_supervisor_workflow(c)) for c in contexts]
    await until(lambda: all(len(g.started) == 2 for g in gates))  # four parts open at once
    for g in gates:
        g.release()
    first, other = await asyncio.gather(*tasks)
    assert first.attempt_id != other.attempt_id
    assert [(o.stage, o.status) for o in first.outcomes] == [(o.stage, o.status) for o in other.outcomes]  # fmt: skip
    for custody, ctx, router in zip(made, contexts, routers, strict=True):
        extraction = custody.kept["intake_extraction"]
        validation = custody.kept["intake_validation"]
        (val,) = [c for c in spy.calls if c[0] == "intake_validation" and c[1][0] is extraction]
        assert val[3] is validation  # this attempt's validation got this attempt's extraction
        (mfg,) = [c for c in spy.calls if c[0] == "manufacturing" and c[1][0] is validation]
        assert mfg[1][1] is ctx.execute  # A2's workers used this run's principal-bound executor
        (sup,) = [c for c in spy.calls if c[0] == "supply" and c[1][0] is custody.kept["manufacturing"]]  # fmt: skip
        assert sup[2]["extraction"] is extraction  # and A3 got the same extraction (L03)
        assert router.calls.count(a2.TOOL) == 2  # two parts, once each, on this run's executor
    assert made[0].kept["intake_extraction"] is not made[1].kept["intake_extraction"]
    assert made[0].kept["manufacturing"] == made[1].kept["manufacturing"]  # equal, not shared
    assert all(c.get(s) is None for c in made for s in STAGES)  # cleared after both runs


@A
@pytest.mark.parametrize("limit", [0, 5, -1, True, 2.0, "2", None])
async def test_security_untrusted_concurrency_setting_fails_before_any_agent(monkeypatch, limit):
    spy = Spy(monkeypatch)
    result = await sv.run_supervisor_workflow(two_part(part_concurrency=limit))
    assert (result.status, result.error) == ("error", "invalid_budget") and spy.calls == []


@A
async def test_security_one_outcome_per_stage_and_no_financial_data_in_the_summary(monkeypatch):
    recorded = []
    original = sv.record_outcome

    def counting(state, ctx, result, held, **kwargs):
        update = original(state, ctx, result, held, **kwargs)
        recorded.append(update["outcomes"][-1].stage)
        return update

    monkeypatch.setattr(sv, "record_outcome", counting)
    result = await sv.run_supervisor_workflow(two_part())
    assert recorded == STAGES  # A2's two workers produced one manufacturing outcome
    dumped = json.dumps(result.summary())
    assert "_usd" not in dumped and "evidence" not in dumped and "margin" not in dumped
    assert result.human_approval_required and not (result.approved or result.released)


def test_security_concurrency_lives_only_in_the_trusted_context():
    assert "part_concurrency" not in sv.RoutingState.__annotations__  # never graph state
    assert "max_concurrency" not in a2.PartCheckInput.model_fields  # never a tool argument


# --- F. execution safety --------------------------------------------------------------------


@A
async def test_safety_attempt_timeout_cancels_every_worker_and_leaves_no_task():
    gate = Gate()  # never released
    before = asyncio.all_tasks()
    a = await evaluate(parts("P-1", "P-2", "P-3"), gate, 2, timeout_seconds=0.05)
    assert [(p.status, p.error) for p in a.parts] == [("error", "timeout")] * 3
    assert (a.status, a.error) == ("error", "timeout")
    assert gate.started == ["P-1", "P-2"]  # P-3 never got a slot
    assert gate.active == 0 and new_tasks(before) == set()


@A
async def test_safety_timeout_keeps_finished_parts_exactly_as_sequentially():
    async def outcome(limit):
        gate = Gate()
        gate.release("P-1")
        return await evaluate(parts("P-1", "P-2"), gate, limit, timeout_seconds=0.05)

    a, b = await outcome(1), await outcome(2)
    assert b == a and [p.status for p in b.parts] == ["feasible", "error"]
    assert (b.parts[1].error, b.status) == ("timeout", "error")  # never shown as evaluated


@A
async def test_safety_a_worker_ignoring_cancellation_is_awaited_and_its_late_result_dropped():
    answered = []

    async def stubborn(tool, request):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:  # swallows the cancel and answers anyway
            answered.append(request.part.part_id)
            return service_result(request.part)

    before = asyncio.all_tasks()
    a = await evaluate(parts("P-1", "P-2"), stubborn, 2, timeout_seconds=0.05)
    assert sorted(answered) == ["P-1", "P-2"]  # both settled before evaluate returned
    assert [(p.status, p.error) for p in a.parts] == [("error", "timeout")] * 2  # not kept
    assert new_tasks(before) == set()


@A
async def test_safety_external_cancellation_during_fan_out_leaves_no_orphan():
    gate = Gate()
    before = asyncio.all_tasks()
    task = asyncio.create_task(evaluate(parts("P-1", "P-2"), gate, 2))
    await until(lambda: gate.active == 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.active == 0 and sorted(gate.finished) == ["P-1", "P-2"]
    assert new_tasks(before) == set()


@A
async def test_safety_supervisor_cancellation_during_fan_out_clears_custody(monkeypatch):
    made = track_custody(monkeypatch)
    gate = Gate()
    ctx = two_part(router=Router(manufacturing=gate))
    task = asyncio.create_task(sv.run_supervisor_workflow(ctx))
    await until(lambda: gate.active == 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await until(lambda: gate.active == 0)  # both workers settle; neither completes the stage
    assert made and all(c.get(s) is None for c in made for s in STAGES)
    assert "manufacturing" not in made[0].kept


def faulty_assess(monkeypatch, failing: str):
    original = a2._assess

    def faulty(i, specification, out, digest, intake):
        if specification.part_id == failing:
            raise RuntimeError(f"internal detail {TOKEN}")
        return original(i, specification, out, digest, intake)

    monkeypatch.setattr(a2, "_assess", faulty)


@A
async def test_safety_unexpected_worker_fault_cancels_and_awaits_siblings(monkeypatch):
    faulty_assess(monkeypatch, "P-2")
    gate = Gate()
    gate.release("P-2")  # P-2 faults at once; P-1 stays blocked until cancelled
    before = asyncio.all_tasks()
    with pytest.raises(ExceptionGroup):
        await evaluate(parts("P-1", "P-2"), gate, 2)
    assert gate.active == 0 and "P-1" in gate.finished and new_tasks(before) == set()


@A
async def test_safety_unexpected_worker_fault_is_a_sanitized_stage_failure(monkeypatch):
    faulty_assess(monkeypatch, "BRK-201")
    spy = Spy(monkeypatch)
    result = await sv.run_supervisor_workflow(two_part())
    assert (result.route.action, result.route.stopped_at, result.route.reason) == ("failed", "manufacturing", "internal_error")  # fmt: skip
    assert spy.stages()[-1] == "manufacturing" and not result.draft_available
    assert TOKEN not in json.dumps(result.summary())


# --- G. compatibility -----------------------------------------------------------------------


def test_compat_defaults_keep_phase6_sequential_and_supervisor_bounded():
    signature = inspect.signature(a2.evaluate_manufacturing_feasibility)
    assert signature.parameters["max_concurrency"].default == 1  # Phase 6 and direct callers
    assert context().part_concurrency == 2 <= a2.MAX_CONCURRENCY == 4
