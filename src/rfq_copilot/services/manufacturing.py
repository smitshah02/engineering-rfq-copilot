"""Manufacturing capability validator (check_manufacturing_capability).

Material, thickness and finish findings come from the material catalog service; this
module adds the part-level rules: mandatory fields, part type, dimension and quantity
ranges, tolerance, hole count, bend count, bend radius and the operation route.

Every limit in the pinned rule document is enforced or rejected. The reference
validate_part skipped the scalar hole_count_max (MFG-001); here an unknown limit raises
instead of being silently ignored, so a new rule cannot be under-enforced.
"""

from collections.abc import Sequence
from dataclasses import dataclass, fields
from decimal import Context, Decimal, DivisionByZero, Inexact, InvalidOperation, localcontext

from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.entities import Part
from rfq_copilot.models import OperationRouting
from rfq_copilot.repositories import RuleVersionDetails, list_part_routing
from rfq_copilot.services.materials import (
    MATERIAL_FIELDS,
    CheckResult,
    MaterialCheck,
    Reads,
    check_part_material,
    memo,
    require_manufacturing_rules,
)

# Exact arithmetic: any result that would need rounding raises instead of drifting.
EXACT = Context(prec=60, traps=[Inexact, InvalidOperation, DivisionByZero])
SCALAR_LIMITS = frozenset(
    {"min_tolerance_mm", "hole_count_max", "bends_max", "min_bend_radius_factor"}
)
_PART_FIELDS = frozenset(f.name for f in fields(Part))


@dataclass(frozen=True)
class ManufacturingCheck:
    """Validation of one part. operations is the routed sequence, () if not routable."""

    result: CheckResult
    findings: tuple[str, ...]
    operations: tuple[str, ...]
    material: MaterialCheck
    rule_version: str
    rule_fingerprint: str
    # factor x thickness for a part with bends (None without bends or before validation).
    min_bend_radius_mm: Decimal | None = None


def _ranges(rules: RuleVersionDetails) -> dict:
    """Split limits into [low, high] ranges; fail closed on any limit not enforced here."""
    ranges, unknown = {}, []
    for key, value in rules.document["limits"].items():
        is_range = isinstance(value, Sequence) and not isinstance(value, str) and len(value) == 2
        if is_range and key in _PART_FIELDS:
            ranges[key] = value
        elif is_range or key not in SCALAR_LIMITS:
            unknown.append(key)
    if unknown:
        raise ValueError(
            f"Rule {rules.rule_version} has limits this validator does not enforce: {sorted(unknown)}."
        )
    return ranges


def route_operations(part: Part, routing: Sequence[OperationRouting]) -> tuple[str, ...]:
    """Operations the stored routing requires for this part, in sequence order."""
    operations = []
    for step in routing:
        if step.required_when not in ("always", "finish_not_none"):
            raise ValueError(f"Unknown routing condition {step.required_when!r}.")
        if step.required_when == "always" or part.finish_id != "none":
            operations.append(step.operation_id)
    return tuple(operations)


def evaluate_manufacturing(
    part: Part,
    rules: RuleVersionDetails,
    material: MaterialCheck,
    routing: Sequence[OperationRouting],
) -> ManufacturingCheck:
    """Pure decision from the part, the pinned rule, the material check and stored routing."""
    require_manufacturing_rules(rules)
    if (material.rule_version, material.rule_fingerprint) != (
        rules.rule_version,
        rules.fingerprint,
    ):
        raise ValueError("The material check used a different rule version.")
    document, limits = rules.document, rules.document["limits"]
    ranges = _ranges(rules)

    def check(
        result: CheckResult, findings: set[str], operations=(), minimum=None
    ) -> ManufacturingCheck:
        return ManufacturingCheck(
            result,
            tuple(sorted(findings)),
            operations,
            material,
            rules.rule_version,
            rules.fingerprint,
            minimum,
        )

    required = [f for f in document["mandatory_fields"] if f not in MATERIAL_FIELDS]
    if part.part_type in document["height_required"]:
        required.append("height_mm")
    if part.part_type in document["weld_length_required"]:
        required.append("weld_length_mm")
    missing = {f"MISSING_{f.upper()}" for f in required if getattr(part, f) is None}
    missing |= {f for f in material.findings if f.startswith("MISSING_")}
    if missing:
        # As in the reference: report what to ask the customer, nothing else.
        return check("needs_clarification", missing)

    findings = set(material.findings)
    routable = part.part_type in document["allowed_part_types"]
    if not routable:
        findings.add("UNSUPPORTED_PART_TYPE")
    for field, (low, high) in ranges.items():
        value = getattr(part, field)
        if value is not None and not low <= value <= high:
            findings.add(f"OUT_OF_RANGE_{field.upper()}")
    if part.tolerance_mm is not None and part.tolerance_mm < limits["min_tolerance_mm"]:
        findings.add("TOLERANCE_UNSUPPORTED")
    # MFG-001: the configured maximum is enforced; an unstated count uses the rule default.
    holes = document["defaults"]["hole_count"] if part.hole_count is None else part.hole_count
    if not 0 <= holes <= limits["hole_count_max"]:
        findings.add("OUT_OF_RANGE_HOLE_COUNT")
    bends = document["bend_counts"].get(part.part_type, 0)
    if bends > limits["bends_max"]:
        findings.add("OUT_OF_RANGE_BEND_COUNT")
    minimum = None
    if bends and part.thickness_mm is not None:
        factor = limits["min_bend_radius_factor"]
        if material.material is not None:
            # A material-specific factor can only tighten the global rule.
            factor = max(factor, material.material.min_bend_radius_factor)
        with localcontext(EXACT):
            minimum = part.thickness_mm * factor
        if part.bend_radius_mm is not None and part.bend_radius_mm < minimum:
            findings.add("BEND_RADIUS_TOO_SMALL")

    operations = route_operations(part, routing) if routable else ()
    if routable and not operations:
        raise ValueError(f"No routing is stored for part type {part.part_type}.")
    blocked = set(document["unsupported_operations"])
    supported = None if material.material is None else material.material.supported_operations
    if any(op in blocked or (supported is not None and op not in supported) for op in operations):
        findings.add("UNSUPPORTED_OPERATION")
    return check("unsupported" if findings else "valid", findings, operations, minimum)


async def check_manufacturing_capability(
    session: AsyncSession, part: Part, rules: RuleVersionDetails, *, reads: Reads | None = None
) -> ManufacturingCheck:
    """Gather catalog facts and routing, then decide with evaluate_manufacturing.

    reads, when given, shares catalog reads across the parts of one calculation."""
    material = await check_part_material(session, part, rules, reads=reads)
    routing = ()
    if part.part_type in rules.document["allowed_part_types"]:
        routing = await memo(
            reads, ("routing", part.part_type), lambda: list_part_routing(session, part.part_type)
        )
    return evaluate_manufacturing(part, rules, material, routing)
