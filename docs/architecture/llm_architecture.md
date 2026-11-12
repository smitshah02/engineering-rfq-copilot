# LLM and Agent Model Architecture (Day 31)

Status: foundation implemented in `src/rfq_copilot/llm.py`. No provider selected. No tool
executes. Tests: `tests/test_llm.py` (mocked models only).

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
  `LANGSMITH_TRACING` variable is set. Day 31 found it unset. Keep it off until there is a
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
3. Execution (implemented Day 34, `graph.execute_tools` via `graph.mcp_executor`; see
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

## 5. Provider comparison (for later selection; re-verify before choosing)

| | Tool calling | Structured output | Cost | Latency | Fit for development |
|---|---|---|---|---|---|
| OpenAI | Mature, parallel calls | Native JSON-schema strict mode | Mid; small models cheap | Low–mid | Strong; widest LangChain coverage |
| Anthropic | Mature, strong at following instructions | Through tool use or native structured outputs | Mid; small models cheap | Low–mid | Strong for agentic and long-context work |
| Gemini | Supported, parallel calls | Native response schema | Often lowest per token among the three | Low (Flash tiers) | Good; schema-dialect quirks |
| Local (Ollama / vLLM) | Depends on the model; weaker | JSON mode or grammar; less reliable | No API fee; hardware cost | Depends on hardware | Good for offline tests and privacy; weaker reasoning |

Recommendation: keep `BaseChatModel` injection as the only seam. Select a provider after
Day 32 graph tests, using a small evaluation on development scenarios. Never use the frozen
evaluation data for this. Measure tool-call validity, schema-failure rate, latency and cost.

## 6. Roadmap

- **Day 32:** a LangGraph `StateGraph` whose nodes call `invoke`, `invoke_structured` and
  `inspect_tool_calls`. Graph state holds facts and `ProposedToolCall`s, never credentials.
- **Day 34 (done):** an execution node maps approved proposals to `mcp.client` calls with the
  runtime principal's token. MCP results come back through `tool_result_message`.

References: LangChain docs on [chat models](https://docs.langchain.com/oss/python/langchain/models),
[messages](https://docs.langchain.com/oss/python/langchain/messages),
[tools](https://docs.langchain.com/oss/python/langchain/tools) and
[structured output](https://docs.langchain.com/oss/python/langchain/structured-output).
API behaviour was checked against the installed `langchain-core` 1.8.0 source
(`BaseChatModel.bind_tools` and `with_structured_output`).
