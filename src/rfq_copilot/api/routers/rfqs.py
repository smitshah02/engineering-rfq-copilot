"""The verified customer's RFQs. Another customer's RFQ is indistinguishable from none.

``create_router`` (``POST /rfqs``) is mounted only under ``permission_policy=v3``
(``api.app``); under v1/v2 the route does not exist. It needs the ``customer_create_rfq``
grant here, then ``rfq_submission`` re-checks the actor, its ``customer`` role, the grant
and the binding inside the write transaction.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from rfq_copilot.api.auth import (
    CUSTOMER_CREATE_RFQ,
    Principal,
    require_permission,
    require_route_permission,
)
from rfq_copilot.api.db import get_read_session, get_write_session
from rfq_copilot.api.errors import VALIDATION_ERROR_RESPONSES, ValidationErrorResponse
from rfq_copilot.api.schemas.common import Identifier
from rfq_copilot.api.schemas.reads import RfqDetail, RfqLine, RfqListQuery, RfqPage, RfqSummary
from rfq_copilot.api.schemas.rfq import (
    PartSpecification,
    RfqReceipt,
    RfqRejection,
    RfqSubmissionRequest,
)

router = APIRouter(prefix="/rfqs", tags=["rfqs"], responses=VALIDATION_ERROR_RESPONSES)
create_router = APIRouter(prefix="/rfqs", tags=["rfqs"], responses=VALIDATION_ERROR_RESPONSES)

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


# Static text; never built from the request.
CREATE_MESSAGES: dict[str, str] = {
    "idempotency_key_invalid": "Idempotency-Key must be 1 to 256 visible ASCII characters.",
    "idempotency_key_conflict": "Idempotency-Key was already used for a different RFQ.",
    "unknown_material": "parts[{index}].material_id is not a known material.",
    "unknown_finish": "parts[{index}].finish_id is not a known finish.",
    "delivery_date_not_stored": "requested_delivery_date cannot be stored yet; omit it.",
    "rejected_by_constraints": "The RFQ violates a data rule (for example bend_radius_mm 0).",
}
_REJECTED = {"model": RfqRejection, "description": "RFQ refused; nothing was stored."}


def create_rejection(reason: str, index: int | None = None) -> JSONResponse:
    status = 409 if reason == "idempotency_key_conflict" else 422
    message = CREATE_MESSAGES[reason].format(index=index)
    body = RfqRejection(code="RFQ_REJECTED", message=message, reason=reason)
    return JSONResponse(status_code=status, content=body.model_dump())


@create_router.post(
    "",
    status_code=201,
    summary="Submit an RFQ for the verified customer",
    responses={
        409: _REJECTED,
        422: {"model": ValidationErrorResponse | RfqRejection, "description": "Invalid request."},
        503: {"description": "Database unavailable."},
    },
)
async def submit_rfq(
    request: Request,
    principal: Annotated[Principal, Depends(require_permission(CUSTOMER_CREATE_RFQ))],
    body: RfqSubmissionRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    session: Annotated[Any, Depends(get_write_session)],
) -> RfqReceipt:
    from rfq_copilot.repositories.idempotency import valid_key
    from rfq_copilot.rfq_submission import RfqRefused, create_customer_rfq

    if not valid_key(idempotency_key):
        return create_rejection("idempotency_key_invalid")
    try:
        receipt, replayed = await create_customer_rfq(
            session, principal, body, idempotency_key,
            enqueue=request.app.state.settings.workflow_queue == "enabled",
        )  # fmt: skip
    except RfqRefused as refused:
        if refused.reason == "permission_denied":
            raise HTTPException(status_code=403, detail="Forbidden.") from None
        if refused.reason == "queue_not_activated":  # a deployment state, not the request
            raise HTTPException(status_code=503, detail="Service unavailable.") from None
        return create_rejection(refused.reason, refused.index)
    await session.commit()  # an ambiguous COMMIT failure is safe to retry with the same key
    if replayed:
        return JSONResponse(status_code=201, content=receipt.model_dump(mode="json"), headers={"Idempotent-Replay": "true"})  # fmt: skip
    return receipt
