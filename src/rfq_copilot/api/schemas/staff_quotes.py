"""Staff quotation facts, limited action hints, and review submission contracts."""

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from rfq_copilot.api.schemas.common import Identifier, Money, PositiveCount
from rfq_copilot.api.schemas.reads import ReleasedQuoteLine, RfqSummary, _PageQuery
from rfq_copilot.api.schemas.rfq import MAX_PARTS, PartSpecification

QuoteStatus = Literal[
    "draft",
    "revised_draft",
    "pending_approval",
    "approved",
    "rejected",
    "expired",
    "clarification_required",
    "blocked",
    "released",
]


class StaffQuoteListQuery(_PageQuery):
    status: QuoteStatus | None = None
    # Exact quote/RFQ identifier lookup, never a wildcard or substring scan.
    q: Identifier | None = None


class StaffQuoteSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    quote_id: Identifier
    rfq_id: Identifier
    status: QuoteStatus
    current_version: PositiveCount
    created_at: AwareDatetime
    expiry_date: date | None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    total_usd: Money | None


class StaffQuotePage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[StaffQuoteSummary, ...]
    limit: int
    offset: int
    has_more: bool


class StaffQuoteLine(ReleasedQuoteLine):
    """Selling price and the explicitly projected, stored manufacturing specification."""

    part: PartSpecification


class StaffQuoteDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quote_id: Identifier
    rfq_id: Identifier
    status: QuoteStatus
    current_version: PositiveCount
    quote_version_id: Identifier
    created_at: AwareDatetime
    version_created_at: AwareDatetime
    expiry_date: date | None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    total_usd: Money
    estimated_lead_business_days: Annotated[int, Field(ge=0)] | None
    rfq: RfqSummary
    lines: Annotated[tuple[StaffQuoteLine, ...], Field(min_length=1, max_length=MAX_PARTS)]


class SubmitForReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=1, strict=True)]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=256)]


class SubmissionResult(BaseModel):
    quote_id: Identifier
    version: PositiveCount
    action: Literal["submitted"]
    status: Literal["pending_approval"]
    replayed: bool


class ActionAvailability(BaseModel):
    allowed: bool
    reason: str | None


class AttestationContext(BaseModel):
    # Facts to review, deliberately no pre-populated human confirmation field.
    rfq_source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    requested_delivery_date: date | None


class ReviewRequirement(BaseModel):
    kind: str
    reasons: tuple[str, ...]


class StaffQuoteActions(BaseModel):
    quote_id: Identifier
    expected_version: PositiveCount
    actions: dict[
        Literal["request_approval", "approve_quote", "reject_quote", "release_quote"],
        ActionAvailability,
    ]
    attestation_context: AttestationContext | None
    reviews: tuple[ReviewRequirement, ...]
