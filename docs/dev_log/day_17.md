# Development Log: Day 17 — Pydantic Models and API Validation

Date: 2026-11-10

## Objective

Contracts for future RFQ endpoints: Pydantic v2 request and response schemas, exact
number handling, one structural cross-field rule, and a structured HTTP 422 error body.
No route uses the RFQ schemas; the production API is unchanged apart from the 422
handler. Technical description:
[day_17_pydantic_api_validation.md](../development/day_17_pydantic_api_validation.md).

## Baseline

- Day 16 reviewed but **uncommitted**. Modified: `.github/workflows/smoke.yml`,
  `README.md`, `pyproject.toml`, `uv.lock`. Untracked: `src/rfq_copilot/api/`,
  `tests/test_api.py`, both Day 16 docs, and `day16_review_bundle.zip` (left untouched).
- 1151 tests passing.

## Files

| File | Day 17 change |
| --- | --- |
| `src/rfq_copilot/api/schemas/__init__.py` | New: package docstring. |
| `src/rfq_copilot/api/schemas/common.py` | New: `Identifier`, `PositiveMillimetres`, `NonNegativeMillimetres`, `Count`, `PositiveCount`, `IsoDate`, `Money`, `reject_blank_text`. |
| `src/rfq_copilot/api/schemas/rfq.py` | New: `PartSpecification`, `RfqSubmissionRequest`, `RfqReceipt`, `QuoteLineAmount`, `QuoteCalculationSummary`. |
| `src/rfq_copilot/api/errors.py` | New: `ValidationIssue`, `ValidationErrorResponse`, `to_issue`, `request_validation_handler`, `VALIDATION_ERROR_RESPONSES`. |
| `src/rfq_copilot/api/app.py` (Day 16 file) | Two imports and `exception_handlers={RequestValidationError: request_validation_handler}`. Nothing else. |
| `tests/test_api_schemas.py` | New: 103 tests. |
| `.github/workflows/smoke.yml` | Appends `tests/test_api_schemas.py`. |
| `README.md` | Status row, "What works today", smoke command, regression count, API contracts paragraph, limitation. |
| `docs/development/day_17_pydantic_api_validation.md` | New. |

No dependency, lockfile, service, repository, ORM, migration, seeding, rule, dataset or
`tests/test_api.py` change.

## Decisions

- **Follow the services, not the tool template.** The dataset's `create_rfq` and
  `calculate_quote` templates require nine part fields. `entities.Part` and the
  manufacturing validator deliberately accept incomplete parts (`needs_clarification`).
  Requiring the fields at the API would turn clarification outcomes into 422s, so only
  `part_id` is required.
- **No business rules in validators.** Ranges, allowed part types, catalog
  materials/finishes, and part-type-dependent requirements (height, weld length) stay
  in the rule documents. Only structural bounds from the tool contract (positive or
  non-negative) and the database precision are enforced.
- **Exact wire format.** JSON integers or plain decimal strings only; floats, booleans,
  exponents, signs and non-finite values are refused, and excess scale is rejected,
  never rounded. A parity test proves the API and `Part.from_mapping` accept and refuse
  the same precision cases, and that a valid request converts with identical values.
- **No identity, rule version or approval in requests.** `customer_id`,
  `pricing_rule_version` and `approved` are unknown fields, so they are rejected.
- **Flat part model**, as in `Part`; no nested material sub-model.
- **Money keeps its scale.** The pricing rules own the scale, so `Money` checks
  exactness and non-negativity only, and serializes with `str()`, like `display_quote`.
- **Errors never echo input.** Only `loc`, `type` and `msg` are used; client-chosen keys
  that do not look like field names are `<redacted>`. 404, 405 and opaque 500 behaviour
  is unchanged.

## Problems and fixes

1. **Spurious `too_short` error.** With `tuple[...]` plus `min_length`, any invalid part
   also produced `parts: too_short` ("at least 1 item after validation, not 0").
   Reproduced in isolation: `tuple` does this and `list` does not. Switched `parts` and
   `lines` to `list`.
2. **Empty string code.** `""` is reported as `blank_or_invalid_text`, because the shared
   before-validator runs before the length check. That is correct; I fixed the test
   expectations.
3. **Schema shape.** Pydantic flattens the optional measurement schema into one
   `anyOf` (integer, pattern string, null); I fixed the test expectation.
4. **Private data in my console output.** One aggregate scan of development and
   validation ground truth used `Counter.update` on dicts, which concatenated field
   values into the session output. Nothing was written to any file. Only aggregate facts
   are used in the docs.

## Commands and results

```text
$ uv lock --check                                              # Resolved 40 packages
$ uv run --no-sync ruff check .                                # All checks passed!
$ uv run --no-sync ruff format --check src tests alembic       # 90 files already formatted
$ uv run --no-sync python -m pytest -q -p no:cacheprovider tests/test_api.py
40 passed in 0.53s
$ uv run --no-sync python -m pytest -q -p no:cacheprovider tests/test_api_schemas.py
103 passed in 0.20s
$ env -u DATABASE_URL -u TEST_DATABASE_URL -u TEST_DATABASE_NAME uv run --no-sync python -m pytest -q \
    -p no:cacheprovider tests/test_database_config.py tests/test_hardening_public.py tests/test_api.py tests/test_api_schemas.py
185 passed in 0.43s
# same four files in a fresh copy after `uv sync --locked` (no .env, no dataset)
185 passed in 1.01s
$ uv run --no-sync python -m pytest -q -p no:cacheprovider
1254 passed in 130.37s (0:02:10)                               # 1151 + 103; 0 failed, 0 skipped, 0 warnings
$ .venv/bin/python -m rfq_copilot.integrity --target development ...   # exit 0; 34 PASS; records 1090
```

Uvicorn (`--factory rfq_copilot.api.app:create_app`, `127.0.0.1:8765`, no database
variables), stopped with SIGTERM:

```text
development: /health/live 200  /api/v1/system/info 200  /openapi.json 200  /docs 200  POST /test-only/rfq 404
             openapi paths ['/api/v1/system/info', '/health/live'], schemas ['Liveness', 'SystemInfo']
production:  /health/live 200  /api/v1/system/info 200  /openapi.json 404  /docs 404  POST /test-only/rfq 404
Both: "Application shutdown complete."; port free afterwards.
```

## Deferred

- live RFQ routes, authentication and authorization
- an evaluation-outcome response, attachments, delivery location and idempotency
- a server-level body size limit
- evidence-based values for `MAX_PARTS` and `MAX_RAW_REQUEST_CHARS`

## Git status

Nothing staged or committed:

```text
 M .github/workflows/smoke.yml      (Day 16 + Day 17 line)
 M README.md                        (Day 16 + Day 17 sections)
 M pyproject.toml                   (Day 16 only)
 M uv.lock                          (Day 16 only)
?? day16_review_bundle.zip          (user's; untouched)
?? docs/dev_log/day_16.md           (Day 16)
?? docs/dev_log/day_17.md           (Day 17)
?? docs/development/day_16_fastapi_backend.md        (Day 16)
?? docs/development/day_17_pydantic_api_validation.md (Day 17)
?? src/rfq_copilot/api/             (Day 16 + Day 17 errors.py, schemas/, app.py handler)
?? tests/test_api.py                (Day 16, unchanged)
?? tests/test_api_schemas.py        (Day 17)
```

## Correction (2026-11-10): security audit, information disclosure in 422 errors

An independent audit found two ways the 422 handler could reflect client data. Both are
fixed in `src/rfq_copilot/api/errors.py`, and earlier entries above are kept unchanged.

**Finding 1, unknown field names.** `_segment()` kept any location segment matching
`[A-Za-z_][A-Za-z0-9_]{0,63}`, so an unknown key like `sk_live_9f8e7d6c5b4a` was echoed in
`field`. **Fix:** segments are now an allowlist: list indexes, location roots, and field
names declared by registered request models (`REQUEST_MODELS = (RfqSubmissionRequest,)`,
collected from their JSON schemas, including nested `PartSpecification`). Everything else is
`<redacted>`, including `customer_id`. Unregistered models fail closed.

**Finding 2, raw messages.** `to_issue()` returned Pydantic's `msg`, so a validator raising
`ValueError(f"... {value}")` reflected the input. **Fix:** messages come from the static
`MESSAGES` table keyed by error code; unknown codes (including `value_error`) get
`"Invalid value."`; codes not matching `[a-z_]{1,64}` become `invalid`. `msg`, `input`,
`ctx` and `url` are never read.

**Unchanged:** HTTP 422, `REQUEST_VALIDATION_ERROR`, the `{code, message, details[field,
code, message]}` shape, error codes, 404/405, the opaque 500 for response-model failures,
schemas, routes and dependencies.

**Tests.** 12 new tests in `tests/test_api_schemas.py` ("Security regressions"):
- token-shaped, nested-sensitive and punctuated unknown keys
- a validator `ValueError` containing a secret (test-only `LeakyProbe` model)
- multiple errors in one response
- known-field messages
- the duplicate-part error
- malformed JSON (3 cases)
- predefined messages only
- Day 16 endpoints unchanged

Run against the previous `errors.py`, **12 failed / 103 passed**; with the fix, all pass.

Three earlier Day 17 tests asserted the old behaviour (raw Pydantic messages,
`body.customer_id` echoed, raw `msg` in `to_issue`). I updated them to the corrected
contract; no Day 16 test changed.

```text
$ uv lock --check                                              # Resolved 40 packages
$ uv run --no-sync ruff check .                                # All checks passed!
$ uv run --no-sync ruff format --check src tests alembic       # 90 files already formatted
$ uv run --no-sync python -m pytest -q -p no:cacheprovider tests/test_api.py
40 passed in 0.29s
$ uv run --no-sync python -m pytest -q -p no:cacheprovider tests/test_api_schemas.py
115 passed in 0.21s
$ env -u DATABASE_URL -u TEST_DATABASE_URL -u TEST_DATABASE_NAME uv run --no-sync python -m pytest -q \
    -p no:cacheprovider tests/test_database_config.py tests/test_hardening_public.py tests/test_api.py tests/test_api_schemas.py
197 passed in 0.43s
$ uv run --no-sync python -m pytest -q -p no:cacheprovider
1266 passed in 119.91s (0:01:59)                               # 0 failed, 0 skipped, 0 warnings
$ .venv/bin/python -m rfq_copilot.integrity --target development ...   # exit 0; 34 PASS; records 1090
```

Changed in this correction: `src/rfq_copilot/api/errors.py`, `tests/test_api_schemas.py`,
`docs/development/day_17_pydantic_api_validation.md`, this log. Nothing staged;
`day16_review_bundle.zip` and `day17_review_bundle.zip` untouched.

## Correction (2026-11-10): error-code allowlist and unknown-field redaction

A second audit found two remaining reflections in `src/rfq_copilot/api/errors.py`.
Earlier entries are kept unchanged, and both earlier fixes are preserved.

**A. Error codes.** `to_issue()` copied any error type matching `[a-z_]{1,64}`. A validator
raising `PydanticCustomError(submitted_value, ...)` therefore published the value in
`code`. **Fix:** `PUBLIC_CODES = frozenset(MESSAGES) | {"value_error"}` is an explicit
allowlist. `value_error` is Pydantic's fixed type for a `ValueError` and carries the
generic message. Any other type is published as `invalid`. The regex is removed.

**B. Unknown-field locations.** An unknown key that is a field name in another registered
model (`quantity` at the root, `raw_request` inside a part) passed the allowlist and was
reflected. **Fix:** for `extra_forbidden`, the last location segment is always
`<redacted>`. Paths of declared fields are unchanged.

**Tests.** 8 new tests ("Security regressions, round 2"):
- the exact `PydanticCustomError(submitted_value)` disclosure (test-only `TypeLeakProbe`)
- the code allowlist
- 5 parametrized unknown-key cases: `raw_request` in a part, `quantity` and `part_id` at
  the root, a token-shaped key, a nested token key
- declared paths staying useful

Against the previous `errors.py`, **5 failed**: the code leak, the allowlist test, and the
three declared-elsewhere keys. The token-shaped, nested-token and declared-path tests
already passed, which confirms the first fix. With the fix, all pass. The 9 tests from the
first correction still pass.

```text
$ uv lock --check                                              # Resolved 40 packages
$ uv run --no-sync ruff check .                                # All checks passed!
$ uv run --no-sync ruff format --check src tests alembic       # 90 files already formatted
$ uv run --no-sync python -m pytest -q -p no:cacheprovider tests/test_api.py
40 passed in 0.33s
$ uv run --no-sync python -m pytest -q -p no:cacheprovider tests/test_api_schemas.py
123 passed in 0.24s
$ env -u DATABASE_URL -u TEST_DATABASE_URL -u TEST_DATABASE_NAME uv run --no-sync python -m pytest -q \
    -p no:cacheprovider tests/test_database_config.py tests/test_hardening_public.py tests/test_api.py tests/test_api_schemas.py
205 passed in 0.68s
$ uv run --no-sync python -m pytest -q -p no:cacheprovider
1274 passed in 118.14s (0:01:58)                               # 0 failed, 0 skipped, 0 warnings
```

Changed in this correction: `src/rfq_copilot/api/errors.py`, `tests/test_api_schemas.py`,
`docs/development/day_17_pydantic_api_validation.md`, this log. No schema, Day 16, service,
ORM, migration, rule or dataset change. Nothing staged; both review bundles untouched.
