"""RFQ request and response contracts for future authenticated endpoints.

Field names and shapes follow ``entities.Part`` (the calculate_quote Part contract used by
every service) and the dataset tool contract v1.1 (``create_rfq``, ``calculate_quote``).

Deliberately structural: everything except ``part_id`` is optional, because the
manufacturing validator turns a missing field into a clarification finding, and range,
catalog and finish decisions come from the pinned rule documents. A request that passes
here may still be unsupported, need clarification or be declined.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from rfq_copilot.api.schemas.common import (
    Count,
    Identifier,
    IsoDate,
    Money,
    NonNegativeMillimetres,
    PositiveCount,
    PositiveMillimetres,
    reject_blank_text,
)

# The part_type vocabulary of tool contract v1.1. Which of these the plant can make is a
# rule-document decision (allowed_part_types, UNSUPPORTED_PART_TYPE), not decided here.
PartType = Literal[
    "flat_panel", "mounting_plate", "l_bracket", "u_bracket", "enclosure", "fabricated_assembly"
]

# ponytail: request-size limits the tool contract requires but does not quantify; raise
# them with evidence (the development and validation scenarios have at most 2 parts).
MAX_PARTS = 50
MAX_RAW_REQUEST_CHARS = 20_000

_STRICT = ConfigDict(extra="forbid", frozen=True)


class PartSpecification(BaseModel):
    """One requested part, in millimetres. Omitted or null means "not stated"."""

    model_config = _STRICT

    part_id: Identifier
    part_type: PartType | None = None
    material_id: Identifier | None = None
    finish_id: Identifier | None = None
    length_mm: PositiveMillimetres | None = None
    width_mm: PositiveMillimetres | None = None
    height_mm: PositiveMillimetres | None = None
    thickness_mm: PositiveMillimetres | None = None
    tolerance_mm: NonNegativeMillimetres | None = None
    bend_radius_mm: NonNegativeMillimetres | None = None
    weld_length_mm: NonNegativeMillimetres | None = None
    quantity: PositiveCount | None = None
    hole_count: Count | None = None


class RfqSubmissionRequest(BaseModel):
    """Untrusted RFQ content. Carries no identity, customer, rule version or approval.

    The customer comes from the verified authentication context, never from this body;
    ``customer_id`` is therefore an unknown field here and is rejected.
    """

    model_config = _STRICT

    # list, not tuple: Pydantic's tuple length check adds a spurious too_short error
    # whenever an item fails, which would mislead clients.
    parts: Annotated[list[PartSpecification], Field(min_length=1, max_length=MAX_PARTS)]
    requested_delivery_date: IsoDate | None = None
    raw_request: (
        Annotated[
            str,
            Field(strict=True, min_length=1, max_length=MAX_RAW_REQUEST_CHARS),
            BeforeValidator(reject_blank_text),
        ]
        | None
    ) = None

    @field_validator("parts")
    @classmethod
    def _unique_part_ids(cls, parts: list[PartSpecification]):
        # Mirrors UniqueConstraint(rfq_id, part_id) on rfq_line_items. The message names
        # the position, never the submitted identifier.
        seen: set[str] = set()
        for index, part in enumerate(parts):
            if part.part_id in seen:
                raise PydanticCustomError(
                    "duplicate_part_id",
                    "part_id must be unique within an RFQ; parts[{index}] repeats an earlier one.",
                    {"index": index},
                )
            seen.add(part.part_id)
        return parts


class RfqReceipt(BaseModel):
    """Acknowledgement of a stored RFQ (tool contract v1.1 create_rfq output)."""

    model_config = _STRICT

    rfq_id: Identifier
    status: Literal["draft"]


class QuoteLineAmount(BaseModel):
    model_config = _STRICT

    part_id: Identifier
    line_total_usd: Money


class QuoteCalculationSummary(BaseModel):
    """A deterministic price calculation (calculate_quote output plus its provenance).

    A calculation is not a quote, not an approval and not a release: it is not persisted
    by returning it, and releasing any quote to a customer always needs a human decision.
    """

    model_config = _STRICT

    calculation_id: Identifier
    pricing_rule_version: Identifier
    currency: Annotated[str, Field(strict=True, pattern=r"^[A-Z]{3}$")]
    quote_total_usd: Money
    lines: Annotated[list[QuoteLineAmount], Field(min_length=1)]
