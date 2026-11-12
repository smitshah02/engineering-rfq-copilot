"""Day 35: grounding guard and flat planning-tool schema. Deterministic: scripted models,
mocked executors; the model outputs below are the ones Qwen3 4B produced on Day 34."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_agent_tools import BASE, INSTRUCTIONS, MCP_AUDIENCE, REST_AUDIENCE, SPEC
from test_llm import fake
from test_mcp_client import Fixed

from rfq_copilot import graph as g
from rfq_copilot import grounding, llm
from rfq_copilot.api.auth import Principal
from rfq_copilot.api.mcp_integration import McpIntegration
from rfq_copilot.mcp import contracts as c
from rfq_copilot.mcp.client import McpToolClient

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.filterwarnings("ignore:The test <Function:pytest.PytestWarning"),  # sync tests here
]
MAT_009 = {
    **SPEC,
    "material_id": "MAT-009",
    "category": "stainless_steel",
    "grade": "430",
    "density_kg_m3": "7750.000000",
    "availability_class": "stocked",
    "min_bend_radius_factor": "1.000",
    "supported_thickness_mm": ["1.000", "1.500", "2.000", "3.000", "4.000", "6.000"],
    "compatible_finishes": ["brushed", "none", "passivated"],
    "supported_operations": ["bend", "finish", "inspect", "laser", "pack", "weld"],
}  # authoritative get_material(MAT-009) output from the Day 34 dev smoke run
FEASIBILITY = {
    "result": "needs_clarification",
    "findings": ["MISSING_FINISH_ID"],
    "operations": [],
    "min_bend_radius_mm": None,
    "material": {
        "result": "needs_clarification",
        "findings": ["MISSING_FINISH_ID"],
        "material": MAT_009,
        "rule_version": "mfg-v1.0",
        "rule_fingerprint": "f" * 16,
    },
    "rule_version": "mfg-v1.0",
    "rule_fingerprint": "f" * 16,
}
PART = {"part_id": "1", "part_type": "l_bracket", "material_id": "MAT-009", "length_mm": "450", "width_mm": "490", "height_mm": "170", "thickness_mm": "1.0", "tolerance_mm": "0.5", "bend_radius_mm": "1.50", "quantity": 85, "hole_count": 11}  # fmt: skip
# Exactly what Qwen3 4B sent to estimate_costs on Day 34 (twice, deterministically).
QWEN_COST_ARGS = {"horizon_start": "2026-11-09", "rfq_id": {}, "parts": {**PART, "finish_id": None}}  # fmt: skip


def tool(name, args, id="c1"):
    return {"name": name, "args": args, "id": id}


def returning(output):
    async def execute(name, arguments):
        return output

    return execute


async def run(replies, tools, output=None, question="q"):
    graph = g.build_graph(fake(*replies), INSTRUCTIONS, tools)
    execute = returning(output) if output is not None else None
    return await g.run_graph(graph, question, execute=execute)


# --- fabricated follow-up density -------------------------------------------------------


async def test_follow_up_density_without_a_tool_is_refused_and_not_remembered(monkeypatch):
    async def get_material(self, principal, name, request, *, rest_credential):
        return c.MaterialSpecOutput.model_validate(MAT_009)

    monkeypatch.setattr(McpIntegration, "call", get_material)
    model = fake(
        AIMessage("", tool_calls=[tool("get_material", {"material_id": "MAT-009"})]),
        AIMessage("The finishes compatible with MAT-009 are: brushed, none, and passivated."),
        AIMessage("The density of MAT-009 is 7.85 g/cm³."),  # Day 34 hallucination
        AIMessage("Its density is 7750 kg/m³."),  # copied from history: still no evidence
    )
    integration = McpIntegration(McpToolClient(BASE), Fixed("unused"), MCP_AUDIENCE, REST_AUDIENCE)  # fmt: skip
    convo = g.Conversations(model, INSTRUCTIONS, ["get_material"], checkpointer=InMemorySaver(serde=g.SAFE_SERDE), mcp=integration)  # fmt: skip
    alice = Principal("CUST-A", frozenset(), user_id="U-1", subject="S1")
    first = await convo.ask(alice, "Which finishes are compatible with MAT-009?", rest_credential="r")  # fmt: skip
    assert (first.result.status, first.result.grounding) == ("answered", "rendered")
    assert "compatible_finishes=brushed, none, passivated" in first.result.reply
    assert "The finishes compatible" not in first.result.reply  # facts never from prose
    for question in ("And what is that material's density?", "And its density?"):
        turn = await convo.ask(alice, question, first.thread_id, rest_credential="r")
        # Phase 5: the model's density (fabricated or copied from history) is never shown.
        assert (turn.result.reply, turn.result.grounding) == (g.GUIDANCE, "guidance")
        assert "7.85" not in turn.result.message and "7750 kg" not in turn.result.message
    history = await convo.history(alice, first.thread_id)
    assert [text for role, text in history if role == "ai"][1:] == [g.GUIDANCE, g.GUIDANCE]


# --- needs_clarification vs unsupported ---------------------------------------------------


async def test_needs_clarification_misread_as_infeasible_falls_back_to_rendered_status():
    qwen = "No, item 1 cannot be manufactured as the finish is not specified."  # Day 34 reply
    result = await run(
        [
            AIMessage("", tool_calls=[tool("check_manufacturing_feasibility", {"part": PART})]),
            AIMessage(qwen),
        ],  # fmt: skip
        ["check_manufacturing_feasibility"],
        c.ManufacturingFeasibilityOutput.model_validate(FEASIBILITY),
    )
    assert (result.status, result.grounding) == ("answered", "rendered")
    assert "cannot be manufactured" not in result.reply
    assert "result=needs_clarification; findings=MISSING_FINISH_ID" in result.reply


# --- invalid cost-tool arguments and the flat schema --------------------------------------


async def test_day34_qwen_cost_arguments_are_rejected_before_execution():
    seen = []

    async def execute(name, arguments):
        seen.append(name)

    graph = g.build_graph(fake(AIMessage("", tool_calls=[tool("estimate_costs", QWEN_COST_ARGS)])), INSTRUCTIONS, ["estimate_costs"])  # fmt: skip
    result = await g.run_graph(graph, "q", execute=execute)
    assert (result.error, seen) == ("invalid_tool_call", [])


def test_flat_arguments_convert_to_the_unchanged_contract():
    args = llm.contract_arguments("estimate_costs", {**PART, "horizon_start": "2026-11-09", "rfq_id": {}, "finish_id": None})  # fmt: skip
    assert isinstance(args, c.PlanningInput) and args.rfq_id is None
    part = args.parts[0]
    assert (part.part_id, part.material_id, part.quantity, part.hole_count) == (
        "1",
        "MAT-009",
        85,
        11,
    )
    assert args.parts[0].finish_id is None  # "not stated" stays unstated: nothing invented
    stored = llm.contract_arguments("estimate_lead_time", {"rfq_id": "RFQ-0001", "horizon_start": "2026-11-09", "requested_delivery_date": "2026-12-22"})  # fmt: skip
    assert isinstance(stored, c.DeliveryPlanningInput) and stored.parts is None


@pytest.mark.parametrize(
    "args",
    [
        {**PART, "horizon_start": "2026-11-09", "customer_id": "CUST-B"},  # forged scope
        {**PART, "horizon_start": "2026-11-09", "role": "admin"},  # forged role
        {**PART, "horizon_start": "2026-11-09", "permissions": ["view_quote"]},
        {**PART, "horizon_start": "2026-11-09", "token": "x"},
        {"material_id": "MAT-009", "quantity": 85, "horizon_start": "2026-11-09"},  # no part_id
        {**PART, "horizon_start": "2026-11-09", "rfq_id": "RFQ-0001"},  # two sources
        {"horizon_start": "2026-11-09"},  # no source
        {**PART},  # no horizon: never defaulted
        {**PART, "horizon_start": "2026-11-10"},  # not a Monday
        {**PART, "horizon_start": "2026-11-09", "quantity": 0},  # contract still validates
        QWEN_COST_ARGS,  # nested parts are not part of the flat view
    ],
)
def test_flat_view_never_invents_or_accepts_identity(args):
    with pytest.raises(Exception) as caught:
        llm.contract_arguments("estimate_costs", args)
    assert type(caught.value).__name__ == "ValidationError"


# --- unsupported claims and ordinary replies ----------------------------------------------


def test_the_model_is_shown_the_flat_schema():
    model = fake()
    llm.bind_contract_tools(model, ["estimate_costs", "get_material"])
    shown = {t["function"]["name"]: t["function"]["parameters"] for t in model.bound}
    assert shown["estimate_costs"] == llm.model_facing("estimate_costs").model_json_schema()
    assert shown["get_material"] == c.GetMaterialInput.model_json_schema()


def test_flat_decimals_are_plain_decimal_strings():
    props = llm.model_facing("estimate_costs").model_json_schema()["properties"]
    assert props["length_mm"]["type"] == "string" and "anyOf" not in props["length_mm"]
    for bad in ({"$numberDecimal": "450"}, 450.5, "4.5e2"):  # Day 35 Qwen output; floats
        with pytest.raises(Exception):
            llm.contract_arguments("estimate_costs", {**PART, "horizon_start": "2026-11-09", "length_mm": bad})  # fmt: skip


# --- Day 35 final: facts only from the rendering ------------------------------------------


def test_flat_cost_schema_has_no_nesting_null_branches_or_required_dates():
    schema = llm.model_facing("estimate_costs").model_json_schema()
    text = json.dumps(schema)
    assert "$defs" not in schema and '"null"' not in text and "parts" not in schema["properties"]
    assert schema["additionalProperties"] is False and "required" not in schema
    assert "application supplies" in schema["properties"]["horizon_start"]["description"]
    assert {"rfq_id", "part_id", "material_id", "quantity"} <= schema["properties"].keys()
    assert llm.model_facing("get_material") is c.GetMaterialInput


# --- Day 35 final: scheduling horizon provenance ------------------------------------------

POLICY = date(2026, 11, 9)
COST_OK = c.CostOutput.model_validate({"result": "needs_clarification", "lines": [], "total_manufacturing_cost": None, "pricing_rule_version": "price-v1.0", "pricing_rule_fingerprint": "f"})  # fmt: skip


async def plan(args, question="Estimate the cost of item 1.", horizon=POLICY, name="estimate_costs", output=None):  # fmt: skip
    seen = []

    async def execute(tool_name, arguments):
        seen.append(arguments)
        return output or (COST_OK if tool_name == "estimate_costs" else LEAD)

    graph = g.build_graph(fake(AIMessage("", tool_calls=[tool(name, args)]), AIMessage("Done.")), INSTRUCTIONS, [name])  # fmt: skip
    result = await g.run_graph(graph, question, execute=execute, horizon_start=horizon)
    return result, seen


LEAD = c.LeadTimeOutput.model_construct()


@pytest.mark.parametrize(
    "args, question, horizon",
    [
        (
            {**PART, "horizon_start": "2026-09-07"},
            "Estimate the cost of item 1.",
            POLICY,
        ),  # model-chosen Monday
        (
            {**PART, "horizon_start": "2026-09-06"},
            "Estimate the cost of item 1.",
            POLICY,
        ),  # Day 35 Qwen (Sunday)
        (
            {**PART, "horizon_start": "2026-11-09"},
            "Estimate the cost of item 1.",
            None,
        ),  # no policy, not stated
        ({**PART}, "Estimate the cost of item 1.", None),  # missing and no policy: never invented
    ],
)
async def test_unsupported_or_missing_horizons_are_rejected(args, question, horizon):
    result, seen = await plan(args, question, horizon)
    assert (result.error, seen) == ("invalid_tool_call", [])


async def test_delivery_date_must_be_user_stated():
    invented, seen = await plan({**PART, "requested_delivery_date": "2026-12-01"}, name="estimate_lead_time")  # fmt: skip
    assert (invented.error, seen) == ("invalid_tool_call", [])
    _, seen = await plan({**PART, "requested_delivery_date": "2026-12-22"}, "Lead time for item 1? Requested delivery date 2026-12-22.", name="estimate_lead_time")  # fmt: skip
    assert seen[0].requested_delivery_date == date(2026, 12, 22)


# --- Day 35 final: "finish none" normalization ---------------------------------------------


@pytest.mark.parametrize(
    "question, model_finish, expected",
    [
        ("Item 1: l bracket; material MAT-009; finish none; holes 11", None, "none"),  # Day 34 Qwen
        ("Item 1: l bracket, no finish required", None, "none"),
        ("Item 1: finish none. Item 2: finish brushed", None, None),  # ambiguous: untouched
        ("Item 1: l bracket; material MAT-009", None, None),  # not stated: not invented
        ("Item 1: finish none", "brushed", "brushed"),  # never overrides a model value
    ],
)
async def test_finish_none_is_normalized_only_when_unambiguous(question, model_finish, expected):
    part = {**PART, "finish_id": model_finish}
    seen = []

    async def execute(name, arguments):
        seen.append(arguments)
        return c.ManufacturingFeasibilityOutput.model_validate(FEASIBILITY)

    graph = g.build_graph(fake(AIMessage("", tool_calls=[tool("check_manufacturing_feasibility", {"part": part})]), AIMessage("Done.")), INSTRUCTIONS, ["check_manufacturing_feasibility"])  # fmt: skip
    await g.run_graph(graph, question, execute=execute)
    assert seen[0].part.finish_id == expected
    _, planned = await plan({**PART, "finish_id": model_finish}, question)
    assert planned[0].parts[0].finish_id == expected  # flat planning view too


def test_none_is_a_master_data_finish():
    import csv
    from pathlib import Path

    rows = csv.DictReader((Path(__file__).resolve().parents[1] / "data/rfq_synthetic_data/master_data/finishes.csv").open())  # fmt: skip
    assert "none" in {row["finish_id"] for row in rows}


# === Phase 5 final remediation =============================================================

from types import SimpleNamespace  # noqa: E402

from test_agent_tools import Agent  # noqa: E402

PRICING_RULE = SimpleNamespace(
    rule_version="price-v1.0",
    document=json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "data/rfq_synthetic_data/config/pricing_rules.json"
        ).read_text()
    ),  # fmt: skip
)
COSTED = {
    "result": "costed",
    "lines": [
        {
            "part_id": "1",
            "quantity": 85,
            "operations": ["laser", "bend"],
            "material_cost_per_unit": "16.8766976",
            "labour_cost_per_unit": "3.141666666666666666666666667",
            "finish_cost_per_unit": "0",
            "packaging_cost_per_unit": "0.65",
            "variable_cost_per_unit": "20.66836426666666666666666667",
            "setup_cost_per_line": "85",
            "total_manufacturing_cost": "1841.81096266666666666666666667",
        }
    ],  # fmt: skip
    "total_manufacturing_cost": "1841.81096266666666666666666667",
    "pricing_rule_version": "price-v1.0",
    "pricing_rule_fingerprint": "f" * 16,
}


# --- Defect 1: tool-free business conclusions ----------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "It costs about seven hundred dollars.",  # written-out number
        "85 dollars each.",  # a number reused from the question
        "We can make this part.",  # feasibility without digits
        "Approved.",  # approval assertion
        "Yes, MAT-009 works fine for this and it is in stock.",
        "The quote comes to roughly two thousand USD.",
        "The density of MAT-009 is 7.85 g/cm³.",  # Day 34 hallucination
        "It needs clarification: the finish is missing.",
        "Hello! Happy to help.",  # even harmless text is not shown: deterministic guidance
    ],
)
async def test_tool_free_model_text_is_never_shown(reply):
    result = await run([AIMessage(reply)], ["get_material"], question="How much for 85 brackets of MAT-009?")  # fmt: skip
    assert (result.status, result.reply, result.grounding) == ("answered", grounding.GUIDANCE, "guidance")  # fmt: skip
    assert reply not in result.message and result.results == ()


async def test_unexecuted_proposals_carry_no_model_text():
    graph = g.build_graph(fake(AIMessage("Approved, it costs 700.", tool_calls=[tool("get_material", {"material_id": "M"})])), INSTRUCTIONS, ["get_material"])  # fmt: skip
    result = await g.run_graph(graph, "q")  # no executor: proposal-only mode
    assert (result.status, result.reply) == ("proposed", None)


# --- Defect 2: contradicting post-tool prose -----------------------------------------------


@pytest.mark.parametrize("status", ["valid", "needs_clarification", "unsupported"])
async def test_post_tool_answer_is_rendered_without_any_model_prose(status):
    model = Agent(calls=[tool("check_manufacturing_feasibility", {"part": PART})], final=AIMessage("Good news, we can make it!"))  # fmt: skip
    output = c.ManufacturingFeasibilityOutput.model_validate({**FEASIBILITY, "result": status})
    result = await g.run_graph(g.build_graph(model, INSTRUCTIONS, ["check_manufacturing_feasibility"]), "Can item 1 be made?", execute=returning(output))  # fmt: skip
    assert len(model.inputs) == 1  # no model call after tools: no prose pathway exists
    assert (result.status, result.grounding) == ("answered", "rendered")
    assert result.reply.startswith("Verified tool data:\n- check_manufacturing_feasibility: ")
    assert f": result={status};" in result.reply and "we can make it" not in result.reply
    assert "material.material.grade=430" in result.reply  # nested results preserved
    assert "fingerprint" not in result.reply


async def test_costed_status_and_nested_lines_render_exactly():
    result = await plan({**PART}, horizon=POLICY, output=c.CostOutput.model_validate(COSTED))
    assert "- estimate_costs: result=costed; lines[0].part_id=1; lines[0].quantity=85" in result[0].reply  # fmt: skip


@pytest.mark.parametrize(
    "code, text",
    [
        ("forbidden", "You are not permitted to retrieve this information."),
        ("not_found", "The requested record was not found or is not visible to you."),
        ("unauthenticated", "Your identity could not be verified for this request."),
        ("timeout", "The data service did not respond in time; nothing was changed."),
    ],
)
async def test_tool_failures_have_fixed_sanitized_messages(code, text):
    from rfq_copilot.mcp.client import McpCallError

    async def failing(name, arguments):
        raise McpCallError(code)

    graph = g.build_graph(fake(AIMessage("", tool_calls=[tool("get_material", {"material_id": "M"})])), INSTRUCTIONS, ["get_material"])  # fmt: skip
    result = await g.run_graph(graph, "q", execute=failing)
    assert (result.reply, result.error, result.tool_error, result.message) == (None, "tool_failed", code, text)  # fmt: skip


# --- Defect 3: cross-item finish contamination ---------------------------------------------

ITEM2 = {"part_id": "2", "part_type": "flat_panel", "material_id": "MAT-009", "thickness_mm": "2.0"}


async def feasibility_finish(question, part):
    seen = []

    async def execute(name, arguments):
        seen.append(arguments)
        return c.ManufacturingFeasibilityOutput.model_validate(FEASIBILITY)

    graph = g.build_graph(fake(AIMessage("", tool_calls=[tool("check_manufacturing_feasibility", {"part": part})])), INSTRUCTIONS, ["check_manufacturing_feasibility"])  # fmt: skip
    await g.run_graph(graph, question, execute=execute)
    return seen[0].part.finish_id


@pytest.mark.parametrize(
    "question, part, expected",
    [
        (
            "Item 1: l bracket; material MAT-009; finish none. Item 2: flat panel; material MAT-009; thickness 2.0 mm",
            ITEM2,
            None,
        ),  # audit reproducer  # fmt: skip
        (
            "Item 1: finish none. Item 2: finish none.",
            ITEM2,
            None,
        ),  # multi-item: no implicit mapping  # fmt: skip
        ("Item 1: finish brushed. Item 2: flat panel, MAT-009", ITEM2, None),  # mixed
        ("Line 1 bracket, unfinished; line 2 panel", ITEM2, None),  # other item markers
        (
            "Flat panel MAT-009 2.0 mm, finish none, qty 10",
            ITEM2,
            "none",
        ),  # single item, explicit  # fmt: skip
        ("Item 1: flat panel MAT-009, unfinished", ITEM2, "none"),  # explicitly unfinished
        ("Item 1: flat panel MAT-009, no finish", ITEM2, "none"),
        (
            "Item 1: flat panel MAT-009, thickness 2.0 mm",
            ITEM2,
            None,
        ),  # unspecified stays missing  # fmt: skip
        ("Flat panel, finish none or brushed?", ITEM2, None),  # ambiguous: two finishes named
        ("Item 1: finish none", {**ITEM2, "finish_id": "brushed"}, "brushed"),  # never overrides
        ("10 panels, finish none, and 5 brackets", ITEM2, None),  # two parts, no item numbers
        ("Mounting plate MAT-009 and an enclosure, unfinished", ITEM2, None),
        (
            "Flat panel MAT-009, finish none; repeat: finish none",
            ITEM2,
            "none",
        ),  # consistent repeat  # fmt: skip
        (
            "Flat panel MAT-009, finish none; later changed to finish brushed",
            ITEM2,
            None,
        ),  # conflicting  # fmt: skip
        (
            "Flat panel MAT-009; 2 panels, finish none",
            ITEM2,
            "none",
        ),  # same kind twice: one part  # fmt: skip
        (
            "Quote the power distribution panels.\nItem 1: mounting plate; MAT-009; finish none",
            ITEM2,
            "none",
        ),  # RFQ-0071 shape: one numbered item; context nouns do not count as parts
    ],
)
async def test_finish_none_only_for_explicit_single_item_requests(question, part, expected):
    assert await feasibility_finish(question, part) == expected


# --- Defect 4: planning-horizon provenance -------------------------------------------------


@pytest.mark.parametrize(
    "args, question, policy, error",
    [
        (
            {**PART},
            "Customer meeting on 2026-11-23. Estimate cost of item 1.",
            POLICY,
            None,
        ),  # meeting Monday ignored  # fmt: skip
        (
            {**PART, "horizon_start": "2026-11-23"},
            "Customer meeting on 2026-11-23. Cost item 1.",
            POLICY,
            "invalid_tool_call",
        ),  # audit reproducer  # fmt: skip
        (
            {**PART, "horizon_start": "2026-12-21"},
            "Requested delivery date 2026-12-21. Cost item 1.",
            POLICY,
            "invalid_tool_call",
        ),  # delivery date is not a horizon  # fmt: skip
        (
            {**PART},
            "Planning start 2026-11-16 and planning start 2026-11-23. Cost item 1.",
            None,
            "clarification_required",
        ),  # several labelled  # fmt: skip
        (
            {**PART},
            "Planning start 2026-11-16. Cost item 1.",
            POLICY,
            "clarification_required",
        ),  # conflicts with policy  # fmt: skip
        (
            {**PART, "horizon_start": "2026-11-09"},
            "Planning start 2026-11-16. Cost item 1.",
            None,
            "invalid_tool_call",
        ),  # model differs from user  # fmt: skip
        (
            {**PART},
            "Cost item 1 sometime next month, maybe from the 16th.",
            None,
            "invalid_tool_call",
        ),  # no horizon at all  # fmt: skip
        (
            {**PART},
            "Planning start 2026-11-17. Cost item 1.",
            None,
            "invalid_tool_call",
        ),  # labelled but not a Monday  # fmt: skip
    ],
)
async def test_horizon_comes_only_from_policy_or_one_labelled_user_horizon(args, question, policy, error):  # fmt: skip
    result, seen = await plan(args, question, policy)
    assert result.error == error
    if error is None:
        assert seen[0].horizon_start == POLICY
    else:
        assert seen == [] and result.message == g.ERROR_MESSAGES[error]


@pytest.mark.parametrize(
    "args, question, policy, expected",
    [
        ({**PART}, "Cost item 1.", POLICY, POLICY),  # policy fills a missing horizon
        ({**PART, "horizon_start": "2026-11-09"}, "Cost item 1.", POLICY, POLICY),
        (
            {**PART},
            "Planning start: 2026-11-16. Cost item 1.",
            None,
            date(2026, 11, 16),
        ),  # labelled user horizon  # fmt: skip
        (
            {**PART},
            "Plan from 2026-11-09, cost item 1.",
            POLICY,
            POLICY,
        ),  # labelled and equal to policy  # fmt: skip
    ],
)
async def test_policy_or_labelled_user_horizon_is_used(args, question, policy, expected):
    result, seen = await plan(args, question, policy)
    assert result.status == "answered" and seen[0].horizon_start == expected


@pytest.mark.parametrize(
    "delivery, question, ok",
    [
        ("2026-12-22", "Lead time for item 1? Requested delivery date 2026-12-22.", True),
        ("2026-12-01", "Lead time for item 1? Requested delivery date 2026-12-22.", False),
        ("2026-12-22", "Lead time for item 1? Meeting on 2026-12-22.", False),  # not labelled
        ("2026-12-22", "Delivery date 2026-12-22, or due date 2026-12-29?", False),  # ambiguous
    ],
)
async def test_delivery_date_must_be_the_one_labelled_user_date(delivery, question, ok):
    result, seen = await plan({**PART, "requested_delivery_date": delivery}, question, name="estimate_lead_time")  # fmt: skip
    assert (len(seen) == 1) == ok  # executed only with the user's own labelled date
    if not ok:
        assert result.error == "invalid_tool_call"


# --- Defect 5: monetary presentation -------------------------------------------------------


def test_money_display_uses_the_pinned_pricing_rule_and_decimal_rounding():
    rule = grounding.MoneyDisplay.from_pricing_rule(PRICING_RULE)
    assert (rule.unit_scale, rule.line_scale, rule.rounding) == (Decimal("0.0001"), Decimal("0.01"), "ROUND_HALF_UP")  # fmt: skip
    fallback = grounding.MoneyDisplay()
    cases = [
        (
            "lines[0].labour_cost_per_unit",
            "3.141666666666666666666666667",
            "3.1417 USD",
            "3.14 USD",
        ),  # fmt: skip
        ("lines[0].setup_cost_per_line", "85", "85.00 USD", "85.00 USD"),
        ("lines[0].finish_cost_per_unit", "0", "0.0000 USD", "0.00 USD"),
        (
            "total_manufacturing_cost",
            "2.345",
            "2.35 USD",
            "2.35 USD",
        ),  # binary float gives 2.34  # fmt: skip
        ("quote_total_usd", "-1.245", "-1.25 USD", "-1.25 USD"),  # HALF_UP away from zero
        (
            "line_total_usd",
            "123456789012345.675",
            "123456789012345.68 USD",
            "123456789012345.68 USD",
        ),  # fmt: skip
        ("lines[0].quoted_unit_price_usd", "0.00005", "0.0001 USD", "0.00 USD"),
    ]
    for path, value, ruled, default in cases:
        assert (rule.format(path, value), fallback.format(path, value)) == (ruled, default), path
    for path in ("density_kg_m3", "lines[0].quantity", "thickness_mm", "gross_margin", "lead_business_days", "min_bend_radius_mm"):  # fmt: skip
        assert rule.format(path, "7750.000000") is None  # never mislabelled as currency


async def test_rendered_money_is_display_only_and_results_stay_exact():
    output = c.CostOutput.model_validate(COSTED)
    money = grounding.MoneyDisplay.from_pricing_rule(PRICING_RULE)
    graph = g.build_graph(fake(AIMessage("", tool_calls=[tool("estimate_costs", {**PART})])), INSTRUCTIONS, ["estimate_costs"], money_display=money)  # fmt: skip
    result = await g.run_graph(graph, "Cost item 1.", execute=returning(output), horizon_start=POLICY)  # fmt: skip
    assert "lines[0].labour_cost_per_unit=3.1417 USD" in result.reply
    assert "lines[0].total_manufacturing_cost=1841.8110 USD" in result.reply  # unit scale, as display_costs  # fmt: skip
    assert "; total_manufacturing_cost=1841.81 USD" in result.reply
    assert "lines[0].quantity=85;" in result.reply  # quantity untouched
    assert "3.141666666666666666666666667" not in result.reply
    assert result.results == (("estimate_costs", output.model_dump(mode="json")),)  # exact
    assert result.results[0][1]["lines"][0]["labour_cost_per_unit"] == "3.141666666666666666666666667"  # fmt: skip


async def test_material_values_keep_their_precision():
    graph = g.build_graph(fake(AIMessage("", tool_calls=[tool("get_material", {"material_id": "MAT-009"})])), INSTRUCTIONS, ["get_material"])  # fmt: skip
    result = await g.run_graph(graph, "q", execute=returning(c.MaterialSpecOutput.model_validate(MAT_009)))  # fmt: skip
    assert "density_kg_m3=7750.000000;" in result.reply and "USD" not in result.reply
    assert "supported_thickness_mm=1.000, 1.500, 2.000, 3.000, 4.000, 6.000" in result.reply


def test_labelled_dates_ignore_invalid_and_unlabelled_dates():
    q = "Meeting 2026-11-23; planning start 2026-13-40; plan from 2026-11-16; delivery date 2026-12-22"  # fmt: skip
    assert grounding.labelled_dates(q, grounding.HORIZON_LABEL) == {date(2026, 11, 16)}
    assert grounding.labelled_dates(q, grounding.DELIVERY_LABEL) == {date(2026, 12, 22)}
