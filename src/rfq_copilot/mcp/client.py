"""MCP client for the backend (Day 29): the ten approved tools on the Day 28 host only.

- Destinations come from ``contracts.TOOLS`` (tool -> server) and the fixed ``SERVER_PATHS``
  on one configured base URL. Callers never supply a URL or an unknown tool name.
- Requests are validated against the tool's input model before sending, and results
  against its output model (exact decimal strings preserved).
- Transport: SDK ``mcp.Client`` over ``streamable_http_client`` with an ``httpx2`` client,
  ``mode="2026-07-28"`` pinned (no legacy probing). One short-lived connection per call.
- Every failure becomes ``McpCallError`` with a fixed ``code``; no token, payload, response
  body or exception text is logged or carried. No retries: 429/503/504 surface as
  ``busy``/``timeout`` to the caller (the tools are read-only, but retry storms are not).
- The bearer credential is passed per call and must be an MCP-audience token from a
  trusted provider (``api.mcp_integration``); a REST token is never forwarded.
"""

from typing import Literal

import anyio
import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ValidationError

from rfq_copilot.mcp.contracts import TOOLS, ErrorCode

PROTOCOL_VERSION = "2026-07-28"
SERVER_PATHS = {
    "manufacturing": "/mcp/manufacturing",
    "supply": "/mcp/supply",
    "pricing": "/mcp/pricing",
}
CONTRACTS = {tool.name: tool for tool in TOOLS}

FailureCode = Literal[
    "invalid_input",  # arguments rejected (locally or by the server)
    "unauthenticated",  # the MCP host refused the credential (401)
    "forbidden",  # the principal lacks the tool's permission
    "not_found",  # absent or not visible to this customer
    "conflict",  # rule or snapshot inconsistency (retry later)
    "busy",  # 429 / 503
    "timeout",  # client deadline or 504
    "unavailable",  # host unreachable or protocol rejection
    "tool_error",  # the tool failed internally
    "invalid_output",  # result outside its contract
]
_TOOL_CODES: dict[str, FailureCode] = {
    ErrorCode.INVALID_INPUT: "invalid_input",
    ErrorCode.UNAUTHENTICATED: "unauthenticated",
    ErrorCode.PERMISSION_DENIED: "forbidden",
    ErrorCode.NOT_FOUND: "not_found",
    ErrorCode.SNAPSHOT_CONFLICT: "conflict",
    ErrorCode.INTERNAL: "tool_error",
}
_HTTP_CODES: dict[int, FailureCode] = {
    401: "unauthenticated",
    403: "unavailable",  # Origin refused: a deployment fault, not the user's permission
    413: "invalid_input",
    421: "unavailable",
    429: "busy",
    503: "busy",
    504: "timeout",
}


class McpCallError(Exception):
    """A safe failure: only the fixed code leaves this module."""

    def __init__(self, code: FailureCode) -> None:
        super().__init__(code)
        self.code = code


def _leaves(error: BaseException):
    if isinstance(error, BaseExceptionGroup):
        for inner in error.exceptions:
            yield from _leaves(inner)
    else:
        yield error


class McpToolClient:
    """Calls approved tools on one MCP host. ``transport`` is for in-memory tests only."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")) or base_url.count("/") != 2:
            raise ValueError("base_url must be scheme://host[:port].")
        if not 0 < timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be within (0, 300].")
        self.base_url, self.timeout, self._transport = base_url, timeout_seconds, transport

    async def call(self, tool: str, request: BaseModel | dict, *, token: str) -> BaseModel:
        contract = CONTRACTS.get(tool)
        if contract is None:
            raise McpCallError("invalid_input")  # never a free-form destination
        try:
            arguments = contract.input_model.model_validate(
                request.model_dump() if isinstance(request, BaseModel) else request
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError:
            raise McpCallError("invalid_input") from None
        if not isinstance(token, str) or not token or "\n" in token:
            raise McpCallError("unauthenticated")
        url = f"{self.base_url}{SERVER_PATHS[contract.server]}"
        statuses: list[int] = []

        async def record(response) -> None:
            statuses.append(response.status_code)

        try:
            with anyio.fail_after(self.timeout):
                async with httpx2.AsyncClient(
                    transport=self._transport,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx2.Timeout(self.timeout, connect=min(5.0, self.timeout)),
                    event_hooks={"response": [record]},
                    follow_redirects=False,
                ) as http:
                    async with Client(
                        streamable_http_client(url, http_client=http), mode=PROTOCOL_VERSION
                    ) as session:
                        result = await session.call_tool(tool, {"request": arguments})
        except TimeoutError:
            raise McpCallError("timeout") from None
        except BaseException as error:  # SDK task groups wrap the cause
            if not isinstance(error, Exception):
                raise
            raise McpCallError(self._classify(error, statuses)) from None
        if result.is_error:
            text = " ".join(getattr(block, "text", "") for block in result.content)
            raise McpCallError(_TOOL_CODES.get(text.partition(":")[0], "tool_error"))
        try:
            return contract.output_model.model_validate(result.structured_content)
        except ValidationError:
            raise McpCallError("invalid_output") from None

    @staticmethod
    def _classify(error: BaseException, statuses: list[int]) -> FailureCode:
        failed = [s for s in statuses if s >= 400]
        if failed:
            return _HTTP_CODES.get(failed[-1], "unavailable")
        for leaf in _leaves(error):
            if isinstance(leaf, (httpx2.TimeoutException, TimeoutError)):
                return "timeout"
            if isinstance(leaf, (httpx2.TransportError, OSError, MCPError)):
                return "unavailable"
        return "unavailable"
