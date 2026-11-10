# API contracts (Day 17): Pydantic models and request validation

This document describes the code in `src/rfq_copilot/api/schemas/` and `src/rfq_copilot/api/errors.py`
as implemented. It adds **contracts, not endpoints**. The production application still
serves only `GET /health/live` and `GET /api/v1/system/info` (Day 16). No RFQ is
submitted, stored, evaluated, priced or approved through the API.

## 1. Objective and scope

The goal is a reusable, tested schema layer that future authenticated RFQ endpoints will
use:
- request and response DTOs
- reusable exact-number types
- structural validation, including one cross-field check
- a structured HTTP 422 error contract installed in `create_app()`
- OpenAPI verification

Excluded: live routes, persistence, service calls, authentication, authorization and
attachments.

## 2. Domain contracts examined

| Source | Used for |
| --- | --- |
| `entities.Part` and `Part.from_mapping` | Field names, `None` = not stated, only `part_id` required, floats and unknown keys refused, `Numeric(12, 3)` precision, `requested_operations` refused (TD-001) |
| `models/rfqs.py` `RFQLineItem` | `INTEGER` counts (32-bit), `Numeric(12, 3)` measurements, `part_id` nonblank and at most 64 characters, `UNIQUE(rfq_id, part_id)` |
| `services/manufacturing.py` | Missing fields become `needs_clarification`; ranges, part-type support, tolerance and hole limits are rule-document findings |
| Dataset tool contract v1.1 (`create_rfq`, `calculate_quote`) | `part_type` vocabulary, lower bounds (quantity ≥ 1, dimensions > 0, tolerance, weld length and hole count ≥ 0), the unsigned decimal-string pattern, the `create_rfq` output (`rfq_id`, `status: "draft"`), the `calculate_quote` output (`calculation_id`, `quote_total_usd`, `lines`), "server must add size limits" |
| Dataset entity schemas | The `id` value type (1–64 characters) |
| `services/pricing.py` `display_quote` | Money as `str(Decimal)` at the scale set by the pinned pricing rules; currency from the rule document |
| Development and validation scenarios (aggregate shape only, no values copied) | Wire values are integers or plain decimal strings; at most 2 parts per RFQ; non-positive values only for `hole_count: 0`; omitted fields occur |

**Where the tool template and the services disagree, the services win.** The template
lists nine required part fields. The services deliberately accept incomplete parts so
that a missing specification becomes a clarification outcome rather than a rejection.
The API follows the services: only `part_id` is required.

## 3. Structure

```
src/rfq_copilot/api/
    app.py              (Day 16) + installs the RequestValidationError handler
    errors.py           ValidationIssue, ValidationErrorResponse, to_issue, handler,
                        VALIDATION_ERROR_RESPONSES
    schemas/
        __init__.py     package docstring (structural validation only)
        common.py       Identifier, PositiveMillimetres, NonNegativeMillimetres, Count,
                        PositiveCount, IsoDate, Money, reject_blank_text
        rfq.py          PartSpecification, RfqSubmissionRequest, RfqReceipt,
                        QuoteLineAmount, QuoteCalculationSummary
```

There is no separate material or manufacturing sub-model. The authoritative `Part`
contract is flat (`material_id`, `finish_id` and `thickness_mm` sit beside the
dimensions), and nesting would force a reshaping step before every service call.

## 4–7. Request and response models

All models use `extra="forbid", frozen=True`. Frozen blocks reassigning fields; the
`list` fields can still be changed in place. `list` is used because Pydantic's tuple
length check reports a spurious `too_short` error whenever an item fails.

**`RfqSubmissionRequest`** (untrusted client input):

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `parts` | `list[PartSpecification]` | yes | 1–50 items (`MAX_PARTS`); `part_id`s unique |
| `requested_delivery_date` | `IsoDate` | no | exactly `YYYY-MM-DD` |
| `raw_request` | `str` | no | 1–20 000 characters, nonblank, no NUL |

It has no `customer_id`, rule version, idempotency key, channel, status or approval
field. Any of them is rejected as `extra_forbidden`.

**`PartSpecification`** (one requested part, millimetres; omitted and `null` both mean "not stated"):

| Field | Type | Rule |
| --- | --- | --- |
| `part_id` | `Identifier` | required; 1–64 characters, nonblank, no NUL, kept verbatim |
| `part_type` | literal | `flat_panel`, `mounting_plate`, `l_bracket`, `u_bracket`, `enclosure`, `fabricated_assembly` |
| `material_id`, `finish_id` | `Identifier` | not checked against the catalog |
| `length_mm`, `width_mm`, `height_mm`, `thickness_mm` | `PositiveMillimetres` | > 0 |
| `tolerance_mm`, `bend_radius_mm`, `weld_length_mm` | `NonNegativeMillimetres` | ≥ 0 |
| `quantity` | `PositiveCount` | strict integer, 1 to 2³¹−1 |
| `hole_count` | `Count` | strict integer, 0 to 2³¹−1 |

**Responses:**
- **`RfqReceipt`:** `rfq_id`, `status: Literal["draft"]`, as in the `create_rfq` output.
- **`QuoteCalculationSummary`:** `calculation_id`, `pricing_rule_version`, `currency`
  (`^[A-Z]{3}$`), `quote_total_usd: Money`, and `lines: [QuoteLineAmount(part_id,
  line_total_usd)]` (at least 1). This is the `calculate_quote` output plus the provenance
  and currency that `display_quote` already reports. It is a calculation, not a quote: it
  is not persisted by being returned, it approves nothing, and release always needs a
  human. It has no approval or release field, and adding one is rejected.

**Deferred:** an evaluation-outcome response (outcome, escalation reason, draft gate,
manager review), until an endpoint returns it; attachments; delivery location;
idempotency (transport header, belongs with persistence).

## 8. Field-level validation

**Coercion policy:**
- strings must be strings
- counts must be JSON integers: no `true`, no `"25"`, no `25.0`
- dates must be `YYYY-MM-DD` strings: no timestamps, no datetimes
- exact numbers must be JSON integers or plain decimal strings matching `^\d+(\.\d+)?$`
  (no sign, exponent, whitespace, `NaN` or `Infinity`, no JSON floats, no booleans)
- identifiers are never stripped or case-folded

**Bounds are structural only:** non-negative or positive, as in the tool contract, and
the database precision. The real envelope comes from the rule documents: for example,
which lengths are manufacturable, which `part_type`s are allowed, minimum tolerance and
maximum holes. A length of 1 mm passes here and is declined later as
`OUT_OF_RANGE_LENGTH_MM`.

## 9. Cross-field validation

Only one check: `part_id` must be unique within `parts`, mirroring
`UNIQUE(rfq_id, part_id)`. The validator's internal message names the position
(`parts[2]`), never the identifier. The public 422 message is the static "part_id values
must be unique within an RFQ.", at field `body.parts`.
It runs only once every part is valid, which is standard Pydantic order.

Rules that depend on the part type, such as height or weld length being required for
some types, are rule-document decisions (`height_required`, `weld_length_required`) and
are not duplicated.

## 10. Decimal handling and JSON

- **Input:** `_exact_decimal` converts an `int` or a matching string to `Decimal`; a
  Python `Decimal` must pass the same check through `str()`. Then `Field(max_digits=12,
  decimal_places=3)` applies, which behaves exactly like `Part`'s check: `1.5000` is
  accepted, `0.0001` and `1000000000` are rejected.
- **No rounding:** excess scale is rejected, never rounded.
- **Output:** JSON strings with the scale they were given (`"4.0"`, `"3.00"`, `"520"`). Money
  uses the same rules with a lower bound of 0 and no fixed scale, because the pricing rules
  own the scale.
- **JSON Schema:** measurements are `anyOf: [integer, string with pattern]`; money is a
  string with pattern. The words "number" or "float" never appear in the request schema.

## 11. Structured error contract

Any `RequestValidationError` returns HTTP 422:

```json
{
  "code": "REQUEST_VALIDATION_ERROR",
  "message": "Request validation failed.",
  "details": [
    {"field": "body.parts.0.quantity", "code": "greater_than_equal", "message": "Value is below the allowed minimum."},
    {"field": "body.parts.1.part_id", "code": "blank_or_invalid_text", "message": "Text must be nonblank, without NUL."},
    {"field": "body.parts.1.length_mm", "code": "exact_decimal_type", "message": "Expected an integer or a plain decimal string such as '4.0'."},
    {"field": "body.<redacted>", "code": "extra_forbidden", "message": "Unknown field."}
  ]
}
```

That is the exact response asserted in
`test_invalid_request_returns_the_structured_422_without_reaching_the_handler`, for this
input:

```json
{"parts": [{"part_id": "TEST-PANEL-1", "quantity": 0, "...": "..."},
           {"part_id": "", "length_mm": 1.5, "...": "..."}],
 "customer_id": "C"}
```

**How each issue is built (`to_issue`), corrected 2026-11-10 after the security audit:**
- **`field`:** location segments are kept only if they are list indexes, location roots
  (`body`, `query`, `path`, `header`, `cookie`), or field names declared by a registered
  request model (`REQUEST_MODELS`, currently `RfqSubmissionRequest` and its nested
  `PartSpecification`). Any other segment is `<redacted>`. That covers every unknown key,
  including identifier-shaped ones such as `sk_live_…` and well-known names such as
  `customer_id`. A model used by a new router must be added to `REQUEST_MODELS`; until
  then its paths are redacted (fail closed). For `extra_forbidden` errors, the last
  segment is always `<redacted>`, even if the name is declared elsewhere. For example,
  `quantity` at the root and `raw_request` inside a part both give `body.<redacted>` and
  `body.parts.0.<redacted>`.
- **`code`:** the Pydantic error type only if it is in the explicit allowlist
  `PUBLIC_CODES` (every code in `MESSAGES`, plus `value_error`); anything else is
  `invalid`. A custom validator can choose its own error type, even one built from input,
  so the type is never copied unless listed.
- **`message`:** taken from the fixed `MESSAGES` table by code. Unknown codes,
  including `value_error` from any validator, get `"Invalid value."`. Pydantic's `msg`
  is never used, because a validator's exception text can contain the submitted value.
  `input`, `ctx` and `url` are never read.

Before the correction, identifier-shaped unknown keys and raw `msg` text (including a
`ValueError` message containing input) were reflected. The regression tests in the
"Security regressions" section of `tests/test_api_schemas.py` reproduce both leaks: 12
of them fail against the old `errors.py`, and all pass now.

**Codes added by this layer:** `exact_decimal_type`, `blank_or_invalid_text`,
`date_format`, `duplicate_part_id`. Standard Pydantic codes (`missing`, `extra_forbidden`,
`literal_error`, `greater_than`, `json_invalid`, …) pass through as codes, each with its own
static message.

**Unchanged:** 404 and 405 keep FastAPI's `{"detail": ...}` body, and a response-model
violation stays an opaque `500 Internal Server Error`. Only `RequestValidationError` is
handled. `VALIDATION_ERROR_RESPONSES` lets a router declare this contract for 422 in
OpenAPI instead of FastAPI's default `HTTPValidationError`.

## 12. Request lifecycle

1. Uvicorn passes the request to the app built by `create_app()`.
2. FastAPI reads the body and runs `json.loads`. Invalid JSON raises
   `RequestValidationError` (`json_invalid`).
3. FastAPI validates the parsed value against the endpoint's body model in Python mode,
   which is why the wire checks are before-validators.
4. On success the handler runs with a typed, frozen model.
5. On failure the handler never runs. `request_validation_handler` (`api/errors.py`)
   builds a `ValidationErrorResponse` from `exc.errors()` through `to_issue`.
6. FastAPI sends HTTP 422 with `application/json`.

## 13. Test-only HTTP strategy

`tests/test_api_schemas.py::harness()` builds `create_app(DEV)` and attaches a router
inside the test: `POST /test-only/rfq`. It records each call and echoes the parsed request
in a test-only `Accepted` model. The route exists only inside that test's application.
Tests confirm that the production application, in both environments, has neither the
route nor its schemas, and the real Uvicorn server returns 404 for it.

## 14. OpenAPI

Verified:
- `PartSpecification` has `additionalProperties: false` and requires only `part_id`.
- Measurements are an integer or a patterned string; quantity is an integer with `minimum: 1`.
- `part_type` is an enum.
- Money uses a string pattern in serialization mode.
- An opted-in route documents `422 → ValidationErrorResponse`, `HTTPValidationError` is
  absent, and `ValidationIssue` forbids extra fields.

Day 16 behaviour is unchanged: `/docs`, `/redoc` and `/openapi.json` are served only in
development, and production OpenAPI lists only the two Day 16 paths.

## 15–17. Security boundaries

- **API validation is not domain validation.** The schemas decide only whether input is
  well formed. Catalog support, manufacturability, inventory, capacity, lead time, price,
  customer standing and approval are decided by the services, under the pinned rules.
- **No identity in the body.** A body is attacker-controlled. The customer must come
  from verified authentication and be passed to the customer-scoped repositories, as
  the Phase 2.5 boundary requires. `customer_id` in the body is rejected, so it cannot
  be mistaken for proof.
- **Rule versions are never accepted from the client.** `pricing_rule_version` in a
  request is rejected, and responses only report the version the server used.
- **No approval vocabulary in requests.** `approved` or any status field is rejected.

## 18. How future endpoints should use these schemas

1. Declare `body: RfqSubmissionRequest` and `responses=VALIDATION_ERROR_RESPONSES`. Add any new
   request model to `errors.REQUEST_MODELS` so that its field names appear in error paths.
2. Obtain the customer and role from an authentication dependency, never from the body.
3. Convert each part with `Part.from_mapping(spec.model_dump(mode="json", exclude_none=True))`.
   A test proves that this accepts every valid request unchanged.
4. Call the services in a `snapshot_session()` with pinned rules (`approval-v1.2` for new
   evaluations).
5. Build `QuoteCalculationSummary` or `RfqReceipt` explicitly from the results, never
   by serializing ORM objects or service dataclasses.

## 19. Limitations and deferred work

- `MAX_PARTS = 50` and `MAX_RAW_REQUEST_CHARS = 20 000` are chosen limits; the contract
  requires limits but gives no numbers. They are marked `ponytail:` in `rfq.py`.
- There is no request body size limit at the server level, which belongs with deployment.
- `RfqReceipt` and `QuoteCalculationSummary` have no route yet; their shapes come from the
  tool contract and `display_quote`.
- An evaluation response, attachments, delivery location and idempotency are deferred.
- `part_type` is a contract vocabulary. If a future rule document allows a new type,
  the contract must change too.

## 20. Verification (2026-11-10, after the security correction)

| Check | Result |
| --- | --- |
| `uv run --no-sync python -m pytest -q tests/test_api_schemas.py` | 123 passed (103 + 12 + 8 security regressions) |
| `uv run --no-sync python -m pytest -q tests/test_api.py` | 40 passed (unchanged) |
| Public smoke set (4 files), database variables unset | 205 passed |
| `uv run --no-sync python -m pytest -q` | 1274 passed in 118.14s; 0 failed, 0 skipped, 0 warnings |
| `uv lock --check`, `ruff check .`, `ruff format --check src tests alembic` | pass |
| Uvicorn, development | `/health/live` 200, `/api/v1/system/info` 200, `/openapi.json` 200 (two paths, schemas `Liveness` and `SystemInfo`), `/docs` 200, `POST /test-only/rfq` 404 |
| Uvicorn, production | health 200, info 200, `/openapi.json` 404, `/docs` 404, `POST /test-only/rfq` 404 |
| Read-only integrity check (development database) | exit 0; 34 PASS; 1090 records |
