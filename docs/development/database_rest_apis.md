# FastAPI services, REST contracts and attachments

The ordinary app exposes customer submission/reads and staff quotation reads, action hints
and review submission. A separate decision app handles approval, rejection and release with
its own restricted role. JWT subjects, database grants and scoped queries determine identity
and visibility; there is no HTTP token-issuance or public account-registration endpoint.

## Current HTTP surface

| Service | Method and path | Contract / authority |
| --- | --- | --- |
| Ordinary | GET `/health/live`, `/api/v1/system/info` | Public liveness/metadata; no dependency readiness guarantee |
| Ordinary | GET `/api/v1/customers/me` | Bound customer profile |
| Ordinary | GET/POST `/api/v1/rfqs` | Scoped listing; validated idempotent customer submission and activated queue |
| Ordinary | GET `/api/v1/rfqs/{rfq_id}` | Scoped persisted specifications and permitted released-quote reference |
| Ordinary | GET `/api/v1/materials` | Bounded catalog filtering |
| Ordinary | GET `/api/v1/materials/{material_id}/specification` | Mounted only with explicit MCP integration |
| Ordinary | GET/POST `/api/v1/rfqs/{rfq_id}/attachments` | Authorized metadata and bounded PDF/CSV upload |
| Ordinary | GET `/api/v1/quotes/{quote_id}` | Released approved-version selling-price projection only |
| Ordinary | GET `/api/v1/staff/quotes` | Trusted staff role and read grant; bounded scoped filters |
| Ordinary | GET `/api/v1/staff/quotes/{quote_id}` | Stored current version and projected manufacturing lines |
| Ordinary | GET `/api/v1/staff/quotes/{quote_id}/actions` | Exact-version rollback-only lock-capable assessment; hints are not mutation authority |
| Ordinary | POST `/api/v1/staff/quotes/{quote_id}/submit-for-review` | `request_approval`, expected version, idempotency receipt |
| Decision | POST `/api/v1/quote-decisions/{quote_id}/approval` | Independent approval with explicit context attestation |
| Decision | POST `/api/v1/quote-decisions/{quote_id}/rejection` | Authorized rejection with bounded reason |
| Decision | POST `/api/v1/quote-decisions/{quote_id}/release` | Authorized release by an actor different from approver |

Read sessions use the ordinary restricted role; ordinary writes and action-assessment locks
use separate session scopes. Assessments roll back and do not commit. Decision sessions use
only decision authority. Missing/foreign/unassigned scoped objects are concealed as 404;
validation errors are sanitized 422 and unavailable infrastructure fails closed.

RFQ page enrichment performs a bounded scoped query and requires quote-read permission.
Only human-released approved versions are discoverable, with no costs, margins or raw evidence.
Browser identity swaps cancel/clear cached queries and reject stale responses. Staff actions
reuse payload-bound idempotency keys on uncertain responses and re-fetch authoritative state.

The detailed framework, schema and upload contracts follow. Their validation examples describe
reusable schema models; the current submission adapter is `api/routers/rfqs.py`.



## API foundation: FastAPI application and backend setup

<a id="fastapi-backend-1-purpose-and-place-in-the-architecture"></a>
### 1. Purpose and place in the architecture

The deterministic services (`src/rfq_copilot/services/`) decide; API adapters call them.
The FastAPI application supplies the HTTP boundary, including customer RFQ creation and automatic queue admission. A customer correction API and explicit Supervisor progress/cancellation APIs remain outside the implemented surface. See [the supervisor architecture](../architecture/supervisor_langgraph_architecture.md).
The foundation provides the shared pieces used by endpoints: the application factory,
routing, response contracts, dependency injection, settings, lifespan, OpenAPI and tests.

```
 HTTP client ─► Uvicorn (ASGI server) ─► FastAPI app (create_app)
                                            ├─ /health/live          health.router   (no I/O)
                                            ├─ /api/v1/system/info   system.router   (app metadata)
                                            └─ /docs /redoc /openapi.json  (development only)

 Protected routers use verified identity and restricted database sessions.
 Human decisions use the separate decision application.
```

<a id="fastapi-backend-2-module-structure"></a>
### 2. Module structure

```
src/rfq_copilot/api/
    __init__.py        package docstring only; importing it creates nothing
    app.py             create_app(), lifespan, OpenAPI metadata and tags
    config.py          ApiSettings: typed RFQ_API_* settings
    routers/
        __init__.py
        health.py      GET /health/live, Liveness response model
        system.py      GET /api/v1/system/info, SystemInfo model, get_system_info dependency
        ...            protected customer, RFQ, catalog, quotation and attachment routers
    auth.py            JWT verification and database-derived principal
    db.py              restricted application database/session dependencies
    schemas/           API contracts and exact-number types
    decision_app.py    separate human-decision application
```

Health and system response models stay beside their routers. Shared business contracts
live in `schemas/`; identity and session dependencies live in `auth.py` and `db.py`.

<a id="fastapi-backend-3-entry-point-and-application-factory"></a>
### 3. Entry point and application factory

`create_app(settings=None, *, database=None, mcp=None) -> FastAPI` builds a new, independent
application each time it is called:

- `settings=None` reads `RFQ_API_*` from the process environment. Tests pass an explicit
  `ApiSettings`.
- Metadata: title `Engineering RFQ Copilot API`; version from the installed package
  metadata (`importlib.metadata.version("engineering-rfq-copilot")`, currently `0.1.0`), so
  it cannot drift from `pyproject.toml`; `debug` stays `False`.
- It registers public health/metadata routes and protected business routers. Human
  decision routes are mounted only by the separate decision application.

There is **no module-level `app`**. Uvicorn calls the factory:

```bash
RFQ_API_ENVIRONMENT=development uv run uvicorn --factory rfq_copilot.api.app:create_app --host 127.0.0.1 --port 8000
```

So importing `rfq_copilot.api.app` reads no environment, cannot fail on configuration and
creates no application. A test asserts that no database module (`sqlalchemy`,
`asyncpg`, `dotenv`, `rfq_copilot.database`, `rfq_copilot.config`, `rfq_copilot.seeding`)
is required merely to serve the public liveness and metadata routes.

<a id="fastapi-backend-4-http-request-lifecycle-get-apiv1systeminfo"></a>
### 4. HTTP request lifecycle (`GET /api/v1/system/info`)

1. Uvicorn accepts the TCP connection, parses HTTP/1.1 and calls the ASGI application
   returned by `create_app()`.
2. Starlette's router (inside FastAPI) matches the path and method. An unknown path
   returns `404 {"detail":"Not Found"}`, and a known path with another method returns
   `405` with `Allow: GET`.
3. FastAPI resolves the endpoint's dependencies: `get_system_info(request)` builds a
   `SystemInfo` from `request.app.title`, `request.app.version` and `API_VERSION`.
4. The endpoint `info()` returns the injected `SystemInfo`.
5. FastAPI validates the return value against the declared return type (`SystemInfo`,
   `extra="forbid"`) and serializes it to JSON. A value that breaks the contract is a
   server error (`500 Internal Server Error`, plain text, no traceback).
6. Uvicorn writes the response (`200`, `application/json`).

<a id="fastapi-backend-5-routers"></a>
### 5. Routers

| Router | Prefix (after include) | Tag | Module |
| --- | --- | --- | --- |
| `health.router` | `/health` | `health` | `routers/health.py` |
| `system.router` | `/api/v1/system` | `system` | `routers/system.py` |

Liveness stays outside the versioned prefix because infrastructure probes target a
stable path that does not change with the API contract. Business endpoints use
`/api/v1/...` routers, one module per tag.

<a id="fastapi-backend-6-response-contracts-pydantic-v2"></a>
### 6. Response contracts (Pydantic v2)

Both models are `frozen=True, extra="forbid"`:

- `Liveness`: `status: Literal["alive"]`.
- `SystemInfo`: `name: str`, `version: str`, `api_version: Literal["v1"]`.

Endpoints declare the model as their return type, so FastAPI uses it both as the
OpenAPI schema and as an output filter. Nothing is serialized from ORM objects or
domain entities, and no domain type is duplicated.

<a id="fastapi-backend-7-dependency-injection"></a>
### 7. Dependency injection

`get_system_info` is a FastAPI dependency (`Annotated[SystemInfo, Depends(get_system_info)]`).
Tests replace it with `app.dependency_overrides[get_system_info] = ...`. Overrides are
per application instance (a test proves that a second instance is unaffected). They are
also how protected endpoints receive a caller-owned `AsyncSession` or a verified
identity.

Protected routes use verified-identity and restricted-session dependencies. Missing trust
configuration or database authority fails closed; no placeholder authenticator grants access.

<a id="fastapi-backend-8-configuration"></a>
### 8. Configuration

`ApiSettings` (`api/config.py`) is a frozen Pydantic model:

| Variable | Values | Default | Effect |
| --- | --- | --- | --- |
| `RFQ_API_ENVIRONMENT` | `development`, `production` | `production` | `development` serves `/docs`, `/redoc`, `/openapi.json`; `production` returns 404 for all three |

- `ApiSettings.from_env(environ=None)` reads only variables that start with `RFQ_API_`,
  from the process environment or from an explicit mapping (tests).
- Invalid values (`staging`, empty, wrong case) and **unknown** `RFQ_API_*` variables
  raise `pydantic.ValidationError`, so a typo stops startup instead of being ignored.
- No dotenv file is discovered by serving configuration. Main API and MCP processes select
  `APP_DATABASE_URL`; the decision process selects `DECISION_DATABASE_URL`. Administrative
  tools retain explicitly selected owner configuration.
- Settings are never returned by an endpoint. JWT, RBAC, revocation, storage and permission
  policies have separate typed settings; see [authentication](authentication.md).
- Production defaults disable docs and debugging. Sanitized validation and error handlers
  prevent sensitive input and internal exception details from appearing in responses.

<a id="fastapi-backend-9-lifespan"></a>
### 9. Lifespan

`lifespan(app)` is an `asynccontextmanager` passed to `FastAPI(lifespan=...)`. It logs
`API startup: <title> <version>` before serving and `API shutdown: <title> <version>`
after the last request on the `rfq_copilot.api.app` logger. The shutdown step sits in a
`try/finally` around `yield`, including exceptional shutdown. Application-owned lazy database
resources are disposed at shutdown; injected resources remain caller-owned. Startup does
not run migrations, seed data or register rules. Uvicorn's default logging configuration
may omit application `INFO` lines; tests inspect lifespan logs explicitly.

<a id="fastapi-backend-11-openapi"></a>
### 11. OpenAPI

When documentation is enabled, `/openapi.json` describes the registered routers and their
response schemas; `/docs` and `/redoc` serve interactive documentation. Production defaults
hide these endpoints. `app.openapi()` remains available for offline contract inspection.

<a id="fastapi-backend-13-public-route-security-boundaries"></a>
### 13. Public-route security boundaries

- Liveness and metadata routes expose only fixed health and package metadata.
- Protected routes derive identity, customer binding and grants from verified authentication.
- Ordinary API handlers cannot use decision authority; human decisions have a separate app.
- No model, request body or browser-supplied state grants approval or release authority.

<a id="fastapi-backend-15-adding-endpoints-safely"></a>
### 15. Adding endpoints safely

- Put each endpoint in a router under `/api/v1`, with an explicit Pydantic request and
  response contract, and never return ORM objects or service results directly.
- Derive customer identity from verified authentication in a dependency, never from a
  request field or header. Pass it as `customer_id` to the customer-scoped repositories.
- Reuse application-owned restricted database factories and yield one `AsyncSession` per
  request through a dependency. Evaluations use `snapshot_session()` (read only,
  REPEATABLE READ) and explicit snapshot selection, as the services require.
- Load rules by pinned version (`approval-v1.2` for new evaluations) through the trusted
  manifest; never accept a rule version or document from the client.
- Keep calculation and approval separate: an endpoint may return
  `draft_creation_allowed`, but release always requires a human decision.
- Keep money values as `Decimal`, serialized as strings, never floats.


## API contracts: Pydantic models and request validation

<a id="pydantic-api-validation-1-objective-and-scope"></a>
### 1. Objective and scope

The goal is a reusable, tested schema layer that authenticated RFQ endpoints will
use:
- request and response DTOs
- reusable exact-number types
- structural validation, including one cross-field check
- a structured HTTP 422 error contract installed in `create_app()`
- OpenAPI verification

Excluded: live routes, persistence, service calls, authentication, authorization and
attachments.

<a id="pydantic-api-validation-2-domain-contracts-examined"></a>
### 2. Domain contracts examined

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

<a id="pydantic-api-validation-3-structure"></a>
### 3. Structure

```
src/rfq_copilot/api/
    app.py + installs the RequestValidationError handler
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

<a id="pydantic-api-validation-47-request-and-response-models"></a>
### 4–7. Request and response models

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

<a id="pydantic-api-validation-8-field-level-validation"></a>
### 8. Field-level validation

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

<a id="pydantic-api-validation-9-cross-field-validation"></a>
### 9. Cross-field validation

Only one check: `part_id` must be unique within `parts`, mirroring
`UNIQUE(rfq_id, part_id)`. The validator's internal message names the position
(`parts[2]`), never the identifier. The public 422 message is the static "part_id values
must be unique within an RFQ.", at field `body.parts`.
It runs only once every part is valid, which is standard Pydantic order.

Rules that depend on the part type, such as height or weld length being required for
some types, are rule-document decisions (`height_required`, `weld_length_required`) and
are not duplicated.

<a id="pydantic-api-validation-10-decimal-handling-and-json"></a>
### 10. Decimal handling and JSON

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

<a id="pydantic-api-validation-11-structured-error-contract"></a>
### 11. Structured error contract

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

<a id="pydantic-api-validation-12-request-lifecycle"></a>
### 12. Request lifecycle

1. Uvicorn passes the request to the app built by `create_app()`.
2. FastAPI reads the body and runs `json.loads`. Invalid JSON raises
   `RequestValidationError` (`json_invalid`).
3. FastAPI validates the parsed value against the endpoint's body model in Python mode,
   which is why the wire checks are before-validators.
4. On success the handler runs with a typed, frozen model.
5. On failure the handler never runs. `request_validation_handler` (`api/errors.py`)
   builds a `ValidationErrorResponse` from `exc.errors()` through `to_issue`.
6. FastAPI sends HTTP 422 with `application/json`.

<a id="pydantic-api-validation-13-test-only-http-strategy"></a>
### 13. Test-only HTTP strategy

`tests/test_api_schemas.py::harness()` builds `create_app(DEV)` and attaches a router
inside the test: `POST /test-only/rfq`. It records each call and echoes the parsed request
in a test-only `Accepted` model. The route exists only inside that test's application.
Tests confirm that the production application, in both environments, has neither the
route nor its schemas, and the real Uvicorn server returns 404 for it.

<a id="pydantic-api-validation-14-openapi"></a>
### 14. OpenAPI

Verified:
- `PartSpecification` has `additionalProperties: false` and requires only `part_id`.
- Measurements are an integer or a patterned string; quantity is an integer with `minimum: 1`.
- `part_type` is an enum.
- Money uses a string pattern in serialization mode.
- An opted-in route documents `422 → ValidationErrorResponse`, `HTTPValidationError` is
  absent, and `ValidationIssue` forbids extra fields.

`/docs`, `/redoc` and `/openapi.json` are exposed only when the application settings
enable documentation. Production defaults keep these endpoints disabled.

<a id="pydantic-api-validation-1517-security-boundaries"></a>
### 15–17. Security boundaries

- **API validation is not domain validation.** The schemas decide only whether input is
  well formed. Catalog support, manufacturability, inventory, capacity, lead time, price,
  customer standing and approval are decided by the services, under the pinned rules.
- **No identity in the body.** A body is attacker-controlled. The customer must come
  from verified authentication and be passed to the customer-scoped repositories, as
  the API validation boundary requires. `customer_id` in the body is rejected, so it cannot
  be mistaken for proof.
- **Rule versions are never accepted from the client.** `pricing_rule_version` in a
  request is rejected, and responses only report the version the server used.
- **No approval vocabulary in requests.** `approved` or any status field is rejected.

<a id="pydantic-api-validation-18-how-future-endpoints-should-use-these-schemas"></a>
### 18. How endpoint adapters use these schemas

1. Declare `body: RfqSubmissionRequest` and `responses=VALIDATION_ERROR_RESPONSES`. Add any new
   request model to `errors.REQUEST_MODELS` so that its field names appear in error paths.
2. Obtain the customer and role from an authentication dependency, never from the body.
3. Convert each part with `Part.from_mapping(spec.model_dump(mode="json", exclude_none=True))`.
   A test proves that this accepts every valid request unchanged.
4. Call the services in a `snapshot_session()` with pinned rules (`approval-v1.2` for new
   evaluations).
5. Build `QuoteCalculationSummary` or `RfqReceipt` explicitly from the results, never
   by serializing ORM objects or service dataclasses.

<a id="pydantic-api-validation-19-limitations-and-deferred-work"></a>
### 19. Limitations and deferred work

- `MAX_PARTS = 50` and `MAX_RAW_REQUEST_CHARS = 20 000` are chosen limits; the contract
  requires limits but gives no numbers. They are marked `ponytail:` in `rfq.py`.
- There is no request body size limit at the server level, which belongs with deployment.
- `RfqReceipt` and `QuoteCalculationSummary` have no route yet; their shapes come from the
  tool contract and `display_quote`.
- An evaluation response, attachments, delivery location and idempotency are deferred.
- `part_type` is a contract vocabulary. If a future rule document allows a new type,
  the contract must change too.


## Attachment uploads and document extraction

The main FastAPI application supports authenticated PDF and CSV BOM upload plus attachment
metadata listing. This guide describes the current contract; the filename is retained for
existing links. Original implementation and verification history is archived privately.

<a id="file-uploads-extraction-endpoints-and-authorization"></a>
### Endpoints and authorization

| Method / route | Input | Output |
| --- | --- | --- |
| `POST /api/v1/rfqs/{rfq_id}/attachments` | `multipart/form-data`, exactly one file field named `file` | `201`, attachment metadata and typed PDF/BOM extraction |
| `GET /api/v1/rfqs/{rfq_id}/attachments` | RFQ identifier | Attachment metadata list |

Both routes require verified identity and an eligible customer binding. Listing requires
`view_assigned_rfqs`. Upload requires `create_rfq` under policy v1 or
`attach_rfq_document` under v2. The RFQ must belong to the bound customer and, when enabled,
be assigned to the user. Missing, foreign and unassigned RFQs receive the same 404 response.

Authentication, permission, storage configuration and RFQ ownership are checked before
reading file content. See [the router](../../src/rfq_copilot/api/routers/attachments.py) and
[current authorization policy](authentication.md).

<a id="file-uploads-extraction-storage-and-input-limits"></a>
### Storage and input limits

Set `RFQ_API_ATTACHMENT_DIR` to an absolute private storage directory. Missing configuration
fails closed. New stored files have server-generated names and mode `0600`; the storage
boundary verifies directory ownership/permissions and refuses unsafe paths and symlinks.
Storage references and directory paths are not returned to clients.

The default file limit is 10 MiB, configured by `RFQ_API_MAX_UPLOAD_BYTES`. The multipart
stream has a byte cap including a bounded allowance for headers. Exactly one file and no
extra form fields are accepted. Filename, extension, declared media type, magic bytes,
empty content and size are validated; ZIP/spreadsheet archives are not expanded.

Only PDF and CSV BOM uploads are supported. Existing metadata can also describe historical
Markdown attachments; this does not make Markdown an accepted upload type.

<a id="file-uploads-extraction-extraction-contracts"></a>
### Extraction contracts

- PDF extraction returns text pages, page count, truncation and `text`/`no_text` status.
  Scanned documents are not OCRed. Parsing uses an isolated child process with an empty
  environment, wall-clock/CPU limits and platform-dependent memory limits. Database and
  decision credentials are not supplied to that process.
- CSV BOM extraction validates UTF-8, the required column set, row shapes and row limits.
  Decimal values remain exact. Parsed rows are untrusted requirements, not catalog,
  feasibility or pricing decisions; missing manufacturing facts still need validation.
- Upload returns extraction immediately. The list route returns metadata only; there is
  no download or later extraction-retrieval HTTP route.

See [response schemas](../../src/rfq_copilot/api/schemas/attachments.py) and
[extraction/storage implementation](../../src/rfq_copilot/attachments.py).

<a id="file-uploads-extraction-transaction-ownership-and-idempotency"></a>
### Transaction ownership and idempotency

The upload uses a separate writable database/session from read routes. It releases its
connection while receiving/parsing the body, then rechecks RFQ ownership before insertion.
The route commits; errors before commit roll back and remove the file once its writer
finishes. An uncertain commit keeps the file for reconciliation rather than risking a
committed row that references a deleted file.

With `RFQ_API_UPLOAD_IDEMPOTENCY=enabled`, an optional `Idempotency-Key` header binds the
verified actor/customer, RFQ and canonical upload content. Identical retries return the
stored response; reuse for different content receives 409. A retry should retain its key.
Enabling the setting alone does not require every caller to send a key.

The [reconciliation CLI](../../src/rfq_copilot/reconcile_attachments.py) is dry-run by default.
Review its output and operational instructions before any explicitly authorized deletion.
Never clean the upload directory blindly after an ambiguous commit.

<a id="file-uploads-extraction-errors-and-verification"></a>
### Errors and verification

Rejected content has a fixed `ATTACHMENT_REJECTED` envelope and reason vocabulary. Path/body
validation uses the API validation envelope. Responses expose neither driver exceptions nor
private storage paths. The manually parsed multipart request still needs an OpenAPI input
contract; see the [current limitations register](technical_debt.md).

Relevant regression modules include `tests/test_attachments.py`,
`tests/test_attachments_database.py`, `tests/test_attachment_reconciliation.py`, and
`tests/test_api_security_database.py`. Database checks require the guarded test target.
No verification result is asserted by this documentation update.

## Attachment reconciliation and uncertain commits

`python -m rfq_copilot.reconcile_attachments --storage-dir /absolute/private/uploads --env-file .env`
is a dry run by default. An upload with an uncertain commit retains its file. Reconciliation
queries metadata read-only and reports aggregate counts; it never repairs missing database
rows or rewrites customer records. `--list-missing` exposes only server-generated attachment IDs.

Destructive orphan removal requires explicit `--delete`, a minimum grace interval, and a
verified version-2 storage/database binding marker. `--bind` is development-only and must
match the configured API attachment directory. A legacy marker stays untouched until explicit
`--replace-legacy`, which preserves the old marker aside. Directory ownership, mode 0700,
trusted ancestors, descriptor-relative no-follow operations and an exclusive directory lock
protect traversal. Candidates are server-named regular files only. Rename/reopen/inode/size/
mtime/age rechecks precede unlink; uncertainty restores or quarantines the file. No automatic
startup cleanup occurs. Missing-file metadata and uncertain pairing require operator review.
