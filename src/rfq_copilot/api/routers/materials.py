"""Material catalog identities for verified callers; no cost or supply data."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from rfq_copilot.api.auth import Principal, require_route_permission
from rfq_copilot.api.db import get_read_session
from rfq_copilot.api.errors import VALIDATION_ERROR_RESPONSES
from rfq_copilot.api.schemas.reads import MaterialListQuery, MaterialPage, MaterialSummary
from rfq_copilot.mcp.contracts import MaterialSpecOutput

router = APIRouter(prefix="/materials", tags=["materials"], responses=VALIDATION_ERROR_RESPONSES)


@router.get("", summary="Catalog materials matching every given filter, by material_id")
async def list_catalog_materials(
    principal: Annotated[Principal, Depends(require_route_permission("catalog_read"))],
    query: Annotated[MaterialListQuery, Query()],
    session: Annotated[Any, Depends(get_read_session)],
) -> MaterialPage:
    return await material_page(session, query)


async def material_page(session: Any, query: MaterialListQuery) -> MaterialPage:
    """One catalog page (shared by the REST route and the MCP search_materials tool)."""
    from rfq_copilot.repositories import find_materials, list_materials

    identifiers = await find_materials(
        session,
        grade=query.grade,
        category=query.category,
        thickness_mm=query.thickness_mm,
        finish_id=query.finish_id,
        limit=query.limit + 1,  # one extra row tells whether another page exists
        offset=query.offset,
    )
    window = identifiers
    rows = await list_materials(session, window[: query.limit])
    return MaterialPage(
        items=[
            MaterialSummary(material_id=m.material_id, category=m.category, grade=m.grade)
            for m in rows
        ],
        limit=query.limit,
        offset=query.offset,
        has_more=len(window) > query.limit,
    )


# Day 29: mounted only when create_app(mcp=...) has a trusted credential provider.
mcp_router = APIRouter(
    prefix="/materials", tags=["materials"], responses=VALIDATION_ERROR_RESPONSES
)
_MCP_STATUS = {
    "not_found": (404, "Not found."),
    "invalid_input": (422, "Invalid request."),
    "forbidden": (403, "Forbidden."),
    "busy": (503, "Service unavailable."),
    "unavailable": (503, "Service unavailable."),
    "unauthenticated": (503, "Service unavailable."),  # delegation refused: our fault
    "conflict": (503, "Service unavailable."),
    "timeout": (504, "Upstream timeout."),
    "tool_error": (502, "Upstream error."),
    "invalid_output": (502, "Upstream error."),
}


@mcp_router.get(
    "/{material_id}/specification",
    summary="One catalog material's specification, through the Manufacturing MCP server",
)
async def material_specification(
    request: Request,
    principal: Annotated[Principal, Depends(require_route_permission("catalog_read"))],
    material_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> MaterialSpecOutput:
    from rfq_copilot.mcp.client import McpCallError

    integration = request.app.state.mcp
    rest_credential = request.headers.get("authorization", "").partition(" ")[2]
    try:
        return await integration.call(
            principal,
            "get_material",
            {"material_id": material_id},
            rest_credential=rest_credential,
        )
    except McpCallError as error:
        status, detail = _MCP_STATUS[error.code]
        raise HTTPException(status_code=status, detail=detail) from None
