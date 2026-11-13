"""Specialized-agent integration (Day 43): one RFQ evaluation through A1 -> A5 with fixed,
observable handoffs. Not a Supervisor, not a planner, not approval or release.

    customer text + trusted source_id / rfq_id + intake graph + trusted ToolExecutor
        + trusted horizon_start + pinned mfg / price / approval rules
        -> evaluate_rfq_workflow -> RFQWorkflowAssessment

- Order is fixed: A1 extraction (``run_intake``) -> A1 validation (``validate_intake``) -> A2
  (``evaluate_manufacturing_feasibility``) -> A3 (``evaluate_supply``) -> A4
  (``evaluate_pricing``) -> A5 (``evaluate_quotation_review``). Each agent runs at most once;
  no retries, no loops, no model call besides A1's single extraction.
- Gating: after every stage its own status is looked up in ``_NEXT``; anything but "continue"
  stops the workflow, so no later agent (and none of its tools) runs. An unknown status stops
  with ``error``. A4 ``requires_review`` continues to A5 because A5 replays A4's evidence and
  returns a no-amount ``requires_review`` without calling its tool.
- A1 pairing: the extraction and its validation are produced here, from the same text, in one
  call; the caller cannot supply either. ``validate_intake`` checks the extraction's
  ``source_sha256`` against that text, and the same extraction object is handed to A3 for the
  verified delivery date. Every stage result must carry the requested source and RFQ identity,
  and A2 must carry exactly the specifications A1 validated, else ``error``. This protects the
  in-process pairing only; it does not authenticate assessment objects built elsewhere.
- Trust: one principal-bound executor (``graph.mcp_executor``) serves every stage, so all
  results come from one verified principal; each MCP server makes its own RBAC decision. Rules
  are the caller's ``mcp.runtime.pinned_rules`` results; price and approval rules are checked
  against the manifest with A4/A5's own helpers before the model call, and A4/A5 check them
  again. No rule, identity or permission ever comes from customer text.
- Timeout: one overall deadline; each agent gets ``min(its default, remaining)`` and fails
  closed through its own timeout path. An agent exception stops with ``internal_error``; its
  text never surfaces.
- Output: ``RFQWorkflowAssessment`` keeps every stage result for downstream validation. It
  holds A4's internal costs and raw evidence, so it is internal only: use ``summary()`` (no
  amounts, no evidence) for logs and handoffs and ``customer_view()`` (A5's projection) for a
  customer-facing draft. Nothing is approved, released, persisted or numbered.
"""

import asyncio
from datetime import date
from typing import Literal

from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict

from rfq_copilot import intake as a1
from rfq_copilot import intake_validation as a1v
from rfq_copilot import manufacturing_agent as a2
from rfq_copilot import pricing_agent as a4
from rfq_copilot import quotation_agent as a5
from rfq_copilot import supply_agent as a3
from rfq_copilot.intake import IntakeResult
from rfq_copilot.intake_validation import IntakeValidation, ToolExecutor
from rfq_copilot.manufacturing_agent import ManufacturingAssessment
from rfq_copilot.pricing_agent import PricingAssessment
from rfq_copilot.quotation_agent import CustomerQuotationDraft, QuotationReviewAssessment
from rfq_copilot.repositories import RuleVersionDetails
from rfq_copilot.supply_agent import SupplyAssessment

# ponytail: sum of the per-agent defaults (120 + 30 + 4 x 60); lower it per caller if needed.
DEFAULT_TIMEOUT = 390.0

Stage = Literal["preflight", "intake_extraction", "intake_validation", "manufacturing", "supply", "pricing", "quotation"]  # fmt: skip
WorkflowStatus = Literal[
    "draft_ready_for_review",  # a verified, non-binding draft awaits human approval
    "requires_review",
    "clarification_required",
    "not_quotable",  # A2 found a part not manufacturable
    "not_evaluated",
    "error",
]
_STRICT = ConfigDict(extra="forbid", frozen=True)
_GO = "continue"
# Per stage: the stage's own status -> continue, or the workflow status it stops with.
_NEXT: dict[Stage, dict[str, str]] = {
    "intake_extraction": {
        "extracted": _GO,
        "extracted_with_issues": _GO,
        "no_parts_found": _GO,
        "error": "error",
    },
    "intake_validation": {
        "ready_for_manufacturing_evaluation": _GO,
        "needs_clarification": "clarification_required",
        "requires_review": "requires_review",
        "not_evaluated": "not_evaluated",
    },
    "manufacturing": {
        "feasible": _GO,
        "infeasible": "not_quotable",
        "needs_clarification": "clarification_required",
        "requires_review": "requires_review",
        "not_evaluated": "not_evaluated",
        "error": "error",
    },
    "supply": {
        "available": _GO,
        "constrained": _GO,  # priced, never concealed: A4/A5 carry the constraint
        "needs_clarification": "clarification_required",
        "requires_review": "requires_review",
        "not_evaluated": "not_evaluated",
        "error": "error",
    },
    "pricing": {
        "priced": _GO,
        "requires_review": _GO,
        "not_evaluated": "not_evaluated",
        "error": "error",
    },
    "quotation": {
        "draft_ready_for_review": "draft_ready_for_review",
        "requires_review": "requires_review",
        "clarification_required": "clarification_required",
        "not_evaluated": "not_evaluated",
        "error": "error",
    },
}
_AGENT = {"preflight": None, "intake_extraction": "A1", "intake_validation": "A1", "manufacturing": "A2", "supply": "A3", "pricing": "A4", "quotation": "A5"}  # fmt: skip
_INTERNAL = {"extraction", "intake", "manufacturing", "supply", "pricing", "quotation"}
_DRAFT = frozenset({"draft_ready_for_review", "requires_review"})  # A5 statuses with a draft


class StageRecord(BaseModel):
    """One executed stage: its own status, unchanged, and a fixed reason code."""

    model_config = _STRICT
    stage: Stage
    agent: Literal["A1", "A2", "A3", "A4", "A5"] | None
    status: str
    reason: str | None = None


class RFQWorkflowAssessment(BaseModel):
    """One integrated RFQ evaluation. Internal: holds A4 costs and raw evidence."""

    model_config = _STRICT
    source_id: str
    rfq_id: str | None
    source_sha256: str | None = None  # A1's hash of the evaluated text
    status: WorkflowStatus
    stopped_at: Stage | None = None  # None: A5 assembled a verified (non-binding) draft
    stages: tuple[StageRecord, ...] = ()
    error: str | None = None  # fixed code; never exception text
    clarifications: tuple[str, ...] = ()  # "<part index|0>:<field>:<kind>"
    review_reasons: tuple[str, ...] = ()
    unevaluated_conditions: tuple[str, ...] = ()  # A5's: never reported as passed
    rules: tuple[tuple[str, str], ...] = ()  # (version, fingerprint) of the pinned rules used
    draft_available: bool = False  # a verified non-binding draft exists for human review
    human_approval_required: Literal[True] = True
    approved: Literal[False] = False
    released: Literal[False] = False
    extraction: IntakeResult | None = None
    intake: IntakeValidation | None = None
    manufacturing: ManufacturingAssessment | None = None
    supply: SupplyAssessment | None = None
    pricing: PricingAssessment | None = None
    quotation: QuotationReviewAssessment | None = None

    def summary(self) -> dict:
        """Stage trace, statuses, codes and rule provenance: no amount, no evidence."""
        return self.model_dump(mode="json", exclude=_INTERNAL)

    def customer_view(self) -> CustomerQuotationDraft | None:
        """A5's customer-safe draft (no cost, no evidence), or None when there is no draft."""
        return self.quotation.customer_view() if self.draft_available else None


def _clarifications(stage: Stage, result) -> tuple[str, ...]:
    if stage == "intake_validation":
        return tuple(c.id for c in result.clarifications)
    if stage == "manufacturing":
        return tuple(f"{p.index}:{f}:missing" for p in result.parts for f in p.clarifications)
    if stage == "supply":
        return (f"0:requested_delivery_date:{result.not_evaluated_reason}",)
    return tuple(f"{i}:{f}:missing" for i, f in result.clarifications)  # quotation


def _reviews(stage: Stage, result) -> tuple[str, ...]:
    if stage == "intake_validation":
        every = (*result.findings, *(f for p in result.parts for f in p.findings))
        return tuple(dict.fromkeys(f"intake_{f.kind}" for f in every if f.action == "review"))
    if stage == "quotation":
        return (*result.review_reasons, *result.manager_review_reasons)
    return (f"{stage}_requires_review",)  # A2 / A3: A5's vocabulary


def _reason(stage: Stage, result) -> str | None:
    if stage == "intake_extraction":
        return result.error
    if stage == "intake_validation":
        return None
    return result.not_evaluated_reason or result.error


async def evaluate_rfq_workflow(
    text: str,
    *,
    source_id: str,
    rfq_id: str | None,
    intake_graph: CompiledStateGraph,
    execute: ToolExecutor | None,
    horizon_start: date | None,
    manufacturing_rules: RuleVersionDetails | None,
    pricing_rules: RuleVersionDetails | None,
    approval_rules: RuleVersionDetails | None,
    timeout_seconds: float = DEFAULT_TIMEOUT,
) -> RFQWorkflowAssessment:
    """Run A1 -> A5 once for this RFQ text, stopping at the first stage that cannot proceed.

    ``source_id`` / ``rfq_id`` and ``horizon_start`` are trusted application values;
    ``execute`` the principal-bound ``graph.mcp_executor``; the rules ``pinned_rules`` results.
    ``text`` is untrusted customer content and is used only by A1."""
    identity = (source_id, rfq_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    done: dict = {}
    trace: list[StageRecord] = []

    def finish(status: WorkflowStatus, stage: Stage | None = None, error=None, **extra):
        quotation = done.get("quotation")
        extraction = done.get("extraction")
        return RFQWorkflowAssessment(
            source_id=source_id,
            rfq_id=rfq_id,
            source_sha256=extraction.source_sha256 if extraction else None,
            status=status,
            stopped_at=stage,
            stages=tuple(trace),
            error=error,
            draft_available=bool(stage is None and quotation and quotation.customer_view()),
            **done,
            **extra,
        )

    # Preflight (no model call, no tool call): what every later stage needs from the caller.
    problem = None
    if execute is None:
        problem = "unauthenticated"
    elif not a4._trusted(pricing_rules):
        problem = "pricing_rules_untrusted"
    elif not a5._approval_trusted(approval_rules):
        problem = "approval_rules_untrusted"
    if problem:
        trace.append(StageRecord(stage="preflight", agent=None, status="blocked", reason=problem))
        status = "error" if problem == "unauthenticated" else "not_evaluated"
        return finish(status, "preflight", problem)
    rules = (manufacturing_rules, pricing_rules, approval_rules)
    used = tuple((r.rule_version, r.fingerprint) for r in rules if r is not None)

    def budget(default: float) -> float:
        return min(default, deadline - loop.time())

    async def run(stage: Stage, key: str, call):
        """Run one stage; return None to continue, or the finished workflow result."""
        if deadline - loop.time() <= 0:
            trace.append(StageRecord(stage=stage, agent=_AGENT[stage], status="not_run", reason="timeout"))  # fmt: skip
            return finish("error", stage, "timeout", rules=used)
        try:
            result = await call()
        except Exception:  # an agent fault: its text never surfaces
            trace.append(StageRecord(stage=stage, agent=_AGENT[stage], status="error", reason="internal_error"))  # fmt: skip
            return finish("error", stage, "internal_error", rules=used)
        status = result.readiness if stage == "intake_validation" else result.status
        trace.append(StageRecord(stage=stage, agent=_AGENT[stage], status=status, reason=_reason(stage, result)))  # fmt: skip
        # A result that fails a handoff check is not kept: nothing downstream may read it.
        if (result.source_id, result.rfq_id) != identity:
            return finish("error", stage, "identity_mismatch", rules=used)
        # A2 with parts must carry exactly A1's specifications (none: A2's own blocked result).
        specs = [p.specification for p in result.parts] if stage == "manufacturing" else None
        if specs and specs != [p.specification for p in done["intake"].parts]:
            return finish("error", stage, "specification_mismatch", rules=used)
        done[key] = result
        nxt = _NEXT[stage].get(status, "error")
        if nxt == _GO:
            return None
        extra = {"rules": used}
        if nxt == "clarification_required":
            extra["clarifications"] = _clarifications(stage, result)
        if nxt == "requires_review" or stage == "quotation":  # A5: manager review too
            extra["review_reasons"] = _reviews(stage, result)
        error = _reason(stage, result) if nxt == "error" else None
        if status not in _NEXT[stage]:
            error = "unexpected_stage_status"
        if stage == "quotation":
            extra["unevaluated_conditions"] = result.unevaluated_review_conditions
            if status in _DRAFT and result.customer_view() is not None:
                return finish(nxt, None, error, **extra)  # A5 assembled a verified draft
        return finish(nxt, stage, error, **extra)

    steps = (
        (
            "intake_extraction",
            "extraction",
            lambda: a1.run_intake(
                intake_graph,
                text,
                source_id=source_id,
                rfq_id=rfq_id,
                timeout_seconds=budget(a1.DEFAULT_TIMEOUT),
            ),
        ),
        (
            "intake_validation",
            "intake",
            lambda: a1v.validate_intake(
                done["extraction"],
                text,
                rules=manufacturing_rules,
                catalog=execute,
                timeout_seconds=budget(a1v.DEFAULT_TIMEOUT),
            ),
        ),
        (
            "manufacturing",
            "manufacturing",
            lambda: a2.evaluate_manufacturing_feasibility(
                done["intake"],
                execute,
                timeout_seconds=budget(a2.DEFAULT_TIMEOUT),
            ),
        ),
        (
            "supply",
            "supply",
            lambda: a3.evaluate_supply(
                done["manufacturing"],
                execute,
                horizon_start=horizon_start,
                extraction=done["extraction"],
                timeout_seconds=budget(a3.DEFAULT_TIMEOUT),
            ),
        ),
        (
            "pricing",
            "pricing",
            lambda: a4.evaluate_pricing(
                done["manufacturing"],
                done["supply"],
                execute,
                pricing_rules=pricing_rules,
                timeout_seconds=budget(a4.DEFAULT_TIMEOUT),
            ),
        ),
        (
            "quotation",
            "quotation",
            lambda: a5.evaluate_quotation_review(
                done["manufacturing"],
                done["supply"],
                done["pricing"],
                execute,
                pricing_rules=pricing_rules,
                approval_rules=approval_rules,
                timeout_seconds=budget(a5.DEFAULT_TIMEOUT),
            ),
        ),
    )
    for stage, key, call in steps:
        stopped = await run(stage, key, call)
        if stopped is not None:
            return stopped
    raise AssertionError("unreachable: the quotation stage always finishes")  # pragma: no cover
