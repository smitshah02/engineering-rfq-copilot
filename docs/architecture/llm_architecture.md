# LLM and Agent Model Architecture

`src/rfq_copilot/llm.py` provides provider-independent model calls and proposal validation.
Tool execution belongs to the secured graph/MCP integration, described below.
Tests: `tests/test_llm.py` (mocked models).

## 1. Model interface

The interface is LangChain's `BaseChatModel` (`langchain-core>=1,<2`, locked 1.8.0), passed
in by the caller. This module never builds a model, reads API keys or turns on tracing.
Provider packages (`langchain-openai`, `langchain-anthropic`, `langchain-google-genai`,
`langchain-ollama`) are added only when a provider is chosen. Agent code stays the same.

| Function | Purpose |
|---|---|
| `build_messages(instructions, untrusted)` | A trusted `SystemMessage` plus one `HumanMessage` of labelled `<untrusted_data>` blocks |
| `invoke(model, messages)` | Async `ainvoke` that must return an `AIMessage` |
| `bind_contract_tools(model, names)` | Binds MCP contract input schemas as tools. Binding only |
| `inspect_tool_calls(ai_message, allowed)` | Turns tool calls into `ProposedToolCall` values that match the contract. Nothing executes |
| `tool_result_message(call, result)` | Builds a `ToolMessage` from a result that already passed contract validation |
| `invoke_structured(model, messages, schema)` | Uses the provider's `with_structured_output(include_raw=True)`, then re-validates with Pydantic |

All failures raise `LlmError.code`, one of `unsafe_content`, `unsupported`, `model_error`,
`malformed_output` or `invalid_tool_call`. The error never carries or chains prompt text,
model output or provider exception text.

## 2. Message flow and trust

```
code-authored instructions ──► SystemMessage (+ TRUST_NOTICE)          trusted
RFQ text / attachments     ──► HumanMessage <untrusted_data label=…>   data only
model reply                ──► AIMessage (text | tool_calls)           untrusted proposal
validated MCP result       ──► ToolMessage                             data only
```

- Only code writes instructions. Customer text cannot close its block because the tag is
  neutralised.
- No principal, JWT, role, permission or customer scope goes into any message. Prompt
  parts shaped like credentials (bearer, JWT, `sk-`/`AIza` keys, PEM) are refused.
- LangSmith tracing (a transitive dependency of `langchain-core`) is off unless the
  `LANGSMITH_TRACING` variable is set. Keep it off until there is a
  data-handling decision.

## 3. Tool-call trust boundary

A model tool call is a request, not an authorization.

1. `inspect_tool_calls` rejects the whole message if any one call fails a check: an
   unparseable call, a tool outside this agent's allow-list, a missing call id, or
   arguments that fail the contract input model. The contracts are `extra="forbid"` and have
   no identity fields, so a model-supplied `customer_id`, `role`, `permissions`, `user_id`
   or `token` is rejected.
2. A `ProposedToolCall` holds only `id`, `name` and validated `arguments`. It has no
   credential and no execute method.
3. Execution (implemented tool execution, `graph.execute_tools` via `graph.mcp_executor`; see
   `langgraph_foundation.md`). The runtime takes the proposal, adds the authenticated
   principal's MCP-audience token from `api.mcp_integration`, and calls
   `mcp.client.McpToolClient.call(..., token=...)` through the secured host. JWT, RBAC, revocation and customer
   isolation are enforced server-side again. The LLM never sees or chooses the identity.

## 4. Structured output

`invoke_structured` uses the provider's native mode: tool calling or JSON schema. If the
provider reports a parsing error, returns no parsed value or returns an off-schema value,
the result is `malformed_output`. Schemas describe wording and classification only. Prices,
quantities, lead times and approvals still come from the deterministic services through
MCP, never from model output.

## 5. Provider selection

Keep `BaseChatModel` injection as the provider boundary. Evaluate candidates on development
scenarios using tool-call validity, schema-failure rate, latency and cost. Never use frozen
evaluation data for iterative provider selection. Provider-specific capabilities and prices
must be checked before selection; local Ollama support is an optional development path.

## 6. Runtime integration

- The [LangGraph workflow](langgraph_foundation.md) calls the model helpers and validates
  proposals without putting credentials into state.
- The execution node maps allowed proposals to secured MCP calls using the runtime
  principal's delegated credential. Tool results pass through `tool_result_message`.

References: LangChain [models](https://docs.langchain.com/oss/python/langchain/models),
[messages](https://docs.langchain.com/oss/python/langchain/messages),
[tools](https://docs.langchain.com/oss/python/langchain/tools), and
[structured output](https://docs.langchain.com/oss/python/langchain/structured-output).
