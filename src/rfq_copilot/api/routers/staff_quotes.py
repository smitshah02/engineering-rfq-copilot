"""Staff quotation reads, limited action assessments, and ordinary-role submission."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from rfq_copilot.api.auth import VIEW_ASSIGNED_RFQS, Principal, require_permission
from rfq_copilot.api.db import get_assessment_session, get_read_session, get_write_session
from rfq_copilot.api.errors import VALIDATION_ERROR_RESPONSES
from rfq_copilot.api.schemas.common import Identifier
from rfq_copilot.api.schemas.reads import RfqSummary
from rfq_copilot.api.schemas.rfq import MAX_PARTS, PartSpecification
from rfq_copilot.api.schemas.staff_quotes import (
    StaffQuoteActions,
    StaffQuoteDetail,
    StaffQuoteLine,
    StaffQuoteListQuery,
    StaffQuotePage,
    StaffQuoteSummary,
    SubmissionResult,
    SubmitForReviewBody,
)

router = APIRouter(
    prefix="/staff/quotes", tags=["staff-quotes"], responses=VALIDATION_ERROR_RESPONSES
)
# The existing operational staff roles. Membership never replaces the read grant.
# In particular, the workflow service shares that grant but is not a staff identity.
_STAFF_ROLES = frozenset(
    {"sales_representative", "estimator", "manufacturing_engineer", "sales_manager"}
)


async def require_staff_reader(
    principal: Annotated[Principal, Depends(require_permission(VIEW_ASSIGNED_RFQS))],
    session: Annotated[Any, Depends(get_read_session)],
) -> Principal:
    return await _require_staff_role(session, principal)


async def _require_staff_role(session: Any, principal: Principal) -> Principal:
    """Check the trusted staff role in the session appropriate to the route's work."""
    from sqlalchemy import select

    from rfq_copilot.models import User

    role = await session.scalar(
        select(User.role).where(
            User.user_id == principal.user_id,
            User.active.is_(True),
            User.role.in_(_STAFF_ROLES),
        )
    )
    if role is None:
        raise HTTPException(status_code=403, detail="Forbidden.")
    return principal


@router.get("", summary="Authorized staff quotations, newest first")
async def list_staff_quotes(
    principal: Annotated[Principal, Depends(require_staff_reader)],
    query: Annotated[StaffQuoteListQuery, Query()],
    session: Annotated[Any, Depends(get_read_session)],
) -> StaffQuotePage:
    from rfq_copilot.repositories import list_staff_quotations

    rows = await list_staff_quotations(
        session,
        principal.customer_id,
        limit=query.limit + 1,
        offset=query.offset,
        assigned_user=principal.assigned_user,
        status=query.status,
        q=query.q,
    )
    return StaffQuotePage(
        items=[StaffQuoteSummary.model_validate(row) for row in rows[: query.limit]],
        limit=query.limit,
        offset=query.offset,
        has_more=len(rows) > query.limit,
    )


@router.get("/{quote_id}", summary="The current stored quotation version for authorized staff")
async def get_staff_quote(
    principal: Annotated[Principal, Depends(require_staff_reader)],
    quote_id: Annotated[Identifier, Path()],
    session: Annotated[Any, Depends(get_read_session)],
) -> StaffQuoteDetail:
    from rfq_copilot.repositories import get_staff_quotation

    match = await get_staff_quotation(
        session,
        quote_id,
        customer_id=principal.customer_id,
        assigned_user=principal.assigned_user,
        max_lines=MAX_PARTS,
    )
    if match is None:
        raise HTTPException(status_code=404, detail="Not found.")
    quote, current = match.quote, match.versions[0]
    version, rfq = current.version, quote.rfq
    return StaffQuoteDetail(
        quote_id=quote.quote_id,
        rfq_id=quote.rfq_id,
        status=quote.status,
        current_version=version.version,
        quote_version_id=version.quote_version_id,
        created_at=quote.created_at,
        version_created_at=version.created_at,
        expiry_date=version.expiry_date,
        currency=version.currency,
        total_usd=version.total_usd,
        estimated_lead_business_days=version.estimated_lead_business_days,
        rfq=RfqSummary(rfq_id=rfq.rfq_id, status=rfq.status, submitted_at=rfq.submitted_at),
        lines=[
            StaffQuoteLine(
                line_number=line.line_number,
                part_id=line.part_id,
                quantity=line.quantity,
                line_total_usd=line.line_total_usd,
                part=PartSpecification(
                    part_id=line.part_id,
                    **{
                        name: line.part_json[name]
                        for name in PartSpecification.model_fields
                        if name != "part_id" and name in line.part_json
                    },
                ),
            )
            for line in current.line_items
        ],
    )


@router.get("/{quote_id}/actions", summary="Current server-assessed staff actions")
async def get_staff_quote_actions(
    response: Response,
    principal: Annotated[Principal, Depends(require_permission(VIEW_ASSIGNED_RFQS))],
    quote_id: Annotated[Identifier, Path()],
    expected_version: Annotated[int, Query(ge=1)],
    session: Annotated[Any, Depends(get_assessment_session)],
) -> StaffQuoteActions:
    from rfq_copilot.quote_approval import assess_staff_actions
    from rfq_copilot.repositories import get_staff_quotation

    response.headers["Cache-Control"] = "no-store"
    # Keep authorization and assessment on the lock-capable, rollback-only session.
    # A SQL read-only dependency held open here would prevent FOR SHARE when injected
    # databases share a connection (as the isolated transaction fixtures do).
    await _require_staff_role(session, principal)
    match = await get_staff_quotation(
        session,
        quote_id,
        customer_id=principal.customer_id,
        assigned_user=principal.assigned_user,
        max_lines=MAX_PARTS,
    )
    if match is None:
        raise HTTPException(status_code=404, detail="Not found.")
    if match.quote.current_version != expected_version:
        raise HTTPException(status_code=409, detail="stale_version")
    actions, context, reviews = await assess_staff_actions(
        session, principal, quote_id, expected_version
    )
    return StaffQuoteActions(
        quote_id=quote_id,
        expected_version=expected_version,
        actions=actions,
        attestation_context=context,
        reviews=reviews,
    )


@router.post("/{quote_id}/submit-for-review", summary="Submit the current version for human review")
async def submit_staff_quote(
    principal: Annotated[Principal, Depends(require_permission("request_approval"))],
    quote_id: Annotated[Identifier, Path()],
    body: SubmitForReviewBody,
    session: Annotated[Any, Depends(get_write_session)],
) -> SubmissionResult:
    from rfq_copilot.quote_approval import submit_for_review
    from rfq_copilot.quote_drafts import DraftRefused

    try:
        result = await submit_for_review(
            session,
            principal,
            quote_id,
            expected_version=body.expected_version,
            idempotency_key=body.idempotency_key,
            correlation_id=f"submission-{uuid.uuid4().hex}",
        )
    except DraftRefused as refused:
        status = {"invalid_request": 422, "permission_denied": 403, "not_found": 404}.get(
            refused.reason, 409
        )
        raise HTTPException(status_code=status, detail=refused.reason) from None
    await session.commit()
    return SubmissionResult(
        quote_id=result.quote_id,
        version=result.version,
        action=result.action,
        status=result.status,
        replayed=result.replayed,
    )
