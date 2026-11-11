"""Material catalog identities for verified callers; no cost or supply data."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from rfq_copilot.api.auth import Principal, require_route_permission
from rfq_copilot.api.db import get_read_session
from rfq_copilot.api.errors import VALIDATION_ERROR_RESPONSES
from rfq_copilot.api.schemas.reads import MaterialListQuery, MaterialPage, MaterialSummary

router = APIRouter(prefix="/materials", tags=["materials"], responses=VALIDATION_ERROR_RESPONSES)


@router.get("", summary="Catalog materials matching every given filter, by material_id")
async def list_catalog_materials(
    principal: Annotated[Principal, Depends(require_route_permission("catalog_read"))],
    query: Annotated[MaterialListQuery, Query()],
    session: Annotated[Any, Depends(get_read_session)],
) -> MaterialPage:
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
