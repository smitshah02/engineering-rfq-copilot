"""Quotes a human has released to the verified customer. Everything else is 404."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path

from rfq_copilot.api.auth import Principal, get_principal
from rfq_copilot.api.db import get_read_session
from rfq_copilot.api.errors import VALIDATION_ERROR_RESPONSES
from rfq_copilot.api.schemas.common import Identifier
from rfq_copilot.api.schemas.reads import ReleasedQuote, ReleasedQuoteLine

router = APIRouter(prefix="/quotes", tags=["quotes"], responses=VALIDATION_ERROR_RESPONSES)


@router.get("/{quote_id}", summary="A released quote of the verified customer")
async def get_released_quote(
    principal: Annotated[Principal, Depends(get_principal)],
    quote_id: Annotated[Identifier, Path()],
    session: Annotated[Any, Depends(get_read_session)],
) -> ReleasedQuote:
    from rfq_copilot.repositories import list_quotation_history

    # ponytail: reuses the customer-scoped history read and picks one quote; add a keyed
    # repository read if a customer's quote count grows large.
    history = await list_quotation_history(session, principal.customer_id)
    match = next((q for q in history if q.quote.quote_id == quote_id), None)
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
