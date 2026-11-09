"""Strict wire conversions; JSON numbers and currency never pass through float."""

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB

from rfq_copilot.seeding import SeedError


def require(condition: bool, location: str, rule: str) -> None:
    if not condition:
        raise SeedError(f"{location}: {rule}.")


def decimal_value(value: object, location: str, precision=18, scale=6) -> Decimal:
    require(type(value) in (str, int, Decimal), location, "expected exact decimal")
    try:
        result = Decimal(value)
        require(result.is_finite(), location, "decimal must be finite")
        require(abs(result) < Decimal(10) ** (precision - scale), location, "decimal overflow")
        with localcontext() as ctx:
            ctx.prec = max(precision + scale + 4, len(result.as_tuple().digits) + scale + 4)
            require(
                result == result.quantize(Decimal(1).scaleb(-scale)),
                location,
                "excess decimal scale",
            )
        return result
    except (InvalidOperation, ValueError, OverflowError):
        raise SeedError(f"{location}: invalid decimal.") from None


def integer(value: object, location: str, *, bigint=False) -> int:
    require(type(value) is int or isinstance(value, str), location, "expected integer")
    require(bool(re.fullmatch(r"-?(0|[1-9][0-9]*)", str(value))), location, "invalid integer")
    result = int(value)
    bits = 64 if bigint else 32
    require(-(2 ** (bits - 1)) <= result < 2 ** (bits - 1), location, "integer overflow")
    return result


def calendar_date(value: object, location: str) -> date:
    require(isinstance(value, str), location, "expected ISO date")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise SeedError(f"{location}: invalid ISO date.") from None
    require(result.isoformat() == value, location, "expected YYYY-MM-DD")
    return result


def instant(value: object, location: str) -> datetime:
    require(isinstance(value, str), location, "expected timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise SeedError(f"{location}: invalid timestamp.") from None
    require("T" in value and result.tzinfo is not None, location, "timezone required")
    require(result.utcoffset() is not None, location, "timezone required")
    return result.astimezone(UTC)


def json_value(value: str | bytes, location: str):
    def pairs(items):
        result = {}
        for key, item in items:
            require(key not in result, location, "duplicate JSON key")
            result[key] = item
        return result

    def constant(_):
        raise SeedError(f"{location}: nonfinite JSON number.")

    try:
        return json.loads(
            value, parse_float=Decimal, object_pairs_hook=pairs, parse_constant=constant
        )
    except (ValueError, UnicodeError, TypeError):
        raise SeedError(f"{location}: malformed JSON.") from None


def json_text(value) -> str:
    """Encode finite Decimal JSON numbers without changing their type or precision."""
    if isinstance(value, dict):
        require(all(isinstance(k, str) for k in value), "JSON", "keys must be strings")
        return "{" + ",".join(json.dumps(k) + ":" + json_text(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(json_text(v) for v in value) + "]"
    if isinstance(value, Decimal):
        require(value.is_finite(), "JSON", "number must be finite")
        return str(value)
    require(type(value) in (str, int, bool, type(None)), "JSON", "unsupported value type")
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def json_equal(actual, expected) -> bool:
    """Compare JSON structure without conflating booleans and numeric zero/one."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and actual.keys() == expected.keys()
            and all(json_equal(actual[k], value) for k, value in expected.items())
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(json_equal(a, e) for a, e in zip(actual, expected, strict=True))
        )
    return actual == expected


def convert(value, column, location: str):
    if value is None or value == "":
        require(column.nullable, location, "required value is missing")
        return None
    kind = column.type
    if isinstance(kind, Boolean):
        require(value in ("True", "False"), location, "expected True or False")
        return value == "True"
    if isinstance(kind, (Integer, BigInteger)):
        return integer(value, location, bigint=isinstance(kind, BigInteger))
    if isinstance(kind, Numeric):
        return decimal_value(value, location, kind.precision, kind.scale)
    if isinstance(kind, DateTime):
        return instant(value, location)
    if isinstance(kind, Date):
        return calendar_date(value, location)
    if isinstance(kind, JSONB):
        return json_value(value, location) if isinstance(value, str) else value
    require(isinstance(kind, Text) and isinstance(value, str), location, "expected text")
    require(bool(value.strip()) and "\x00" not in value, location, "blank/invalid text")
    if column.name.endswith("_id") or column.name in {
        "created_by",
        "submitted_by",
        "approver_id",
        "actor_id",
    }:
        require(len(value) <= 64, location, "identifier exceeds 64 characters")
    return value
