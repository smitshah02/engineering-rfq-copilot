"""Shared adapter plumbing for the three MCP servers (no business logic).

- ``Snapshot``: the caller-owned session factory; production passes
  ``Database.snapshot_session`` (one REPEATABLE READ, READ ONLY transaction per call).
- ``pinned_rules``: server-chosen rule versions, sha256-checked against the manifest.
- ``to_parts``: the only place argument data becomes ``entities.Part``; its ValueErrors
  are input errors (INVALID_INPUT). Anywhere else a ValueError is a defect (INTERNAL).
- ``build_server``: registers one logical server's tools from ``contracts.TOOLS``, each
  wrapped by ``_authorized`` (the single enforcement point, Day 27): the request-scoped
  ``McpPrincipal`` from the SDK auth context must exist and hold the tool's v2 permission
  before the handler runs; otherwise UNAUTHENTICATED / PERMISSION_DENIED and no business
  snapshot is opened. An in-memory ``mcp.Client`` without HTTP bearer auth therefore gets
  UNAUTHENTICATED: in-memory dispatch is never proof of authentication.
- ``planning_parts``: a stored ``rfq_id`` is loaded inside the business snapshot with
  ``get_rfq(customer_id=principal.customer_id, assigned_user=...)``; absent, foreign and
  unassigned RFQs are all NOT_FOUND.

Servers built here must be called through the SDK dispatch path (``mcp.Client`` or a
transport), because ``sanitize_tool_errors`` is wire middleware. Direct
``MCPServer.call_tool()`` calls bypass it and are for nothing but SDK internals.
"""

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from rfq_copilot.api.schemas.rfq import PartSpecification
from rfq_copilot.entities import Part
from rfq_copilot.mcp.contracts import (
    READ_ONLY,
    TOOLS,
    ErrorCode,
    PlanningInput,
    ToolContract,
    tool_failure,
)
from rfq_copilot.mcp.safety import sanitize_tool_errors
from rfq_copilot.mcp.security import McpPrincipal, McpTokenVerifier
from rfq_copilot.repositories.queries import RuleIntegrityError, get_rfq, get_rule_version
from rfq_copilot.repositories.results import RuleVersionDetails
from rfq_copilot.repositories.rule_manifest import TRUSTED_RULES
from rfq_copilot.services.quotation import validate_lines

# Server-side pins; never arguments. Changing one is a code-reviewed change.
MANUFACTURING_RULE_VERSION = "mfg-v1.0"
PRICING_RULE_VERSION = "price-v1.0"

Snapshot = Callable[[], AbstractAsyncContextManager[AsyncSession]]
Handler = Callable[[BaseModel, McpPrincipal], Awaitable[BaseModel]]


async def pinned_rules(session: AsyncSession, version: str) -> RuleVersionDetails:
    kind, source_sha256, _ = TRUSTED_RULES[version]
    rules = await get_rule_version(session, kind, version, expected_sha256=source_sha256)
    if rules is None:
        raise RuleIntegrityError(f"Rule {version} is not seeded.")
    return rules


async def planning_rules(session: AsyncSession) -> dict[str, RuleVersionDetails]:
    """The keyword arguments every supply and pricing service takes."""
    return {
        "manufacturing_rules": await pinned_rules(session, MANUFACTURING_RULE_VERSION),
        "pricing_rules": await pinned_rules(session, PRICING_RULE_VERSION),
    }


def to_parts(specs: Sequence[PartSpecification]) -> tuple[Part, ...]:
    try:
        parts = tuple(Part.from_mapping(s.model_dump(exclude_none=True)) for s in specs)
        validate_lines(parts, range(1, len(parts) + 1))  # e.g. duplicate part_ids
    except ValueError:
        raise tool_failure(ErrorCode.INVALID_INPUT) from None
    return parts


def prospective_parts(request: PlanningInput) -> tuple[Part, ...] | None:
    """Converted caller parts (before any session opens), or None for a stored RFQ."""
    return None if request.rfq_id is not None else to_parts(request.parts)


async def planning_parts(
    session: AsyncSession,
    request: PlanningInput,
    principal: McpPrincipal,
    prospective: tuple[Part, ...] | None,
) -> tuple[tuple[Part, ...], tuple[int, ...] | None]:
    """(parts, line numbers) for the evaluation; a stored RFQ is read in this snapshot."""
    if prospective is not None:
        return prospective, None
    details = await get_rfq(
        session,
        request.rfq_id,
        customer_id=principal.customer_id,  # never an argument
        assigned_user=principal.assigned_user,
    )
    if details is None:
        raise tool_failure(ErrorCode.NOT_FOUND)  # absent, foreign or unassigned: identical
    lines = details.runtime_lines
    if not lines:
        raise tool_failure(ErrorCode.INVALID_INPUT)  # visible RFQ without evaluable lines
    return tuple(Part.from_line(line) for line in lines), tuple(line.line_number for line in lines)


def _authorized(contract: ToolContract, handler: Handler) -> Callable:
    async def tool(request):
        principal = get_access_token()  # request-scoped (SDK context variable)
        if not isinstance(principal, McpPrincipal):
            raise tool_failure(ErrorCode.UNAUTHENTICATED)
        if contract.permission not in principal.permissions:
            raise tool_failure(ErrorCode.PERMISSION_DENIED)
        return await handler(request, principal)

    tool.__name__ = contract.name
    tool.__signature__ = inspect.Signature(
        [
            inspect.Parameter(
                "request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=contract.input_model
            )
        ],
        return_annotation=contract.output_model,
    )
    tool.__annotations__ = {"request": contract.input_model, "return": contract.output_model}
    return tool


def build_server(
    server: str, handlers: Mapping[str, Handler], verifier: McpTokenVerifier | None = None
) -> MCPServer:
    """``verifier`` arms the SDK HTTP bearer layer (Day 28 host). Without it the server
    still fails closed: no request can carry an ``McpPrincipal``."""
    contracts = [tool for tool in TOOLS if tool.server == server]
    if {tool.name for tool in contracts} != set(handlers):
        raise RuntimeError(f"{server}: handlers do not match the tool contracts.")
    auth = {}
    if verifier is not None:
        auth = {"token_verifier": verifier, "auth": verifier.settings.auth_settings()}
    mcp = MCPServer(f"rfq-{server}", middleware=[sanitize_tool_errors], **auth)
    for tool in contracts:
        mcp.add_tool(
            _authorized(tool, handlers[tool.name]),
            name=tool.name,
            description=tool.description,
            annotations=READ_ONLY,
            structured_output=True,
        )
    return mcp


def project(model: type[BaseModel], result: object, **flattened: object) -> BaseModel:
    """Validate a frozen service result into its output model; ``flattened`` supplies the
    few contract fields that live on nested service objects (named in contracts.py)."""
    data = {
        name: flattened[name] if name in flattened else getattr(result, name)
        for name in model.model_fields
    }
    return model.model_validate(data, from_attributes=True)
