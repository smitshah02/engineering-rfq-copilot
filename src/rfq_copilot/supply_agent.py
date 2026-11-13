"""Supply and Scheduling Agent A3 (Day 39): manufacturable parts -> evidence-backed stock,
capacity and lead-time assessment. Not price, not approval, not a delivery commitment.

    ManufacturingAssessment (A2, feasible) + trusted horizon_start [+ IntakeResult]
        + trusted ToolExecutor -> evaluate_supply -> SupplyAssessment

- Input gate: A2's assessment must be ``feasible`` with every part ``feasible`` and backed by a
  ``valid`` ``mfg-v1.0`` tool result; otherwise ``not_evaluated`` and no tool is called.
- Dates: ``horizon_start`` is the trusted planning policy (a Monday), never customer text. A
  requested delivery date is used only when the Day 36 ``IntakeResult`` of the same source
  holds exactly one ``verified`` ``requested_delivery_date``; a stated but unverified one is
  ``needs_clarification`` and nothing is called.
- Authority: three read-only MCP tools (RBAC v2 ``view_assigned_rfqs``) on the specifications
  A2 verified, in one RFQ-level call each (stock and capacity are shared by all parts):
  ``check_inventory``, ``check_capacity``, ``estimate_lead_time``. Each server call runs the
  existing services (``services.inventory``, ``services.capacity``, ``services.lead_time``)
  in its own READ ONLY snapshot with server-selected snapshots and pinned rules. Sequential,
  once each, no retries, one timeout. Nothing is reserved or written.
- No model. Availability, schedule and dates are the tools'; the explanation is rendered from
  the validated results by fixed templates and holds no customer text.
- Evidence checks (fail closed, ``invalid_evidence``): one pricing rule, horizon, delivery date
  and snapshot pair across the three results; results that agree with each other (the lead
  time follows from the capacity plan, which follows from the stock check); stock lines that
  cover exactly the requested parts, each once, with that part's material and thickness. A
  pricing version other than the server pin, or a supply verdict that contradicts A2
  (``needs_clarification`` / ``not_manufacturable``), is ``requires_review``.
- Isolation: arguments are only parts, horizon and delivery date (contract ``extra="forbid"``);
  the customer comes only from the executor's verified principal.
"""

import asyncio
import hashlib
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from rfq_copilot.intake import IntakeResult
from rfq_copilot.intake_validation import ToolExecutor
from rfq_copilot.manufacturing_agent import ManufacturingAssessment, PartFeasibility, _forged
from rfq_copilot.mcp.client import FailureCode, McpCallError
from rfq_copilot.mcp.contracts import (
    CapacityOutput,
    DeliveryPlanningInput,
    InventoryLineOutput,
    InventoryOutput,
    LeadTimeOutput,
    PartCheckInput,
    PlanningInput,
)
from rfq_copilot.mcp.runtime import MANUFACTURING_RULE_VERSION, PRICING_RULE_VERSION
from rfq_copilot.services.lead_time import (
    CAPACITY_FINDINGS,
    FRIDAY_OFFSET,
    add_business_days,
    business_days_between,
    plan_start,
)

TOOLS = ("check_inventory", "check_capacity", "estimate_lead_time")  # call order
ALLOWED_TOOLS = frozenset(TOOLS)  # a subset of graph.READ_ONLY_TOOLS (tested)
DEFAULT_TIMEOUT = 60.0

Status = Literal[
    "available", "constrained", "needs_clarification", "requires_review", "not_evaluated", "error"
]
ErrorCode = FailureCode | Literal["invalid_evidence"]
_STRICT = ConfigDict(extra="forbid", frozen=True)


class SupplyEvidence(BaseModel):
    """Provenance: the arguments digest and the three exact, validated tool results."""

    model_config = _STRICT
    arguments_sha256: str  # of the exact DeliveryPlanningInput JSON (PlanningInput is a subset)
    inventory: InventoryOutput  # check_inventory: stock lines, shortages, snapshot
    capacity: CapacityOutput  # check_capacity: workcentre weeks, start week, horizon
    lead_time: LeadTimeOutput  # estimate_lead_time: ready date, delivery feasibility


class PartSupply(BaseModel):
    model_config = _STRICT
    index: int  # A2 / Intake part index
    part_id: str
    stock: InventoryLineOutput | None = None  # the stock line that covers this part


class SupplyAssessment(BaseModel):
    """A3 result. A planning estimate from verified tools, never a reservation or promise."""

    model_config = _STRICT
    source_id: str
    rfq_id: str | None
    status: Status
    horizon_start: date | None = None  # trusted policy
    requested_delivery_date: date | None = None  # verified Intake value, when stated
    parts: tuple[PartSupply, ...] = ()
    explanation: str
    evidence: SupplyEvidence | None = None  # None: no tool results, so no conclusion
    not_evaluated_reason: str | None = None
    error: ErrorCode | None = None


def _delivery(extraction: IntakeResult | None, a2: ManufacturingAssessment):
    """(date | None, problem | None) from the Intake extraction of the same source."""
    if extraction is None:
        return None, None
    if not isinstance(extraction, IntakeResult):
        raise ValueError("extraction must be an IntakeResult")
    if (extraction.source_id, extraction.rfq_id) != (a2.source_id, a2.rfq_id):
        raise ValueError("extraction is not the Intake result of this assessment")
    stated = [f for f in extraction.rfq_fields if f.field == "requested_delivery_date"]
    if not stated:
        return None, None
    if len(stated) > 1 or stated[0].status != "verified" or type(stated[0].normalized) is not date:  # fmt: skip
        return None, "requested_delivery_date_unverified"
    return stated[0].normalized, None


def _bound(p: PartFeasibility) -> bool:
    """A2's evidence belongs to this part's unchanged specification (D39-01): same part_id,
    the exact arguments A2 sent (sha256), and a result A2's own checks accept for it."""
    request = PartCheckInput(part=p.specification)
    digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    return (
        p.part_id == p.specification.part_id
        and p.evidence.arguments_sha256 == digest
        and not _forged(p.evidence.output, p.specification)  # A2's evidence checks, reused
    )


def _gate(a2: ManufacturingAssessment) -> str | None:
    """Why A2's assessment cannot be scheduled, or None."""
    if a2.status != "feasible" or not a2.parts:
        return f"manufacturing_{a2.status}" if a2.parts else "no_parts"
    pin = (MANUFACTURING_RULE_VERSION, a2.manufacturing_rule_fingerprint)
    for p in a2.parts:
        out = p.evidence.output if p.evidence else None
        if p.status != "feasible" or p.error or out is None or out.result != "valid":
            return "manufacturing_evidence_missing"
        if (out.rule_version, out.rule_fingerprint) != pin or a2.manufacturing_rule_version != pin[0]:  # fmt: skip
            return "manufacturing_rule_mismatch"  # stale: not the rules Intake validated with
        if not _bound(p):
            return "manufacturing_evidence_invalid"
    if len({p.part_id for p in a2.parts}) != len(a2.parts):
        return "duplicate_part_ids"
    return None


def _inconsistent(
    inv: InventoryOutput,
    cap: CapacityOutput,
    lead: LeadTimeOutput,
    a2: ManufacturingAssessment,
    horizon: date,
    delivery: date | None,
) -> bool:
    """True when the three results cannot be one evaluation of these parts. Consistency checks
    over the evidence only; nothing is recalculated."""
    if (inv.pricing_rule_version, inv.pricing_rule_fingerprint) != (
        cap.pricing_rule_version,
        cap.pricing_rule_fingerprint,
    ):
        return True
    if (cap.horizon_start, lead.horizon_start, lead.requested_delivery_date) != (horizon, horizon, delivery):  # fmt: skip
        return True
    if (lead.inventory_snapshot_id, lead.capacity_snapshot_id) != (inv.snapshot_id, cap.snapshot_id):  # fmt: skip
        return True
    if inv.result in ("needs_clarification", "not_manufacturable"):
        return (cap.result, lead.result) != (inv.result, inv.result)
    lines = inv.lines
    if inv.snapshot_id is not None and any(set(line.snapshot_ids) - {inv.snapshot_id} for line in lines):  # fmt: skip
        return True
    if cap.snapshot_id is not None and set(cap.capacity_snapshot_ids) - {cap.snapshot_id}:
        return True
    # Every requested part is covered by exactly one line of its own material and thickness.
    specs = {p.part_id: p.specification for p in a2.parts}
    covered = [part_id for line in lines for part_id in line.part_ids]
    if sorted(covered) != sorted(specs):
        return True
    # One line per (material, thickness): parts sharing stock share its line (D39-02).
    if len({(line.material_id, line.thickness_mm) for line in lines}) != len(lines):
        return True
    for line in lines:
        for part_id in line.part_ids:
            spec = specs[part_id]
            if (line.material_id, line.thickness_mm) != (spec.material_id, spec.thickness_mm):
                return True
        if _line_impossible(line):
            return True
    sufficient = all(line.status == "sufficient" for line in lines)
    if (inv.result == "sufficient") != sufficient or (lead.procurement_business_days == 0) != sufficient:  # fmt: skip
        return True
    if cap.result not in ("feasible", "infeasible") or cap.start_week != lead.start_week:
        return True
    # Dates (D39-03): the start follows from the procurement delay counted from the trusted
    # horizon, and no planned capacity week lies before the horizon's start week.
    if lead.earliest_start_date != add_business_days(horizon, lead.procurement_business_days):
        return True
    # D42-A3-01: the delay and start week are the lead-time service's plan_start of these stock
    # lines. Its default delay is a reference constant of the pinned pricing rule only; under
    # rule drift the evidence is kept for review (_status) without this check.
    if inv.pricing_rule_version == PRICING_RULE_VERSION:
        start = plan_start(inv, horizon)
        if (lead.procurement_business_days, cap.start_week) != (start.procurement_business_days, start.start_week):  # fmt: skip
            return True
    first_week = horizon + timedelta(weeks=cap.start_week)
    if cap.start_week < 0 or any(w.week_start < first_week for w in cap.weeks):
        return True
    if cap.result == "infeasible":
        expected = (CAPACITY_FINDINGS.get(cap.reason),)
        return lead.result != "infeasible" or lead.findings != expected or lead.estimated_ready_date is not None  # fmt: skip
    infeasible_date = lead.delivery_feasible is False
    ready, final = lead.estimated_ready_date, cap.final_week_start
    if ready is None or final is None or not cap.weeks:
        return True
    if final != max(w.week_start for w in cap.weeks) or ready != max(final + FRIDAY_OFFSET, lead.earliest_start_date):  # fmt: skip
        return True  # not the last planned Friday, or the start if later (D42-A3-01)
    if lead.lead_business_days != business_days_between(horizon, ready):
        return True
    if delivery is not None and ready is not None and lead.delivery_feasible != (ready <= delivery):
        return True  # "achievable" must mean the verified ready date is on or before it
    return (
        lead.result != "estimated"
        or cap.reason is not None
        or cap.final_week_start is None
        or lead.estimated_ready_date is None
        or lead.lead_business_days is None
        or lead.earliest_start_date is None
        or lead.estimated_ready_date < lead.earliest_start_date
        or (lead.delivery_feasible is None) != (delivery is None)
        or lead.findings != (("DELIVERY_DATE_INFEASIBLE",) if infeasible_date else ())
    )


def _line_impossible(line: InventoryLineOutput) -> bool:
    """The stock service's own semantics for one line: positive demand, shortage =
    max(required - available, 0), and status from available versus required (D39-02)."""
    required, available = line.required_kg, line.available_kg
    if required <= 0 or line.shortage_kg != max(required - available, Decimal(0)):
        return True
    if available >= required:
        return line.status != "sufficient"
    return line.status != ("insufficient" if available > 0 else "unavailable")


def _status(inv: InventoryOutput, cap: CapacityOutput, lead: LeadTimeOutput) -> Status:
    if inv.pricing_rule_version != PRICING_RULE_VERSION:
        return "requires_review"  # rule drift: the evidence is kept for the reviewer
    if inv.result in ("needs_clarification", "not_manufacturable"):
        return "requires_review"  # contradicts A2's verified feasibility
    if lead.result == "infeasible":
        return "constrained" if cap.reason == "NO_CAPACITY_IN_HORIZON" else "requires_review"
    if inv.result == "insufficient" or lead.delivery_feasible is False:
        return "constrained"
    return "available"


def _explain(inv: InventoryOutput, cap: CapacityOutput, lead: LeadTimeOutput) -> str:
    head = (
        f"Verified by {', '.join(TOOLS)} under {inv.pricing_rule_version} "
        f"(inventory snapshot {inv.snapshot_id}, capacity snapshot {cap.snapshot_id}, "
        f"horizon start {lead.horizon_start}); a planning estimate, not a commitment"
    )
    if inv.pricing_rule_version != PRICING_RULE_VERSION:
        return f"{head}: the pricing rules differ from {PRICING_RULE_VERSION}; review is required."
    if inv.result in ("needs_clarification", "not_manufacturable"):
        return f"{head}: the supply check found the parts {inv.result}, contradicting the manufacturing assessment; review is required."  # fmt: skip
    short = [
        f"{line.material_id} {line.thickness_mm} mm is {line.status}, short {line.shortage_kg} kg"
        for line in inv.lines
        if line.status != "sufficient"
    ]
    stock = "all material is in stock"
    if short:
        stock = f"{'; '.join(short)} (procurement {lead.procurement_business_days} business days)"
    if lead.result == "infeasible":
        if cap.reason == "NO_CAPACITY_IN_HORIZON":
            return f"{head}: {stock}; no capacity in the planning horizon."
        return f"{head}: {stock}; workcentre capacity data is missing; review is required."
    text = f"{head}: {stock}; estimated ready date {lead.estimated_ready_date} ({lead.lead_business_days} business days)"  # fmt: skip
    if lead.requested_delivery_date is not None:
        achievable = "achievable" if lead.delivery_feasible else "not achievable"
        text += f"; requested delivery {lead.requested_delivery_date} is {achievable}"
    return f"{text}."


async def evaluate_supply(
    a2: ManufacturingAssessment,
    execute: ToolExecutor | None,
    *,
    horizon_start: date | None,
    extraction: IntakeResult | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT,
) -> SupplyAssessment:
    """Stock, capacity and lead time for A2's manufacturable parts.

    ``execute`` must be a trusted, principal-bound executor (``graph.mcp_executor``);
    ``horizon_start`` the application's planning policy; ``extraction`` the Day 36 result of
    the same source (only its verified requested delivery date is used)."""
    if not isinstance(a2, ManufacturingAssessment):
        raise ValueError("evaluate_supply needs a ManufacturingAssessment")
    delivery, problem = _delivery(extraction, a2)
    base = {"source_id": a2.source_id, "rfq_id": a2.rfq_id}

    def blocked(status: Status, reason: str | None = None, error=None) -> SupplyAssessment:
        text = f"Supply and scheduling were not evaluated ({reason or error}); no conclusion."
        return SupplyAssessment(**base, status=status, explanation=text, not_evaluated_reason=reason, error=error)  # fmt: skip

    reason = _gate(a2)
    if reason is None and horizon_start is None:
        reason = "planning_horizon_missing"
    elif reason is None and (type(horizon_start) is not date or horizon_start.weekday() != 0):
        reason = "invalid_planning_horizon"
    if reason:
        return blocked("not_evaluated", reason)
    if problem:
        return blocked("needs_clarification", problem)
    if execute is None:
        return blocked("error", error="unauthenticated")
    specs = tuple(p.specification for p in a2.parts)
    try:
        lead_input = DeliveryPlanningInput(parts=specs, horizon_start=horizon_start, requested_delivery_date=delivery)  # fmt: skip
        plan_input = PlanningInput(parts=specs, horizon_start=horizon_start)
    except ValidationError:
        return blocked("error", error="invalid_input")
    requests = {"check_inventory": plan_input, "check_capacity": plan_input, "estimate_lead_time": lead_input}  # fmt: skip
    outputs = {"check_inventory": InventoryOutput, "check_capacity": CapacityOutput, "estimate_lead_time": LeadTimeOutput}  # fmt: skip
    results = {}
    try:
        async with asyncio.timeout(timeout_seconds):
            for tool in TOOLS:  # sequential, once each, no retries
                raw = await execute(tool, requests[tool])
                data = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw
                results[tool] = outputs[tool].model_validate(data)
    except McpCallError as error:
        return blocked("error", error=error.code)
    except ValidationError:
        return blocked("error", error="invalid_output")
    except TimeoutError:
        return blocked("error", error="timeout")
    except Exception:  # an executor fault: its text never surfaces
        return blocked("error", error="unavailable")
    inv, cap, lead = (results[tool] for tool in TOOLS)
    if _inconsistent(inv, cap, lead, a2, horizon_start, delivery):
        return blocked("error", error="invalid_evidence")  # unverifiable evidence is not kept
    status = _status(inv, cap, lead)
    by_part = {part_id: line for line in inv.lines for part_id in line.part_ids}
    return SupplyAssessment(
        **base,
        status=status,
        horizon_start=horizon_start,
        requested_delivery_date=delivery,
        parts=tuple(
            PartSupply(index=p.index, part_id=p.part_id, stock=by_part.get(p.part_id))
            for p in a2.parts
        ),
        explanation=_explain(inv, cap, lead),
        evidence=SupplyEvidence(
            arguments_sha256=hashlib.sha256(lead_input.model_dump_json().encode()).hexdigest(),
            inventory=inv,
            capacity=cap,
            lead_time=lead,
        ),
    )
