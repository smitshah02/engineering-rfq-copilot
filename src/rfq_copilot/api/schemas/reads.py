"""Response and query contracts for the customer-scoped read routes.

Each model lists the fields a verified customer may see, chosen explicitly. Not exposed:
- credit limit and exposure, account status, restrictions, approval requirements
- internal user IDs, row versions, source evidence
- material cost, density, bend factor, availability class, pricing version
- cost breakdowns, part JSON, rule versions, snapshots
- every quote that is not released
"""

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from rfq_copilot.api.schemas.common import Identifier, Money, PositiveMillimetres
from rfq_copilot.api.schemas.rfq import PartSpecification

_STRICT = ConfigDict(extra="forbid", frozen=True)

MAX_PAGE_SIZE = 100
MAX_OFFSET = 10_000


class _PageQuery(BaseModel):
    model_config = _STRICT

    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)] = 20
    offset: Annotated[int, Field(ge=0, le=MAX_OFFSET)] = 0


class RfqListQuery(_PageQuery):
    pass


class MaterialListQuery(_PageQuery):
    """Filters of ``repositories.find_materials``; each one must match."""

    category: Identifier | None = None
    grade: Identifier | None = None
    thickness_mm: PositiveMillimetres | None = None
    finish_id: Identifier | None = None


class CustomerContact(BaseModel):
    model_config = _STRICT

    contact_name: str
    email: str
    phone: str
    is_primary: bool


class CustomerProfile(BaseModel):
    model_config = _STRICT

    customer_id: Identifier
    company_name: str
    industry: str
    city: str
    state: str
    country: str
    payment_terms: str
    preferred_shipping: str
    contacts: list[CustomerContact]


class RfqSummary(BaseModel):
    model_config = _STRICT

    rfq_id: Identifier
    status: str
    submitted_at: AwareDatetime


class RfqPage(BaseModel):
    model_config = _STRICT

    items: list[RfqSummary]
    limit: int
    offset: int
    has_more: bool


class RfqLine(BaseModel):
    model_config = _STRICT

    line_number: Annotated[int, Field(ge=1)]
    part: PartSpecification


class RfqDetail(BaseModel):
    """A customer's RFQ with its runtime lines (historical RFQs have none)."""

    model_config = _STRICT

    rfq_id: Identifier
    status: str
    submitted_at: AwareDatetime
    lines: list[RfqLine]


class MaterialSummary(BaseModel):
    model_config = _STRICT

    material_id: Identifier
    category: str
    grade: str


class MaterialPage(BaseModel):
    model_config = _STRICT

    items: list[MaterialSummary]
    limit: int
    offset: int
    has_more: bool


class ReleasedQuoteLine(BaseModel):
    model_config = _STRICT

    line_number: Annotated[int, Field(ge=1)]
    part_id: Identifier
    quantity: Annotated[int, Field(ge=1)]
    line_total_usd: Money


class ReleasedQuote(BaseModel):
    """The approved version of a quote a human has released to the customer.

    Drafts, pending, approved-but-unreleased, rejected, expired and blocked quotes are
    internal and are never returned.
    """

    model_config = _STRICT

    quote_id: Identifier
    rfq_id: Identifier
    status: Literal["released"]
    released_at: AwareDatetime
    version: Annotated[int, Field(ge=1)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    total_usd: Money
    expiry_date: date | None
    estimated_lead_business_days: Annotated[int, Field(ge=0)] | None
    lines: list[ReleasedQuoteLine]
