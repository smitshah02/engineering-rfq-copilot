"""The verified customer's own profile."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from rfq_copilot.api.auth import Principal, require_route_permission
from rfq_copilot.api.db import get_read_session
from rfq_copilot.api.schemas.reads import CustomerContact, CustomerProfile

router = APIRouter(prefix="/customers", tags=["customers"])


@router.get("/me", summary="The verified customer's profile")
async def me(
    principal: Annotated[Principal, Depends(require_route_permission("customer_profile"))],
    session: Annotated[Any, Depends(get_read_session)],
) -> CustomerProfile:
    from rfq_copilot.repositories import get_customer

    details = await get_customer(session, principal.customer_id)
    if details is None:
        raise HTTPException(status_code=404, detail="Not found.")
    customer = details.customer
    return CustomerProfile(
        customer_id=customer.customer_id,
        company_name=customer.company_name,
        industry=customer.industry,
        city=customer.city,
        state=customer.state,
        country=customer.country,
        payment_terms=customer.payment_terms,
        preferred_shipping=customer.preferred_shipping,
        contacts=[
            CustomerContact(
                contact_name=c.contact_name, email=c.email, phone=c.phone, is_primary=c.is_primary
            )
            for c in details.contacts
        ],
    )
