"""Clarification and escalation dispositions (Day 48): what a settled Supervisor run means
for a person. Derived only from the Supervisor's validated route; never a route, never an
approval.

    SupervisorExecutionResult (route = decide(outcomes)) + trusted custody codes
        -> for_result -> SupervisorDisposition -> customer_view -> CustomerOutcome

- Authority: Day 45's ``decide`` stays the only router. The disposition maps its terminal
  action one-to-one (``blocked`` splits on the route's own outcome: ``not_quotable`` is A2's
  verified infeasibility, ``not_evaluated`` is "could not be evaluated", never an
  impossibility). ``failed`` and a run that never settled are ``technical_failure``.
- Inputs are codes only: the route's clarification and review codes (Phase 6 vocabulary),
  A2's finding codes for a ``not_quotable`` stop and A5's unevaluated conditions for a draft,
  both read by the Supervisor from this attempt's custody and checked against the agents'
  own vocabularies here.
- Text: every question, blocker and notice is a fixed template keyed by code. Customer text,
  part labels, exception text and amounts never enter; a part is named by its validated index.
  Unknown codes get a conservative generic sentence.
- Customer status: no service verifies account standing or credit (A5 reports both as
  unevaluated), so it is always ``not_verified``, never a pass and never a block. A draft must
  carry exactly one of A5's two unevaluated-condition shapes (``A5_UNEVALUATED``).
- Authenticity: ``for_result`` is called with codes only by the trusted Supervisor runner,
  from this attempt's custody. After serialization the model enforces every invariant it can
  check alone (route consistency, A2/A5 vocabularies and shapes), but it cannot prove *which*
  allowed A2 finding codes, or whether A5's optional ``borderline_bend_radius``, came from the
  original results: that needs the cleared custody or signed evidence (L04/L21/L26).
- Human boundary: ``automatic_continuation``, ``approved`` and ``released`` are
  ``Literal[False]``; ``human_approval_required`` is ``Literal[True]``. Nothing is assigned,
  queued, notified, persisted or resumed (resumption is Day 49).
"""

import re
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rfq_copilot import intake_validation as a1v
from rfq_copilot import manufacturing_agent as a2
from rfq_copilot import quotation_agent as a5
from rfq_copilot.agent_integration import Stage

MAX_QUESTIONS = 25
MAX_CODES = 1024  # as the Supervisor's MAX_INDICATORS

Category = Literal[
    "clarification_required",  # the customer must supply or confirm information
    "not_quotable",  # verified evidence (A2) does not permit manufacturing as specified
    "not_evaluated",  # a stage could not evaluate on current evidence: not an impossibility
    "review_required",  # a human decision is needed before anything proceeds
    "draft_ready_for_approval",  # a verified non-binding draft awaits human approval
    "technical_failure",  # execution failed; nothing is concluded about the request
]
Action = Literal["clarification", "review", "blocked", "failed", "complete"]
ReviewCategory = Literal["engineering", "supply_delivery", "commercial", "intake", "other"]
Blocker = Literal[
    "material", "finish", "thickness", "part_type", "tolerance", "bend_radius", "operation",
    "outside_supported_range", "other",
]  # fmt: skip
_Code = Annotated[str, Field(min_length=1, max_length=128)]
_Codes = Annotated[tuple[_Code, ...], Field(max_length=MAX_CODES)]
_Text = Annotated[str, Field(min_length=1, max_length=300)]
_STRICT = ConfigDict(extra="forbid", frozen=True)

_CATEGORY: dict[str, Category] = {
    "clarification": "clarification_required",
    "review": "review_required",
    "complete": "draft_ready_for_approval",
    "failed": "technical_failure",
}
_BLOCKED = frozenset({"not_quotable", "not_evaluated"})
_REVIEWED = frozenset({"review_required", "draft_ready_for_approval"})

# --- clarification questions ----------------------------------------------------------------

# Field labels: Intake's fixed labels (code-authored), plus A2's PartSpecification names.
_MM = frozenset({"length", "width", "height", "thickness", "bend_radius", "weld_length"})
_EXTRA_LABELS = {"bend_radius": "bend radius", "material_id": "material and grade", "finish_id": "finish"}  # fmt: skip
_ASK = {
    "missing": "please provide the {label}.",
    "ambiguous": "please confirm the {label}; the value given is unclear or offers alternatives.",
    "contradiction": "please confirm the {label}; it is stated in more than one way.",
    "ownership_unverified": "please confirm which part the {label} applies to.",
    "invalid": "please restate the {label}; the value given is not valid.",
}
_FIXED = {
    "unknown_operation": "please describe the required operation in more detail.",
    "requested_delivery_date_unverified": "please confirm one firm requested delivery date.",
}
NO_PARTS = "Please list each part with its quantity, material, dimensions and finish."
GENERIC_QUESTION = "Please review your request and provide any missing or unclear details."
# "<part index | 0>:<field | ->:<kind>" (Intake ids; A2/A3/A5 codes in Phase 6 _clarifications)
_CLARIFICATION = re.compile(r"(\d{1,3}):([a-z_]{1,40}|-):([a-z_]{1,64})")


def _label(field: str) -> str | None:
    base = field.removesuffix("_mm")
    label = _EXTRA_LABELS.get(field) or a1v._LABELS.get(field) or a1v._LABELS.get(base)
    if label is None:
        return None
    return f"{label} in millimetres" if base in _MM else label


def question(code: str) -> str:
    """The fixed question for one validated clarification code; generic when unknown."""
    match = _CLARIFICATION.fullmatch(code)
    if match is None:
        return GENERIC_QUESTION
    index, field, kind = int(match[1]), match[2], match[3]
    if kind == "no_parts":
        return NO_PARTS
    label = _label(field) if field != "-" else None
    ask = _FIXED.get(kind) or (_ASK[kind].format(label=label) if kind in _ASK and label else None)
    if ask is None:
        return GENERIC_QUESTION
    return f"{f'Part {index}' if index else 'Your request'}: {ask}"


def questions(codes: tuple[str, ...]) -> tuple[str, ...]:
    """One question per distinct code, deduplicated, in the route's order, bounded."""
    asked = tuple(dict.fromkeys(question(c) for c in codes)) or (GENERIC_QUESTION,)
    return asked[:MAX_QUESTIONS]


# --- blockers and reviews ---------------------------------------------------------------------

_BLOCKER_OF: dict[str, Blocker] = {
    "UNSUPPORTED_MATERIAL": "material",
    "UNSUPPORTED_FINISH": "finish",
    "MATERIAL_FINISH_INCOMPATIBLE": "finish",
    "MATERIAL_THICKNESS_UNSUPPORTED": "thickness",
    "UNSUPPORTED_PART_TYPE": "part_type",
    "TOLERANCE_UNSUPPORTED": "tolerance",
    "BEND_RADIUS_TOO_SMALL": "bend_radius",
    "UNSUPPORTED_OPERATION": "operation",
}
_ENGINEERING_INTAKE = ("engineering_review", "unsupported_requirement", "unknown_operation", "catalog_no_match", "catalog_ambiguous")  # fmt: skip
_INTAKE_REVIEW = {f"intake_{k}": "engineering" if k in _ENGINEERING_INTAKE else "intake" for k in get_args(a1v.FindingKind)}  # fmt: skip
_REVIEW_OF: dict[str, ReviewCategory] = {
    **_INTAKE_REVIEW,  # Phase 6: "intake_<finding kind>" for review findings
    "manufacturing_requires_review": "engineering",  # A2: rules differ from Intake's
    "BORDERLINE_BEND_RADIUS": "engineering",  # A5 approval policy
    "supply_requires_review": "supply_delivery",  # A3: drift, contradiction, capacity data
    "supply_constrained": "supply_delivery",  # A4: short stock was priced, not hidden
    "DELIVERY_DATE_INFEASIBLE": "supply_delivery",
    "CAPACITY_OUT_OF_HORIZON": "supply_delivery",
    "CAPACITY_DATA_MISSING": "supply_delivery",
    "pricing_requires_review": "commercial",
    "pricing_rule_drift": "commercial",
    "pricing_contradicts_upstream": "commercial",
    "QUOTE_TOTAL_AT_OR_ABOVE_THRESHOLD": "commercial",  # A5 manager review
}
# The only shapes A5 ever builds for a draft (``evaluate_quotation_review``): customer account
# status and credit limit are always unchecked; borderline bend only when bent with no ratio.
A5_UNEVALUATED = (a5.CUSTOMER_CONDITIONS, (*a5.CUSTOMER_CONDITIONS, "borderline_bend_radius"))


def _blocker(code: str) -> Blocker:
    return "outside_supported_range" if code.startswith("OUT_OF_RANGE_") else _BLOCKER_OF.get(code, "other")  # fmt: skip


def _ordered(values, vocabulary) -> tuple:
    present = set(values)
    return tuple(v for v in get_args(vocabulary) if v in present)


def blockers(findings: tuple[str, ...]) -> tuple[Blocker, ...]:
    return _ordered(map(_blocker, findings), Blocker) or ("other",)


def review_categories(reasons: tuple[str, ...]) -> tuple[ReviewCategory, ...]:
    return _ordered((_REVIEW_OF.get(r, "other") for r in reasons), ReviewCategory)


# --- the disposition --------------------------------------------------------------------------


class SupervisorDisposition(BaseModel):
    """What a settled run requires next, for operators (codes) and, via ``customer_view``,
    for the customer (fixed text). Every field follows from the route; see ``for_result``."""

    model_config = _STRICT
    category: Category
    route_action: Action | None  # None: the run failed before a route settled
    stage: Stage | None  # the stage whose outcome produced the route
    reason: _Code | None = None  # internal fixed code (operator diagnosis), never shown
    clarification_required: bool
    blocked: bool
    review_required: bool
    technical_failure: bool
    clarification_codes: _Codes = ()
    questions: tuple[_Text, ...] = ()
    blocking_findings: _Codes = ()  # A2's finding codes for a not_quotable stop (internal)
    blockers: tuple[Blocker, ...] = ()
    review_reasons: _Codes = ()  # internal
    review_categories: tuple[ReviewCategory, ...] = ()  # recommended reviewers, not assigned
    unevaluated_conditions: _Codes = ()  # A5's policy checks no agent could evaluate
    draft_available: bool = False
    customer_status: Literal["not_verified"] = "not_verified"
    automatic_continuation: Literal[False] = False
    human_approval_required: Literal[True] = True
    approved: Literal[False] = False
    released: Literal[False] = False

    @model_validator(mode="after")
    def _consistent(self):
        if not all(_violations(self)):
            raise ValueError("inconsistent disposition")
        return self

    def customer_view(self) -> "CustomerOutcome":
        """Fixed customer-safe text: no codes, amounts, evidence or customer text."""
        c = self.category
        notices = ()
        if c == "review_required":
            notices = tuple(_REVIEW_TEXT[r] for r in self.review_categories)
        return CustomerOutcome(
            outcome=_CUSTOMER[c],
            message=_MESSAGE[c],
            questions=self.questions,
            blockers=tuple(_BLOCKER_TEXT[b] for b in self.blockers),
            review_notices=notices,
            draft_available=self.draft_available,
        )


def _violations(d: SupervisorDisposition) -> tuple[bool, ...]:
    """Each rule as a check that holds; any False makes the disposition invalid."""
    c, action = d.category, d.route_action
    flags = (d.clarification_required, d.blocked, d.review_required, d.technical_failure)
    expected = (c == "clarification_required", c in _BLOCKED, c == "review_required", c == "technical_failure")  # fmt: skip
    if c in _BLOCKED:
        expected_action = "blocked"
    else:
        expected_action = next((a for a, k in _CATEGORY.items() if k == c), None)
    asked = questions(d.clarification_codes) if c == "clarification_required" else ()
    blocked_by = blockers(d.blocking_findings) if c == "not_quotable" else ()
    return (
        flags == expected,
        action == expected_action or (c == "technical_failure" and action is None),
        (action is not None) == (d.stage is not None),
        # clarification: the questions are exactly the rendering of the route's codes
        d.questions == asked,
        c == "clarification_required" or not d.clarification_codes,
        # blockers: only a verified A2 infeasibility, in A2's own vocabulary. An infeasible
        # part always has at least one finding and never a MISSING_* one (A2 _result_matches).
        set(d.blocking_findings) <= a2._KNOWN,
        len(set(d.blocking_findings)) == len(d.blocking_findings),
        not any(code.startswith("MISSING_") for code in d.blocking_findings),
        c != "not_quotable" or bool(d.blocking_findings),
        d.blockers == blocked_by,
        c == "not_quotable" or not d.blocking_findings,
        c != "not_quotable" or d.stage == "manufacturing",
        # reviews: recommended categories follow from the codes; a review always has one
        c in _REVIEWED or not d.review_reasons,
        d.review_categories == _review_view(c, d.review_reasons),
        # draft: only A5's verified draft, which completion requires
        not d.draft_available or (c in _REVIEWED and d.stage == "quotation"),
        c != "draft_ready_for_approval" or d.draft_available,
        # A5 policy, not the caller: a draft always lists the unchecked customer conditions
        d.unevaluated_conditions in (A5_UNEVALUATED if d.draft_available else ((),)),
    )


def _review_view(category: str, reasons: tuple[str, ...]) -> tuple[ReviewCategory, ...]:
    if category == "review_required":
        return review_categories(reasons) or ("other",)
    return review_categories(reasons) if category in _REVIEWED else ()


def for_result(route, error: str | None, draft_available: bool, findings=(), unevaluated=()):
    """The disposition of a settled run: ``route`` is the result's ``RouteDecision`` (already
    ``decide(outcomes)``), or None with ``error`` for a run that failed before settling."""
    if route is None:
        return SupervisorDisposition(
            category="technical_failure",
            route_action=None,
            stage=None,
            reason=error,
            clarification_required=False,
            blocked=False,
            review_required=False,
            technical_failure=True,
        )
    action = route.action
    if action == "blocked":
        category = "not_quotable" if route.outcome == "not_quotable" else "not_evaluated"
    elif action in _CATEGORY:
        category = _CATEGORY[action]
    else:  # "delegate": not settled
        raise ValueError("the route has not settled")
    clarifications = route.clarifications if category == "clarification_required" else ()
    reasons = route.review_reasons if category in _REVIEWED else ()
    findings = tuple(findings) if category == "not_quotable" else ()
    return SupervisorDisposition(
        category=category,
        route_action=action,
        stage=route.stopped_at or "quotation",  # complete: A5's outcome settled it
        reason=route.reason,
        clarification_required=category == "clarification_required",
        blocked=category in _BLOCKED,
        review_required=category == "review_required",
        technical_failure=category == "technical_failure",
        clarification_codes=clarifications,
        questions=questions(clarifications) if category == "clarification_required" else (),
        blocking_findings=findings,
        blockers=blockers(findings) if category == "not_quotable" else (),
        review_reasons=reasons,
        review_categories=_review_view(category, reasons),
        unevaluated_conditions=tuple(unevaluated),
        draft_available=draft_available,
    )


# --- customer projection ----------------------------------------------------------------------

CustomerOutcomeName = Literal[
    "information_needed", "not_quotable", "unable_to_evaluate", "under_review",
    "draft_pending_approval", "technical_issue",
]  # fmt: skip
_CUSTOMER: dict[str, CustomerOutcomeName] = {
    "clarification_required": "information_needed",
    "not_quotable": "not_quotable",
    "not_evaluated": "unable_to_evaluate",
    "review_required": "under_review",
    "draft_ready_for_approval": "draft_pending_approval",
    "technical_failure": "technical_issue",
}
_MESSAGE = {
    "clarification_required": "We need more information before we can evaluate your request.",
    "not_quotable": "We cannot quote this request as specified with our current manufacturing capabilities.",  # fmt: skip
    "not_evaluated": "We could not complete the evaluation of this request on the information available. No decision has been made.",  # fmt: skip
    "review_required": "Your request needs a review by our team before a quotation can be prepared.",  # fmt: skip
    "draft_ready_for_approval": "A draft quotation has been prepared and is awaiting approval by our team. It is not a binding offer.",  # fmt: skip
    "technical_failure": "We could not complete processing because of a technical issue. No decision has been made about your request.",  # fmt: skip
}
_BLOCKER_TEXT = {
    "material": "The requested material is not supported.",
    "finish": "The requested finish is not supported for this material.",
    "thickness": "The requested thickness is not supported for this material.",
    "part_type": "The requested part type is not supported.",
    "tolerance": "The requested tolerance is tighter than we can manufacture.",
    "bend_radius": "The requested bend radius is below our minimum for this thickness.",
    "operation": "A required operation is not supported.",
    "outside_supported_range": "A dimension or quantity is outside our supported range.",
    "other": "The request does not meet our current manufacturing capabilities.",
}
_REVIEW_TEXT = {
    "engineering": "Our engineering team will review the manufacturing requirements.",
    "supply_delivery": "Our planning team will review material availability, capacity and delivery timing.",  # fmt: skip
    "commercial": "Our sales team will review the commercial terms.",
    "intake": "Our team will review the details of your request.",
    "other": "Our team will review your request.",
}


class CustomerOutcome(BaseModel):
    """The customer-safe disposition. Never approved, released or binding."""

    model_config = _STRICT
    outcome: CustomerOutcomeName
    message: _Text
    questions: tuple[_Text, ...] = ()
    blockers: tuple[_Text, ...] = ()
    review_notices: tuple[_Text, ...] = ()
    draft_available: bool = False
    human_approval_required: Literal[True] = True
    approved: Literal[False] = False
    released: Literal[False] = False
