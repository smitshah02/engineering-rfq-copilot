"""The verified customer's RFQs. Another customer's RFQ is indistinguishable from none."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from rfq_copilot.api.auth import Principal, require_route_permission
from rfq_copilot.api.db import get_read_session
from rfq_copilot.api.errors import VALIDATION_ERROR_RESPONSES
from rfq_copilot.api.schemas.common import Identifier
from rfq_copilot.api.schemas.reads import RfqDetail, RfqLine, RfqListQuery, RfqPage, RfqSummary
from rfq_copilot.api.schemas.rfq import PartSpecification

router = APIRouter(prefix="/rfqs", tags=["rfqs"], responses=VALIDATION_ERROR_RESPONSES)

_PART_FIELDS = tuple(PartSpecification.model_fields)


@router.get("", summary="The verified customer's RFQs, newest first")
async def list_customer_rfqs(
    principal: Annotated[Principal, Depends(require_route_permission("rfq_read"))],
    query: Annotated[RfqListQuery, Query()],
    session: Annotated[Any, Depends(get_read_session)],
) -> RfqPage:
    from rfq_copilot.repositories import list_rfqs

    rows = await list_rfqs(
        session,
        principal.customer_id,
        limit=query.limit + 1,
        offset=query.offset,
        assigned_user=principal.assigned_user,
    )
    return RfqPage(
        items=[
            RfqSummary(rfq_id=r.rfq_id, status=r.status, submitted_at=r.submitted_at)
            for r in rows[: query.limit]
        ],
        limit=query.limit,
        offset=query.offset,
        has_more=len(rows) > query.limit,
    )


@router.get("/{rfq_id}", summary="One of the verified customer's RFQs")
async def get_customer_rfq(
    principal: Annotated[Principal, Depends(require_route_permission("rfq_read"))],
    rfq_id: Annotated[Identifier, Path()],
    session: Annotated[Any, Depends(get_read_session)],
) -> RfqDetail:
    from rfq_copilot.repositories import get_rfq

    details = await get_rfq(
        session, rfq_id, customer_id=principal.customer_id, assigned_user=principal.assigned_user
    )
    if details is None:
        raise HTTPException(status_code=404, detail="Not found.")
    rfq = details.rfq
    return RfqDetail(
        rfq_id=rfq.rfq_id,
        status=rfq.status,
        submitted_at=rfq.submitted_at,
        lines=[
            RfqLine(
                line_number=line.line_number,
                part=PartSpecification(**{f: getattr(line, f) for f in _PART_FIELDS}),
            )
            for line in details.runtime_lines
        ],
    )
