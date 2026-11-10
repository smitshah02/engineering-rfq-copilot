# Development Log: Day 18 — Database-Backed REST APIs

Date: 2026-11-10

## Objective

Read-only, customer-scoped REST routes over the existing repositories, behind a fail-closed
identity seam. There is no authenticator yet, so every protected route returns 401 outside
tests. Technical description:
[day_18_database_rest_apis.md](../development/day_18_database_rest_apis.md).

Baseline: Days 16–17 uncommitted (preserved); 1274 tests; smoke 205.

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/api/auth.py` | New: `Principal`, `get_principal` (always 401; reads no request input). |
| `src/rfq_copilot/api/db.py` | New: `get_database` (injected, or lazy read-only `Database` from `DATABASE_URL`), `get_read_session` (depends on the principal; `DatabaseError` → 503). |
| `src/rfq_copilot/api/schemas/reads.py` | New: query models and response models. |
| `src/rfq_copilot/api/routers/{customers,rfqs,materials,quotes}.py` | New: 5 GET routes. |
| `src/rfq_copilot/api/app.py` | `create_app(..., database=None)`; routers and tags; app state; lifespan disposes only a database it created; description updated. |
| `src/rfq_copilot/api/errors.py` | Registers `RfqListQuery`, `MaterialListQuery` and the path names `rfq_id`, `quote_id`; allowlists `int_parsing`. |
| `src/rfq_copilot/repositories/queries.py`, `__init__.py` | Additive: `list_rfqs` (23 lines added, none removed). |
| `tests/test_api_reads.py` | New: 55 public tests. |
| `tests/test_day18_api_database.py` | New: 13 PostgreSQL integration tests. |
| `tests/test_api.py`, `tests/test_api_schemas.py` | 3 exact route-set assertions updated to the new exact set (see Problems). |
| `.github/workflows/smoke.yml` | Adds `tests/test_api_reads.py`. |
| `README.md` | Status, "What works today", endpoint table, testing table, limitations. |
| `docs/development/day_18_database_rest_apis.md` | New. |

No ORM, migration, service, rule, dataset or dependency change.

## Decisions

- **Customer-facing quotes need a human release.** The architecture requires human
  approval for every customer-facing release, and drafts are internal. So only
  `status == "released"` quotes are returned, using the approved version. Aggregate check
  of the development database: 80 quotes, 10 in each of 8 statuses, **0 released**. Every
  real quote lookup is therefore 404 today, which is correct, not a defect.
- **Minimal response fields.** Credit, account-status, restriction, approval, cost,
  supply, internal-user and provenance fields are excluded explicitly.
- **Reuse.** `get_customer`, `get_rfq`, `find_materials`, `list_materials` and
  `list_quotation_history` are reused unchanged. A customer RFQ list had no repository
  function, so `list_rfqs` was added beside `get_rfq` instead of writing SQL in the API.
- **Lazy database modules.** The Day 16 test requires that the application starts
  without SQLAlchemy. Routers and dependencies import database modules inside functions,
  so that guarantee holds.
- **Read-only at the server.** The lazily built `Database` uses `read_only=True`
  (`default_transaction_read_only=on`). Sessions are never committed.

## Problems and fixes

1. **Exact route-set assertions.** Two Day 16 tests and one Day 17 test asserted that only
   the two Day 16 routes exist. Day 18 adds routes by design. I updated them to the new
   exact set (`ROUTES`), still exact, still GET-only, and still without
   `RfqSubmissionRequest`. The API description, "Business endpoints are not exposed yet",
   was no longer true and was updated with its assertion.
2. **Dependency order versus validation.** With a valid principal, FastAPI resolves the
   session dependency before reporting parameter errors, so a tripwire on `get_database`
   fired for 422 cases. That is not a leak, because `AsyncSession` connects lazily. The
   test now proves no I/O happens: an unreachable database still yields 422, not 503.
   Unauthenticated requests never reach the database dependency.
3. **`int_parsing` reported as `invalid`.** The Day 17 allowlist worked as designed; I
   added the standard code for text-to-integer query parameters, with a static message.
4. **Pool assertion.** `checkedout() == 0` failed because the `seed_database` fixture holds
   its own lease. The test now compares with the pre-request baseline.
5. **zsh word splitting.** A smoke command with an unquoted `$SMOKE` ran nothing; I reran it
   with an array. No result was taken from that run.

**Mutation checks** (each reverted and verified with `cmp`):
- removing the release filter: 8 tests fail
- ignoring the RFQ scope: 2 tests fail
- reading identity from a header: the spoofing test fails

## Commands and results

```text
$ uv lock --check                                              # Resolved 40 packages
$ uv run --no-sync ruff check .                                # All checks passed!
$ uv run --no-sync ruff format --check src tests alembic       # 99 files already formatted
$ uv run --no-sync python -m pytest -q -p no:cacheprovider tests/test_api.py               # 40 passed
$ uv run --no-sync python -m pytest -q -p no:cacheprovider tests/test_api_schemas.py       # 123 passed
$ uv run --no-sync python -m pytest -q -p no:cacheprovider tests/test_api_reads.py         # 55 passed
$ uv run --no-sync python -m pytest -q -p no:cacheprovider tests/test_day18_api_database.py  # 13 passed in 5.27s
$ env -u DATABASE_URL -u TEST_DATABASE_URL -u TEST_DATABASE_NAME uv run --no-sync python -m pytest -q \
    -p no:cacheprovider tests/test_database_config.py tests/test_hardening_public.py tests/test_api.py \
    tests/test_api_schemas.py tests/test_api_reads.py
260 passed in 1.13s
# same five files in a fresh `uv sync --locked` copy (no .env, no dataset), before the last 6 tests: 254 passed
$ uv run --no-sync python -m pytest -q -p no:cacheprovider
1342 passed in 135.45s (0:02:15)                               # 1274 + 55 + 13; 0 failed, 0 skipped, 0 warnings
$ .venv/bin/python -m rfq_copilot.integrity --target development ...   # exit 0; 34 PASS; records 1090
```

Uvicorn (`127.0.0.1:8765`, no database variables, with a spoofed `X-Customer-Id` header):

```text
development: /health/live 200  /api/v1/system/info 200  /openapi.json 200
             /api/v1/customers/me, /rfqs, /rfqs/{id}, /materials, /quotes/{id}: 401 each
production:  /health/live 200  /api/v1/system/info 200  /openapi.json 404  protected routes 401
Both: "Application shutdown complete."; port free afterwards.
```

## Deferred

- authentication and RBAC (replace `get_principal`; define staff account scope)
- 403 semantics
- total counts in pages
- a keyed quote read if a customer's quote count grows

## Git status

Nothing staged. Day 18 additions: `src/rfq_copilot/api/{auth.py,db.py,schemas/reads.py,
routers/customers.py,routers/rfqs.py,routers/materials.py,routers/quotes.py}`,
`tests/test_api_reads.py`, `tests/test_day18_api_database.py`, both Day 18 docs. Modified
by Day 18: `src/rfq_copilot/repositories/{queries,__init__}.py` (tracked),
`api/app.py`, `api/errors.py`, `tests/test_api.py` and `tests/test_api_schemas.py`
(untracked Day 16–17 files), `smoke.yml` and `README.md`. Days 16–17 work and both review
bundles are preserved.
