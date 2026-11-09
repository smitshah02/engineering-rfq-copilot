"""Canonical JSON text and sha256 fingerprints, independent of dict order and Decimal context.

One value, one spelling: object keys sorted, no whitespace, ASCII escapes, Decimals written
as JSON number tokens from str() (context independent), dates as ISO strings, frozen
dataclasses as objects of their fields. Floats and unknown types raise, so nothing inexact
or unexpected (an ORM row, a session) can be hashed by accident.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal


def canonical_json(value: object) -> str:
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Canonical JSON numbers must be finite.")
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, datetime):  # before date: datetime is a date subclass
        raise ValueError("Canonical JSON has no datetime spelling; use a date.")
    if isinstance(value, date):
        return json.dumps(value.isoformat())
    if is_dataclass(value) and not isinstance(value, type):
        value = {f.name: getattr(value, f.name) for f in fields(value)}
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Canonical JSON object keys must be text.")
        items = sorted(value.items())
        return "{" + ",".join(json.dumps(k) + ":" + canonical_json(v) for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    raise ValueError(f"No canonical JSON spelling for {type(value).__name__}.")


def fingerprint(value: object) -> str:
    """sha256 hex of the canonical JSON text."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
