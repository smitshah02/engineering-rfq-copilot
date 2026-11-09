"""Business entities shared by the deterministic business services.

A Part checks types only: exact Decimals, strict integers and bounded identifiers.
Manufacturing ranges, missing-field findings and catalog support are decided by the
runtime validator, so an incomplete or out-of-range request still reaches it intact.
"""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal

from rfq_copilot.models import RFQLineItem
from rfq_copilot.seeding.transform import decimal_value, integer

_TEXT = ("part_type", "material_id", "finish_id")
_DECIMAL = (
    "length_mm",
    "width_mm",
    "height_mm",
    "thickness_mm",
    "tolerance_mm",
    "bend_radius_mm",
    "weld_length_mm",
)
_INTEGER = ("quantity", "hole_count")


def _text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64 or "\x00" in value:
        raise ValueError(f"{location}: expected nonblank text of at most 64 characters.")
    return value


@dataclass(frozen=True)
class Part:
    """One requested part in millimetres; None means the request did not state it."""

    part_id: str
    part_type: str | None = None
    material_id: str | None = None
    finish_id: str | None = None
    length_mm: Decimal | None = None
    width_mm: Decimal | None = None
    height_mm: Decimal | None = None
    thickness_mm: Decimal | None = None
    tolerance_mm: Decimal | None = None
    bend_radius_mm: Decimal | None = None
    weld_length_mm: Decimal | None = None
    quantity: int | None = None
    hole_count: int | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "Part":
        """Build from the calculate_quote Part contract shape; floats and unknown keys fail."""
        if not isinstance(data, Mapping):
            raise ValueError("part: expected a mapping.")
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"part: unknown fields {sorted(unknown)}.")
        values: dict[str, object] = {"part_id": _text(data.get("part_id"), "part.part_id")}
        for name in _TEXT + _DECIMAL + _INTEGER:
            value = data.get(name)
            if value is None:
                continue
            location = f"part.{name}"
            if name in _TEXT:
                values[name] = _text(value, location)
            elif name in _DECIMAL:
                # Same precision as the rfq_line_items Numeric(12, 3) columns.
                values[name] = decimal_value(value, location, 12, 3)
            else:
                if type(value) is not int:
                    raise ValueError(f"{location}: expected integer.")
                values[name] = integer(value, location)
        return cls(**values)

    @classmethod
    def from_line(cls, line: RFQLineItem) -> "Part":
        """Copy a stored runtime RFQ line; the database already enforced its types."""
        return cls(**{f.name: getattr(line, f.name) for f in fields(cls)})
