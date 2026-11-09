"""Material catalog service: specs, supported thickness and finish compatibility.

The catalog is shared master data, not customer data, so nothing here is customer
scoped; the get_material_specs role check belongs to the tool server.
Only material-dependent findings are produced here. Envelope limits, tolerances,
bends, holes and operations belong to the manufacturing validator.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.entities import Part
from rfq_copilot.models import Finish
from rfq_copilot.repositories import RuleVersionDetails, get_manufacturing_capabilities

# Part fields this service owns; the pinned rule decides which of them are mandatory.
MATERIAL_FIELDS = ("material_id", "thickness_mm", "finish_id")

CheckResult = Literal["valid", "unsupported", "needs_clarification"]


@dataclass(frozen=True)
class MaterialSpec:
    """Catalog view of one material. Cost and pricing version are deliberately excluded.

    availability_class ('stocked' or 'procure') is how the material is normally sourced;
    it says nothing about current free stock, which is the inventory service's answer.
    """

    material_id: str
    category: str
    grade: str
    density_kg_m3: Decimal
    min_bend_radius_factor: Decimal
    availability_class: str
    supported_thickness_mm: tuple[Decimal, ...]
    compatible_finishes: tuple[str, ...]
    supported_operations: tuple[str, ...]


@dataclass(frozen=True)
class MaterialCheck:
    """Outcome for one part; findings use the reference manufacturing codes, sorted."""

    result: CheckResult
    findings: tuple[str, ...]
    material: MaterialSpec | None
    rule_version: str
    rule_fingerprint: str


async def get_material_spec(session: AsyncSession, material_id: str) -> MaterialSpec | None:
    """Catalog spec for one material, or None when the ID is not in the catalog."""
    # ponytail: reuses the existing capability read, which also loads workcentres and routing
    # (two extra small SELECTs); add a narrower query if this becomes hot.
    capabilities = await get_manufacturing_capabilities(session, material_id)
    if capabilities is None:
        return None
    material = capabilities.material
    return MaterialSpec(
        material_id=material.material_id,
        category=material.category,
        grade=material.grade,
        density_kg_m3=material.density_kg_m3,
        min_bend_radius_factor=material.min_bend_radius_factor,
        availability_class=material.availability_class,
        supported_thickness_mm=capabilities.supported_thickness_mm,
        compatible_finishes=tuple(f.finish_id for f in capabilities.finishes),
        supported_operations=tuple(op.operation_id for op in capabilities.operations),
    )


def require_manufacturing_rules(rules: RuleVersionDetails) -> None:
    if not isinstance(rules, RuleVersionDetails) or rules.rule_kind != "manufacturing":
        raise ValueError("Material checks require a manufacturing rule version.")


def evaluate_material(
    part: Part,
    rules: RuleVersionDetails,
    spec: MaterialSpec | None,
    *,
    finish_exists: bool,
) -> MaterialCheck:
    """Pure decision. spec is the catalog entry for part.material_id (None if unknown)."""
    require_manufacturing_rules(rules)

    def check(result: CheckResult, findings: set[str]) -> MaterialCheck:
        return MaterialCheck(
            result, tuple(sorted(findings)), spec, rules.rule_version, rules.fingerprint
        )

    mandatory = [f for f in rules.document["mandatory_fields"] if f in MATERIAL_FIELDS]
    missing = {f"MISSING_{f.upper()}" for f in mandatory if getattr(part, f) is None}
    if missing:
        return check("needs_clarification", missing)
    findings = set()
    if spec is None:
        findings.add("UNSUPPORTED_MATERIAL")
    if part.finish_id is not None and not finish_exists:
        findings.add("UNSUPPORTED_FINISH")
    if spec is not None:
        # Decimal equality: 1, 1.0 and 1.00 mm are the same thickness.
        if part.thickness_mm is not None and part.thickness_mm not in spec.supported_thickness_mm:
            findings.add("MATERIAL_THICKNESS_UNSUPPORTED")
        if part.finish_id is not None and part.finish_id not in spec.compatible_finishes:
            findings.add("MATERIAL_FINISH_INCOMPATIBLE")
    return check("unsupported" if findings else "valid", findings)


async def check_part_material(
    session: AsyncSession, part: Part, rules: RuleVersionDetails
) -> MaterialCheck:
    """Load the catalog facts the part names, then decide with evaluate_material."""
    require_manufacturing_rules(rules)
    if not isinstance(part, Part):
        raise ValueError("Material checks require a Part.")
    spec = None if part.material_id is None else await get_material_spec(session, part.material_id)
    finish_exists = spec is not None and part.finish_id in spec.compatible_finishes
    if part.finish_id is not None and not finish_exists:
        # A compatible finish exists by foreign key; only otherwise ask the finish catalog.
        with session.no_autoflush:
            found = await session.scalar(
                select(Finish.finish_id).where(Finish.finish_id == part.finish_id)
            )
        finish_exists = found is not None
    return evaluate_material(part, rules, spec, finish_exists=finish_exists)
