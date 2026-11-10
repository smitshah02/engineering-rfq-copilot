"""Structured HTTP 422 contract for request validation failures.

Only ``RequestValidationError`` is handled. Response-model violations stay opaque 500s,
and 404/405 keep FastAPI's defaults.

Nothing client-controlled is reflected:
- ``field`` keeps only location roots, list indexes and field names declared by the
  registered request schemas; any other segment (an unknown key, whatever its shape) is
  ``<redacted>``.
- ``code`` is copied only if it is in ``PUBLIC_CODES``; anything else is ``invalid``.
  The last segment of an ``extra_forbidden`` location is always redacted.
- ``message`` comes from a fixed table keyed by error code, never from Pydantic's
  ``msg`` (a validator's exception text can contain the submitted value). Unknown codes
  get a generic message. ``input``, ``ctx`` and ``url`` are never read.
"""

from typing import Any, Literal

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from rfq_copilot.api.schemas.reads import MaterialListQuery, RfqListQuery
from rfq_copilot.api.schemas.rfq import RfqSubmissionRequest

ERROR_CODE = "REQUEST_VALIDATION_ERROR"
ERROR_MESSAGE = "Request validation failed."
REDACTED = "<redacted>"
GENERIC_MESSAGE = "Invalid value."

# Request models whose field names may appear in error paths. A model used by a new
# router must be added here; until then its paths are redacted (fail closed).
REQUEST_MODELS: tuple[type[BaseModel], ...] = (
    RfqSubmissionRequest,
    RfqListQuery,
    MaterialListQuery,
)
# Path parameter names declared by routers (fixed names, never client-chosen).
PATH_PARAMETERS = frozenset({"rfq_id", "quote_id"})


def _declared_field_names(models: tuple[type[BaseModel], ...]) -> frozenset[str]:
    names: set[str] = set()
    for model in models:
        schema = model.model_json_schema()
        for definition in (schema, *schema.get("$defs", {}).values()):
            names.update(definition.get("properties", {}))
    return frozenset(names)


TRUSTED_SEGMENTS = (
    frozenset({"body", "query", "path", "header", "cookie"})
    | PATH_PARAMETERS
    | _declared_field_names(REQUEST_MODELS)
)

# Messages are static text written here; they never include input or limits.
MESSAGES: dict[str, str] = {
    "missing": "Field required.",
    "extra_forbidden": "Unknown field.",
    "json_invalid": "Request body is not valid JSON.",
    "model_attributes_type": "Expected a JSON object.",
    "model_type": "Expected a JSON object.",
    "dict_type": "Expected a JSON object.",
    "list_type": "Expected a list.",
    "too_short": "Too few items.",
    "too_long": "Too many items.",
    "string_type": "Expected a string.",
    "string_too_short": "Text is too short.",
    "string_too_long": "Text is too long.",
    "string_pattern_mismatch": "Text has an invalid format.",
    "int_type": "Expected an integer.",
    "int_parsing": "Expected an integer.",  # query and path integers arrive as text
    "literal_error": "Value is not one of the allowed options.",
    "greater_than": "Value is below the allowed minimum.",
    "greater_than_equal": "Value is below the allowed minimum.",
    "less_than": "Value is above the allowed maximum.",
    "less_than_equal": "Value is above the allowed maximum.",
    "decimal_max_places": "Too many decimal places.",
    "decimal_max_digits": "Too many digits.",
    "decimal_whole_digits": "Too many digits before the decimal point.",
    "exact_decimal_type": "Expected an integer or a plain decimal string such as '4.0'.",
    "blank_or_invalid_text": "Text must be nonblank, without NUL.",
    "date_format": "Expected a calendar date as YYYY-MM-DD.",
    "duplicate_part_id": "part_id values must be unique within an RFQ.",
}

# Codes that may be published. A custom validator chooses its own error type, which can
# be built from input, so only codes listed here are copied; anything else is "invalid".
# value_error is Pydantic's fixed type for a ValueError raised in a validator.
PUBLIC_CODES = frozenset(MESSAGES) | {"value_error"}


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    code: str
    message: str


class ValidationErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal["REQUEST_VALIDATION_ERROR"]
    message: str
    details: list[ValidationIssue]


# For future routers: documents this contract instead of FastAPI's default 422 schema.
VALIDATION_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"model": ValidationErrorResponse, "description": ERROR_MESSAGE}
}


def _segment(part: object) -> str:
    if isinstance(part, int) and not isinstance(part, bool):
        return str(part)
    return part if isinstance(part, str) and part in TRUSTED_SEGMENTS else REDACTED


def _field(error: dict[str, Any]) -> str:
    segments = [_segment(part) for part in error.get("loc", ())]
    if error.get("type") == "extra_forbidden" and segments:
        # The last segment of an unknown field is the client's key, even when it matches
        # a field declared elsewhere (quantity at the root, raw_request inside a part).
        segments[-1] = REDACTED
    return ".".join(segments)


def to_issue(error: dict[str, Any]) -> ValidationIssue:
    code = error.get("type")
    code = code if code in PUBLIC_CODES else "invalid"
    return ValidationIssue(
        field=_field(error),
        code=code,
        message=MESSAGES.get(code, GENERIC_MESSAGE),
    )


async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    body = ValidationErrorResponse(
        code=ERROR_CODE,
        message=ERROR_MESSAGE,
        details=[to_issue(error) for error in exc.errors()],
    )
    return JSONResponse(status_code=422, content=body.model_dump())
