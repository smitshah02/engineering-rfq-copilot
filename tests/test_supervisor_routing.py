"""routing Supervisor routing: decide(), record_outcome(), routing-state validation and the
conditional LangGraph edges. Offline: no model, no MCP, no database.

Stage results come from the specialist workflow offline harness (real A1-A5 agents over the existing
fakes), never built by hand. The delegation node in ``delegating_graph`` is a test fixture:
it hands the delegated stage's precomputed result to the production ``record_outcome``.
delegation replaces it with real delegation; the production graph has no such node.

Sections (-k): test_decision_, test_graph_, test_prereq_, test_scenario_, test_security_,
test_compat_, test_failure_."""

import asyncio
import inspect
import json
import typing

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime
from test_hardening_services import APPROVAL_V12
from test_intake import ev
from test_intake_validation import COMPLETE, RULES, complete_args
from test_manufacturing_agent import Tool as ManufacturingTool
from test_pricing_agent import TRUSTED, Pricing
from test_specialized_agent_integration import SHORT, Router, bent_intake, drifted, override, run
from test_supervisor import TOKEN, Executor, raises_code, slow_graph
from test_supply_agent import manufacturing, part, tweak

from rfq_copilot import agent_integration as wf
from rfq_copilot import llm
from rfq_copilot import manufacturing_agent as a2
from rfq_copilot import supervisor as sv
from rfq_copilot.api.auth import Principal
from rfq_copilot.mcp.client import McpToolClient

A = pytest.mark.asyncio
STAGES = [s.stage for s in sv.STAGE_PLAN]
TERMINALS = set(sv.DESTINATIONS.values())
ACTION_OF_STATUS = {"draft_ready_for_review": "complete", "requires_review": "review", "clarification_required": "clarification", "not_quotable": "blocked", "not_evaluated": "blocked", "error": "failed"}  # fmt: skip
AMBIGUOUS = " Deliver by 2026-12-15 or 2026-12-22."
LATE = " Deliver by 2026-11-12."
PRINCIPAL = Principal("CUST-008", frozenset({"view_quote"}), user_id="U-1", subject="sub-1")


@pytest.fixture(autouse=True)
def offline_harness(monkeypatch):
    """No direct MCP. The specialist workflow offline A2 fake is bound to the RULES fixture (sha "test"),
    so exactly that object is admitted as the manufacturing rule; every other check stands."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("routing must not call MCP")

    monkeypatch.setattr(McpToolClient, "call", forbidden)
    original = sv._manufacturing_trusted
    monkeypatch.setattr(sv, "_manufacturing_trusted", lambda r: r is RULES or original(r))


def context(**over) -> sv.SupervisorContext:
    kwargs = {
        "principal": PRINCIPAL,
        "execute": Executor(),
        "rfq_id": "RFQ-1",
        "source_id": "src-1",
        "source_text": COMPLETE,
        "manufacturing_rules": RULES,
        "pricing_rules": TRUSTED,
        "approval_rules": APPROVAL_V12,
    }
    return sv.SupervisorContext(**(kwargs | over))


def results_of(a: wf.RFQWorkflowAssessment) -> dict:
    """The stage results specialist workflow kept, by stage."""
    return {s.stage: getattr(a, s.key) for s in sv.STAGE_PLAN if getattr(a, s.key) is not None}


def pricing_drift() -> Router:
    drift = tweak(pricing_rule_fingerprint="0" * 64)
    return Router(pricing=Pricing(rewrite={"estimate_costs": drift, "calculate_price": drift}))


# name -> (text, intake model arguments, router factory)
# fmt: off
SCENARIOS = {
    "baseline": (COMPLETE, None, Router),
    "missing_thickness": (COMPLETE.replace("2 mm thick, ", ""), complete_args(thickness=None), Router),
    "ambiguous_delivery": (COMPLETE + AMBIGUOUS, complete_args() | {"requested_delivery_date": ev("2026-12-15", AMBIGUOUS.strip(" ."))}, Router),
    "unsupported_requirement": (COMPLETE + " Also cast the panels.", None, Router),
    "unsupported_thickness": (COMPLETE.replace("2 mm thick", "5 mm thick"), complete_args(thickness=ev("5 mm", "5 mm thick")), Router),
    "manufacturing_rule_drift": (COMPLETE, None, lambda: Router(manufacturing=ManufacturingTool(rewrite=drifted))),
    "insufficient_stock": (COMPLETE, None, lambda: Router(available=SHORT)),
    "unachievable_delivery": (COMPLETE + LATE, complete_args() | {"requested_delivery_date": ev("2026-11-12", LATE.strip(" ."))}, Router),
    "pricing_rule_drift": (COMPLETE, None, pricing_drift),
    "borderline_bend": (COMPLETE, None, Router),
    "manufacturing_error": (COMPLETE, None, Router),
}
# fmt: on
# name -> (route action, stopped_at): what the specialist workflow gating requires
EXPECTED = {
    "baseline": ("complete", None),
    "missing_thickness": ("clarification", "intake_validation"),
    "ambiguous_delivery": ("clarification", "intake_validation"),
    "unsupported_requirement": ("review", "intake_validation"),
    "unsupported_thickness": ("blocked", "manufacturing"),
    "manufacturing_rule_drift": ("review", "manufacturing"),  # engineering review
    "insufficient_stock": ("review", "quotation"),  # constrained supply is priced, not blocked
    "unachievable_delivery": ("review", "quotation"),
    "pricing_rule_drift": ("review", "quotation"),  # advisory: A5 replays A4, no amount
    "borderline_bend": ("review", "quotation"),
    "manufacturing_error": ("failed", "manufacturing"),  # a technical failure is not hidden
}


async def scenario(name: str, monkeypatch) -> tuple[str, wf.RFQWorkflowAssessment]:
    if name == "borderline_bend":
        minimum = (await manufacturing()).parts[0].evidence.output.min_bend_radius_mm
        bent_intake(monkeypatch, part(bend_radius_mm=minimum))
    if name == "manufacturing_error":
        override(monkeypatch, a2, "evaluate_manufacturing_feasibility", status="error")
    text, args, router = SCENARIOS[name]
    a, _ = await run(text, args, router())
    return text, a


def delegating_graph(results: dict):
    """The routing graph plus a fixture delegation node (see the module docstring)."""

    async def deliver(state, runtime: Runtime[sv.SupervisorContext]):
        stage = state["route"].next_stage
        prior = {o.stage: results[o.stage] for o in state.get("outcomes", ())}
        return sv.record_outcome(state, runtime.context, results.get(stage), prior)

    graph = sv._foundation(sv.ROUTING_RECURSION_LIMIT, sv.RoutingState)
    graph.add_node("deliver", deliver)
    sv.attach_routing(graph, after="initialize_supervisor", delegate="deliver")
    return graph.compile()


async def node_order(graph, ctx) -> list[str]:
    chunks = [c async for c in graph.astream({}, context=ctx, stream_mode="updates")]
    return [name for chunk in chunks for name in chunk if name != "__metadata__"]


async def first_route_state(ctx) -> dict:
    """The production routing graph's final state: intake_extraction delegated, nothing run."""
    return await sv.build_routing_graph().ainvoke({}, context=ctx)


def reroute(state: dict) -> dict:
    """What route_supervisor writes after a record (the graph tests cover the node itself)."""
    d = sv.decide(state.get("outcomes", ()))
    return state | {"route": d, "clarifications": d.clarifications, "review_reasons": d.review_reasons}  # fmt: skip


def advance(state: dict, ctx, results: dict, stages) -> dict:
    """Record ``stages`` in order through record_outcome, re-routing after each."""
    for stage in stages:
        prior = {o.stage: results[o.stage] for o in state.get("outcomes", ())}
        state = reroute(state | sv.record_outcome(state, ctx, results[stage], prior))
    return state


def oc(stage: str, decision: str = "continue", **kw) -> sv.StageOutcome:
    spec = sv._SPEC[stage]
    fields = {"status": "x", "attempt_id": "a" * 32} | kw
    return sv.StageOutcome(stage=stage, agent=spec.agent, decision=decision, **fields)


def continued(n: int) -> tuple[sv.StageOutcome, ...]:
    return tuple(oc(s) for s in STAGES[:n])


async def baseline_results(monkeypatch) -> dict:
    return results_of((await scenario("baseline", monkeypatch))[1])


# --- A. routing decisions (policy) --------------------------------------------------------


def test_decision_initial_stage_is_intake_extraction():
    assert sv.decide(()) == sv.RouteDecision(action="delegate", next_stage="intake_extraction")


@pytest.mark.parametrize("n", range(len(STAGES)))
def test_decision_selects_the_next_planned_stage_after_verified_outcomes(n):
    route = sv.decide(continued(n))
    assert (route.action, route.next_stage, route.stopped_at) == ("delegate", STAGES[n], None)


STOPS = [
    (i, status, nxt)
    for i, stage in enumerate(STAGES)
    for status, nxt in wf._NEXT[stage].items()
    if nxt != wf._GO
]


@pytest.mark.parametrize(("i", "status", "decision"), STOPS)
def test_decision_every_specialist_workflow_stop_maps_to_one_route(i, status, decision):
    stage = STAGES[i]
    stop = oc(stage, decision, status=status, reason="code", clarifications=("0:f:missing",))
    route = sv.decide((*continued(i), stop))
    assert route.action == ACTION_OF_STATUS[decision]
    assert route.next_stage is None and route.outcome == decision and route.reason == "code"
    assert route.stopped_at == (None if decision == "draft_ready_for_review" else stage)
    assert route.clarifications == ("0:f:missing",)
    assert (route.human_approval_required, route.approved, route.released) == (True, False, False)


def test_decision_review_conditions_that_specialist_workflow_continues_do_not_stop():
    continues = {(s, status) for s in STAGES for status, n in wf._NEXT[s].items() if n == wf._GO}
    assert ("supply", "constrained") in continues  # priced with the constraint carried
    assert ("pricing", "requires_review") in continues  # A5 replays A4, no amount
    route = sv.decide((*continued(3), oc("supply", status="constrained")))
    assert (route.action, route.next_stage) == ("delegate", "pricing")


@pytest.mark.parametrize(
    "outcomes",
    [
        continued(6),  # A5 never continues
        (oc("manufacturing"),),  # intake never recorded
        (oc("intake_extraction"), oc("supply")),  # prerequisites skipped
    ],
)
def test_decision_impossible_outcomes_fail_closed(outcomes):
    with raises_code("invalid_state"):
        sv.decide(outcomes)


# --- B. compiled graph and conditional edges ----------------------------------------------


def test_graph_routes_through_genuine_conditional_edges():
    drawn = sv.build_routing_graph().get_graph()
    out = [e for e in drawn.edges if e.source == "route_supervisor"]
    assert {e.target for e in out} == TERMINALS
    assert out and all(e.conditional for e in out)  # no unconditional edge beside them
    assert not any(e.conditional for e in drawn.edges if e.source != "route_supervisor")
    assert {e.source for e in drawn.edges if e.target == "__end__"} == TERMINALS
    names = set(drawn.nodes) - {"__start__", "__end__"}
    assert names == {"trusted_entry", "initialize_supervisor", "route_supervisor", *TERMINALS}


@A
async def test_graph_production_run_selects_only_the_first_stage():
    ctx = context()
    result = await sv.run_supervisor_routing(ctx)
    assert result.status == "routed" and result.scope == "supervisor_routing", result.summary()
    assert (result.route.action, result.route.next_stage) == ("delegate", "intake_extraction")
    assert {s.status for s in result.stages} == {"pending"}  # selected is not executed
    assert not (result.evaluated or result.draft_available or result.approved or result.released)
    assert ctx.execute.calls == []


@A
async def test_graph_production_run_visits_only_the_selected_branch():
    order = await node_order(sv.build_routing_graph(), context())
    assert order == ["trusted_entry", "initialize_supervisor", "route_supervisor", "await_delegation"]  # fmt: skip


@A
async def test_graph_delegation_loop_is_ordered_and_bounded(monkeypatch):
    graph = delegating_graph(await baseline_results(monkeypatch))
    order = await node_order(graph, context())
    loop = [n for pair in zip(["deliver"] * 6, ["route_supervisor"] * 6) for n in pair]
    assert order == ["trusted_entry", "initialize_supervisor", "route_supervisor", *loop, "routing_complete"]  # fmt: skip
    assert len(order) < sv.ROUTING_RECURSION_LIMIT


@A
async def test_graph_stop_branch_runs_no_later_stage_or_other_terminal(monkeypatch):
    text, a = await scenario("missing_thickness", monkeypatch)
    order = await node_order(delegating_graph(results_of(a)), context(source_text=text))
    assert order.count("deliver") == 2  # extraction and validation only
    assert order[-1] == "clarification_required"
    assert len(TERMINALS & set(order)) == 1


@A
async def test_graph_production_run_invokes_no_specialist_llm_or_mcp(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("must not run during routing-only evaluation")

    for spec in sv.STAGE_PLAN:
        monkeypatch.setattr(inspect.getmodule(spec.entry), spec.entry.__name__, boom)
    monkeypatch.setattr(llm, "invoke", boom)
    monkeypatch.setattr(llm, "invoke_structured", boom)
    monkeypatch.setattr(wf, "evaluate_rfq_workflow", boom)
    assert (await sv.run_supervisor_routing(context())).status == "routed"


# --- C. prerequisites and transitions -----------------------------------------------------

WRONG_ORDER = [
    ("intake_validation", "manufacturing"),  # manufacturing before validated intake
    ("manufacturing", "supply"),  # supply before manufacturing evidence
    ("supply", "pricing"),  # pricing without supply evidence
    ("pricing", "quotation"),  # quotation before pricing
    ("intake_extraction", "quotation"),  # straight to the quotation
]


@A
@pytest.mark.parametrize(("delegated", "offered"), WRONG_ORDER)
async def test_prereq_a_later_stage_result_cannot_fill_the_delegated_stage(monkeypatch, delegated, offered):  # fmt: skip
    ctx, results = context(), await baseline_results(monkeypatch)
    state = advance(await first_route_state(ctx), ctx, results, STAGES[: STAGES.index(delegated)])
    assert state["route"].next_stage == delegated
    prior = {o.stage: results[o.stage] for o in state.get("outcomes", ())}
    state = reroute(state | sv.record_outcome(state, ctx, results[offered], prior))
    route = state["route"]
    assert (route.action, route.stopped_at, route.reason) == ("failed", delegated, "unexpected_result_type")  # fmt: skip
    assert [s.status for s in state["stages"]][STAGES.index(delegated) + 1 :] == ["pending"] * (5 - STAGES.index(delegated))  # fmt: skip


@A
async def test_prereq_a_stage_is_recorded_once(monkeypatch):
    ctx, results = context(), await baseline_results(monkeypatch)
    state = await first_route_state(ctx)
    recorded = state | sv.record_outcome(state, ctx, results["intake_extraction"], {})
    with raises_code("invalid_route"):  # same delegation, already answered
        sv.record_outcome(recorded, ctx, results["intake_extraction"], {})


@A
async def test_prereq_nothing_is_recorded_after_a_terminal_route(monkeypatch):
    text, a = await scenario("missing_thickness", monkeypatch)
    ctx, results = context(source_text=text), results_of(a)
    state = advance(await first_route_state(ctx), ctx, results, STAGES[:2])
    assert state["route"].action == "clarification"
    prior = {s: results[s] for s in STAGES[:2]}
    with raises_code("invalid_route"):
        sv.record_outcome(state, ctx, results["intake_validation"], prior)


@A
async def test_prereq_prior_results_must_be_exactly_the_recorded_stages(monkeypatch):
    ctx, results = context(), await baseline_results(monkeypatch)
    state = advance(await first_route_state(ctx), ctx, results, STAGES[:2])
    for prior in [
        {"intake_extraction": results["intake_extraction"]},  # one missing
        {s: results[s] for s in STAGES[:3]},  # one extra
        dict.fromkeys(STAGES[:2], results["intake_validation"]),  # a result of the wrong type
    ]:
        with raises_code("invalid_state"):
            sv.record_outcome(state, ctx, results["manufacturing"], prior)


@A
async def test_prereq_skipped_reordered_or_backward_state_is_rejected(monkeypatch):
    ctx, results = context(), await baseline_results(monkeypatch)
    state = advance(await first_route_state(ctx), ctx, results, STAGES[:3])
    outcomes = state["outcomes"]
    for forged in [
        state | {"outcomes": (outcomes[0], outcomes[2])},  # intake validation skipped
        state | {"outcomes": outcomes[::-1]},
        state | {"route": sv.decide(outcomes[:1])},  # backwards: intake_validation again
        state | {"route": sv.RouteDecision(action="delegate", next_stage="quotation")},  # ahead
    ]:
        with raises_code("invalid_state"):
            sv.validate_routing_state(forged, ctx)


# --- D. clarification, review, blocked and failure scenarios ------------------------------


@A
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_scenario_routing_matches_the_specialist_workflow_gating(monkeypatch, name):
    text, a = await scenario(name, monkeypatch)
    result = await sv._route(delegating_graph(results_of(a)), context(source_text=text))
    route = result.route
    assert result.status == "routed", result.summary()
    assert (route.action, route.stopped_at) == EXPECTED[name], route
    assert route.action == ACTION_OF_STATUS[a.status], a.summary()
    if route.action != "complete":
        assert route.stopped_at == (a.stopped_at or "quotation")
    assert (route.clarifications, route.review_reasons) == (a.clarifications, a.review_reasons)
    if route.action == "failed":
        assert route.reason == a.error
    executed = [s.stage for s in result.stages if s.status != "pending"]
    assert executed == [s.stage for s in a.stages]  # the same stages ran, in the same order
    assert (route.approved, route.released, result.approved, result.draft_available) == (False, False, False, False)  # fmt: skip


@A
async def test_scenario_missing_and_ambiguous_values_are_asked_not_invented(monkeypatch):
    asked = [("missing_thickness", "thickness"), ("ambiguous_delivery", "requested_delivery_date")]
    for name, field in asked:
        text, a = await scenario(name, monkeypatch)
        result = await sv._route(delegating_graph(results_of(a)), context(source_text=text))
        assert any(field in c for c in result.route.clarifications), result.route
        assert result.stages[2].status == "pending"  # manufacturing never selected


@A
async def test_scenario_supply_constraint_is_review_not_a_manufacturing_failure(monkeypatch):
    text, a = await scenario("insufficient_stock", monkeypatch)
    result = await sv._route(delegating_graph(results_of(a)), context(source_text=text))
    by_stage = {s.stage: s.status for s in result.stages}
    assert by_stage["manufacturing"] == by_stage["supply"] == by_stage["pricing"] == "completed"
    assert "supply_constrained" in result.route.review_reasons
    assert result.route.action == "review" and result.route.outcome == "requires_review"


@A
@pytest.mark.parametrize(("name", "status"), [("baseline", "available"), ("insufficient_stock", "constrained")])  # fmt: skip
async def test_scenario_supply_outcome_continues_to_pricing(monkeypatch, name, status):
    text, a = await scenario(name, monkeypatch)
    ctx, results = context(source_text=text), results_of(a)
    state = advance(await first_route_state(ctx), ctx, results, STAGES[:4])
    supply = state["outcomes"][3]
    assert (supply.stage, supply.status, supply.decision, supply.reason) == ("supply", status, "continue", None)  # fmt: skip
    assert state["stages"][3].status == "completed"  # a constraint is not a failure
    assert (state["route"].action, state["route"].next_stage) == ("delegate", "pricing")


@A
async def test_scenario_pricing_review_stays_explicit_and_carries_no_amount(monkeypatch):
    text, a = await scenario("pricing_rule_drift", monkeypatch)
    result = await sv._route(delegating_graph(results_of(a)), context(source_text=text))
    assert result.route.review_reasons == ("pricing_rule_drift",)
    assert "_usd" not in json.dumps(result.summary())


@A
async def test_scenario_complete_route_is_not_an_approval(monkeypatch):
    text, a = await scenario("baseline", monkeypatch)
    result = await sv._route(delegating_graph(results_of(a)), context(source_text=text))
    assert (result.route.action, result.route.outcome) == ("complete", "draft_ready_for_review")
    assert result.route.human_approval_required is True
    assert (result.approved, result.released, result.evaluated) == (False, False, False)


# --- E. adversarial -----------------------------------------------------------------------


def forge_result(stage: str, **update):
    return lambda results: results | {stage: results[stage].model_copy(update=update)}


# fmt: off
FORGED_RESULTS = [
    ("intake_extraction", forge_result("intake_extraction", source_sha256="0" * 64), "source_mismatch"),
    ("intake_validation", forge_result("intake_validation", rfq_id="RFQ-9"), "identity_mismatch"),
    ("intake_validation", forge_result("intake_validation", source_id="src-other"), "identity_mismatch"),
    ("intake_validation", forge_result("intake_validation", manufacturing_rule_fingerprint="f" * 64), "rule_mismatch"),
    ("manufacturing", forge_result("manufacturing", manufacturing_rule_version="mfg-v9"), "rule_mismatch"),
    ("pricing", forge_result("pricing", pricing_rule_fingerprint="f" * 64), "rule_mismatch"),
    ("quotation", forge_result("quotation", approval_rule_fingerprint="f" * 64), "rule_mismatch"),
    ("quotation", forge_result("quotation", status="approved"), "unexpected_stage_status"),
    ("supply", forge_result("supply", status="reserved"), "unexpected_stage_status"),
    ("manufacturing", lambda r: r | {"manufacturing": {"status": "feasible"}}, "unexpected_result_type"),
    ("pricing", lambda r: r | {"pricing": None}, "unexpected_result_type"),  # missing evidence
    ("supply", forge_result("supply", rfq_id="RFQ-9"), "identity_mismatch"),
    ("supply", forge_result("supply", source_id="src-other"), "identity_mismatch"),
    ("supply", lambda r: r | {"supply": None}, "unexpected_result_type"),  # missing supply result
    ("supply", lambda r: r | {"supply": r["manufacturing"]}, "unexpected_result_type"),  # another stage's
    # rule-bearing stages that advance without rule provenance fail closed
    ("manufacturing", forge_result("manufacturing", manufacturing_rule_version=None, manufacturing_rule_fingerprint=None), "rule_mismatch"),
    ("pricing", forge_result("pricing", pricing_rule_fingerprint=None), "rule_mismatch"),
    ("quotation", forge_result("quotation", approval_rule_version=None, approval_rule_fingerprint=None), "rule_mismatch"),
]
# fmt: on


@A
@pytest.mark.parametrize(("stage", "forge", "reason"), FORGED_RESULTS)
async def test_security_forged_stage_results_fail_routing_at_that_stage(monkeypatch, stage, forge, reason):  # fmt: skip
    results = forge(await baseline_results(monkeypatch))
    result = await sv._route(delegating_graph(results), context())
    assert (result.route.action, result.route.stopped_at, result.route.reason) == ("failed", stage, reason)  # fmt: skip
    later = [s.status for s in result.stages][STAGES.index(stage) + 1 :]
    assert set(later) <= {"pending"}  # nothing after the forged stage was selected


@A
async def test_security_results_from_another_workflow_are_refused(monkeypatch):
    results = await baseline_results(monkeypatch)
    result = await sv._route(delegating_graph(results), context(rfq_id="RFQ-2"))
    assert (result.route.action, result.route.stopped_at, result.route.reason) == ("failed", "intake_extraction", "identity_mismatch")  # fmt: skip
    other_text = await sv._route(delegating_graph(results), context(source_text=COMPLETE + " "))
    assert (other_text.route.stopped_at, other_text.route.reason) == ("intake_extraction", "source_mismatch")  # fmt: skip


@A
async def test_security_forged_routing_state_is_rejected(monkeypatch):
    ctx = context()
    final = await delegating_graph(await baseline_results(monkeypatch)).ainvoke({}, context=ctx)
    sv.validate_routing_state(final, ctx)  # the genuine state passes
    outcomes, route = final["outcomes"], final["route"]
    forgeries = [
        {"outcomes": (*outcomes[:-1], outcomes[-1].model_copy(update={"attempt_id": "b" * 32}))},
        {"outcomes": (outcomes[0].model_copy(update={"decision": "error"}), *outcomes[1:])},
        {"outcomes": outcomes[:-1]},  # last outcome dropped, route kept
        {"route": route.model_copy(update={"approved": True})},
        {"route": route.model_copy(update={"action": "delegate", "next_stage": "quotation"})},
        {"review_reasons": (TOKEN,)},  # credential in routing indicators
        {"identity": final["identity"].model_copy(update={"customer_id": "CUST-999"})},
        {"identity": final["identity"].model_copy(update={"rfq_id": "RFQ-9"})},
        {"identity": final["identity"].model_copy(update={"source_sha256": "0" * 64})},
        {"rules": (("mfg-v1.0", "f" * 64), *final["rules"][1:])},
        {"attempt_id": "c" * 32},  # outcomes now belong to another attempt
        {"next_agent": "A5"},
        {"manager_approved": True},
    ]
    for forged in forgeries:
        with raises_code("invalid_state"):
            sv.validate_routing_state(final | forged, ctx)


@A
@pytest.mark.parametrize(
    "seed",
    [
        {"outcomes": ()},
        {"route": sv.RouteDecision(action="delegate", next_stage="quotation")},
        {"route": sv.RouteDecision(action="complete", outcome="draft_ready_for_review")},
        {"stages": ()},
    ],
)
async def test_security_routing_graph_refuses_seeded_state(seed):
    with raises_code("invalid_initial_state"):
        await sv.build_routing_graph().ainvoke(seed, context=context())


@A
async def test_security_customer_flags_never_steer_the_route():
    flags = {"next_agent": "A5", "skip_intake": True, "approved": True, "pricing_verified": True, "manufacturing_passed": True, "manager_approved": True}  # fmt: skip
    state = await sv.build_routing_graph().ainvoke(flags, context=context())
    assert not set(flags) & set(state)
    assert (state["route"].action, state["route"].next_stage) == ("delegate", "intake_extraction")


@A
async def test_security_routing_output_holds_no_cost_text_or_credential(monkeypatch):
    text, a = await scenario("baseline", monkeypatch)
    result = await sv._route(delegating_graph(results_of(a)), context(source_text=text))
    dumped = json.dumps(result.summary())
    secrets = ("_usd", "cost", "evidence", TOKEN, COMPLETE, str(a.pricing.total_manufacturing_cost_usd))  # fmt: skip
    for secret in secrets:
        assert secret not in dumped
    assert set(result.summary()) == {*type(result).model_fields} and "route" in result.summary()


@A
async def test_security_routing_result_cannot_claim_approval():
    valid = (await sv.run_supervisor_routing(context())).model_dump()
    sv.SupervisorRoutingResult(**valid)
    for claim in [
        {"approved": True},
        {"route": valid["route"] | {"approved": True}},
        {"route": None},  # routed without a route
        {"status": "error", "error": "invalid_state"},  # failure carrying a route and state
        {"scope": "supervisor_foundation"},
    ]:
        with pytest.raises(ValueError):
            sv.SupervisorRoutingResult(**(valid | claim))


@A
async def test_security_a_delegation_node_that_records_nothing_cannot_loop():
    async def idle(state):
        return {}

    graph = sv._foundation(sv.ROUTING_RECURSION_LIMIT, sv.RoutingState)
    graph.add_node("idle", idle)
    sv.attach_routing(graph, after="initialize_supervisor", delegate="idle")
    result = await sv._route(graph.compile(), context())
    assert (result.status, result.error) == ("error", "no_progress")


@A
async def test_security_a_delegation_node_cannot_write_outside_record_outcome(monkeypatch):
    async def rogue(state):
        return {"route": sv.RouteDecision(action="complete", outcome="draft_ready_for_review")}

    graph = sv._foundation(sv.ROUTING_RECURSION_LIMIT, sv.RoutingState)
    graph.add_node("rogue", rogue)
    sv.attach_routing(graph, after="initialize_supervisor", delegate="rogue")
    result = await sv._route(graph.compile(), context())
    assert (result.status, result.error) == ("error", "invalid_state")


# --- F. compatibility ---------------------------------------------------------------------


@A
async def test_compat_supervisor_foundation_foundation_is_unchanged():
    graph = sv.build_supervisor_graph()
    nodes = set(graph.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"trusted_entry", "initialize_supervisor"}
    result = await sv.run_supervisor_foundation(context())
    assert (result.scope, result.status) == ("supervisor_foundation", "initialized")
    assert not hasattr(result, "route")
    assert sv.Budget(timeout_seconds=1).recursion_limit == sv.RECURSION_LIMIT == 4


def test_compat_routing_bounds_and_destinations_follow_the_plan():
    assert set(sv.DESTINATIONS) == set(typing.get_args(sv.RouteAction))
    assert len(set(sv.DESTINATIONS.values())) == len(sv.DESTINATIONS)
    assert set(sv._ACTION_OF) | {"continue"} == set(typing.get_args(sv.Decision))
    workflow_stops = {n for table in wf._NEXT.values() for n in table.values()} - {wf._GO}
    assert workflow_stops == set(
        sv._ACTION_OF
    )  # every specialist workflow stop has exactly one route
    assert sv.ROUTING_RECURSION_LIMIT > 3 + 2 * len(sv.STAGE_PLAN) + 1
    assert sv.DEFAULT_TIMEOUT == wf.DEFAULT_TIMEOUT == 390.0


def test_compat_router_reuses_specialist_workflow_gating_instead_of_copying_it():
    source = inspect.getsource(sv)
    reused = ("wf._NEXT", "wf._handoff_error", "wf._clarifications", "wf._reviews", "wf._reason")
    for name in reused:
        assert name in source
    for copied in ("def _clarifications", "def _reviews", "_NEXT: dict", '"identity_mismatch"'):
        assert copied not in source
    assert "evaluate_rfq_workflow" not in source and "rfq_copilot.graph" not in source


def test_compat_rule_provenance_covers_every_stage_with_real_fields():
    assert list(sv._RULE_FIELDS) == STAGES  # every planned stage, explicitly, in order
    for stage, fields in sv._RULE_FIELDS.items():
        declared = sv._SPEC[stage].produces.model_fields
        rule_fields = {f for f in declared if f.endswith(("_rule_version", "_rule_fingerprint"))}
        if fields is None:  # A1 extraction (source hash) and A3 supply carry no rule field
            assert stage in ("intake_extraction", "supply") and not rule_fields
        else:
            index, version, fingerprint = fields
            assert {version, fingerprint} <= rule_fields  # no fictitious field
            assert index == {"manufacturing": 0, "pricing": 1, "approval": 2}[version.split("_")[0]]


# --- G. failure and bounded execution -----------------------------------------------------


def test_failure_unknown_stage_provenance_is_refused():
    with raises_code("invalid_state"):
        sv._provenance_error("routing", object(), {}, True)


@A
@pytest.mark.parametrize("state", [None, {}, "state", {"route": "delegate"}])
async def test_failure_invalid_routing_state_fails_closed(state):
    with raises_code("invalid_state"):
        sv.validate_routing_state(state, context())


@A
async def test_failure_budget_exhaustion_is_execution_limit():
    result = await sv._route(slow_graph(), context(timeout_seconds=0.05))
    assert (result.status, result.error, result.route) == ("error", "execution_limit", None)


@A
async def test_failure_cancellation_propagates():
    task = asyncio.create_task(sv._route(slow_graph(), context()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@A
async def test_failure_checkpointer_is_refused():
    with pytest.raises(ValueError, match="without a checkpointer"):
        await sv._route(slow_graph(InMemorySaver()), context())


@A
async def test_failure_delegation_fault_is_sanitised_and_not_retried():
    calls = []

    async def broken(state):
        calls.append(1)
        raise RuntimeError(f"pricing database password {TOKEN}")

    graph = sv._foundation(sv.ROUTING_RECURSION_LIMIT, sv.RoutingState)
    graph.add_node("broken", broken)
    sv.attach_routing(graph, after="initialize_supervisor", delegate="broken")
    result = await sv._route(graph.compile(), context())
    assert (result.status, result.error) == ("error", "internal_error")
    assert TOKEN not in json.dumps(result.summary()) and calls == [1]


@A
async def test_failure_untrusted_context_routes_nothing():
    assert (await sv.run_supervisor_routing(None)).error == "untrusted_context"
    assert (await sv.run_supervisor_routing(context(pricing_rules=None))).error == "pricing_rules_untrusted"  # fmt: skip
    assert (await sv.run_supervisor_routing(context(source_text="   "))).error == "invalid_source"  # fmt: skip
