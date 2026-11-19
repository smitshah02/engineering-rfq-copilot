"""Human-only quotation decisions (K5, human decision): a separate ASGI application and process.

Serve with ``uvicorn --factory rfq_copilot.api.decision_app:create_decision_app``. The main
API (``api/app.py``) never mounts these routes and never reads the decision credential.

Boundary. The database cannot authenticate a person; it can only refuse decisions from any
role outside ``rfq_quote_decision`` (the k5_* triggers). This process is the only holder of
that credential (``DECISION_DATABASE_URL``; missing or invalid configuration fails startup,
unsafe live privileges fail before a session is yielded), and it records a decision only for:

- a bearer token verified by the existing ``TokenVerifier`` (signature, issuer, audience,
  lifetime, subject; ``api/auth.get_principal``), whose subject is an active user;
- the customer from that user's database binding, and permissions re-read from
  ``business.role_permissions`` (claims, headers and body never grant anything);
- the route's own permission (``approve_quote`` / ``reject_quote`` / ``release_quote``);
- then the service itself re-reads the user, grant, binding and approver role, locks the
  quote, recomputes the assessment and records the decision on the decision role
  (``require_decision_runtime``), which K5 re-checks row by row.

The actor is always the verified user: no body field names a user, role, customer or
approver. Correlation ids are server-generated. Responses carry the quotation id, version,
action, status and a replay flag only: no cost, margin, evidence, token or actor detail.
Agents, the Supervisor and MCP tools have no route here and no import of this module.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from importlib.metadata import version
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rfq_copilot.api.auth import Principal, get_principal, require_permission
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.errors import request_validation_handler
from rfq_copilot.api.routers import health, system
from rfq_copilot.api.schemas.common import Identifier
from rfq_copilot.api.tokens import TokenVerifier

if TYPE_CHECKING:
    from rfq_copilot.database import Database

# A refusal's HTTP status; any other fixed-vocabulary reason is a 409 conflict.
REFUSAL_STATUS = {"invalid_request": 422, "permission_denied": 403, "not_found": 404}

router = APIRouter(prefix="/quote-decisions", tags=["quote-decisions"])


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=256)


class ApprovalRequestBody(_Request):
    """What the approving human attests, bound to the version's RFQ source digest."""

    rfq_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_delivery_date: date | None = None
    no_contradictions: Literal[True]

    @field_validator("no_contradictions", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        # Python equality treats 1 as True; a human attestation requires JSON true.
        if value is not True:
            raise ValueError("Explicit confirmation required.")
        return value


class RejectionRequestBody(_Request):
    reason: str = Field(min_length=1, max_length=1000)


class ReleaseRequestBody(_Request):
    pass


class DecisionResult(BaseModel):
    quote_id: str
    version: int
    action: Literal["approved", "rejected", "released"]
    status: str
    replayed: bool


def unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="Service unavailable.")


async def get_decision_session(
    request: Request, principal: Annotated[Principal, Depends(get_principal)]
) -> AsyncIterator[Any]:
    """A session on the decision role; the route commits, anything else rolls back."""
    from rfq_copilot.database import DatabaseError

    try:
        async with request.app.state.decision_database.session() as session:
            yield session
    except DatabaseError:  # includes PrivilegedRuntimeRole: never an unchecked decision
        raise unavailable() from None


async def _decide(session, action, principal: Principal, quote_id: str, body, **extra):
    from rfq_copilot.quote_drafts import DraftRefused

    try:
        decision = await action(
            session,
            principal,
            quote_id,
            expected_version=body.expected_version,
            idempotency_key=body.idempotency_key,
            correlation_id=f"decision-{uuid.uuid4().hex}",
            **extra,
        )
    except DraftRefused as refused:
        status = REFUSAL_STATUS.get(refused.reason, 409)
        raise HTTPException(status_code=status, detail=refused.reason) from None
    await session.commit()
    return DecisionResult(
        quote_id=decision.quote_id,
        version=decision.version,
        action=decision.action,
        status=decision.status,
        replayed=decision.replayed,
    )


@router.post("/{quote_id}/approval", summary="Approve the pending current version")
async def approve(
    principal: Annotated[Principal, Depends(require_permission("approve_quote"))],
    quote_id: Annotated[Identifier, Path()],
    body: ApprovalRequestBody,
    session: Annotated[Any, Depends(get_decision_session)],
) -> DecisionResult:
    from rfq_copilot.quote_approval import ContextAttestation, approve_quote

    attested = ContextAttestation(body.rfq_source_sha256, body.requested_delivery_date, True)
    return await _decide(session, approve_quote, principal, quote_id, body, attestation=attested)


@router.post("/{quote_id}/rejection", summary="Reject the pending current version")
async def reject(
    principal: Annotated[Principal, Depends(require_permission("reject_quote"))],
    quote_id: Annotated[Identifier, Path()],
    body: RejectionRequestBody,
    session: Annotated[Any, Depends(get_decision_session)],
) -> DecisionResult:
    from rfq_copilot.quote_approval import reject_quote

    return await _decide(session, reject_quote, principal, quote_id, body, reason=body.reason)


@router.post("/{quote_id}/release", summary="Release the approved current version")
async def release(
    principal: Annotated[Principal, Depends(require_permission("release_quote"))],
    quote_id: Annotated[Identifier, Path()],
    body: ReleaseRequestBody,
    session: Annotated[Any, Depends(get_decision_session)],
) -> DecisionResult:
    from rfq_copilot.quote_approval import release_quote

    return await _decide(session, release_quote, principal, quote_id, body)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        if app.state.owns_decision_database:
            database, app.state.decision_database = app.state.decision_database, None
            app.state.database = None
            app.state.owns_decision_database = False
            await database.dispose()


def create_decision_app(
    settings: ApiSettings | None = None, *, database: "Database | None" = None
) -> FastAPI:
    """The decision application. ``database`` is an optional caller-owned decision
    ``Database``; without one, ``DECISION_DATABASE_URL`` is required now (fail closed)."""
    settings = ApiSettings.from_env() if settings is None else settings
    docs = settings.docs_enabled
    app = FastAPI(
        title="Engineering RFQ Copilot Decisions",
        version=version("engineering-rfq-copilot"),
        description="Human-only quotation approval, rejection and release. Requires identity.",
        openapi_url="/openapi.json" if docs else None,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        lifespan=lifespan,
        exception_handlers={RequestValidationError: request_validation_handler},
    )
    owned = database is None
    if owned:
        from rfq_copilot.config import DatabaseConfig
        from rfq_copilot.database import Database

        database = Database(DatabaseConfig.from_serving_env("development", decision=True))
    app.state.settings = settings
    app.state.token_verifier = TokenVerifier.from_settings(settings)
    # Identity reads (get_principal) use the same decision credential: no owner URL here.
    app.state.database = database
    app.state.owns_database = False
    app.state.write_database = None
    app.state.owns_write_database = False
    app.state.decision_database = database
    app.state.owns_decision_database = owned
    app.state.database_lock = asyncio.Lock()
    app.state.mcp = None
    app.include_router(health.router)
    app.include_router(router, prefix=f"/api/{system.API_VERSION}")
    return app
