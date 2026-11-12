"""LangGraph workflow (Days 32-34): a bounded, acyclic pass over the Day 31 LLM helpers,
with optional checkpointed conversation memory and read-only MCP tool execution.

    START -> prepare_input -> invoke_llm -> inspect_response -> END   (answer / proposal)
                                                   |
                                                   +-> execute_tools -> respond -> END
              (any node) ------------------------------------------> safe_error -> END

- Day 34: proposals execute only when the caller supplies a trusted ``ToolExecutor`` through
  the LangGraph run *context* (never state, never checkpointed). ``mcp_executor`` builds one
  from a verified ``Principal`` and ``api.mcp_integration.McpIntegration``, so every call goes
  through the delegated MCP credential and the authenticated gateway, which re-verifies the
  token and re-resolves user, customer and RBAC v2 permissions. Only ``READ_ONLY_TOOLS``
  can execute, at most ``MAX_TOOL_CALLS`` per turn, once, with no retries. Results are
  re-validated against the tool's output contract and returned to the model as
  ``ToolMessage`` data; the final model step runs without tools and may not request any.
  Any tool failure fails the turn (``tool_failed`` + the MCP code); nothing is invented.

- All validation and security lives in ``rfq_copilot.llm``. This graph only sequences
  ``build_messages``, ``invoke``, ``bind_contract_tools``, ``inspect_tool_calls`` and
  ``reject_credentials``.
- Without an executor, a "proposed" result lists contract-valid tool calls as plain dicts.
  A proposal is not authorization.
- State never holds identity, tokens, roles, permissions, customer scope, ORM objects or
  sessions. ``history`` holds only completed turns, as untrusted data blocks plus
  assistant text with tool calls stripped. Instructions are rebuilt from code on every
  turn and never persisted.
- Checkpointing is optional. ``Conversations`` is the only supported way to use a
  checkpointed graph. It takes a verified ``api.auth.Principal``, binds each opaque thread to
  that principal and checks ownership before every read or resume. Each turn runs on a graph
  with no checkpointer. Only after a successful turn is the new ``history`` saved, owner
  bound. A failed, cancelled or limit-exceeded turn persists nothing.
- Phase 5 final answers (``rfq_copilot.grounding``): model text is never shown. A turn with
  no tool result answers with the fixed ``GUIDANCE``; a turn with results answers with the
  deterministic rendering of this turn's validated results (money display-rounded by the
  pinned pricing rule's ``MoneyDisplay``). The model only proposes tools; ``respond`` makes no
  model call. ``GraphResult.results`` keeps the exact results; ``message`` maps errors to
  fixed text.
- Phase 5 final arguments (``_inspect``): the planning horizon is the trusted policy value
  (``TurnContext.horizon_start``) or one explicitly labelled user horizon ("planning start
  YYYY-MM-DD"); both present and different, or several labelled, fails closed as
  ``clarification_required``. A model value must equal it; a missing one is filled with it.
  A delivery date must equal the one labelled user delivery date. ``finish_id`` left unset
  becomes ``"none"`` only for a single-item request whose one stated finish is none.
- The graph has no cycles. ``RECURSION_LIMIT`` and a per-turn timeout bound every run.
"""

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, RemoveMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, ValidationError

from rfq_copilot import grounding, llm
from rfq_copilot.api.auth import Principal
from rfq_copilot.api.mcp_integration import McpIntegration
from rfq_copilot.grounding import GUIDANCE, Grounding, MoneyDisplay
from rfq_copilot.mcp.client import CONTRACTS, McpCallError
from rfq_copilot.mcp.client import FailureCode as ToolFailure

RECURSION_LIMIT = 8  # longest path is 5 node steps
DEFAULT_HISTORY_TURNS = 10
DEFAULT_TURN_TIMEOUT = 120.0  # seconds, model and tool calls included
MAX_TOOL_CALLS = 3

# Verified read-only (Day 34): every handler runs inside Database.snapshot_session(), a
# REPEATABLE READ, READ ONLY transaction that PostgreSQL enforces; rfq_copilot.mcp issues
# no writes; all are registered with READ_ONLY annotations. Listed by name so a future
# contract is never executable by default.
READ_ONLY_TOOLS = frozenset(
    {
        "search_materials",
        "get_material",
        "check_material_suitability",
        "check_manufacturing_feasibility",
        "check_inventory",
        "check_capacity",
        "estimate_lead_time",
        "estimate_costs",
        "calculate_price",
        "evaluate_quotation",
    }
)

# Trusted, per-turn: (tool name, validated arguments) -> contract output. Raises McpCallError.
ToolExecutor = Callable[[str, BaseModel], Awaitable[BaseModel]]


@dataclass(frozen=True)
class TurnContext:
    """LangGraph run context: held only for the run, never in state or a checkpoint."""

    execute: ToolExecutor | None = None
    horizon_start: date | None = None  # trusted application policy, never model-chosen


def mcp_executor(mcp: McpIntegration, principal: Principal, rest_credential: str) -> ToolExecutor:
    """Bind tool execution to a verified principal via the delegated MCP credential."""

    async def execute(tool: str, arguments: BaseModel) -> BaseModel:
        if not isinstance(principal, Principal) or not rest_credential:
            raise McpCallError("unauthenticated")  # fail closed
        return await mcp.call(principal, tool, arguments, rest_credential=rest_credential)

    return execute


# No pickle fallback, and msgpack decoding limited to LangGraph's safe-type allow-list
# (primitives and LangChain messages): checkpoints cannot revive arbitrary objects.
SAFE_SERDE = JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)

Status = Literal["pending", "executing", "answered", "proposed", "error"]
ErrorCode = (
    llm.FailureCode
    | Literal["execution_limit", "internal_error", "tool_failed", "clarification_required"]
)


class GraphState(TypedDict, total=False):
    question: str  # untrusted user input, credential-checked before any checkpoint
    history: Annotated[list[BaseMessage], add_messages]  # completed turns, untrusted
    messages: list[BaseMessage]  # this turn's model input
    response: AIMessage | None
    reply: str | None
    proposals: list[dict[str, Any]]  # {"id", "name", "arguments"}: JSON-safe, unexecuted
    tool_messages: list[BaseMessage]  # this turn's validated tool results (never persisted)
    executed: list[str]  # names of tools that returned validated results this turn
    grounding: Grounding
    status: Status
    error: ErrorCode | None
    tool_error: ToolFailure | None


# Every turn starts from these, so a previous turn's proposals or error cannot carry over.
TURN_RESET: GraphState = {
    "messages": [],
    "response": None,
    "reply": None,
    "proposals": [],
    "tool_messages": [],
    "executed": [],
    "grounding": "none",
    "status": "pending",
    "error": None,
    "tool_error": None,
}


@dataclass(frozen=True)
class GraphResult:
    status: Status
    reply: str | None
    proposals: tuple[dict[str, Any], ...]
    error: ErrorCode | None
    executed: tuple[str, ...] = ()  # tools whose validated results grounded the reply
    tool_error: ToolFailure | None = None  # MCP failure code when error == "tool_failed"
    grounding: Grounding = "none"  # "rendered" from tool results, or fixed "guidance"
    # Exact validated results (tool name, contract JSON): never display-rounded, never stored.
    results: tuple[tuple[str, dict[str, Any]], ...] = ()

    @property
    def message(self) -> str:
        """Customer-visible text: the deterministic reply, or a fixed sanitized error text."""
        if self.reply:
            return self.reply
        if self.error == "tool_failed":
            return TOOL_FAILURE_MESSAGES.get(self.tool_error, ERROR_MESSAGES["tool_failed"])
        return ERROR_MESSAGES.get(self.error, ERROR_MESSAGES["internal_error"])


ERROR_MESSAGES: dict[str, str] = {
    "tool_failed": "The data service could not complete this request; nothing was changed.",
    "clarification_required": "The planning start date is ambiguous or conflicts with the "
    'application\'s planning horizon. State one date as "planning start YYYY-MM-DD", or '
    "leave it out to use the default.",
    "invalid_tool_call": "The request could not be turned into a valid system query. "
    "Please restate it with the part details or a stored RFQ id.",
    "execution_limit": "The request exceeded the allowed processing limits.",
    "unsafe_content": "The message appears to contain a credential and was not processed.",
    "malformed_output": "No usable answer was produced. Please rephrase the request.",
    "internal_error": "The request could not be completed.",
}
TOOL_FAILURE_MESSAGES: dict[str, str] = {
    "forbidden": "You are not permitted to retrieve this information.",
    "not_found": "The requested record was not found or is not visible to you.",
    "unauthenticated": "Your identity could not be verified for this request.",
    "invalid_input": "The data service rejected the request details.",
    "invalid_output": "The data service returned an invalid result, so nothing is shown.",
    "timeout": "The data service did not respond in time; nothing was changed.",
    "busy": "The data service is busy; nothing was changed. Please try again later.",
    "unavailable": "The data service is unavailable; nothing was changed.",
}


def build_graph(
    model: BaseChatModel,
    instructions: str,
    allowed_tools: Iterable[str] = (),
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    max_history_turns: int = DEFAULT_HISTORY_TURNS,
    money_display: MoneyDisplay = MoneyDisplay(),
) -> CompiledStateGraph:
    """Compile the workflow for an injected chat model, tool allow-list and checkpointer."""
    if not isinstance(max_history_turns, int) or max_history_turns < 0:
        raise ValueError("max_history_turns must be a non-negative integer")
    allowed = tuple(allowed_tools)
    if not set(allowed) <= READ_ONLY_TOOLS:
        raise ValueError("only verified read-only MCP tools may be offered to the agent")
    bound = llm.bind_contract_tools(model, allowed) if allowed else model
    max_messages = 2 * max_history_turns

    async def prepare_input(state: GraphState) -> GraphState:
        try:
            system, human = llm.build_messages(instructions, {"question": state["question"]})
        except llm.LlmError as error:
            return {"error": error.code}
        history = state.get("history", [])[-max_messages:] if max_messages else []
        return {"messages": [system, *history, human], "status": "pending", "error": None}

    async def invoke_llm(state: GraphState) -> GraphState:
        try:
            response = minimal(await llm.invoke(bound, state["messages"]))
            screen(response)
        except llm.LlmError as error:
            return {"error": error.code}
        return {"response": response}

    def minimal(raw: AIMessage) -> AIMessage:
        # Keep only text and tool calls: provider extras (reasoning blocks,
        # additional_kwargs, response_metadata) are never screened, so never stored.
        return AIMessage(
            content=raw.text, tool_calls=raw.tool_calls, invalid_tool_calls=raw.invalid_tool_calls
        )

    def screen(response: AIMessage) -> None:
        # Model output can reach a checkpoint, so screen it before it leaves its node.
        llm.reject_credentials(response.text)
        calls = response.tool_calls + response.invalid_tool_calls
        llm.reject_credentials(json.dumps(calls, default=str))

    def remember(state: GraphState, reply: str | None) -> list[BaseMessage]:
        stored = state.get("history", [])
        new = [state["messages"][-1], AIMessage(reply or "")]  # text only: no tool calls
        excess = len(stored) + len(new) - max_messages
        stale = [RemoveMessage(id=m.id) for m in stored[: max(excess, 0)]]
        return stale + (new if max_messages else [])

    def _inspect(state: GraphState, runtime) -> tuple[llm.ProposedToolCall, ...]:
        question = state["question"]
        policy = policy_horizon(runtime)
        stated = grounding.labelled_dates(question, grounding.HORIZON_LABEL)
        delivery = grounding.labelled_dates(question, grounding.DELIVERY_LABEL)
        if len(stated) > 1 or (stated and policy and stated != {policy}):
            raise ClarificationRequired()  # conflicting horizons: never silently choose
        horizon = next(iter(stated), None) or policy
        trusted = {"horizon_start": horizon} if horizon else {}
        checked = []
        for call in llm.inspect_tool_calls(state["response"], allowed, trusted):
            args = call.arguments
            value = getattr(args, "horizon_start", None)
            if value is not None and value != horizon:
                raise llm.LlmError("invalid_tool_call")  # a horizon the model chose itself
            value = getattr(args, "requested_delivery_date", None)
            if value is not None and (len(delivery) != 1 or value not in delivery):
                raise llm.LlmError("invalid_tool_call")  # not the user's labelled delivery date
            parts = getattr(args, "parts", None) or [getattr(args, "part", None)]
            if grounding.single_item_finish_none(question) and len(parts) == 1 and parts[0]:
                data = args.model_dump()
                parts = data.get("parts") or ([data["part"]] if data.get("part") else [])
                for part in parts:
                    if part.get("finish_id") is None:
                        part["finish_id"] = "none"  # the user's explicit, unambiguous finish
                try:
                    args = type(args).model_validate(data)  # the unchanged contract again
                except ValidationError:
                    raise llm.LlmError("invalid_tool_call") from None
            checked.append(llm.ProposedToolCall(call.id, call.name, args))
        return tuple(checked)

    def inspected(state: GraphState, runtime):
        try:
            return _inspect(state, runtime), None
        except ClarificationRequired:
            return None, "clarification_required"
        except llm.LlmError as error:
            return None, error.code

    async def inspect_response(
        state: GraphState, runtime: Runtime[TurnContext] | None = None
    ) -> GraphState:
        proposals, error = inspected(state, runtime)
        if error:
            return {"error": error}
        if len(proposals) > MAX_TOOL_CALLS:
            return {"error": "execution_limit"}
        if proposals:
            plain = [
                {"id": p.id, "name": p.name, "arguments": p.arguments.model_dump(mode="json")}
                for p in proposals
            ]
            if executor(runtime) is not None:
                return {"proposals": plain, "status": "executing"}  # history written later
            # Unexecuted proposals are data, not an answer: model text is not shown.
            return {"proposals": plain, "reply": None, "status": "proposed", "history": remember(state, "")}  # fmt: skip
        if not state["response"].text:
            return {"error": "malformed_output"}  # neither text nor a proposal
        # No tool result: model prose is discarded; the reply is fixed guidance, not a fact.
        return {
            "proposals": [],
            "reply": GUIDANCE,
            "grounding": "guidance",
            "status": "answered",
            "history": remember(state, GUIDANCE),
        }

    async def execute_tools(
        state: GraphState, runtime: Runtime[TurnContext] | None = None
    ) -> GraphState:
        execute = executor(runtime)
        if execute is None:  # unreachable via routing; fail closed anyway
            return {"error": "tool_failed", "tool_error": "unauthenticated"}
        # Re-inspect from the model message: tool names and arguments are untrusted input.
        proposals, error = inspected(state, runtime)
        if error:
            return {"error": error}
        results, executed = [], []
        for call in proposals:  # sequential, exactly once each, no retries
            if call.name not in READ_ONLY_TOOLS:
                return {"error": "invalid_tool_call"}
            contract = CONTRACTS[call.name]
            try:
                raw = await execute(call.name, call.arguments)
                data = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw
                result = contract.output_model.model_validate(data)
            except McpCallError as error:
                return {"error": "tool_failed", "tool_error": error.code}
            except ValidationError:
                return {"error": "tool_failed", "tool_error": "invalid_output"}
            except Exception:  # an executor fault: text never surfaces
                return {"error": "tool_failed", "tool_error": "unavailable"}
            try:
                results.append(llm.tool_result_message(call, result))
            except llm.LlmError as error:
                return {"error": error.code}
            executed.append(call.name)
        return {"tool_messages": results, "executed": executed}

    async def respond(state: GraphState) -> GraphState:
        # No model call: the answer is built only from this turn's validated results, so no
        # model prose (contradicting or not) can reach the user, and no further tool runs.
        results = [(m.name, json.loads(m.text)) for m in state["tool_messages"]]
        reply = grounding.render(results, money_display)
        return {
            "reply": reply,
            "grounding": "rendered",
            "status": "answered",
            "history": remember(state, reply),
        }

    async def safe_error(state: GraphState) -> GraphState:
        # Drop this turn's input, raw model output and tool results. History keeps only
        # earlier turns.
        return {
            "question": "",
            "messages": [],
            "response": None,
            "reply": None,
            "proposals": [],
            "tool_messages": [],
            "executed": [],
            "grounding": "none",
            "status": "error",
        }

    def on_error(next_node: str):
        return lambda state: "safe_error" if state.get("error") else next_node

    def after_inspect(state: GraphState) -> str:
        if state.get("error"):
            return "safe_error"
        return "execute_tools" if state.get("status") == "executing" else END

    graph = StateGraph(GraphState, context_schema=TurnContext)
    graph.add_node("prepare_input", prepare_input)
    graph.add_node("invoke_llm", invoke_llm)
    graph.add_node("inspect_response", inspect_response)
    graph.add_node("execute_tools", execute_tools)
    graph.add_node("respond", respond)
    graph.add_node("safe_error", safe_error)
    graph.add_edge(START, "prepare_input")
    graph.add_conditional_edges(
        "prepare_input", on_error("invoke_llm"), ["invoke_llm", "safe_error"]
    )
    graph.add_conditional_edges(
        "invoke_llm", on_error("inspect_response"), ["inspect_response", "safe_error"]
    )
    graph.add_conditional_edges(
        "inspect_response", after_inspect, [END, "execute_tools", "safe_error"]
    )
    graph.add_conditional_edges("execute_tools", on_error("respond"), ["respond", "safe_error"])
    graph.add_conditional_edges("respond", on_error(END), [END, "safe_error"])
    graph.add_edge("safe_error", END)
    return graph.compile(checkpointer=checkpointer)


class ClarificationRequired(Exception):
    """Conflicting or ambiguous user/policy input that must not be resolved silently."""


def executor(runtime: Runtime[TurnContext] | None) -> ToolExecutor | None:
    context = getattr(runtime, "context", None)
    return context.execute if isinstance(context, TurnContext) else None


def policy_horizon(runtime: Runtime[TurnContext] | None) -> date | None:
    context = getattr(runtime, "context", None)
    return context.horizon_start if isinstance(context, TurnContext) else None


async def _run(
    graph: CompiledStateGraph,
    question: str,
    config: dict,
    history: list[BaseMessage] = (),
    *,
    execute: ToolExecutor | None = None,
    timeout_seconds: float = DEFAULT_TURN_TIMEOUT,
    horizon_start: date | None = None,
) -> tuple[GraphResult, list[BaseMessage]]:
    """One bounded pass. Returns the result and the turn's resulting history."""
    if execute is not None and graph.checkpointer is not None:
        raise ValueError("tool execution runs only on a graph without a checkpointer")
    try:
        llm.reject_credentials(question)  # before the input reaches any graph state
    except llm.LlmError as error:
        return GraphResult("error", None, (), error.code), []
    try:
        async with asyncio.timeout(timeout_seconds):
            state = await graph.ainvoke(
                {"question": question, "history": list(history), **TURN_RESET},
                config,
                context=TurnContext(execute, horizon_start),
            )
    except (GraphRecursionError, TimeoutError):
        return GraphResult("error", None, (), "execution_limit"), []
    result = GraphResult(
        status=state["status"],
        reply=state.get("reply"),
        proposals=tuple(state.get("proposals", ())),
        error=state.get("error"),
        executed=tuple(state.get("executed", ())),
        grounding=state.get("grounding", "none"),
        results=tuple((m.name, json.loads(m.text)) for m in state.get("tool_messages", ())),
        tool_error=state.get("tool_error"),
    )
    return result, state.get("history", [])


async def run_graph(
    graph: CompiledStateGraph,
    question: str,
    *,
    recursion_limit: int = RECURSION_LIMIT,
    execute: ToolExecutor | None = None,
    timeout_seconds: float = DEFAULT_TURN_TIMEOUT,
    horizon_start: date | None = None,
) -> GraphResult:
    """Run one bounded, memoryless pass and return an immutable result without the input.
    ``execute`` (trusted, e.g. ``mcp_executor``) enables read-only tool execution;
    ``horizon_start`` is the application's planning-horizon policy for this turn."""
    config = {"recursion_limit": recursion_limit}
    result, _ = await _run(
        graph,
        question,
        config,
        execute=execute,
        timeout_seconds=timeout_seconds,
        horizon_start=horizon_start,
    )
    return result


# --- checkpointed conversations ----------------------------------------------------------

_THREAD_ID = re.compile(r"[A-Za-z0-9_-]{32}")  # secrets.token_urlsafe(24)


class ThreadAccessError(Exception):
    """One opaque answer for: missing trusted context, malformed, unknown or foreign thread."""

    def __init__(self) -> None:
        super().__init__("thread_unavailable")


@dataclass(frozen=True)
class Turn:
    thread_id: str
    result: GraphResult


def _owner(principal: Principal) -> str:
    # Fail closed: only a verified principal with both a customer and a user owns threads.
    if not isinstance(principal, Principal) or not principal.customer_id or not principal.user_id:
        raise ThreadAccessError()
    key = f"{principal.customer_id}\x00{principal.user_id}".encode()
    return hashlib.sha256(key).hexdigest()  # pseudonymous binding, never model-visible


class Conversations:
    """Principal-scoped, checkpointed conversations. Callers never see the graph or saver."""

    def __init__(
        self,
        model: BaseChatModel,
        instructions: str,
        allowed_tools: Iterable[str] = (),
        *,
        checkpointer: BaseCheckpointSaver | None = None,
        max_history_turns: int = DEFAULT_HISTORY_TURNS,
        recursion_limit: int = RECURSION_LIMIT,
        mcp: McpIntegration | None = None,
        turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT,
        money_display: MoneyDisplay = MoneyDisplay(),
    ) -> None:
        # ponytail: InMemorySaver is process-local and lost on restart; inject a PostgreSQL
        # saver (constructed with SAFE_SERDE) for durability.
        saver = checkpointer if checkpointer is not None else InMemorySaver(serde=SAFE_SERDE)
        # Turns execute here, with no checkpointer: question, model output, provider data and
        # exception text exist only in memory while the turn runs.
        self._runner = build_graph(
            model,
            instructions,
            allowed_tools,
            max_history_turns=max_history_turns,
            money_display=money_display,
        )
        # Persistence only: read snapshots and save completed history. Never invokes the model.
        self._graph = build_graph(model, instructions, checkpointer=saver)
        self._recursion_limit = recursion_limit
        # Without an integration, tool calls stay unexecuted proposals (Days 32-33).
        self._mcp, self._timeout = mcp, turn_timeout_seconds
        # ponytail: one lock per thread, never evicted; bounded by the saver's own lifetime.
        self._locks: dict[str, asyncio.Lock] = {}

    async def _snapshot(self, owner: str, thread_id: object):
        if not isinstance(thread_id, str) or not _THREAD_ID.fullmatch(thread_id):
            raise ThreadAccessError()
        snapshot = await self._graph.aget_state({"configurable": {"thread_id": thread_id}})
        bound_to = (snapshot.metadata or {}).get("owner")
        if snapshot.created_at is None or not isinstance(bound_to, str):
            raise ThreadAccessError()
        if not hmac.compare_digest(bound_to, owner):
            raise ThreadAccessError()
        return snapshot

    async def ask(
        self,
        principal: Principal,
        question: str,
        thread_id: str | None = None,
        *,
        rest_credential: str | None = None,
        horizon_start: date | None = None,
    ) -> Turn:
        """Start a thread (``thread_id=None``) or continue one the principal owns.

        With an MCP integration, approved read-only tools execute as ``principal`` through the
        delegated MCP credential; ``rest_credential`` is the caller's REST bearer token, used
        only to prove it is never forwarded. It is never stored, logged or model-visible."""
        owner = _owner(principal)
        execute = None
        if self._mcp is not None:
            execute = mcp_executor(self._mcp, principal, rest_credential or "")
        new = thread_id is None
        if new:
            thread_id = secrets.token_urlsafe(24)  # server-chosen; callers cannot pick one
        elif not isinstance(thread_id, str) or not _THREAD_ID.fullmatch(thread_id):
            raise ThreadAccessError()
        config = {
            "configurable": {"thread_id": thread_id},
            "metadata": {"owner": owner},  # written into every checkpoint of the thread
        }
        async with self._locks.setdefault(thread_id, asyncio.Lock()):
            history = []
            if not new:  # ownership checked under the lock
                history = (await self._snapshot(owner, thread_id)).values.get("history", [])
            try:
                result, remembered = await _run(
                    self._runner,
                    question,
                    {"recursion_limit": self._recursion_limit},
                    history,
                    execute=execute,
                    timeout_seconds=self._timeout,
                    horizon_start=horizon_start,
                )
                if result.status in ("answered", "proposed"):
                    # The only write: the completed, trimmed history (one checkpoint per turn).
                    await self._graph.aupdate_state(
                        config,
                        {"history": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *remembered]},
                        as_node="inspect_response",
                    )
            except Exception:
                # Unexpected failure: this turn persisted nothing, and its exception text is
                # never surfaced. CancelledError is a BaseException and propagates unchanged.
                result = GraphResult("error", None, (), "internal_error")
        return Turn(thread_id, result)

    async def history(self, principal: Principal, thread_id: str) -> tuple[tuple[str, str], ...]:
        """The remembered turns as (role, text) pairs, for the owning principal only."""
        snapshot = await self._snapshot(_owner(principal), thread_id)
        return tuple((m.type, m.text) for m in snapshot.values.get("history", []))
