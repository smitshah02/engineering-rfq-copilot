"""MCP tool contracts: names, typed inputs and outputs, permissions and the error model.

Contracts only. Nothing here registers a tool, opens a session or calls a service; the
Day 25-26 adapters do that, inside one ``Database.snapshot_session()`` per call.

Security rules the shapes enforce (``extra="forbid"`` everywhere):
- No input carries identity, role, permission or customer scope. The customer comes only
  from the verified principal. An ``rfq_id`` is an untrusted lookup key: the adapter loads
  it with ``get_rfq(..., customer_id=principal.customer_id)`` and answers NOT_FOUND
  whether it is absent or someone else's.
- No input carries an upstream result (InventoryCheck, CostEstimate, ...), a rule version
  or a snapshot pin. Adapters recompute prerequisites in the same snapshot and select
  rules and snapshots server-side (``select_snapshots``).
- No tool approves, releases or persists anything. Every tool is read-only.

Exact numbers reuse the API wire rules (``api.schemas.common``): integers or plain decimal
strings, never JSON floats.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from rfq_copilot.api.auth import VIEW_ASSIGNED_RFQS, VIEW_CATALOG, VIEW_QUOTE
from rfq_copilot.api.schemas.common import Identifier, IsoDate, Money
from rfq_copilot.api.schemas.reads import MaterialListQuery, MaterialPage
from rfq_copilot.api.schemas.rfq import MAX_PARTS, PartSpecification

_STRICT = ConfigDict(extra="forbid", frozen=True)

# Every service quantity exposed here (kg, mm, hours, USD, fractions) is non-negative, so
# Money's exact non-negative decimal-string type is the one wire type for all of them.
ExactDecimal = Money
Text = Annotated[str, Field(max_length=200)]
Codes = tuple[Annotated[str, Field(max_length=100)], ...]

# Server-side pin for every new commercial evaluation. Never an input: historical
# approval-v1.1 replays stay on trusted internal paths (repositories.rule_manifest).
NEW_EVALUATION_APPROVAL_RULE_VERSION = "approval-v1.2"

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def _monday(value: date) -> date:
    # services.lead_time.validate_planning_dates rejects any other weekday.
    if value.weekday() != 0:
        raise ValueError("horizon_start must be a Monday.")
    return value


# --- inputs ------------------------------------------------------------------------------


class GetMaterialInput(BaseModel):
    model_config = _STRICT

    material_id: Identifier


class PartCheckInput(BaseModel):
    """One prospective part; missing fields become clarification findings, not errors."""

    model_config = _STRICT

    part: PartSpecification


class PlanningInput(BaseModel):
    """Exactly one of a stored RFQ (its trusted runtime lines) or prospective parts."""

    model_config = _STRICT

    rfq_id: Identifier | None = None
    parts: (
        Annotated[tuple[PartSpecification, ...], Field(min_length=1, max_length=MAX_PARTS)] | None
    ) = None
    horizon_start: Annotated[IsoDate, AfterValidator(_monday)]

    @model_validator(mode="after")
    def _one_source(self) -> "PlanningInput":
        if (self.rfq_id is None) == (self.parts is None):
            raise ValueError("Give exactly one of rfq_id or parts.")
        return self


class DeliveryPlanningInput(PlanningInput):
    requested_delivery_date: IsoDate | None = None


# --- outputs (mapped from the frozen service dataclasses, field names unchanged) ---------

CheckResult = Literal["valid", "unsupported", "needs_clarification"]


class MaterialSpecOutput(BaseModel):  # services.materials.MaterialSpec
    model_config = _STRICT

    material_id: Identifier
    category: Text
    grade: Text
    density_kg_m3: ExactDecimal
    min_bend_radius_factor: ExactDecimal
    availability_class: Text
    supported_thickness_mm: tuple[ExactDecimal, ...]
    compatible_finishes: Codes
    supported_operations: Codes


class MaterialSuitabilityOutput(BaseModel):  # services.materials.MaterialCheck
    model_config = _STRICT

    result: CheckResult
    findings: Codes
    material: MaterialSpecOutput | None
    rule_version: Identifier
    rule_fingerprint: Text


class ManufacturingFeasibilityOutput(BaseModel):  # services.manufacturing.ManufacturingCheck
    model_config = _STRICT

    result: CheckResult
    findings: Codes
    operations: Codes
    min_bend_radius_mm: ExactDecimal | None
    material: MaterialSuitabilityOutput
    rule_version: Identifier
    rule_fingerprint: Text


class InventoryLineOutput(BaseModel):  # services.inventory.MaterialRequirement
    model_config = _STRICT

    material_id: Identifier
    thickness_mm: ExactDecimal
    part_ids: tuple[Identifier, ...]
    required_kg: ExactDecimal
    available_kg: ExactDecimal
    shortage_kg: ExactDecimal
    status: Literal["sufficient", "insufficient", "unavailable"]
    availability_class: Text
    snapshot_ids: tuple[Identifier, ...]
    supplier_lead_business_days: tuple[int, ...]


class InventoryOutput(BaseModel):  # services.inventory.InventoryCheck
    model_config = _STRICT

    result: Literal["sufficient", "insufficient", "needs_clarification", "not_manufacturable"]
    lines: tuple[InventoryLineOutput, ...]
    scrap_fraction: ExactDecimal
    pricing_rule_version: Identifier
    pricing_rule_fingerprint: Text
    snapshot_id: Identifier | None


class WeekLoadOutput(BaseModel):  # services.capacity.WeekLoad
    model_config = _STRICT

    workcentre_id: Identifier
    week_start: date
    remaining_hours: ExactDecimal
    planned_hours: ExactDecimal


class CapacityOutput(BaseModel):  # services.capacity.CapacityCheck
    model_config = _STRICT

    result: Literal["feasible", "infeasible", "needs_clarification", "not_manufacturable"]
    reason: Literal["NO_CAPACITY_IN_HORIZON", "MISSING_CAPACITY_DATA"] | None
    weeks: tuple[WeekLoadOutput, ...]
    horizon_start: date
    start_week: int
    final_week_start: date | None
    capacity_snapshot_ids: tuple[Identifier, ...]
    pricing_rule_version: Identifier
    pricing_rule_fingerprint: Text
    snapshot_id: Identifier | None


class LeadTimeOutput(BaseModel):  # services.lead_time.LeadTimeEstimate
    model_config = _STRICT

    result: Literal["estimated", "infeasible", "needs_clarification", "not_manufacturable"]
    estimated_ready_date: date | None
    lead_business_days: int | None
    procurement_business_days: int
    horizon_start: date
    earliest_start_date: date | None
    start_week: int | None
    requested_delivery_date: date | None
    delivery_feasible: bool | None
    findings: Codes
    inventory_snapshot_id: Identifier | None  # snapshots.inventory.snapshot_id
    capacity_snapshot_id: Identifier | None  # snapshots.capacity.snapshot_id


class CostLineOutput(BaseModel):  # services.costing.LineCost
    model_config = _STRICT

    part_id: Identifier
    quantity: int
    operations: Codes
    material_cost_per_unit: ExactDecimal
    labour_cost_per_unit: ExactDecimal
    finish_cost_per_unit: ExactDecimal
    packaging_cost_per_unit: ExactDecimal
    variable_cost_per_unit: ExactDecimal
    setup_cost_per_line: ExactDecimal
    total_manufacturing_cost: ExactDecimal


class CostOutput(BaseModel):  # services.costing.CostEstimate
    model_config = _STRICT

    result: Literal["costed", "needs_clarification", "not_manufacturable"]
    lines: tuple[CostLineOutput, ...]
    total_manufacturing_cost: ExactDecimal | None
    pricing_rule_version: Identifier
    pricing_rule_fingerprint: Text


class PricedLineOutput(BaseModel):  # services.pricing.PricedLine
    model_config = _STRICT

    part_id: Identifier  # cost.part_id
    quoted_unit_price_usd: ExactDecimal
    line_total_usd: ExactDecimal


class PriceOutput(BaseModel):  # services.pricing.PriceCalculation
    """A deterministic calculation: not a quote, not persisted, not approved, not released."""

    model_config = _STRICT

    result: Literal["priced", "needs_clarification", "not_manufacturable"]
    calculation_id: Identifier | None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    gross_margin: ExactDecimal
    lines: tuple[PricedLineOutput, ...]
    quote_total_usd: ExactDecimal | None
    tax_included: bool
    freight_included: bool
    shipping_included: bool
    pricing_rule_version: Identifier
    pricing_rule_fingerprint: Text


class QuotationLineOutput(BaseModel):  # services.quotation.LineOutcome
    model_config = _STRICT

    line_number: int
    part_id: Identifier
    manufacturing_result: CheckResult  # manufacturing.result
    findings: Codes  # manufacturing.findings
    stock_status: Text | None
    procurement_business_days: int | None
    line_total_usd: ExactDecimal | None  # priced.line_total_usd


class QuotationOutput(BaseModel):  # services.quotation.Quotation
    """A prospective multi-part evaluation. It creates no draft and cannot be released."""

    model_config = _STRICT

    result: Literal["quoted", "needs_clarification", "not_manufacturable"]
    lines: tuple[QuotationLineOutput, ...]
    blocking_lines: tuple[int, ...]
    calculation_id: Identifier | None
    quote_total_usd: ExactDecimal | None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None
    estimated_ready_date: date | None
    lead_business_days: int | None
    findings: Codes


# --- errors ------------------------------------------------------------------------------


class ErrorCode(StrEnum):
    """Tool failures. A business rejection (unsupported, needs_clarification, insufficient,
    infeasible, not_manufacturable) is NOT an error: it is a successful structured result
    whose ``result`` and ``findings`` say why."""

    INVALID_INPUT = "INVALID_INPUT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"  # absent or not visible to this customer: indistinguishable
    SNAPSHOT_CONFLICT = "SNAPSHOT_CONFLICT"  # SnapshotMismatchError / RuleIntegrityError
    INTERNAL = "INTERNAL"


# Fixed texts: no caller input, exception text, SQL, path or identifier is ever echoed.
SAFE_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.INVALID_INPUT: "Invalid tool arguments.",
    ErrorCode.UNAUTHENTICATED: "Authentication required.",
    ErrorCode.PERMISSION_DENIED: "Permission denied.",
    ErrorCode.NOT_FOUND: "Resource not found or not visible.",
    ErrorCode.SNAPSHOT_CONFLICT: "Planning data or rule versions are inconsistent; retry later.",
    ErrorCode.INTERNAL: "Internal error.",
}


def tool_failure(code: ErrorCode) -> ToolError:
    """The SDK-native exception for a failure; Day 25+ adapters raise it.

    Every code is a tool execution error (``is_error=True``) with a fixed "CODE: message"
    text, matching mcp 2.3.0: its high-level server reports argument-validation failures
    as tool errors too (not JSON-RPC -32602). Missing or invalid bearer tokens are refused
    by the transport with 401 before any tool runs; UNAUTHENTICATED covers a principal that
    becomes unavailable mid-call.
    """
    code = ErrorCode(code)
    return ToolError(f"{code.value}: {SAFE_MESSAGES[code]}")


# --- inventory ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolContract:
    name: str
    server: Literal["manufacturing", "supply", "pricing"]
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    permission: str  # rbac-policy-v2 name; MCP runs under v2 only (F1)
    service: str  # the existing entry point Day 25/26 calls
    rfq_scoped: bool  # rfq_id is ownership-checked against the principal's customer


_RFQ = "Give rfq_id (a stored RFQ visible to you) or parts. horizon_start is a Monday."

TOOLS: tuple[ToolContract, ...] = (
    ToolContract(
        "search_materials",
        "manufacturing",
        "Search the material catalog by category, grade, thickness and finish.",
        MaterialListQuery,
        MaterialPage,
        VIEW_CATALOG,
        "repositories.queries.find_materials",
        False,
    ),
    ToolContract(
        "get_material",
        "manufacturing",
        "Get one catalog material's specification.",
        GetMaterialInput,
        MaterialSpecOutput,
        VIEW_CATALOG,
        "services.materials.get_material_spec",
        False,
    ),
    ToolContract(
        "check_material_suitability",
        "manufacturing",
        "Check a part's material, thickness and finish against the pinned manufacturing rules.",
        PartCheckInput,
        MaterialSuitabilityOutput,
        VIEW_CATALOG,
        "services.materials.check_part_material",
        False,
    ),
    ToolContract(
        "check_manufacturing_feasibility",
        "manufacturing",
        "Check whether a part can be made and which operations it routes through.",
        PartCheckInput,
        ManufacturingFeasibilityOutput,
        VIEW_CATALOG,
        "services.manufacturing.check_manufacturing_capability",
        False,
    ),
    ToolContract(
        "check_inventory",
        "supply",
        "Check material stock for the parts against the server-selected inventory snapshot. "
        + _RFQ,
        PlanningInput,
        InventoryOutput,
        VIEW_ASSIGNED_RFQS,
        "services.inventory.check_inventory",
        True,
    ),
    ToolContract(
        "check_capacity",
        "supply",
        "Schedule the parts' operations into workcentre capacity weeks. " + _RFQ,
        PlanningInput,
        CapacityOutput,
        VIEW_ASSIGNED_RFQS,
        "services.capacity.check_capacity",
        True,
    ),
    ToolContract(
        "estimate_lead_time",
        "supply",
        "Estimate the ready date and whether a requested delivery date is feasible. " + _RFQ,
        DeliveryPlanningInput,
        LeadTimeOutput,
        VIEW_ASSIGNED_RFQS,
        "services.lead_time.estimate_lead_time",
        True,
    ),
    ToolContract(
        "estimate_costs",
        "pricing",
        "Estimate manufacturing cost per line under the pinned pricing rules. " + _RFQ,
        PlanningInput,
        CostOutput,
        VIEW_QUOTE,
        "services.costing.estimate_costs",
        True,
    ),
    ToolContract(
        "calculate_price",
        "pricing",
        "Calculate the deterministic, non-binding price. Not a quote; nothing is saved. " + _RFQ,
        PlanningInput,
        PriceOutput,
        VIEW_QUOTE,
        "services.pricing.calculate_quote",
        True,
    ),
    ToolContract(
        "evaluate_quotation",
        "pricing",
        "Evaluate a multi-part quotation: feasibility, schedule, cost and price. "
        "Creates no draft and cannot approve or release. " + _RFQ,
        DeliveryPlanningInput,
        QuotationOutput,
        VIEW_QUOTE,
        "services.quotation.quote_parts / quote_rfq",
        True,
    ),
)
