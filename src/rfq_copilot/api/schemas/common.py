"""Reusable validated types: identifiers, millimetre measurements and money.

Exact numbers travel as JSON integers or plain decimal strings ("4.0"), never as JSON
floats, so no value passes through binary floating point. Values are never rounded:
input with excess scale is rejected, and output keeps the scale it was given.
"""

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, StrictInt, WithJsonSchema
from pydantic_core import PydanticCustomError

PLAIN_DECIMAL = r"^\d+(\.\d+)?$"
_PLAIN_DECIMAL = re.compile(PLAIN_DECIMAL)
INT32_MAX = 2**31 - 1  # rfq_line_items INTEGER columns


def reject_blank_text(value: Any) -> Any:
    # Same rule as entities.Part and the part_id_identity check: kept verbatim, never
    # stripped or case-folded, so an identifier cannot silently change meaning.
    if isinstance(value, str) and (not value.strip() or "\x00" in value):
        raise PydanticCustomError("blank_or_invalid_text", "Text must be nonblank, without NUL.")
    return value


def _exact_decimal(value: Any) -> Any:
    if isinstance(value, Decimal):
        value = str(value)  # one wire rule for Python and JSON input: no exponent, sign, NaN
    if type(value) is int:  # excludes bool
        return Decimal(value)
    if isinstance(value, str) and _PLAIN_DECIMAL.fullmatch(value):
        return Decimal(value)
    raise PydanticCustomError(
        "exact_decimal_type", "Expected an integer or a plain decimal string such as '4.0'."
    )


def _iso_date(value: Any) -> Any:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.isoformat() == value:
            return parsed
    raise PydanticCustomError("date_format", "Expected a calendar date as YYYY-MM-DD.")


def _decimal_schema(**bounds: int) -> dict[str, Any]:
    return {"anyOf": [{"type": "integer", **bounds}, {"type": "string", "pattern": PLAIN_DECIMAL}]}


# Identifier: the dataset's id value type (1-64 characters) and entities.Part._text.
Identifier = Annotated[
    str, Field(strict=True, min_length=1, max_length=64), BeforeValidator(reject_blank_text)
]

# Millimetres at the rfq_line_items Numeric(12, 3) precision used by entities.Part.
_Millimetres = Annotated[Decimal, Field(max_digits=12, decimal_places=3)]
PositiveMillimetres = Annotated[
    _Millimetres,
    Field(gt=0),
    BeforeValidator(_exact_decimal),
    WithJsonSchema(_decimal_schema(exclusiveMinimum=0)),
]
NonNegativeMillimetres = Annotated[
    _Millimetres,
    Field(ge=0),
    BeforeValidator(_exact_decimal),
    WithJsonSchema(_decimal_schema(minimum=0)),
]

Count = Annotated[StrictInt, Field(ge=0, le=INT32_MAX)]
PositiveCount = Annotated[StrictInt, Field(ge=1, le=INT32_MAX)]

IsoDate = Annotated[
    date, BeforeValidator(_iso_date), WithJsonSchema({"type": "string", "format": "date"})
]

# Money: exact, non-negative, serialized with str() as services.pricing.display_quote
# does. The scale is set by the pinned pricing rules, so it is preserved, not imposed.
Money = Annotated[
    Decimal,
    Field(ge=0),
    BeforeValidator(_exact_decimal),
    WithJsonSchema({"type": "string", "pattern": PLAIN_DECIMAL}),
]
