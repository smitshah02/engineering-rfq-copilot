"""Verified-identity seam for customer-scoped routes. Fails closed.

No authenticator exists yet, so ``get_principal`` always answers 401 and every protected
route is unreachable in a real deployment. Nothing from the request (headers, query,
path, body, cookies) is read here, so no client-supplied value can become an identity.

Tests inject a principal with ``app.dependency_overrides[get_principal]``. The planned
authentication work replaces the body of ``get_principal`` with real verification, keeping
its signature, so routes and their customer scoping stay unchanged.
"""

from dataclasses import dataclass

from fastapi import HTTPException


@dataclass(frozen=True)
class Principal:
    """A verified caller, scoped to exactly one customer account."""

    customer_id: str

    def __post_init__(self) -> None:
        value = self.customer_id
        if not isinstance(value, str) or not value.strip() or len(value) > 64 or "\x00" in value:
            raise ValueError("Principal requires a verified customer identifier.")


async def get_principal() -> Principal:
    raise HTTPException(
        status_code=401,
        detail="Authentication required.",
        headers={"WWW-Authenticate": "Bearer"},
    )
