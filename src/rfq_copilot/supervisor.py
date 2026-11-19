"""Supervisor foundation (Supervisor foundation) and deterministic routing (routing): typed state, trusted run
context, the A1 -> A5 stage plan, and two compiled graphs. Neither runs a specialist, a model
or an MCP tool.

    foundation: START -> trusted_entry -> initialize_supervisor -> END
    routing:    START -> trusted_entry -> initialize_supervisor -> route_supervisor
                route_supervisor --(conditional on route.action)--> await_delegation |
                    clarification_required | review_required | blocked | workflow_failed |
                    routing_complete -> END

- Routing (routing): ``decide`` picks the next stage from ``STAGE_PLAN`` and the recorded
  ``StageOutcome``s, or stops. Outcomes are written only by ``record_outcome``, which derives
  them from a stage's own result with the specialist workflow checks (``_NEXT``, the handoff checks) plus
  the attempt's source hash and rule fingerprints; it records only the stage the current route
  delegated, once. Until delegation adds a delegation node (``attach_routing(delegate=...)``)
  nothing records outcomes, so ``run_supervisor_routing`` always routes to
  ``intake_extraction`` and stops at ``await_delegation``. A route is a control decision, never
  a feasibility, price or approval decision.

- Trust: everything with authority arrives in ``SupervisorContext``, the LangGraph run
  *context* (conversational graph ``TurnContext`` pattern): the verified ``Principal``, the principal-bound
  ``graph.mcp_executor``, the trusted ``rfq_id`` / ``source_id``, the customer text and the
  ``pinned_rules`` results. It is held only for the run, never in state, a result or a
  checkpoint. The customer id comes from the principal only; the source hash is computed here
  from the text with A1's formula, never supplied.
- Input: ``run_supervisor_foundation`` (the supported entry point) takes a context and no
  state. The compiled graph is an internal interface: invoked directly, any recognised
  ``SupervisorState`` key in the input fails with ``invalid_initial_state``; unrecognised
  keys are dropped by LangGraph before any node runs, so they never become state and carry
  no authority (they are filtered, not rejected).
- Ownership (``owned_update``): each node may write only its own keys. ``identity``,
  ``attempt_id``, ``rules`` and ``budget`` are written once, by ``trusted_entry``.
  ``validate_state`` re-derives them from the context and enforces the foundation state:
  every stage "pending" with no reason and no clarification or review indicator. No
  validated evidence exists yet, so nothing may claim completion. The runner validates the
  final state again before reporting success, so a supplied graph cannot forge a result.
- Stage plan: ``STAGE_PLAN`` is the code-controlled allow-list of the six specialist workflow stages,
  each naming the existing agent function, the result type it hands off and the stages it
  consumes. It is a description only; delegation delegates through it. ``agent_integration`` (the
  sequential baseline) is untouched and still the only way to evaluate an RFQ.
- Failure: a fixed error code, never exception text; cancellation propagates; no retries.
- Parallelism (parallel execution, L01): the six stages stay a strict chain. Inside A2, the per-part
  feasibility checks are independent read-only calls; the trusted ``part_concurrency``
  (default 2, max ``a2.MAX_CONCURRENCY``) bounds how many overlap. A2 joins them back into one
  ``ManufacturingAssessment``, which is the stage's single recorded outcome.
- Output: ``SupervisorFoundationResult`` says only that initialisation happened. It has no
  assessment, no amount, no draft and no ``customer_view``.
- Checkpoints: the graph compiles without a checkpointer and ``run_supervisor_foundation``
  refuses one. Its Pydantic state values are outside ``graph.SAFE_SERDE``'s allow-list, so this
  state is not checkpoint-safe; recovery decides what may be persisted.
"""

import asyncio
import hashlib
import secrets
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Annotated, Literal, TypedDict, get_args, get_origin

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
    with_config,
)

from rfq_copilot import agent_integration as wf
from rfq_copilot import escalation as esc
from rfq_copilot import intake as a1
from rfq_copilot import intake_validation as a1v
from rfq_copilot import manufacturing_agent as a2
from rfq_copilot import observability as obs
from rfq_copilot import pricing_agent as a4
from rfq_copilot import quotation_agent as a5
from rfq_copilot import supply_agent as a3
from rfq_copilot.agent_integration import DEFAULT_TIMEOUT, Stage
from rfq_copilot.api.auth import Principal
from rfq_copilot.api.schemas.common import Identifier
from rfq_copilot.api.schemas.rfq import MAX_RAW_REQUEST_CHARS
from rfq_copilot.intake_validation import (
    ToolExecutor,
)  # = graph.ToolExecutor, as specialist workflow
from rfq_copilot.mcp.client import FailureCode, McpCallError
from rfq_copilot.mcp.runtime import MANUFACTURING_RULE_VERSION
from rfq_copilot.repositories import RuleVersionDetails

RECURSION_LIMIT = 4  # foundation graph: two node steps
# Routing graph: entry, init, route, (delegate + route) per stage x 6, terminal = 16 steps.
ROUTING_RECURSION_LIMIT = 18
MAX_INDICATORS = 1024  # clarification / review codes one stage outcome may carry

AgentId = Literal["A1", "A2", "A3", "A4", "A5"]
StageStatus = Literal["pending", "completed", "stopped", "error", "not_run"]
ErrorCode = Literal[
    "invalid_initial_state",  # the graph was started with state: only the context may seed it
    "untrusted_context",
    "unauthenticated",
    "missing_dependency",  # delegation needs the trusted intake graph and planning horizon
    "invalid_identity",
    "invalid_source",
    "invalid_budget",
    "manufacturing_rules_untrusted",
    "pricing_rules_untrusted",
    "approval_rules_untrusted",
    "ownership_violation",
    "invalid_state",
    "invalid_route",  # a record or terminal node reached without the route that permits it
    "no_progress",  # routed to a stage that recorded no outcome: refused, never re-selected
    "execution_limit",
    "internal_error",
    "paused",  # recovery: stopped at a requested stage boundary (recovery.SupervisorRecovery)
    "stale_recovery",  # recovery: a held result no longer re-records as it did before the pause
]
_STRICT = ConfigDict(extra="forbid", frozen=True)


class SupervisorError(Exception):
    """A foundation failure with a fixed code; the message is the code, nothing else."""

    def __init__(self, code: ErrorCode):
        super().__init__(code)
        self.code = code


# --- stage plan (code-controlled allow-list; described, not executed) --------------------


@dataclass(frozen=True)
class StageSpec:
    stage: Stage
    agent: AgentId
    entry: Callable  # the existing agent function; referenced, never copied
    produces: type[BaseModel]  # its validated result: the typed handoff contract
    key: str  # that result's field on agent_integration.RFQWorkflowAssessment
    consumes: tuple[Stage, ...]  # earlier stage results it takes as input


# fmt: off
STAGE_PLAN: tuple[StageSpec, ...] = (
    StageSpec("intake_extraction", "A1", a1.run_intake, a1.IntakeResult, "extraction", ()),
    StageSpec("intake_validation", "A1", a1v.validate_intake, a1v.IntakeValidation, "intake", ("intake_extraction",)),
    StageSpec("manufacturing", "A2", a2.evaluate_manufacturing_feasibility, a2.ManufacturingAssessment, "manufacturing", ("intake_validation",)),
    StageSpec("supply", "A3", a3.evaluate_supply, a3.SupplyAssessment, "supply", ("manufacturing", "intake_extraction")),
    StageSpec("pricing", "A4", a4.evaluate_pricing, a4.PricingAssessment, "pricing", ("manufacturing", "supply")),
    StageSpec("quotation", "A5", a5.evaluate_quotation_review, a5.QuotationReviewAssessment, "quotation", ("manufacturing", "supply", "pricing")),
)
# fmt: on
_AGENT_OF: dict[str, AgentId] = {s.stage: s.agent for s in STAGE_PLAN}


# --- state --------------------------------------------------------------------------------


class WorkflowIdentity(BaseModel):
    """Trusted, immutable: who and what this workflow is about."""

    model_config = _STRICT
    customer_id: Identifier  # the verified principal's, never from text or a model
    rfq_id: Identifier
    source_id: Identifier
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]  # computed here


class Budget(BaseModel):
    """Trusted, immutable execution bounds for this attempt."""

    model_config = _STRICT
    timeout_seconds: Annotated[float, Field(gt=0, le=DEFAULT_TIMEOUT, allow_inf_nan=False)]
    recursion_limit: Literal[4, 18] = RECURSION_LIMIT  # foundation or routing graph


class StageState(BaseModel):
    """Supervisor-owned status of one planned stage."""

    model_config = _STRICT
    stage: Stage
    agent: AgentId
    status: StageStatus = "pending"
    reason: str | None = None  # fixed code, never exception text

    @model_validator(mode="after")
    def _planned(self):
        if _AGENT_OF.get(self.stage) != self.agent:
            raise ValueError("stage is not in the plan for this agent")
        return self


@with_config(ConfigDict(extra="forbid"))
class SupervisorState(TypedDict):
    # trusted immutable (trusted_entry only, write-once)
    identity: WorkflowIdentity
    attempt_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    rules: tuple[tuple[str, str], ...]  # (version, fingerprint), as RFQWorkflowAssessment
    budget: Budget
    # mutable workflow execution state (Supervisor only)
    phase: Literal["initialized"]
    stages: tuple[StageState, ...]
    clarifications: tuple[str, ...]
    review_reasons: tuple[str, ...]
    # No validated evidence, internal costs, customer text, credentials or callables:
    # evidence stays in the specialists' typed results (STAGE_PLAN.produces), outside state.


_STATE = TypeAdapter(SupervisorState)
_TRUSTED = frozenset({"identity", "attempt_id", "rules", "budget"})
_WRITES: dict[str, frozenset[str]] = {
    "trusted_entry": _TRUSTED,
    "initialize_supervisor": frozenset({"phase", "stages", "clarifications", "review_reasons"}),
    "route_supervisor": frozenset({"route", "clarifications", "review_reasons"}),
    "record_outcome": frozenset({"outcomes", "stages"}),  # only via record_outcome()
}


def owned_update(node: str, state: Mapping, update: dict) -> dict:
    """Allow ``node`` to write only its own keys, and trusted keys only once."""
    if not set(update) <= _WRITES.get(node, frozenset()):
        raise SupervisorError("ownership_violation")
    if any(key in state for key in update.keys() & _TRUSTED):
        raise SupervisorError("ownership_violation")
    return update


# --- trusted run context ------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorContext:
    """LangGraph run context: trusted, held only for the run, never in state or a checkpoint."""

    principal: Principal
    execute: ToolExecutor | None = field(repr=False)  # principal-bound graph.mcp_executor
    rfq_id: str
    source_id: str
    source_text: str = field(repr=False)  # untrusted customer content; hashed, not stored
    manufacturing_rules: RuleVersionDetails | None = field(repr=False)
    pricing_rules: RuleVersionDetails | None = field(repr=False)
    approval_rules: RuleVersionDetails | None = field(repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT
    # Delegation only (delegation); the foundation and routing graphs never read them.
    intake_graph: CompiledStateGraph | None = field(default=None, repr=False)  # A1's graph
    horizon_start: date | None = (
        None  # trusted planning policy, as specialist workflow's horizon_start
    )
    # parallel execution: simultaneous A2 part checks per attempt (1 = sequential, max a2.MAX_CONCURRENCY)
    part_concurrency: int = 2
    # telemetry: optional observability sink (observability.emit). Write-only, never read back,
    # never authority; None (the default) means no telemetry work at all.
    telemetry: object | None = field(default=None, repr=False)
    # Worker-only identity resolution; current RFQ persistence, never model/customer input.
    trusted_part_ids: tuple[str, ...] | None = field(default=None, repr=False)
    # Worker-only structured portal facts, absent when the request contains free text.
    structured_source: a1.StructuredIntakeSource | None = field(default=None, repr=False)


def _manufacturing_trusted(rules: object) -> bool:
    return a4._trusted(rules, MANUFACTURING_RULE_VERSION)  # A4's generic manifest check


def _admit(context: object, recursion_limit: int = RECURSION_LIMIT) -> dict:
    """The trusted keys, derived only from a valid context; fails closed with a fixed code."""
    if not isinstance(context, SupervisorContext):
        raise SupervisorError("untrusted_context")
    if not isinstance(context.principal, Principal) or not callable(context.execute):
        raise SupervisorError("unauthenticated")
    text = context.source_text  # A1's input limits; A1 still screens it when it runs
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise SupervisorError("invalid_source")
    if len(text) > MAX_RAW_REQUEST_CHARS:
        raise SupervisorError("invalid_source")
    if context.structured_source is not None and (
        type(context.structured_source) is not a1.StructuredIntakeSource
        or context.structured_source.render()[0] != text
    ):
        raise SupervisorError("invalid_source")
    try:
        identity = WorkflowIdentity(
            customer_id=context.principal.customer_id,
            rfq_id=context.rfq_id,
            source_id=context.source_id,
            source_sha256=hashlib.sha256(text.encode()).hexdigest(),  # A1's source_sha256
        )
    except ValidationError:
        raise SupervisorError("invalid_identity") from None
    try:
        budget = Budget(timeout_seconds=context.timeout_seconds, recursion_limit=recursion_limit)
    except ValidationError:
        raise SupervisorError("invalid_budget") from None
    checks = (  # the A4 / A5 manifest checks the specialist workflow preflight uses
        (context.manufacturing_rules, _manufacturing_trusted, "manufacturing_rules_untrusted"),
        (context.pricing_rules, a4._trusted, "pricing_rules_untrusted"),
        (context.approval_rules, a5._approval_trusted, "approval_rules_untrusted"),
    )
    for rules, trusted, code in checks:
        if not trusted(rules):
            raise SupervisorError(code)
    used = tuple((r.rule_version, r.fingerprint) for r, _, _ in checks)
    return {"identity": identity, "rules": used, "budget": budget}


def _pending() -> tuple[StageState, ...]:
    return tuple(StageState(stage=s.stage, agent=s.agent) for s in STAGE_PLAN)


def validate_state(
    state: Mapping, context: SupervisorContext, recursion_limit: int = RECURSION_LIMIT
) -> SupervisorState:
    """The full state, typed, bound to this context, and with no stage claiming a result."""
    try:
        checked = _STATE.validate_python(dict(state))
    except (ValidationError, TypeError, ValueError):  # not a mapping, or not this schema
        raise SupervisorError("invalid_state") from None
    trusted = _admit(context, recursion_limit)
    if any(checked[key] != value for key, value in trusted.items()):
        raise SupervisorError("invalid_state")
    plan = tuple((s.stage, s.agent) for s in STAGE_PLAN)
    if tuple((s.stage, s.agent) for s in checked["stages"]) != plan:
        raise SupervisorError("invalid_state")
    # Supervisor foundation records no evidence, so a status, reason or indicator would be a forged result.
    if any((s.status, s.reason) != ("pending", None) for s in checked["stages"]):
        raise SupervisorError("invalid_state")
    if checked["clarifications"] or checked["review_reasons"]:
        raise SupervisorError("invalid_state")
    return checked


# --- routing (routing) -------------------------------------------------------------------

Decision = Literal[
    "continue",  # specialist workflow _NEXT "continue": the next planned stage may run
    "clarification_required",
    "requires_review",
    "not_quotable",
    "not_evaluated",
    "error",
    "draft_ready_for_review",  # A5 assembled a verified, non-binding draft for human review
]
RouteAction = Literal["delegate", "clarification", "review", "blocked", "failed", "complete"]
_ACTION_OF: dict[str, RouteAction] = {
    "clarification_required": "clarification",
    "requires_review": "review",
    "not_quotable": "blocked",
    "not_evaluated": "blocked",
    "error": "failed",
    "draft_ready_for_review": "complete",
}
# The node each route action leads to. "delegate" ends at await_delegation until delegation
# supplies a delegation node (attach_routing(delegate=...)).
DESTINATIONS: dict[RouteAction, str] = {
    "delegate": "await_delegation",
    "clarification": "clarification_required",
    "review": "review_required",
    "blocked": "blocked",
    "failed": "workflow_failed",
    "complete": "routing_complete",
}
_STATUS_OF: dict[str, StageStatus] = {"continue": "completed", "draft_ready_for_review": "completed", "error": "error"}  # fmt: skip
_SPEC: dict[str, StageSpec] = {s.stage: s for s in STAGE_PLAN}
# Every planned stage, explicitly: the rule provenance its result carries, as (index in
# state["rules"], version field, fingerprint field), or None when the result has no rule field.
# A1 extraction is bound by its source hash instead. A3's SupplyAssessment has no rule field:
# A3 checks its own evidence's pricing rule (drift -> requires_review) and A4 re-checks it.
_RULE_FIELDS: dict[str, tuple[int, str, str] | None] = {
    "intake_extraction": None,
    "intake_validation": (0, "manufacturing_rule_version", "manufacturing_rule_fingerprint"),
    "manufacturing": (0, "manufacturing_rule_version", "manufacturing_rule_fingerprint"),
    "supply": None,
    "pricing": (1, "pricing_rule_version", "pricing_rule_fingerprint"),
    "quotation": (2, "approval_rule_version", "approval_rule_fingerprint"),
}
_Code = Annotated[str, Field(min_length=1, max_length=128)]
_Codes = Annotated[tuple[_Code, ...], Field(max_length=MAX_INDICATORS)]


class StageOutcome(BaseModel):
    """What one executed stage permits next. Derived by ``record_outcome`` from the stage's
    validated result, never supplied. Codes only: no amount, evidence or customer text."""

    model_config = _STRICT
    stage: Stage
    agent: AgentId
    attempt_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    status: _Code  # the agent's own status, unchanged ("invalid": not the stage's result type)
    decision: Decision
    reason: _Code | None = None
    clarifications: _Codes = ()
    review_reasons: _Codes = ()


class RouteDecision(BaseModel):
    """A control decision: the stage eligible to run next, or why routing stopped. Never a
    feasibility, schedule, price or approval decision."""

    model_config = _STRICT
    action: RouteAction
    next_stage: Stage | None = None  # eligible for delegation; not executed
    stopped_at: Stage | None = None
    outcome: Decision | None = (
        None  # the stopping stage's decision (specialist workflow vocabulary)
    )
    reason: _Code | None = None
    clarifications: _Codes = ()
    review_reasons: _Codes = ()
    human_approval_required: Literal[True] = True
    approved: Literal[False] = False
    released: Literal[False] = False


@with_config(ConfigDict(extra="forbid"))
class RoutingState(SupervisorState, total=False):
    # validated stage outcomes, in plan order: written only by record_outcome()
    outcomes: tuple[StageOutcome, ...]
    # Supervisor routing metadata: written only by route_supervisor
    route: RouteDecision


_ROUTING = TypeAdapter(RoutingState)


def decide(outcomes: tuple[StageOutcome, ...]) -> RouteDecision:
    """The route for these outcomes: deterministic, prerequisites from ``STAGE_PLAN``."""
    planned = [s.stage for s in STAGE_PLAN[: len(outcomes)]]
    past_a_stop = any(o.decision != "continue" for o in outcomes[:-1])
    if [o.stage for o in outcomes] != planned or past_a_stop:
        raise SupervisorError("invalid_state")  # skipped, reordered or past a stop
    if outcomes and outcomes[-1].decision != "continue":
        last = outcomes[-1]
        action = _ACTION_OF[last.decision]
        return RouteDecision(
            action=action,
            stopped_at=None if action == "complete" else last.stage,
            outcome=last.decision,
            reason=last.reason,
            clarifications=last.clarifications,
            review_reasons=last.review_reasons,
        )
    if len(outcomes) >= len(STAGE_PLAN):  # A5 never continues: impossible
        raise SupervisorError("invalid_state")
    spec = STAGE_PLAN[len(outcomes)]
    if not set(spec.consumes) <= {o.stage for o in outcomes if o.decision == "continue"}:
        raise SupervisorError("invalid_state")
    return RouteDecision(action="delegate", next_stage=spec.stage)


def _routes(outcomes: tuple[StageOutcome, ...]) -> tuple[RouteDecision, ...]:
    """The current route, and the delegation the last outcome answered (one record old)."""
    current = decide(outcomes)
    return (current, decide(outcomes[:-1])) if outcomes else (current,)


def _stage_states(outcomes: tuple[StageOutcome, ...]) -> tuple[StageState, ...]:
    recorded = {o.stage: o for o in outcomes}

    def one(spec: StageSpec) -> StageState:
        o = recorded.get(spec.stage)
        if o is None:
            return StageState(stage=spec.stage, agent=spec.agent)
        status = _STATUS_OF.get(o.decision, "stopped")
        return StageState(stage=spec.stage, agent=spec.agent, status=status, reason=o.reason)

    return tuple(one(s) for s in STAGE_PLAN)


def validate_routing_state(state: Mapping, context: SupervisorContext) -> RoutingState:
    """A routing state bound to this context: outcomes a prefix of ``STAGE_PLAN`` for this
    attempt, stage statuses and indicators derived from them, the route current or one record
    old. Supervisor foundation's ``validate_state`` keeps guarding the initialised state unchanged."""
    try:
        checked = _ROUTING.validate_python(dict(state))
    except (TypeError, ValueError):  # not a mapping, or not this schema
        raise SupervisorError("invalid_state") from None
    trusted = _admit(context, ROUTING_RECURSION_LIMIT)
    if any(checked[key] != value for key, value in trusted.items()):
        raise SupervisorError("invalid_state")
    outcomes = checked.get("outcomes", ())
    plan = [(s.stage, s.agent, checked["attempt_id"]) for s in STAGE_PLAN]
    if [(o.stage, o.agent, o.attempt_id) for o in outcomes] != plan[: len(outcomes)]:
        raise SupervisorError("invalid_state")
    if any(o.decision != "continue" for o in outcomes[:-1]):
        raise SupervisorError("invalid_state")
    if checked["stages"] != _stage_states(outcomes):
        raise SupervisorError("invalid_state")
    route = checked.get("route")
    if route is not None and route not in _routes(outcomes):
        raise SupervisorError("invalid_state")
    indicators = (route.clarifications, route.review_reasons) if route else ((), ())
    if (checked["clarifications"], checked["review_reasons"]) != indicators:
        raise SupervisorError("invalid_state")
    return checked


def _provenance_error(stage: str, result, state: Mapping, advancing: bool) -> str | None:
    """Source hash (A1 extraction) or rule provenance against the attempt's trusted rules."""
    if stage not in _RULE_FIELDS:  # never reached through decide(); an unknown stage is refused
        raise SupervisorError("invalid_state")
    if stage == "intake_extraction":
        ok = result.status == "error" or result.source_sha256 == state["identity"].source_sha256
        return None if ok else "source_mismatch"
    fields = _RULE_FIELDS[stage]
    if fields is None:
        return None  # supply: identity checked by the handoff check, evidence by A3 and A4
    if stage == "pricing" and result.status != "priced":
        return None  # A4 reports a drifted tool rule as requires_review; A5 replays A4
    index, version_field, fingerprint_field = fields
    claimed = (getattr(result, version_field), getattr(result, fingerprint_field))
    if claimed == (None, None) and not advancing:
        return None  # nothing was evaluated, so no rule was applied
    return None if claimed == state["rules"][index] else "rule_mismatch"


def _outcome(spec: StageSpec, result: object, state: Mapping, prior: Mapping) -> StageOutcome:
    def outcome(status: str, decision: Decision, reason: str | None, **codes) -> StageOutcome:
        return StageOutcome(stage=spec.stage, agent=spec.agent, attempt_id=state["attempt_id"], status=status, decision=decision, reason=reason, **codes)  # fmt: skip

    if type(result) is not spec.produces:
        return outcome("invalid", "error", "unexpected_result_type")
    stage = spec.stage
    status = result.readiness if stage == "intake_validation" else result.status
    nxt = wf._NEXT[stage].get(status)
    if nxt is None:
        return outcome(status, "error", "unexpected_stage_status")
    decision = "continue" if nxt == wf._GO else nxt
    identity = (state["identity"].source_id, state["identity"].rfq_id)
    advancing = decision in ("continue", "draft_ready_for_review")
    problem = wf._handoff_error(stage, result, identity, prior.get("intake_validation"))
    problem = problem or _provenance_error(stage, result, state, advancing)
    if problem:
        return outcome(status, "error", problem)
    if decision == "draft_ready_for_review" and result.customer_view() is None:
        return outcome(status, "error", "draft_unavailable")
    clarifications = wf._clarifications(stage, result) if decision == "clarification_required" else ()  # fmt: skip
    reviews = wf._reviews(stage, result) if decision == "requires_review" or stage == "quotation" else ()  # fmt: skip
    return outcome(status, decision, wf._reason(stage, result), clarifications=clarifications, review_reasons=reviews)  # fmt: skip


def record_outcome(
    state: Mapping,
    context: SupervisorContext,
    result: object,
    prior: Mapping[str, BaseModel],
    *,
    failure: Literal["timeout", "internal_error"] | None = None,
) -> dict:
    """The update recording the stage the current route delegated (the only writer of
    outcomes; the delegation node calls it). ``result`` is that stage's own result and
    ``prior`` the results already recorded for this attempt, by stage. A result failing a
    type, status, identity, specification, source or rule check is recorded as an "error"
    outcome (routing fails) and nothing downstream may use it. ``failure`` records a stage
    that returned nothing (no budget left, or the agent raised), as specialist workflow does."""
    if failure is not None and result is not None:
        raise SupervisorError("invalid_state")
    checked = validate_routing_state(state, context)
    route = checked.get("route")
    outcomes = checked.get("outcomes", ())
    if route is None or route.action != "delegate" or route != decide(outcomes):
        raise SupervisorError("invalid_route")  # not delegated, or already recorded
    expected = {o.stage for o in outcomes}
    if set(prior) != expected or any(type(r) is not _SPEC[s].produces for s, r in prior.items()):
        raise SupervisorError("invalid_state")
    spec = _SPEC[route.next_stage]
    if failure is None:
        outcome = _outcome(spec, result, checked, prior)
    else:  # specialist workflow's trace: "not_run" when no budget was left, "error" when the agent raised
        status = "not_run" if failure == "timeout" else "error"
        outcome = StageOutcome(stage=spec.stage, agent=spec.agent, attempt_id=checked["attempt_id"], status=status, decision="error", reason=failure)  # fmt: skip
    recorded = (*outcomes, outcome)
    update = {"outcomes": recorded, "stages": _stage_states(recorded)}
    return owned_update("record_outcome", state, update)


# --- graphs -------------------------------------------------------------------------------


def _foundation(recursion_limit: int, schema: type, attempt: Callable | None = None) -> StateGraph:
    """START -> trusted_entry -> initialize_supervisor, shared by both graphs. Nodes take the
    graph's own schema, so a pre-seeded key of either schema is seen and refused. ``attempt``
    (recovery, execution graph only) returns a resumed attempt's own id, or None for a new one."""

    async def trusted_entry(state, runtime: Runtime[SupervisorContext]) -> dict:
        if state:  # a recognised key was pre-seeded: only the trusted context may seed
            raise SupervisorError("invalid_initial_state")
        trusted = _admit(runtime.context, recursion_limit)
        attempt_id = (attempt and attempt()) or secrets.token_hex(16)
        return owned_update("trusted_entry", state, {**trusted, "attempt_id": attempt_id})

    async def initialize_supervisor(state, runtime: Runtime[SupervisorContext]) -> dict:
        update = {
            "phase": "initialized",
            "stages": _pending(),
            "clarifications": (),
            "review_reasons": (),
        }
        owned_update("initialize_supervisor", state, update)
        validate_state({**state, **update}, runtime.context, recursion_limit)
        return update

    graph = StateGraph(schema, context_schema=SupervisorContext)
    graph.add_node("trusted_entry", trusted_entry)
    graph.add_node("initialize_supervisor", initialize_supervisor)
    graph.add_edge(START, "trusted_entry")
    graph.add_edge("trusted_entry", "initialize_supervisor")
    return graph


def build_supervisor_graph() -> CompiledStateGraph:
    """Compile the foundation graph. No checkpointer, no conditional edges, no specialist."""
    graph = _foundation(RECURSION_LIMIT, SupervisorState)
    graph.add_edge("initialize_supervisor", END)
    return graph.compile()


def _route_action(state) -> str:
    return state["route"].action


def attach_routing(graph: StateGraph, *, after: str, delegate: str | None = None) -> None:
    """Add ``route_supervisor`` after ``after``, its conditional edges and the terminal nodes.
    ``delegate`` names an existing node that records the delegated stage with
    ``record_outcome`` (delegation); it gets the only back-edge, to ``route_supervisor``. Without
    it a "delegate" route ends at ``await_delegation``."""

    async def route_supervisor(state, runtime: Runtime[SupervisorContext]) -> dict:
        checked = validate_routing_state(state, runtime.context)
        outcomes = checked.get("outcomes", ())
        previous = checked.get("route")
        # One route per recorded outcome: the first, or right after a delegated stage recorded.
        if previous is not None and (not outcomes or previous != decide(outcomes[:-1])):
            raise SupervisorError("no_progress")
        decision = decide(outcomes)
        update = {
            "route": decision,
            "clarifications": decision.clarifications,
            "review_reasons": decision.review_reasons,
        }
        return owned_update("route_supervisor", state, update)

    def terminal(action: RouteAction):
        async def node(state) -> dict:
            route = state.get("route")
            if route is None or route.action != action:  # reached only through its own edge
                raise SupervisorError("invalid_route")
            return {}

        return node

    graph.add_node("route_supervisor", route_supervisor)
    graph.add_edge(after, "route_supervisor")
    destinations = dict(DESTINATIONS)
    if delegate is not None:
        destinations["delegate"] = delegate
        graph.add_edge(delegate, "route_supervisor")
    for action, name in destinations.items():
        if name != delegate:
            graph.add_node(name, terminal(action))
            graph.add_edge(name, END)
    graph.add_conditional_edges("route_supervisor", _route_action, destinations)


def build_routing_graph() -> CompiledStateGraph:
    """Compile the routing routing graph. No delegation node exists yet, so every run routes to
    the first eligible stage and ends at ``await_delegation``. No checkpointer."""
    graph = _foundation(ROUTING_RECURSION_LIMIT, RoutingState)
    attach_routing(graph, after="initialize_supervisor")
    return graph.compile()


# --- results and runners ------------------------------------------------------------------


class SupervisorFoundationResult(BaseModel):
    """Supervisor foundation output: initialisation only. No assessment, amount, evidence or draft."""

    model_config = _STRICT
    scope: Literal["supervisor_foundation"] = "supervisor_foundation"
    status: Literal["initialized", "error"]
    error: ErrorCode | None = None
    identity: WorkflowIdentity | None = None
    attempt_id: str | None = None
    rules: tuple[tuple[str, str], ...] = ()
    stages: tuple[StageState, ...] = ()
    evaluated: Literal[False] = False  # no specialist ran: no feasibility, supply or price
    draft_available: Literal[False] = False
    human_approval_required: Literal[True] = True
    approved: Literal[False] = False
    released: Literal[False] = False

    @model_validator(mode="after")
    def _consistent(self):
        """Success carries the full trusted state and no error; failure carries neither."""
        if self.status != "error":
            complete = self.identity is not None and self.attempt_id is not None
            ok = self.error is None and complete and bool(self.rules) and bool(self.stages)
        else:
            empty = (self.identity, self.attempt_id, self.rules, self.stages) == (None, None, (), ())  # fmt: skip
            ok = self.error is not None and empty
        if not ok:
            raise ValueError("inconsistent foundation result")
        return self

    def summary(self) -> dict:
        """JSON-safe; every field is already free of amounts, evidence, text and credentials."""
        return self.model_dump(mode="json")


class SupervisorRoutingResult(SupervisorFoundationResult):
    """routing output: one routing decision. Not an evaluation, approval or release."""

    scope: Literal["supervisor_routing"] = "supervisor_routing"
    status: Literal["routed", "error"]
    route: RouteDecision | None = None

    @model_validator(mode="after")
    def _routed(self):
        if (self.status == "routed") != (self.route is not None):
            raise ValueError("inconsistent routing result")
        return self


def _trusted_fields(state: Mapping) -> dict:
    return {key: state[key] for key in ("identity", "attempt_id", "rules", "stages")}


async def _settle(graph: CompiledStateGraph, context, recursion_limit: int, build):
    """Run ``graph`` once from ``context`` within its budget and ``build`` a result from the
    final state; a handled failure returns its fixed code instead."""
    if graph.checkpointer is not None:
        raise ValueError("the Supervisor runs only without a checkpointer")
    try:
        # Checked here for the deadline (a dict is never coerced into a context on this path),
        # and again by trusted_entry, which owns the state.
        budget = _admit(context, recursion_limit)["budget"]
        async with asyncio.timeout(budget.timeout_seconds):
            state = await graph.ainvoke(
                {}, {"recursion_limit": budget.recursion_limit}, context=context
            )
        return build(state)
    except SupervisorError as error:
        return error.code
    except (GraphRecursionError, TimeoutError):
        return "execution_limit"
    except Exception:  # its text never surfaces; CancelledError is not caught
        return "internal_error"


async def run_supervisor_foundation(
    context: SupervisorContext, *, graph: CompiledStateGraph | None = None
) -> SupervisorFoundationResult:
    """Initialise one Supervisor attempt from a trusted context, within its budget."""

    def build(state) -> SupervisorFoundationResult:
        # The graph may be caller-supplied: success only for a final state bound to this context.
        checked = validate_state(state, context)
        return SupervisorFoundationResult(status="initialized", **_trusted_fields(checked))

    graph = graph if graph is not None else build_supervisor_graph()
    result = await _settle(graph, context, RECURSION_LIMIT, build)
    if isinstance(result, SupervisorFoundationResult):
        return result
    return SupervisorFoundationResult(status="error", error=result)


async def run_supervisor_routing(context: SupervisorContext) -> SupervisorRoutingResult:
    """Route one attempt from a trusted context. Takes no state and no graph: until delegation adds
    delegation, the result is the first eligible stage (a control decision, not an evaluation)."""
    return await _route(build_routing_graph(), context)


async def _route(graph: CompiledStateGraph, context) -> SupervisorRoutingResult:
    """Internal routing runner. Not public: a supplied graph could carry outcomes that no
    delegation node produced, which state validation cannot tell apart."""

    def build(state) -> SupervisorRoutingResult:
        checked = validate_routing_state(state, context)
        route = checked.get("route")
        if route is None or route != decide(checked.get("outcomes", ())):
            raise SupervisorError("invalid_state")  # routing never settled
        return SupervisorRoutingResult(status="routed", route=route, **_trusted_fields(checked))

    result = await _settle(graph, context, ROUTING_RECURSION_LIMIT, build)
    if isinstance(result, SupervisorRoutingResult):
        return result
    return SupervisorRoutingResult(status="error", error=result)


# --- delegation (delegation) ------------------------------------------------------------------

# Each stage's own default budget, capped by what is left of the attempt's deadline (specialist workflow).
_STAGE_TIMEOUTS: dict[str, float] = {
    "intake_extraction": a1.DEFAULT_TIMEOUT,
    "intake_validation": a1v.DEFAULT_TIMEOUT,
    "manufacturing": a2.DEFAULT_TIMEOUT,
    "supply": a3.DEFAULT_TIMEOUT,
    "pricing": a4.DEFAULT_TIMEOUT,
    "quotation": a5.DEFAULT_TIMEOUT,
}


class _Custody:
    """Attempt-scoped custody of the typed results this run's specialists returned.

    Created by the runner for one attempt and captured only by that run's delegation node:
    never graph state, never a checkpoint, never caller input, never shared between runs.
    Holding the returned object is the ownership record: downstream stages receive exactly
    these objects (A1 validation gets the extraction A1 returned in this attempt, A3 the same
    one). Accepted results are never replaced; the runner clears custody when the run ends."""

    def __init__(self) -> None:
        self.attempt_id: str | None = None
        self.identity: WorkflowIdentity | None = None
        self.deadline: float | None = None
        self._results: dict[str, BaseModel] = {}
        # recovery (recovery.SupervisorRecovery only): a stage boundary to pause before, what
        # the pause recorded, and the held results a resumed run re-records without agents.
        self.pause_before: str | None = None
        self.paused_at: str | None = None
        self.expected: tuple[StageOutcome, ...] = ()
        self.budget_left: float | None = None
        self.carried: dict[str, BaseModel] = {}
        # telemetry: this run's telemetry sink and random run id (set by _execute; write-only).
        self.telemetry: object | None = None
        self.run: str | None = None
        self.run_kind: Literal["start", "resume", "restart"] = "start"
        self.previous_attempt: str | None = None  # restart: the superseded attempt id

    def bind(self, state: Mapping) -> None:
        """Bind to the attempt on first use; refuse any other attempt's state afterwards. A
        run's deadline starts at its first bind; a resumed run gets only the unused budget."""
        if self.attempt_id is None:
            self.attempt_id, self.identity = state["attempt_id"], state["identity"]
        elif (state["attempt_id"], state["identity"]) != (self.attempt_id, self.identity):
            raise SupervisorError("invalid_state")
        if self.deadline is None:
            budget = state["budget"].timeout_seconds
            if self.budget_left is not None:
                budget = min(budget, self.budget_left)
            self.deadline = asyncio.get_running_loop().time() + budget

    def suspend(self, outcomes: tuple[StageOutcome, ...], stage: str) -> None:
        """Record a pause before ``stage``: held results stay; nothing else is kept."""
        self.paused_at, self.expected = stage, outcomes
        self.budget_left = max(0.0, _remaining(self.deadline))

    def prepare_resume(self, pause_before: str | None = None) -> None:
        """Move the held results to ``carried`` for re-recording; a fresh run deadline."""
        if self.paused_at is None:
            raise SupervisorError("invalid_state")
        self.carried, self._results = dict(self._results), {}
        self.deadline, self.paused_at, self.pause_before = None, None, pause_before

    def held(self, outcomes: tuple[StageOutcome, ...]) -> dict[str, BaseModel]:
        """A copy of the results of exactly the recorded stages, all kept by this run."""
        if set(self._results) != {o.stage for o in outcomes}:
            raise SupervisorError("invalid_state")
        return dict(self._results)

    def get(self, stage: str) -> BaseModel | None:
        return self._results.get(stage)

    def keep(self, stage: str, result: BaseModel) -> None:
        if stage in self._results:  # an accepted result is never replaced
            raise SupervisorError("invalid_state")
        self._results[stage] = result

    def clear(self) -> None:
        self._results.clear()
        self.carried.clear()
        self.expected = ()


def _remaining(deadline: float) -> float:
    return deadline - asyncio.get_running_loop().time()


def _require_dependencies(context: SupervisorContext) -> None:
    horizon = context.horizon_start
    if not isinstance(context.intake_graph, CompiledStateGraph):
        raise SupervisorError("missing_dependency")
    if not isinstance(horizon, date) or isinstance(horizon, datetime):
        raise SupervisorError("missing_dependency")
    limit = context.part_concurrency
    if type(limit) is not int or not 1 <= limit <= a2.MAX_CONCURRENCY:
        raise SupervisorError("invalid_budget")  # before any agent runs


def _arguments(stage: str, ctx: SupervisorContext, held: Mapping, execute=None) -> tuple[tuple, dict]:  # fmt: skip
    """The specialist workflow call for ``stage``: trusted context values and this attempt's own results.
    ``execute`` is ``ctx.execute`` or its transparent timing wrapper (``_observed``)."""
    execute = execute or ctx.execute
    if stage == "intake_extraction":
        kwargs = {"source_id": ctx.source_id, "rfq_id": ctx.rfq_id}
        if ctx.structured_source is not None:
            kwargs["structured_source"] = ctx.structured_source
        return (ctx.intake_graph, ctx.source_text), kwargs
    if stage == "intake_validation":  # the extraction A1 returned in this attempt, same text
        kwargs = {"rules": ctx.manufacturing_rules, "catalog": execute}
        if ctx.trusted_part_ids is not None:
            kwargs["trusted_part_ids"] = ctx.trusted_part_ids
        if ctx.structured_source is not None:
            kwargs["structured_source"] = ctx.structured_source
        return (held["intake_extraction"], ctx.source_text), kwargs
    if (
        stage == "manufacturing"
    ):  # independent parts, bounded by the trusted policy (parallel execution)
        return (held["intake_validation"], execute), {"max_concurrency": ctx.part_concurrency}
    if stage == "supply":  # the same extraction object, for the verified delivery date
        return (held["manufacturing"], execute), {"horizon_start": ctx.horizon_start, "extraction": held["intake_extraction"]}  # fmt: skip
    if stage == "pricing":
        return (held["manufacturing"], held["supply"], execute), {"pricing_rules": ctx.pricing_rules}  # fmt: skip
    if stage == "quotation":
        return (held["manufacturing"], held["supply"], held["pricing"], execute), {"pricing_rules": ctx.pricing_rules, "approval_rules": ctx.approval_rules}  # fmt: skip
    raise SupervisorError("invalid_route")


def _entry(spec: StageSpec) -> Callable:
    # The plan's agent function, looked up on its module at call time as specialist workflow does.
    return getattr(sys.modules[spec.entry.__module__], spec.entry.__name__)


_MAX_MS = 86_400_000  # observability.Event's duration bound


def _elapsed_ms(started: float) -> int:
    """Milliseconds since ``started`` (a ``time.monotonic()`` reading), clamped to the event
    bound. Uses no event loop, so the terminal event can be built during coroutine teardown."""
    return min(_MAX_MS, max(0, int((time.monotonic() - started) * 1000)))


def _literals(annotation) -> frozenset[str]:
    """Every string a (possibly nested / optional / annotated) ``Literal`` type admits."""
    if get_origin(annotation) is Literal:
        return frozenset(a for a in get_args(annotation) if isinstance(a, str))
    return frozenset().union(*(_literals(a) for a in get_args(annotation)))


def _fields(name: str) -> frozenset[str]:
    """The literal values of ``name`` on every planned stage's result type."""
    models = [s.produces.model_fields for s in STAGE_PLAN]
    return frozenset().union(*(_literals(m[name].annotation) for m in models if name in m))


_AGENT_STATUS = _fields("status") | _fields("readiness")
_STATUS_OF_AGENT = {"manufacturing": a2.Status, "supply": a3.Status, "pricing": a4.Status}
# Approved telemetry vocabularies (pre-recovery hardening, Task 2). A value outside them is exported as
# "unlisted": a code-shaped string is not trusted for its shape. Reasons come from the agents'
# typed error codes and readiness values, the "<stage>_<status>" gate codes, and the fixed
# codes the Supervisor, the specialist workflow handoff checks and the A2-A5 gates return (enumerated
# from source). ``not_evaluated_reason`` is a free ``str`` on A2-A5, hence this boundary.
_FIXED_REASONS = """
unexpected_result_type unexpected_stage_status source_mismatch rule_mismatch draft_unavailable
timeout internal_error identity_mismatch specification_mismatch specification_missing no_parts
manufacturing_evidence_missing manufacturing_rule_mismatch manufacturing_evidence_invalid
duplicate_part_ids planning_horizon_missing invalid_planning_horizon
requested_delivery_date_unverified supply_assessment_mismatch supply_evidence_missing
supply_parts_mismatch supply_evidence_invalid pricing_rule_mismatch upstream_identity_mismatch
manufacturing_needs_clarification supply_needs_clarification manufacturing_requires_review
supply_requires_review pricing_assessment_invalid pricing_requires_review
""".split()
_GATE_REASONS = {f"{stage}_{s}" for stage, st in _STATUS_OF_AGENT.items() for s in get_args(st)}
_REASONS = _fields("error") | _AGENT_STATUS | _GATE_REASONS | frozenset(_FIXED_REASONS)
_OUTCOMES = frozenset(get_args(esc.Category)) | frozenset(get_args(Decision))
_TELEMETRY_CODES: dict[str, frozenset[str]] = {
    "status": _AGENT_STATUS | {"invalid", "not_run", "error", "cancelled"},
    "decision": frozenset(get_args(Decision)),
    "reason": _REASONS,
    "error": frozenset(get_args(ErrorCode)),
    "action": frozenset(get_args(RouteAction)),
    "outcome": _OUTCOMES | {"paused", "cancelled", "interrupted"},
}
_TOOL_STATUS = frozenset(get_args(FailureCode)) | {"ok", "error", "cancelled"}


def telemetry_code(field_name: str, value, event: str = "") -> object:
    """``value`` if it is in the approved vocabulary for ``field_name`` (or the field has
    none), else ``"unlisted"``. Applied to every Supervisor event before it is emitted."""
    allowed = _TOOL_STATUS if (event, field_name) == ("tool_call", "status") else _TELEMETRY_CODES.get(field_name)  # fmt: skip
    if value is None or allowed is None or value in allowed:
        return value
    return "unlisted"


def _emit(custody: _Custody, name: str, **fields) -> None:
    """telemetry telemetry for this run (no-op without a sink); never raises, never read back.
    Code fields are normalized through the approved vocabularies first."""
    if custody.telemetry is not None:
        fields = {k: telemetry_code(k, v, name) for k, v in fields.items()}
        obs.emit(custody.telemetry, name, run=custody.run, attempt=custody.attempt_id, **fields)


def _emit_route(custody: _Custody, route: "RouteDecision") -> None:
    _emit(custody, "route_decided", action=route.action, next_stage=route.next_stage, stage=route.stopped_at, outcome=route.outcome, reason=route.reason)  # fmt: skip


def _emit_stage(custody: _Custody, spec: StageSpec, update: dict, started: float | None, **extra) -> None:  # fmt: skip
    """``stage_finished`` from the outcome record_outcome just derived (codes only)."""
    if custody.telemetry is None or not update.get("outcomes"):
        return
    o = update["outcomes"][-1]
    ms = _elapsed_ms(started) if started is not None else None
    _emit(custody, "stage_finished", stage=spec.stage, agent=spec.agent, status=o.status, decision=o.decision, reason=o.reason, duration_ms=ms, **extra)  # fmt: skip


def _emit_fanout(custody: _Custody, ctx: SupervisorContext, held: Mapping, result) -> None:
    """A2's concurrency summary: counts and the join outcome, never part specifications."""
    if custody.telemetry is None or type(result) is not a2.ManufacturingAssessment:
        return
    n = len(held["intake_validation"].parts)
    if result.status == "error" and result.error == "invalid_evidence":
        fan_in = "refused"  # _join refused the collected results
    elif not result.parts:
        fan_in = "not_run"
    else:
        fan_in = "joined" if ctx.part_concurrency > 1 and n > 1 else "sequential"
    failed = sum(p.status == "error" for p in result.parts)
    _emit(custody, "a2_fanout", part_count=n, concurrency=ctx.part_concurrency, completed=len(result.parts) - failed, failed=failed, fan_in=fan_in)  # fmt: skip


def _observed(execute: ToolExecutor, custody: _Custody, stage: str) -> ToolExecutor:
    """A transparent wrapper reporting each tool call's allow-listed name, fixed status and
    duration. Same arguments, same result, same exception: it never retries or alters."""

    async def call(tool, arguments):
        started, status = time.monotonic(), "error"
        try:
            result = await execute(tool, arguments)
            status = "ok"  # the executor returned; the agent still validates the result
            return result
        except McpCallError as error:
            status = error.code
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            name = tool if isinstance(tool, str) and tool in obs.TOOL_NAMES else "unlisted"
            _emit(custody, "tool_call", stage=stage, tool=name, status=status, duration_ms=_elapsed_ms(started))  # fmt: skip

    return call


def _delegation_node(custody: _Custody):
    async def delegate_stage(state, runtime: Runtime[SupervisorContext]) -> dict:
        """Run the one stage the route delegated, once, and record its outcome."""
        ctx = runtime.context
        checked = validate_routing_state(state, ctx)
        _require_dependencies(ctx)  # before the first model call
        custody.bind(checked)
        route = checked.get("route")
        if route is None or route.action != "delegate":
            raise SupervisorError("invalid_route")
        _emit_route(custody, route)
        spec = _SPEC[route.next_stage]
        held = custody.held(checked.get("outcomes", ()))
        if custody.carried:  # recovery continuation: the original result, re-recorded, no agent
            update = _re_record(custody, spec.stage, state, ctx, held)
            _emit_stage(custody, spec, update, None, carried=True)
            return update
        if custody.pause_before == spec.stage:  # a requested boundary: before any work
            custody.suspend(checked.get("outcomes", ()), spec.stage)
            raise SupervisorError("paused")
        remaining = _remaining(custody.deadline)
        if remaining <= 0:  # no stage starts after the deadline
            update = record_outcome(state, ctx, None, held, failure="timeout")
            _emit_stage(custody, spec, update, None)
            return update
        execute = _observed(ctx.execute, custody, spec.stage) if custody.telemetry is not None else None  # fmt: skip
        args, kwargs = _arguments(spec.stage, ctx, held, execute)
        timeout = min(_STAGE_TIMEOUTS[spec.stage], remaining)
        started = time.monotonic()
        _emit(custody, "stage_started", stage=spec.stage, agent=spec.agent)
        try:
            result = await _entry(spec)(*args, **kwargs, timeout_seconds=timeout)
        except asyncio.CancelledError:
            _emit(custody, "stage_finished", stage=spec.stage, agent=spec.agent, status="cancelled", duration_ms=_elapsed_ms(started))  # fmt: skip
            raise
        except Exception:  # an agent fault: its text never surfaces, it is not retried
            update = record_outcome(state, ctx, None, held, failure="internal_error")
            _emit_stage(custody, spec, update, started)
            return update
        if spec.stage == "manufacturing":
            _emit_fanout(custody, ctx, held, result)
        update = record_outcome(state, ctx, result, held)
        _emit_stage(custody, spec, update, started)
        recorded = update.get("outcomes", ())  # nothing recorded: route_supervisor stops it
        if recorded and recorded[-1].decision != "error":  # a refused result is never kept
            custody.keep(spec.stage, result)
        return update

    return delegate_stage


def _re_record(custody: _Custody, stage: str, state, ctx, held) -> dict:
    """Re-record a held result exactly as before the pause, under the resumed context
    (identity, rules, handoffs and provenance re-checked by record_outcome), else refuse."""
    result = custody.carried.pop(stage, None)
    if result is None:  # decide() asks for a stage the pause did not hold
        raise SupervisorError("stale_recovery")
    update = record_outcome(state, ctx, result, held)
    recorded = update.get("outcomes", ())
    index = len(recorded) - 1
    if not recorded or index >= len(custody.expected) or recorded[-1] != custody.expected[index]:
        raise SupervisorError("stale_recovery")
    custody.keep(stage, result)
    return update


def _execution_graph(custody: _Custody) -> CompiledStateGraph:
    graph = _foundation(ROUTING_RECURSION_LIMIT, RoutingState, attempt=lambda: custody.attempt_id)
    graph.add_node("delegate_stage", _delegation_node(custody))
    attach_routing(graph, after="initialize_supervisor", delegate="delegate_stage")
    return graph.compile()


def build_execution_graph() -> CompiledStateGraph:
    """The delegation graph, for inspection: the routing graph with ``delegate_stage`` in place of
    ``await_delegation``. Runs go through ``run_supervisor_workflow``, which owns custody."""
    return _execution_graph(_Custody())


class SupervisorExecutionResult(BaseModel):
    """delegation output: one delegated A1 -> A5 run. Codes, stage statuses and the route; the
    typed results (A4 costs, raw evidence) stay in custody and are discarded. ``draft`` is
    A5's customer-safe projection, kept out of ``summary()``. Never approved or released."""

    model_config = _STRICT
    scope: Literal["supervisor_execution"] = "supervisor_execution"
    status: Literal["executed", "error"]  # technical: the run finished, or failed closed
    error: ErrorCode | None = None
    identity: WorkflowIdentity | None = None
    attempt_id: str | None = None
    rules: tuple[tuple[str, str], ...] = ()
    stages: tuple[StageState, ...] = ()
    outcomes: tuple[StageOutcome, ...] = ()  # what actually ran, with each agent's own status
    route: RouteDecision | None = None  # the business outcome: complete, review, ...
    draft_available: bool = False
    draft: a5.CustomerQuotationDraft | None = None
    # escalation: what the settled route requires next (clarify, blocked, review, draft, failure).
    disposition: esc.SupervisorDisposition | None = None
    human_approval_required: Literal[True] = True
    approved: Literal[False] = False
    released: Literal[False] = False

    @model_validator(mode="after")
    def _consistent(self):
        if self.status == "executed":
            trusted = None not in (self.identity, self.attempt_id, self.route)
            settled = trusted and self.route.action != "delegate"
            ok = self.error is None and settled and bool(self.rules) and bool(self.outcomes)
            try:  # the route is the one routing's decide derives from the outcomes (escalation)
                ok = ok and self.route == decide(self.outcomes)
            except SupervisorError:
                ok = False
        else:
            empty = (self.identity, self.attempt_id, self.route, self.draft) == (None,) * 4
            ok = self.error is not None and empty and not (self.rules or self.stages or self.outcomes)  # fmt: skip
        if not ok or self.draft_available != (self.draft is not None):
            raise ValueError("inconsistent execution result")
        # escalation: every disposition field must follow from this route. blocking_findings and
        # unevaluated_conditions are re-used here, so they are bounded only by the model's own
        # invariants (A2 vocabulary, A5's fixed shapes); their exact provenance is established
        # by run_supervisor_workflow, which builds them from custody, not by this check.
        d = self.disposition
        if d is not None and d != esc.for_result(self.route, self.error, self.draft_available, d.blocking_findings, d.unevaluated_conditions):  # fmt: skip
            raise ValueError("inconsistent execution result")
        return self

    def summary(self) -> dict:
        """Stage trace, codes, route and rule provenance: no amount, evidence or text."""
        return self.model_dump(mode="json", exclude={"draft"})

    def customer_view(self) -> a5.CustomerQuotationDraft | None:
        """A5's customer-safe draft (selling prices only), or None. Still a draft."""
        return self.draft

    def customer_outcome(self) -> esc.CustomerOutcome:
        """The customer-safe disposition (escalation): fixed text, questions, blockers and review
        notices; no codes, amounts, evidence or customer text. Never approved or released."""
        if self.disposition is None:  # not built by the trusted runner: conclude nothing
            return esc.for_result(None, self.error or "disposition_unavailable", False).customer_view()  # fmt: skip
        return self.disposition.customer_view()


async def run_supervisor_workflow(context: SupervisorContext) -> SupervisorExecutionResult:
    """Run A1 -> A5 through the routing graph for one trusted context. Takes no state, no
    stage results and no graph; custody is created here, used by this run only and cleared
    when it ends (success, failure, timeout or cancellation)."""
    custody = _Custody()
    try:
        return await _execute(_execution_graph(custody), context, custody)
    finally:
        custody.clear()


async def _execute(graph: CompiledStateGraph, context, custody: _Custody):
    """One run, with telemetry workflow telemetry around the unchanged delegation settle/build.
    ``custody.run_kind`` / ``previous_attempt`` are set by recovery (resume, restart)."""
    custody.run = secrets.token_hex(16)  # random per run; never derived from customer data
    custody.telemetry = context.telemetry if isinstance(context, SupervisorContext) else None
    limit = getattr(context, "part_concurrency", None)
    limit = limit if type(limit) is int and 1 <= limit <= a2.MAX_CONCURRENCY else None
    _emit(custody, "workflow_started", run_kind=custody.run_kind, previous_attempt=custody.previous_attempt, concurrency=limit)  # fmt: skip
    started = time.monotonic()
    try:
        result = await _run_execute(graph, context, custody)
    except BaseException as error:  # cancelled, or torn down abruptly: still close the trace
        outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "interrupted"
        _emit(custody, "workflow_finished", outcome=outcome, duration_ms=_elapsed_ms(started))
        raise
    route, d = result.route, result.disposition
    outcome = "paused" if result.error == "paused" else (d.category if d else "technical_failure")  # fmt: skip
    stage = custody.paused_at if result.error == "paused" else (d.stage if d else None)
    if route is not None and route.action != "delegate":
        _emit_route(custody, route)
    _emit(custody, "workflow_finished", outcome=outcome, error=result.error, action=route.action if route else None, stage=stage, reason=route.reason if route else None, duration_ms=_elapsed_ms(started))  # fmt: skip
    return result


async def _run_execute(graph: CompiledStateGraph, context, custody: _Custody):
    def build(state) -> SupervisorExecutionResult:
        checked = validate_routing_state(state, context)
        outcomes = checked.get("outcomes", ())
        route = checked.get("route")
        if route is None or route != decide(outcomes) or route.action == "delegate":
            raise SupervisorError("invalid_state")  # the run never settled
        custody.bind(checked)
        quotation = custody.get("quotation")  # kept only when A5's outcome was accepted
        draft = None
        if quotation is not None and quotation.status in wf._DRAFT:
            draft = quotation.customer_view()
        # escalation: codes from this attempt's own accepted results, for the disposition only.
        findings, unevaluated = (), ()
        if route.outcome == "not_quotable":  # A2's verified infeasible parts
            parts = custody.get("manufacturing").parts
            findings = tuple(dict.fromkeys(c for p in parts if p.status == "infeasible" for c in p.evidence.output.findings))  # fmt: skip
        if draft is not None:
            unevaluated = quotation.unevaluated_review_conditions
        disposition = esc.for_result(route, None, draft is not None, findings, unevaluated)
        return SupervisorExecutionResult(
            status="executed",
            outcomes=outcomes,
            route=route,
            draft_available=draft is not None,
            draft=draft,
            disposition=disposition,
            **_trusted_fields(checked),
        )

    result = await _settle(graph, context, ROUTING_RECURSION_LIMIT, build)
    if isinstance(result, SupervisorExecutionResult):
        return result
    failure = esc.for_result(None, result, False)  # technical: nothing concluded
    return SupervisorExecutionResult(status="error", error=result, disposition=failure)
