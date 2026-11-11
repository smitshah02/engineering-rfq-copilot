"""Verified identity for customer-scoped routes. Fails closed.

``get_principal`` accepts only ``Authorization: Bearer <JWT>``:
1. No bearer credentials: 401. Headers such as ``X-Customer-Id``, cookies, query and
   body are never read, so no client-supplied value can become an identity.
2. No configured trust boundary (``RFQ_API_JWT_*``): 503. Nothing is authorized.
3. ``TokenVerifier.subject`` checks signature, algorithm, issuer, audience, lifetime and
   subject (``api/tokens.py``). Any failure: 401, before any database access.
4. In a short session of its own (the read-only ``Database``, not the route's session,
   which itself depends on this function), the subject must name an active user in
   ``business.users``; otherwise 401, the same answer as a bad token.
5. The customer comes only from ``repositories.customer_binding``: an unrevoked row in
   ``business.user_customer_bindings`` whose customer's account status is eligible.
   Claims in the token (customer, role, scope) are ignored. No eligible binding: 403.
6. In the same session, the permissions come only from ``repositories.granted_permissions``:
   ``business.role_permissions`` rows with ``allowed = true`` for ``users.role``. Nothing
   is cached, so a grant, role, binding or ``active`` change applies to the next request.

Authorization (Day 21): every protected route depends on ``require_permission(name)``,
which runs ``get_principal`` and answers 403 unless the permission was granted. It never
compares role names. Customer scoping stays in each route's queries (cross-customer: 404).
Tests may still inject a principal with ``app.dependency_overrides[get_principal]``; the
injected principal's ``permissions`` are then checked the same way.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from rfq_copilot.api.tokens import InvalidToken  # noqa: F401 - re-exported for callers

bearer = HTTPBearer(
    auto_error=False,
    scheme_name="BearerToken",
    description="JWT from the configured issuer; its subject must be an active user.",
)


@dataclass(frozen=True)
class Principal:
    """A verified caller, scoped to exactly one customer account."""

    customer_id: str
    permissions: frozenset[str] = frozenset()  # from business.role_permissions; none by default
    user_id: str | None = None  # the verified business.users row (None only in injected tests)
    # Set when RFQ assignments are enforced: RFQ-scoped queries then also require an
    # active business.rfq_assignments row for user_id.
    assignment_required: bool = False

    @property
    def assigned_user(self) -> str | None:
        """The user RFQ queries must be assigned to, or None when assignments are off."""
        if not self.assignment_required:
            return None
        if self.user_id is None:
            raise ValueError("Assignment enforcement needs a verified user.")
        return self.user_id

    def __post_init__(self) -> None:
        value = self.customer_id
        if not isinstance(value, str) or not value.strip() or len(value) > 64 or "\x00" in value:
            raise ValueError("Principal requires a verified customer identifier.")
        if not isinstance(self.permissions, frozenset) or not all(
            isinstance(item, str) for item in self.permissions
        ):
            raise ValueError("Principal permissions must be a frozenset of names.")


def unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Authentication required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="Service unavailable.")


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if credentials is None:
        raise unauthenticated()
    verifier = request.app.state.token_verifier
    if verifier is None:
        raise _unavailable()
    try:
        token = verifier.verify(credentials.credentials)
    except InvalidToken:
        raise unauthenticated() from None

    import hashlib

    from sqlalchemy.exc import SQLAlchemyError

    from rfq_copilot import repositories
    from rfq_copilot.api.db import get_database
    from rfq_copilot.database import DatabaseError

    settings = request.app.state.settings
    requested = _requested_customer(request) if settings.customer_context == "selectable" else None
    database = await get_database(request)
    try:
        async with database.session() as session:
            user = await repositories.find_active_user(session, token.subject)
            if user is not None and settings.token_revocation == "enforced":
                digest = hashlib.sha256(credentials.credentials.encode()).hexdigest()
                if await repositories.token_revoked(session, user, digest, token.issued_at):
                    user = None  # a revoked token is no longer a credential: 401
            customer_id = None
            if user is not None:
                customer_id = await _customer_for(session, user, settings, requested)
            granted = None
            if customer_id is not None:
                granted = await repositories.granted_permissions(session, user)
    except (DatabaseError, SQLAlchemyError):
        raise _unavailable() from None  # e.g. revocation status unknown: never "not revoked"
    if user is None:
        raise unauthenticated()  # unknown, inactive or revoked: indistinguishable from bad
    if customer_id is None:
        raise HTTPException(status_code=403, detail="Forbidden.")
    return Principal(
        customer_id,
        granted,
        user_id=user.user_id,
        assignment_required=settings.rfq_assignments == "required",
    )


CUSTOMER_CONTEXT_HEADER = "X-RFQ-Customer-Context"


def _requested_customer(request: Request) -> str | None:
    """The customer a multi-customer user selects; a selection, never an authorization."""
    values = request.headers.getlist(CUSTOMER_CONTEXT_HEADER)
    if not values:
        return None
    if len(values) != 1 or not values[0].isascii() or not values[0].isprintable():
        return ""  # ambiguous or malformed: matches no binding
    value = values[0]
    return value if 0 < len(value) <= 64 and value == value.strip() else ""


async def _customer_for(session, user, settings, requested: str | None) -> str | None:
    from rfq_copilot import repositories

    if settings.customer_context != "selectable":
        return await repositories.customer_binding(session, user)  # exactly one, or none
    customers = await repositories.eligible_customers(session, user)
    if requested is None:
        return customers[0] if len(customers) == 1 else None  # never a default among many
    return requested if requested in customers else None


# Permission names as stored in business.permissions (dataset roles_permissions.csv).
VIEW_CUSTOMER = "view_customer"
VIEW_ASSIGNED_RFQS = "view_assigned_rfqs"
CREATE_RFQ = "create_rfq"
# Policy overlay rbac-policy-v2 (seeding/policy.py, pinned); absent until applied.
VIEW_CATALOG = "view_catalog"
VIEW_QUOTE = "view_quote"
ATTACH_RFQ_DOCUMENT = "attach_rfq_document"

# The one table of route operation -> required permission, per policy version. v1 is the
# approved Day 21 matrix; v2 activates only with RFQ_API_PERMISSION_POLICY=v2.
ROUTE_PERMISSIONS: dict[str, dict[str, str]] = {
    "v1": {
        "customer_profile": VIEW_CUSTOMER,
        "rfq_read": VIEW_ASSIGNED_RFQS,
        "attachment_list": VIEW_ASSIGNED_RFQS,
        "catalog_read": VIEW_ASSIGNED_RFQS,
        "quote_read": VIEW_ASSIGNED_RFQS,
        "attachment_upload": CREATE_RFQ,
    },
    "v2": {
        "customer_profile": VIEW_CUSTOMER,
        "rfq_read": VIEW_ASSIGNED_RFQS,
        "attachment_list": VIEW_ASSIGNED_RFQS,
        "catalog_read": VIEW_CATALOG,
        "quote_read": VIEW_QUOTE,
        "attachment_upload": ATTACH_RFQ_DOCUMENT,
    },
}


def require_permission(permission: str):
    """A dependency that yields the verified principal only if it holds ``permission``.

    Order: authentication (401), customer binding (403), then this check (403), all before
    the route's own dependencies (session, storage, body). Deny by default.
    """

    async def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if permission not in principal.permissions:
            raise HTTPException(status_code=403, detail="Forbidden.")
        return principal

    dependency.required_permission = permission  # read by the route-coverage test
    return dependency


def require_route_permission(operation: str):
    """``require_permission`` for a route operation under the configured policy version.

    The permission is looked up per request from ``ROUTE_PERMISSIONS`` and the app's
    settings; an unknown operation or policy is a startup-time KeyError, never an allow.
    """
    for table in ROUTE_PERMISSIONS.values():
        table[operation]  # noqa: B018 - fail at import/registration time on a typo

    async def dependency(
        request: Request, principal: Annotated[Principal, Depends(get_principal)]
    ) -> Principal:
        policy = request.app.state.settings.permission_policy
        if ROUTE_PERMISSIONS[policy][operation] not in principal.permissions:
            raise HTTPException(status_code=403, detail="Forbidden.")
        return principal

    dependency.route_operation = operation
    return dependency
