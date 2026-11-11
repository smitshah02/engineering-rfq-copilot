"""Quotes a human has released to the verified customer. Everything else is 404."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path

from rfq_copilot.api.auth import Principal, require_route_permission
from rfq_copilot.api.db import get_read_session
from rfq_copilot.api.errors import VALIDATION_ERROR_RESPONSES
from rfq_copilot.api.schemas.common import Identifier
from rfq_copilot.api.schemas.reads import ReleasedQuote, ReleasedQuoteLine

router = APIRouter(prefix="/quotes", tags=["quotes"], responses=VALIDATION_ERROR_RESPONSES)


@router.get("/{quote_id}", summary="A released quote of the verified customer")
async def get_released_quote(
    principal: Annotated[Principal, Depends(require_route_permission("quote_read"))],
    quote_id: Annotated[Identifier, Path()],
    session: Annotated[Any, Depends(get_read_session)],
) -> ReleasedQuote:
    from rfq_copilot.repositories import get_released_quotation

    # Ownership and release are filtered in SQL; the check below is defence in depth.
    match = await get_released_quotation(
        session, quote_id, customer_id=principal.customer_id, assigned_user=principal.assigned_user
    )
    # Only a human release makes a quote customer-facing; approved is not released.
    if match is None or match.quote.status != "released" or match.quote.released_at is None:
        raise HTTPException(status_code=404, detail="Not found.")
    quote = match.quote
    version = next((v for v in match.versions if v.version.version == quote.approved_version), None)
    if version is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return ReleasedQuote(
        quote_id=quote.quote_id,
        rfq_id=quote.rfq_id,
        status="released",
        released_at=quote.released_at,
        version=version.version.version,
        currency=version.version.currency,
        total_usd=version.version.total_usd,
        expiry_date=version.version.expiry_date,
        estimated_lead_business_days=version.version.estimated_lead_business_days,
        lines=[
            ReleasedQuoteLine(
                line_number=line.line_number,
                part_id=line.part_id,
                quantity=line.quantity,
                line_total_usd=line.line_total_usd,
            )
            for line in version.line_items
        ],
    )
