"""Day 32 LangGraph foundation: mocked chat models only, no provider, MCP or database."""

import asyncio
import json

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.errors import GraphRecursionError
from test_llm import FailingModel, FakeToolModel, call

from rfq_copilot import graph as g
from rfq_copilot import llm
from rfq_copilot.mcp.client import McpToolClient

TOOLS = ["get_material", "search_materials"]
pytestmark = pytest.mark.asyncio


def fake(*replies) -> FakeToolModel:
    return FakeToolModel(messages=iter(replies))


def build(model, tools=TOOLS):
    return g.build_graph(model, "You answer RFQ questions.", tools)


class EchoModel(BaseChatModel):
    """Answers from its own input after yielding, to interleave concurrent runs."""

    @property
    def _llm_type(self) -> str:
        return "echo"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("sync path not expected")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        text = messages[-1].content
        await asyncio.sleep(0.01 if "slow" in text else 0)
        material = text.split("material=")[1].split("\n")[0] if "material=" in text else None
        reply = (
            AIMessage("", tool_calls=[call(args={"material_id": material}, id=material)])
            if material
            else AIMessage("echo " + "x" * text.count("slow"))  # non-numeric (Day 35 guard)
        )
        return ChatResult(generations=[ChatGeneration(message=reply)])


@pytest.fixture(autouse=True)
def no_mcp(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("MCP must not be called on Day 32")

    monkeypatch.setattr(McpToolClient, "call", forbidden)


# --- construction ------------------------------------------------------------------------


async def test_graph_compiles_with_expected_topology():
    drawable = build(fake()).get_graph()
    assert set(drawable.nodes) == {
        "__start__",
        "prepare_input",
        "invoke_llm",
        "inspect_response",
        "execute_tools",  # Day 34
        "respond",  # Day 34
        "safe_error",
        "__end__",
    }
    edges = {(e.source, e.target, e.conditional) for e in drawable.edges}
    assert ("__start__", "prepare_input", False) in edges
    assert ("prepare_input", "invoke_llm", True) in edges
    assert ("inspect_response", "__end__", True) in edges
    assert ("inspect_response", "execute_tools", True) in edges
    assert ("execute_tools", "respond", True) in edges
    assert ("respond", "__end__", True) in edges
    assert ("safe_error", "__end__", False) in edges
    mermaid = drawable.draw_mermaid()
    assert mermaid.startswith("---") and "inspect_response" in mermaid


async def test_tool_binding_reuses_day31_helper():
    model = fake()
    build(model, ["get_material"])
    assert [t["function"]["name"] for t in model.bound] == ["get_material"]
    with pytest.raises(llm.LlmError):  # setup errors surface at build time
        build(GenericFakeChatModel(messages=iter([])), ["get_material"])
    build(GenericFakeChatModel(messages=iter([])), [])  # no tools: no binding needed


# --- routing -----------------------------------------------------------------------------


async def test_direct_response_runs_start_to_end():
    # Phase 5: tool-free model text is never shown; the reply is the fixed guidance.
    graph = build(fake(AIMessage("I can look that up in the material catalog for you.")))
    state = await graph.ainvoke({"question": "Is 304 available?"})
    assert state["status"] == "answered" and state["reply"] == g.GUIDANCE
    assert state["proposals"] == [] and state["error"] is None
    assert [type(m).__name__ for m in state["messages"]] == ["SystemMessage", "HumanMessage"]
    assert "Is 304 available?" in state["messages"][1].content  # untrusted block


async def test_node_order_and_state_updates():
    graph = build(fake(AIMessage("hi")))
    steps = [list(u) async for u in graph.astream({"question": "q"}, stream_mode="updates")]
    assert steps == [["prepare_input"], ["invoke_llm"], ["inspect_response"]]


async def test_valid_tool_proposal_ends_without_execution():
    result = await g.run_graph(build(fake(AIMessage("", tool_calls=[call()]))), "q")
    assert result == g.GraphResult(
        "proposed",
        None,
        ({"id": "c1", "name": "get_material", "arguments": {"material_id": "MAT-1"}},),
        None,
    )


async def test_tool_free_stock_claim_is_refused_day35():
    # Day 32 showed this reply; Phase 5 never shows tool-free model text (fixed guidance).
    result = await g.run_graph(build(fake(AIMessage("Steel 304 is in stock."))), "Is 304 available?")  # fmt: skip
    assert (result.status, result.reply, result.grounding) == ("answered", g.GUIDANCE, "guidance")
    assert "in stock" not in result.message


async def test_multiple_proposals_kept_in_order():
    calls = [call(id="a"), call(name="search_materials", args={"limit": 5}, id="b")]
    result = await g.run_graph(build(fake(AIMessage("checking", tool_calls=calls))), "q")
    assert result.status == "proposed" and result.reply is None  # Phase 5: no model text
    assert [p["id"] for p in result.proposals] == ["a", "b"]
    assert result.proposals[1]["arguments"]["limit"] == 5
    json.dumps(result.proposals)  # plain, serializable data


@pytest.mark.parametrize(
    "bad",
    [
        call(name="calculate_price", args={}),  # not on this graph's allow-list
        call(args={"material_id": "MAT-1", "customer_id": "CUST-OTHER"}),  # forged scope
        call(args={"material_id": "MAT-1", "role": "admin"}),
        call(args={}),
    ],
)
async def test_invalid_tool_calls_route_to_safe_error(bad):
    result = await g.run_graph(build(fake(AIMessage("", tool_calls=[call(id="ok"), bad]))), "q")
    assert result == g.GraphResult("error", None, (), "invalid_tool_call")


async def test_model_failure_is_sanitized():
    result = await g.run_graph(build(FailingModel(messages=iter([])), []), "q")
    assert result == g.GraphResult("error", None, (), "model_error")
    assert "secret" not in repr(result)


@pytest.mark.parametrize("reply", [AIMessage(""), AIMessage(content=[])])
async def test_empty_response_is_malformed(reply):
    result = await g.run_graph(build(fake(reply)), "q")
    assert result == g.GraphResult("error", None, (), "malformed_output")


async def test_non_message_output_is_malformed():
    class TextModel(EchoModel):
        def bind_tools(self, tools, **kwargs):
            from langchain_core.runnables import RunnableLambda

            return RunnableLambda(lambda _: "plain string")

    result = await g.run_graph(build(TextModel()), "q")
    assert result.error == "malformed_output"


async def test_credential_in_question_is_refused_and_dropped():
    graph = build(fake(AIMessage("never reached")))
    state = await graph.ainvoke({"question": "use Bearer abcdefghijklmnop"})
    assert state["status"] == "error" and state["error"] == "unsafe_content"
    assert "abcdefghijklmnop" not in repr(state)  # input and model output cleared


# --- execution safety --------------------------------------------------------------------


async def test_recursion_limit_bounds_execution():
    graph = build(fake(AIMessage("hi"), AIMessage("hi")))
    with pytest.raises(GraphRecursionError):
        await graph.ainvoke({"question": "q"}, {"recursion_limit": 2})
    assert await g.run_graph(graph, "q", recursion_limit=2) == g.GraphResult(
        "error", None, (), "execution_limit"
    )


async def test_concurrent_runs_are_isolated():
    graph = build(EchoModel())
    questions = ["slow slow", "material=M-1", "slow", "material=M-2", "plain"]
    results = await asyncio.gather(*(g.run_graph(graph, q) for q in questions))
    assert [r.reply for r in results] == [g.GUIDANCE, None, g.GUIDANCE, None, g.GUIDANCE]
    assert results[1].proposals[0]["arguments"] == {"material_id": "M-1"}
    assert results[3].proposals[0]["arguments"] == {"material_id": "M-2"}


async def test_state_has_no_identity_or_credentials():
    forbidden = {"token", "principal", "user_id", "customer_id", "role", "roles", "permissions"}
    assert not forbidden & set(g.GraphState.__annotations__)
    state = await build(fake(AIMessage("", tool_calls=[call()]))).ainvoke({"question": "q"})
    assert not forbidden & set(state)
    assert not forbidden & set(state["proposals"][0]["arguments"])
