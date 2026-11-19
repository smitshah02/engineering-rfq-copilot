"""Manufacturing Feasibility Agent A2 (manufacturing evidence): Intake-ready parts -> evidence-backed
manufacturing feasibility. Not inventory, capacity, price or approval.

    IntakeValidation (intake validation, ready) + trusted ToolExecutor
        -> evaluate_manufacturing_feasibility -> ManufacturingAssessment

- Input is the intake validation ``IntakeValidation``. Unless it and every part are
  ``ready_for_manufacturing_evaluation`` with a built ``PartSpecification``, the result is
  ``not_evaluated`` and no tool is called. The specification is passed unchanged.
- Authority: one read-only MCP tool, ``check_manufacturing_feasibility`` (it runs the material
  and finish check inside ``services.manufacturing.check_manufacturing_capability`` under the
  server-pinned ``mfg-v1.0``). It is called once per part through the caller's trusted executor
  (``graph.mcp_executor``: delegated credential, authenticated gateway, RBAC v2
  ``view_catalog``), once each, with no retries, inside one timeout. Sequential by default;
  a trusted caller may allow up to ``MAX_CONCURRENCY`` simultaneous part checks (parallel execution).
- No model is involved. Feasibility, findings and routing are the tool's; the explanation is
  rendered from finding codes by fixed templates. A part is ``feasible`` only with a validated,
  internally consistent tool result; anything else is an explicit error or blocker.
- Evidence checks (fail closed): the result must match its output contract, use one rule
  version throughout, be self-consistent (``valid`` has a route and no findings, and so on) and
  carry only known finding codes, else ``invalid_evidence``. A rule version or fingerprint
  differing from the one Intake validated against -> ``requires_review``.
- Parallel part checks (parallel execution, L01): each part's check is one read-only call on that part
  alone (own token, HTTP client, MCP session and READ ONLY snapshot; nothing shared, nothing
  written), and its result depends on no other part, so parts are independent work items.
  ``max_concurrency`` > 1 runs them under a semaphore in one task group. Fan-in (``_join``)
  keys each result by its position in ``intake.parts``, refuses a missing, duplicate, unknown
  or mismatched result, and rebuilds Intake part order; completion order is ignored. The
  aggregate (``_PRECEDENCE``) is unchanged. A per-part tool failure stays that part's ``error``
  exactly as sequentially (siblings still run); an unexpected fault cancels and awaits every
  sibling. Timeout or cancellation cancels and awaits every worker (no orphan task); a result
  that arrives after its worker was cancelled is never kept.
- Isolation: each call carries only its own part; no input carries identity, customer scope,
  role or rule version (the contract forbids them); the customer comes only from the
  executor's verified principal. Customer text (``part_id``) never enters the explanation.
"""

import asyncio
import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from rfq_copilot.api.schemas.rfq import MAX_PARTS, PartSpecification
from rfq_copilot.intake_validation import IntakeValidation, ToolExecutor
from rfq_copilot.mcp.client import FailureCode, McpCallError
from rfq_copilot.mcp.contracts import ManufacturingFeasibilityOutput, PartCheckInput
from rfq_copilot.mcp.runtime import MANUFACTURING_RULE_VERSION
from rfq_copilot.services.materials import MATERIAL_FIELDS

TOOL = "check_manufacturing_feasibility"
ALLOWED_TOOLS = frozenset({TOOL})  # a subset of graph.READ_ONLY_TOOLS (tested)
DEFAULT_TIMEOUT = 60.0
MAX_CONCURRENCY = 4  # hard cap on simultaneous part checks in one evaluation (16 GB host)
READY = "ready_for_manufacturing_evaluation"

Status = Literal[
    "feasible", "infeasible", "needs_clarification", "requires_review", "not_evaluated", "error"
]
ErrorCode = FailureCode | Literal["invalid_evidence", "execution_limit"]
_RESULT_STATUS = {
    "valid": "feasible",
    "unsupported": "infeasible",
    "needs_clarification": "needs_clarification",
}
# Most blocking first: the RFQ-level status is the first one any part has.
_PRECEDENCE = ("error", "infeasible", "needs_clarification", "requires_review", "feasible")

# The finding codes services.materials / services.manufacturing produce. Others are refused.
_FINDING_TEXT = {
    "UNSUPPORTED_MATERIAL": "the material is not in the catalog",
    "UNSUPPORTED_FINISH": "the finish is not in the catalog",
    "MATERIAL_THICKNESS_UNSUPPORTED": "the material is not supported in this thickness",
    "MATERIAL_FINISH_INCOMPATIBLE": "the finish is not compatible with the material",
    "UNSUPPORTED_PART_TYPE": "the part type is not supported",
    "TOLERANCE_UNSUPPORTED": "the tolerance is tighter than the rules' minimum",
    "BEND_RADIUS_TOO_SMALL": "the bend radius is below the minimum for this thickness",
    "UNSUPPORTED_OPERATION": "a required operation is excluded by the rules or not supported "
    "for the material",
}
# Code -> part field, exactly as the services build them: MISSING_<field> for a required part
# field; OUT_OF_RANGE_<field> for a numeric part field, plus the rule's derived bend count.
_FIELDS = tuple(f for f in PartSpecification.model_fields if f != "part_id")
_MISSING = {f"MISSING_{f.upper()}": f for f in _FIELDS}
_NUMERIC = [f for f in _FIELDS if f not in ("part_type", "material_id", "finish_id")]
_RANGE = {f"OUT_OF_RANGE_{f.upper()}": f for f in _NUMERIC} | {"OUT_OF_RANGE_BEND_COUNT": "bend_count"}  # fmt: skip
_MATERIAL_MISSING = frozenset(f"MISSING_{f.upper()}" for f in MATERIAL_FIELDS)
# Codes only services.materials produces (MaterialCheck.findings).
_MATERIAL_CODES = _MATERIAL_MISSING | {
    "UNSUPPORTED_MATERIAL",
    "UNSUPPORTED_FINISH",
    "MATERIAL_THICKNESS_UNSUPPORTED",
    "MATERIAL_FINISH_INCOMPATIBLE",
}
_KNOWN = _MATERIAL_CODES | _FINDING_TEXT.keys() | _MISSING.keys() | _RANGE.keys()
_STRICT = ConfigDict(extra="forbid", frozen=True)


class ToolEvidence(BaseModel):
    """Provenance: the exact tool, arguments digest and validated result."""

    model_config = _STRICT
    tool: Literal["check_manufacturing_feasibility"] = TOOL
    server: Literal["manufacturing"] = "manufacturing"
    arguments_sha256: str  # of the exact PartCheckInput JSON sent
    output: ManufacturingFeasibilityOutput  # material assessment, findings, operations, rules


class PartFeasibility(BaseModel):
    model_config = _STRICT
    index: int  # IntakeValidation part index
    part_id: str
    status: Status
    specification: PartSpecification  # exactly as Intake built it; never modified
    clarifications: tuple[str, ...] = ()  # PartSpecification fields to ask the customer for
    explanation: str
    evidence: ToolEvidence | None = None  # None: no tool result, so no conclusion
    error: ErrorCode | None = None


class ManufacturingAssessment(BaseModel):
    """A2 result. Feasibility only: never inventory, capacity, price or approval."""

    model_config = _STRICT
    source_id: str
    rfq_id: str | None
    status: Status
    manufacturing_rule_version: str | None = None  # the version Intake validated against
    manufacturing_rule_fingerprint: str | None = None
    parts: tuple[PartFeasibility, ...] = ()
    not_evaluated_reason: str | None = None  # the Intake readiness that blocked evaluation
    error: ErrorCode | None = None


def _words(code: str) -> str:
    return code.lower().removesuffix("_mm").replace("_", " ")


def _reason(code: str) -> str:
    if code in _FINDING_TEXT:
        return _FINDING_TEXT[code]
    return f"the {_words(_RANGE[code])} is outside the supported range"


def _result_matches(result: str, findings: tuple[str, ...]) -> bool:
    """The services' own status rule: valid has no findings, needs_clarification only
    MISSING_* findings, unsupported at least one finding and no MISSING_*."""
    missing = sum(code in _MISSING for code in findings)
    if result == "valid":
        return not findings
    if result == "unsupported":
        return bool(findings) and not missing
    return bool(findings) and missing == len(findings)


def _forged(out: ManufacturingFeasibilityOutput, spec: PartSpecification) -> bool:
    """True when the result cannot be the services' output for this part (forged, corrupted
    or another part's). Consistency checks over the evidence only; nothing is recalculated."""
    material, catalog = out.material, out.material.material
    outer, inner = set(out.findings), set(material.findings)
    if (material.rule_version, material.rule_fingerprint) != (
        out.rule_version,
        out.rule_fingerprint,
    ):
        return True
    if not outer <= _KNOWN or not inner <= _MATERIAL_CODES:
        return True
    if not (_result_matches(out.result, out.findings) and _result_matches(material.result, material.findings)):  # fmt: skip
        return True
    if any(getattr(spec, _MISSING[code]) is not None for code in outer & _MISSING.keys()):
        return True  # "missing" a field this part states
    # The catalog entry is this part's material; it is absent only when unknown or unasked.
    if catalog is None:
        if "UNSUPPORTED_MATERIAL" not in inner and material.result != "needs_clarification":
            return True
    elif catalog.material_id != spec.material_id or "UNSUPPORTED_MATERIAL" in inner:
        return True
    # Material findings reach the outer result unchanged; a clarification keeps only the asks.
    if outer & _MATERIAL_MISSING != inner & _MATERIAL_MISSING:
        return True
    if out.result != "needs_clarification" and outer & _MATERIAL_CODES != inner:
        return True
    if out.result == "needs_clarification":
        return bool(out.operations)
    if out.result == "valid":  # a positive conclusion needs complete, matching evidence
        minimum = (
            out.min_bend_radius_mm
        )  # A5's borderline-bend input (cross-agent evidence control)
        return (
            catalog is None
            or not out.operations
            or spec.thickness_mm not in catalog.supported_thickness_mm
            or spec.finish_id not in catalog.compatible_finishes
            or not set(out.operations) <= set(catalog.supported_operations)
            or ("bend" in out.operations and minimum is None)
            or (
                minimum is not None
                and (
                    minimum < spec.thickness_mm * catalog.min_bend_radius_factor  # service floor
                    or (spec.bend_radius_mm is not None and spec.bend_radius_mm < minimum)
                )
            )
        )
    return False


def _part(i: int, spec: PartSpecification, **fields) -> PartFeasibility:
    return PartFeasibility(index=i, part_id=spec.part_id, specification=spec, **fields)


def _failed(i: int, spec: PartSpecification, code: ErrorCode) -> PartFeasibility:
    text = f"Part {i}: manufacturing feasibility could not be determined ({code}); no conclusion."
    return _part(i, spec, status="error", error=code, explanation=text)


def _assess(
    i: int,
    spec: PartSpecification,
    out: ManufacturingFeasibilityOutput,
    digest: str,
    intake: IntakeValidation,
) -> PartFeasibility:
    if _forged(out, spec):
        return _failed(i, spec, "invalid_evidence")  # unverifiable evidence is not kept
    evidence = ToolEvidence(arguments_sha256=digest, output=out)
    pinned = (MANUFACTURING_RULE_VERSION, MANUFACTURING_RULE_VERSION, intake.manufacturing_rule_fingerprint)  # fmt: skip
    if (out.rule_version, intake.manufacturing_rule_version, out.rule_fingerprint) != pinned:
        text = (
            f"Part {i}: the manufacturing check ran under rules that differ from the Intake "
            "validation; engineering review is required."
        )
        return _part(i, spec, status="requires_review", explanation=text, evidence=evidence)
    status = _RESULT_STATUS[out.result]
    head = f"Part {i} (verified by {TOOL} under {out.rule_version})"
    if status == "feasible":
        text = f"{head}: manufacturable; route: {' -> '.join(out.operations)}."
        return _part(i, spec, status=status, explanation=text, evidence=evidence)
    if status == "needs_clarification":
        asks = tuple(_MISSING[code] for code in out.findings)
        text = f"{head}: needs customer information: {', '.join(map(_words, asks))}."
        return _part(
            i, spec, status=status, clarifications=asks, explanation=text, evidence=evidence
        )
    text = f"{head}: not manufacturable: {'; '.join(map(_reason, out.findings))}."
    return _part(i, spec, status=status, explanation=text, evidence=evidence)


def _digest(spec: PartSpecification) -> str:
    """sha256 of the exact PartCheckInput JSON sent for this part."""
    return hashlib.sha256(PartCheckInput(part=spec).model_dump_json().encode()).hexdigest()


async def _check(
    i: int, spec: PartSpecification, execute: ToolExecutor, intake: IntakeValidation
) -> PartFeasibility:
    request = PartCheckInput(part=spec)
    digest = _digest(spec)
    try:
        raw = await execute(TOOL, request)
        data = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw
        out = ManufacturingFeasibilityOutput.model_validate(data)
    except McpCallError as error:
        return _failed(i, spec, error.code)
    except ValidationError:
        return _failed(i, spec, "invalid_output")
    except Exception:  # an executor fault: its text never surfaces
        return _failed(i, spec, "unavailable")
    return _assess(i, spec, out, digest, intake)


class _JoinError(Exception):
    """Fan-in refused the collected part results (an internal defect, never customer data)."""


def _join(
    intake: IntakeValidation, collected: list[tuple[int, object]]
) -> tuple[PartFeasibility, ...]:
    """Fan-in: exactly one result per expected part (keyed by its position in
    ``intake.parts``), each the right type and status for that part's index, id and unchanged
    specification, returned in Intake part order whatever order the workers finished in.

    Evidence: an ``error`` part carries none (``_failed``); any other part must be exactly what
    ``_assess`` derives from its own evidence and this part's arguments digest under this
    ``intake``, so a status, explanation or evidence that does not follow is refused. These
    are consistency checks, not cryptographic provenance (L04/L21)."""
    expected = intake.parts
    slots: dict[int, object] = {}
    for position, result in collected:
        if position in slots:
            raise _JoinError("duplicate")
        slots[position] = result
    if set(slots) != set(range(len(expected))):
        raise _JoinError("missing or unknown")
    joined = tuple(slots[i] for i in range(len(expected)))
    for p, r in zip(expected, joined, strict=True):
        if not isinstance(r, PartFeasibility):
            raise _JoinError("type")
        owner = (p.index, p.specification.part_id, p.specification)
        if (r.index, r.part_id, r.specification) != owner:
            raise _JoinError("another part")
        if r.status not in _PRECEDENCE or (r.status == "error") != (r.error is not None):
            raise _JoinError("status")
        if r.status == "error":
            if r.evidence is not None:
                raise _JoinError("error with evidence")
            continue
        if not isinstance(r.evidence, ToolEvidence):
            raise _JoinError("conclusion without evidence")
        spec = p.specification
        if r != _assess(p.index, spec, r.evidence.output, _digest(spec), intake):
            raise _JoinError("evidence does not support the result")
    return joined


async def _parallel(
    intake: IntakeValidation, execute: ToolExecutor, timeout_seconds: float, limit: int
) -> tuple[PartFeasibility, ...]:
    """Bounded fan-out of the per-part checks, then the validated fan-in."""
    gate = asyncio.Semaphore(limit)
    collected: list[tuple[int, PartFeasibility]] = []

    async def work(position: int, p) -> None:
        async with gate:
            result = await _check(p.index, p.specification, execute, intake)
        if not asyncio.current_task().cancelling():  # a late result after cancel is not kept
            collected.append((position, result))

    try:
        async with asyncio.timeout(timeout_seconds):
            async with asyncio.TaskGroup() as group:
                for position, p in enumerate(intake.parts):
                    group.create_task(work(position, p))
    except TimeoutError:  # every worker is cancelled and awaited before this is raised
        done = {position for position, _ in collected}
        collected += [
            (i, _failed(p.index, p.specification, "timeout"))
            for i, p in enumerate(intake.parts)
            if i not in done
        ]
    return _join(intake, collected)


async def evaluate_manufacturing_feasibility(
    intake: IntakeValidation,
    execute: ToolExecutor | None,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT,
    max_concurrency: int = 1,
) -> ManufacturingAssessment:
    """Evaluate every Intake-ready part with the read-only feasibility tool.

    ``execute`` must be a trusted, principal-bound executor (``graph.mcp_executor``);
    ``max_concurrency`` (1..MAX_CONCURRENCY) is trusted configuration, 1 = sequential."""
    if not isinstance(intake, IntakeValidation):
        raise ValueError("evaluate_manufacturing_feasibility needs an IntakeValidation")
    if type(max_concurrency) is not int or not 1 <= max_concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"max_concurrency must be an int in 1..{MAX_CONCURRENCY}")
    base = {
        "source_id": intake.source_id,
        "rfq_id": intake.rfq_id,
        "manufacturing_rule_version": intake.manufacturing_rule_version,
        "manufacturing_rule_fingerprint": intake.manufacturing_rule_fingerprint,
    }
    blocked = [
        p.readiness if p.readiness != READY else "specification_missing"
        for p in intake.parts
        if p.readiness != READY or p.specification is None
    ]
    if intake.readiness != READY or not intake.parts or blocked:
        reason = intake.readiness if intake.readiness != READY else (blocked or ["no_parts"])[0]
        return ManufacturingAssessment(**base, status="not_evaluated", not_evaluated_reason=reason)
    if len(intake.parts) > MAX_PARTS:
        return ManufacturingAssessment(**base, status="error", error="execution_limit")
    if execute is None:
        return ManufacturingAssessment(**base, status="error", error="unauthenticated")
    parts: list[PartFeasibility] = []
    if max_concurrency > 1 and len(intake.parts) > 1:  # independent parts, bounded overlap
        try:
            parts = list(await _parallel(intake, execute, timeout_seconds, max_concurrency))
        except _JoinError:
            return ManufacturingAssessment(**base, status="error", error="invalid_evidence")
    else:
        try:
            async with asyncio.timeout(timeout_seconds):
                for p in intake.parts:  # sequential, once each, no retries
                    parts.append(await _check(p.index, p.specification, execute, intake))
        except TimeoutError:
            parts += [_failed(p.index, p.specification, "timeout") for p in intake.parts[len(parts) :]]  # fmt: skip
    status = next(s for s in _PRECEDENCE if any(p.status == s for p in parts))
    error = next((p.error for p in parts if p.error), None)
    return ManufacturingAssessment(**base, status=status, parts=tuple(parts), error=error)
