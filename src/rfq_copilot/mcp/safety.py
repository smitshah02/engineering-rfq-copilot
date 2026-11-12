"""Error safety shared by every MCP server: domain-error mapping and wire sanitizing.

Two layers, because the SDK validates arguments *before* a tool body runs:
- ``safe_call`` maps failures raised inside a tool body to the fixed ``tool_failure`` codes.
- ``sanitize_tool_errors`` is SDK ``ServerMiddleware``. It rewrites every error result
  leaving the server to an allow-listed "CODE: message" text. That covers the SDK's
  argument-validation errors, which would otherwise echo the caller's input. It runs on
  the wire path (``Client``, stdio, HTTP), not on direct in-process
  ``MCPServer.call_tool()`` calls.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from mcp import MCPError
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import INVALID_PARAMS, CallToolResult, TextContent
from pydantic import ValidationError

from rfq_copilot.database import DatabaseError
from rfq_copilot.mcp.contracts import SAFE_MESSAGES, ErrorCode, tool_failure
from rfq_copilot.repositories.queries import RuleIntegrityError, SnapshotMismatchError

logger = logging.getLogger(__name__)
T = TypeVar("T")

ALLOWED_ERROR_TEXTS = frozenset(f"{code.value}: {text}" for code, text in SAFE_MESSAGES.items())


async def safe_call(tool: str, work: Callable[[], Awaitable[T]]) -> T:
    """Run a tool body; every failure leaves as a fixed-text ToolError (``from None``)."""
    try:
        return await work()
    except ToolError:
        raise  # already a tool_failure
    except (RuleIntegrityError, SnapshotMismatchError):
        raise tool_failure(ErrorCode.SNAPSHOT_CONFLICT) from None
    except DatabaseError:
        raise tool_failure(ErrorCode.INTERNAL) from None
    except ValueError:
        # Arguments were validated by the contract and converted by runtime.to_parts (which
        # raises INVALID_INPUT itself), so any other ValueError, including a pydantic
        # ValidationError on an output, is our defect: INTERNAL, details logged only.
        logger.exception("MCP tool %s failed a service or output check", tool)
        raise tool_failure(ErrorCode.INTERNAL) from None
    except Exception:
        logger.exception("MCP tool %s failed unexpectedly", tool)  # trusted logs only
        raise tool_failure(ErrorCode.INTERNAL) from None


def _safe_text(text: str) -> str:
    # SDK form: "Error executing tool <name>[: <detail>]". Keep only our own fixed texts.
    detail = text.partition(": ")[2]
    if detail in ALLOWED_ERROR_TEXTS:
        return detail
    code = ErrorCode.INVALID_INPUT if detail else ErrorCode.INTERNAL
    return f"{code.value}: {SAFE_MESSAGES[code]}"


def _error_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


async def sanitize_tool_errors(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
    if ctx.method == "tools/call":
        params = ctx.params if isinstance(ctx.params, dict) else {}
        arguments = params.get("arguments") or {}
        # Every tool takes exactly one ``request`` argument. The SDK silently drops unknown
        # top-level arguments; refuse them instead, so no override is ever "accepted".
        if not isinstance(arguments, dict) or set(arguments) - {"request"}:
            return _error_result(f"INVALID_INPUT: {SAFE_MESSAGES[ErrorCode.INVALID_INPUT]}")
    try:
        result = await call_next(ctx)
    except ValidationError:  # malformed request params: never echo them
        raise MCPError(
            code=INVALID_PARAMS, message=SAFE_MESSAGES[ErrorCode.INVALID_INPUT]
        ) from None
    # call_next returns the wire form: a camelCase dict (mcp 2.3.0), or a model.
    if isinstance(result, CallToolResult):
        result = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    if ctx.method == "tools/call" and isinstance(result, dict) and result.get("isError"):
        text = " ".join(str(b.get("text", "")) for b in result.get("content", ()))
        return _error_result(_safe_text(text))  # drops content, structuredContent, _meta
    return result
