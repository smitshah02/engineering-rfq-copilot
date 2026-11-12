"""Day 24 MCP tool contracts: shapes, precision, forged-field rejection, errors, SDK schemas."""

import json
from decimal import Decimal

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ValidationError

from rfq_copilot.api.auth import ROUTE_PERMISSIONS
from rfq_copilot.mcp import contracts as c
from rfq_copilot.repositories.rule_manifest import TRUSTED_RULES

PART = {"part_id": "P-1", "material_id": "AL5052", "thickness_mm": "2.0", "quantity": 10}
PLANNING = {"parts": [PART], "horizon_start": "2026-11-16"}  # a Monday
INPUTS = {
    c.GetMaterialInput: {"material_id": "AL5052"},
    c.PartCheckInput: {"part": PART},
    c.PlanningInput: PLANNING,
    c.DeliveryPlanningInput: {**PLANNING, "requested_delivery_date": "2026-12-01"},
}
INPUT_MODELS = sorted({t.input_model for t in c.TOOLS}, key=lambda m: m.__name__)
OUTPUT_MODELS = sorted({t.output_model for t in c.TOOLS}, key=lambda m: m.__name__)
FORBIDDEN = [
    # forged identity / scope
    "customer_id",
    "user_id",
    "role",
    "roles",
    "permissions",
    "principal",
    "tenant",
    # agent-supplied upstream results and pins
    "inventory",
    "costs",
    "capacity",
    "pricing",
    "quotation",
    "inventory_snapshot",
    "capacity_snapshot",
    "manufacturing_rules",
    "pricing_rules",
    # approval-policy overrides and release
    "approval_rules",
    "approval_rule_version",
    "approve",
    "release",
    "persist",
]


def _fields(model: type[BaseModel], seen=None) -> set[str]:
    """All field names, recursively through nested models."""
    seen = set() if seen is None else seen
    names = set()
    for name, info in model.model_fields.items():
        names.add(name)
        for arg in [info.annotation, *getattr(info.annotation, "__args__", ())]:
            for inner in [arg, *getattr(arg, "__args__", ())]:
                if isinstance(inner, type) and issubclass(inner, BaseModel) and inner not in seen:
                    seen.add(inner)
                    names |= _fields(inner, seen)
    return names


def test_inventory_is_complete_unique_and_read_only():
    names = [t.name for t in c.TOOLS]
    assert len(names) == len(set(names)) == 10
    assert {t.server for t in c.TOOLS} == {"manufacturing", "supply", "pricing"}
    assert set(t.permission for t in c.TOOLS) <= set(ROUTE_PERMISSIONS["v2"].values())
    assert c.READ_ONLY.read_only_hint and not c.READ_ONLY.destructive_hint
    for t in c.TOOLS:
        assert t.rfq_scoped == ("rfq_id" in t.input_model.model_fields)


@pytest.mark.parametrize("model", INPUT_MODELS, ids=lambda m: m.__name__)
def test_valid_inputs(model):
    assert model.model_validate(INPUTS.get(model, {})).model_dump()


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"horizon_start": "2026-11-16"}, "neither source"),
        ({**PLANNING, "rfq_id": "RFQ-1"}, "both sources"),
        ({"parts": [], "horizon_start": "2026-11-16"}, "empty parts"),
        ({"parts": [PART], "horizon_start": "2026-11-17"}, "not a Monday"),
        ({"parts": [PART], "horizon_start": "16/11/2026"}, "bad date"),
        ({"parts": [PART]}, "missing horizon"),
        ({"parts": [{**PART, "thickness_mm": 2.0}], "horizon_start": "2026-11-16"}, "float"),
        ({"parts": [{**PART, "thickness_mm": "1e3"}], "horizon_start": "2026-11-16"}, "exponent"),
        ({"parts": [PART] * 51, "horizon_start": "2026-11-16"}, "too many parts"),
        ({"rfq_id": " ", "horizon_start": "2026-11-16"}, "blank id"),
    ],
)
def test_invalid_planning_input(payload, why):
    with pytest.raises(ValidationError):
        c.PlanningInput.model_validate(payload)


@pytest.mark.parametrize("model", INPUT_MODELS, ids=lambda m: m.__name__)
@pytest.mark.parametrize("field", FORBIDDEN)
def test_forged_and_override_fields_are_rejected(model, field):
    assert field not in _fields(model)
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        model.model_validate({**INPUTS.get(model, {}), field: "x"})


def test_approval_policy_is_server_pinned_and_registered():
    assert c.NEW_EVALUATION_APPROVAL_RULE_VERSION == "approval-v1.2"
    assert "approval-v1.2" in TRUSTED_RULES and "approval-v1.1" in TRUSTED_RULES
    for model in INPUT_MODELS:
        assert not any("approval" in f or "rule" in f for f in _fields(model))


@pytest.mark.parametrize("model", OUTPUT_MODELS, ids=lambda m: m.__name__)
def test_outputs_carry_no_approval_release_or_identity_fields(model):
    leaked = {
        f
        for f in _fields(model)
        if any(w in f for w in ("approv", "release", "persist", "draft", "customer", "role"))
    }
    assert not leaked


def test_no_tool_can_approve_release_or_persist():
    words = ("approve", "release", "persist", "submit", "draft", "upload", "delete", "update")
    for t in c.TOOLS:
        assert not any(w in t.name for w in words)


def test_exact_decimals_round_trip_as_strings_never_floats():
    line = c.PricedLineOutput(
        part_id="P-1", quoted_unit_price_usd="12.3450", line_total_usd=Decimal("123.45")
    )
    wire = json.loads(line.model_dump_json())
    assert wire == {
        "part_id": "P-1",
        "quoted_unit_price_usd": "12.3450",
        "line_total_usd": "123.45",
    }
    for bad in (12.5, "-1", "NaN", True):
        with pytest.raises(ValidationError):
            c.PricedLineOutput(part_id="P", quoted_unit_price_usd=bad, line_total_usd=1)


def test_nested_output_validates():
    spec = dict(
        material_id="AL5052",
        category="aluminium",
        grade="5052",
        density_kg_m3="2680",
        min_bend_radius_factor="1.0",
        availability_class="stock",
        supported_thickness_mm=["1.5", "2.0"],
        compatible_finishes=["anodize"],
        supported_operations=["laser_cut"],
    )
    out = c.ManufacturingFeasibilityOutput(
        result="valid",
        findings=[],
        operations=["laser_cut"],
        min_bend_radius_mm="2.000",
        material=dict(
            result="valid", findings=[], material=spec, rule_version="mfg-v1", rule_fingerprint="ab"
        ),
        rule_version="mfg-v1",
        rule_fingerprint="ab",
    )
    assert out.material.material.supported_thickness_mm == (Decimal("1.5"), Decimal("2.0"))
    with pytest.raises(ValidationError):
        c.ManufacturingFeasibilityOutput.model_validate({**out.model_dump(), "result": "approved"})


@pytest.mark.parametrize("code", list(c.ErrorCode))
def test_error_contract_is_sdk_native_and_safe(code):
    err = c.tool_failure(code)
    assert isinstance(err, ToolError) and str(err) == f"{code.value}: {c.SAFE_MESSAGES[code]}"
    text = str(err)
    for leak in ("Traceback", "SELECT", "postgres", "password", "/Users", "CUST-"):
        assert leak not in text
    assert set(c.SAFE_MESSAGES) == set(c.ErrorCode)


@pytest.mark.asyncio
async def test_sdk_generates_input_and_output_schemas_for_every_contract():
    server = MCPServer("contract-check")  # in-memory only: no transport, nothing exposed
    for t in c.TOOLS:

        async def fn(request):  # never called
            raise AssertionError

        fn.__annotations__ = {"request": t.input_model, "return": t.output_model}
        server.add_tool(fn, name=t.name, description=t.description, annotations=c.READ_ONLY)
    listed = {tool.name: tool for tool in await server.list_tools()}
    assert set(listed) == {t.name for t in c.TOOLS}
    for t in c.TOOLS:
        tool = listed[t.name]
        assert tool.output_schema is not None
        assert tool.annotations.read_only_hint is True
        dumped = json.dumps(tool.input_schema)
        assert "customer_id" not in dumped and "approval" not in dumped
    # SDK-side validation enforces extra="forbid": a forged role never reaches a tool body.
    with pytest.raises(ToolError, match="extra_forbidden"):
        await server.call_tool("get_material", {"request": {"material_id": "X", "role": "admin"}})
