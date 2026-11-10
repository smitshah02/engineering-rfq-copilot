# Development Log: Day 16 — FastAPI Fundamentals and Backend Setup

Date: 2026-11-10

## Objective

Start Phase 3 with the HTTP application foundation: a FastAPI application factory, a
modular router layout, typed response contracts, dependency injection, typed settings,
lifespan, OpenAPI and tests. There are no business endpoints, no authentication and no
database access. Technical description: [day_16_fastapi_backend.md](../development/day_16_fastapi_backend.md).

Baseline before editing: working tree clean at `7df00e0`; 1111 tests (Phase 2.5).

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/api/__init__.py` | New: package docstring only. |
| `src/rfq_copilot/api/app.py` | New: `create_app()`, `lifespan`, OpenAPI metadata and tags. |
| `src/rfq_copilot/api/config.py` | New: `ApiSettings` (`RFQ_API_ENVIRONMENT`), `from_env()`. |
| `src/rfq_copilot/api/routers/__init__.py` | New: package docstring. |
| `src/rfq_copilot/api/routers/health.py` | New: `GET /health/live`, `Liveness`. |
| `src/rfq_copilot/api/routers/system.py` | New: `GET /api/v1/system/info`, `SystemInfo`, `get_system_info`. |
| `tests/test_api.py` | New: 39 tests; no PostgreSQL, credentials, dataset or network. |
| `pyproject.toml`, `uv.lock` | `fastapi`, `uvicorn` (runtime); `httpx` (dev). The lockfile only gains entries; no existing package changed version. |
| `.github/workflows/smoke.yml` | Adds `tests/test_api.py` to the public smoke run. |
| `README.md` | Status row, "What works today", new "API" section, testing table, limitations, structure. |
| `docs/development/day_16_fastapi_backend.md` | New: technical description. |

No changes to services, repositories, ORM models, migrations, seeding, rule manifests,
`rfq_copilot/config.py`, `rfq_copilot/database.py`, `tests/conftest.py` or the dataset.

## Dependencies

| Package | Group | Resolved | Why |
| --- | --- | --- | --- |
| `fastapi>=0.115,<1` | runtime | 0.162.4 | The web framework. It pulls in `starlette` 1.10.0, `pydantic` 2.14.3, `pydantic-core` 2.50.4, `anyio` 4.16.0, `annotated-doc`, `annotated-types`, `typing-inspection`, `idna` and `opentelemetry-api` 1.45.2 (a required dependency of FastAPI 0.162). |
| `uvicorn>=0.30,<1` | runtime | 0.54.0 | ASGI server (plus `click`, `h11`). Plain `uvicorn`, without the `[standard]` extras. |
| `httpx>=0.27,<1` | dev | 0.28.1 | Required by FastAPI/Starlette `TestClient` (plus `httpcore`, `certifi`). |

Not added: `pydantic-settings`. The API has one setting, and a small `from_env` follows the
existing `DatabaseConfig.from_env` pattern (explicit mapping, no second dotenv loader).

## Design decisions

- **Factory, no module-level app.** Uvicorn runs `--factory rfq_copilot.api.app:create_app`.
  Import has no side effects, and invalid configuration fails when the application is
  created, with a clear `ValidationError`.
- **Secure default.** `RFQ_API_ENVIRONMENT` defaults to `production` (no `/docs`, `/redoc`
  or `/openapi.json`); developers opt into `development`. Unknown `RFQ_API_*` variables
  are rejected, so typos are not silently ignored.
- **Separate from the database configuration.** The API reads no dotenv file and does not
  import `rfq_copilot.config` or `rfq_copilot.database`. A test checks this in a fresh
  interpreter.
- **Liveness only.** `{"status": "alive"}` claims nothing about PostgreSQL. A readiness
  check belongs with the first database-backed endpoint.
- **One real dependency.** `get_system_info` is the injection and override seam. There is
  no placeholder session or identity dependency.
- **Lifespan logs only.** There are no resources to open yet; nothing is fabricated.
- **Models live with their router**, without a separate `schemas.py` or `dependencies.py`.

## Problems and fixes

1. **Route introspection.** In FastAPI 0.162, `app.routes` contains `_IncludedRouter`
   wrappers without `.path`. The registration test now uses the public `app.openapi()`
   (generated even in production, where it is not served) instead of private internals.
2. **`httpx` deprecation in Starlette 1.10.** `TestClient` warns: "Using `httpx` with
   `starlette.testclient` is deprecated; install `httpx2` instead." I tried `httpx2`:
   its only release requires Python ≥ 3.14, and the project pins 3.12, so it cannot be
   resolved. I restored `httpx`, and the single warning remains (documented in the README
   limitations).
3. **Venv side effect.** Running `uv remove --dev httpx` synced `.venv` against the
   lockfile and uninstalled `pillow` 13.0.0, `pypdf` 6.21.4 and `reportlab` 4.5.1. These
   had been installed outside `uv.lock` and are not used by `src/` or `tests/`. I
   reinstalled the same versions with `uv pip install` (plus `charset-normalizer`
   3.5.2, which reportlab requires). Any future `uv sync` removes them again, because they are
   not project dependencies.
4. **Formatting.** `ruff format` reflowed two lines in Day 16 files only.

## Commands and results

```text
$ .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_api.py
39 passed, 1 warning in 0.45s

$ .venv/bin/python -m pytest -q -p no:cacheprovider        # full regression, dataset + .env + PostgreSQL
1150 passed, 1 warning in 122.12s (0:02:02)                 # 1111 baseline + 39 new, 0 failed, 0 skipped

$ env -u DATABASE_URL -u TEST_DATABASE_URL -u TEST_DATABASE_NAME \
    uv run pytest -q -p no:cacheprovider tests/test_database_config.py tests/test_hardening_public.py tests/test_api.py
81 passed, 1 warning in 0.53s                              # public smoke set

$ uv lock --check                                          # Resolved 39 packages
$ uv run ruff check .                                      # All checks passed!
$ uv run ruff format --check src tests alembic             # 85 files already formatted

$ .venv/bin/python -m rfq_copilot.integrity --target development --source data/rfq_synthetic_data \
    --archive data/rfq_synthetic_data_v1.1.0.zip --snapshot-dir data/rfq_seed_snapshots --env-file .env
exit 0; 34 checks "PASS"; "records": 1090                  # unchanged from Phase 2.5 (read only)
$ .venv/bin/python -m alembic -x target=development -x env_file=.env current --check-heads
20261109_01 (head)
```

The one warning in every run is the Starlette `httpx` deprecation described above.

Real server, from an empty scratch directory with the database variables unset, bound to
`127.0.0.1:8765`, stopped with SIGTERM:

```text
RFQ_API_ENVIRONMENT=development
  /health/live 200 {"status":"alive"}
  /api/v1/system/info 200 {"name":"Engineering RFQ Copilot API","version":"0.1.0","api_version":"v1"}
  /openapi.json 200   /docs 200
  Uvicorn: Waiting for application shutdown. / Application shutdown complete.
RFQ_API_ENVIRONMENT=production
  /health/live 200   /api/v1/system/info 200   /openapi.json 404   /docs 404
  Uvicorn: Application shutdown complete.
Port 8765 free afterwards.
```

## Remaining limitations

- No authentication, authorization, readiness check or business endpoint (later days).
- The `httpx` deprecation warning remains until the project can use `httpx2` (Python 3.14)
  or Starlette changes its client.
- Lifespan log lines are not visible under Uvicorn's default log configuration; there is
  no application logging configuration yet.

## Git status

Nothing staged or committed:

```text
 M .github/workflows/smoke.yml
 M README.md
 M pyproject.toml
 M uv.lock
?? docs/dev_log/day_16.md
?? docs/development/day_16_fastapi_backend.md
?? src/rfq_copilot/api/
?? tests/test_api.py
```

## Corrections (2026-11-10, after review)

This section supersedes the earlier statements above about `httpx2`, the test counts and
the warning. Earlier text is kept unchanged because this log is append-only.

**1. `httpx2`: the earlier claim was wrong.** "httpx2 requires Python 3.14" is false.
My first attempt used the range `httpx2>=0,<1`, which excludes every 2.x release, and the
resolver then reported that the only remaining candidate (`0.0.0`) needs Python ≥ 3.14. I
reported that hint as a fact about the package. `httpx2` 2.16.0 supports Python 3.11+ and
resolves on this project's Python 3.12. The dev dependency `httpx>=0.27,<1` was replaced by
`httpx2>=2.16,<3`:

| Lock change | Detail |
| --- | --- |
| removed | `httpx` 0.28.1, `httpcore` 1.0.9, `certifi` 2026.10.23 |
| added | `httpx2` 2.16.0, `httpcore2` 2.16.0, `truststore` 0.10.5, `httpx2-jsfetch` 1.0 (locked for `sys_platform == 'emscripten'` only, not installed) |

Starlette 1.10's `TestClient` imports `httpx2` first, so the deprecation warning is gone.
The lockfile was updated with `--no-sync`, then `uv sync --inexact`, so the packages
installed outside the lock (`pillow`, `pypdf`, `reportlab`, see problem 3) stay installed.
`httpx`, `httpcore` and `certifi` were then uninstalled from `.venv`, so no test can use
them by accident.

**2. Lifespan shutdown in `try/finally`.** `lifespan()` in `src/rfq_copilot/api/app.py` now
wraps `yield` in `try/finally`, so the shutdown step also runs when the context exits with
an exception. New test: `test_lifespan_shutdown_runs_when_the_context_raises`. I checked that it
fails against the previous handler and passes with the fix. No resource was added.

**3. Pydantic declared explicitly.** `pydantic>=2,<3` is now a runtime dependency, because
`api/` imports it directly. The locked version is unchanged (2.14.3).

Against `HEAD`, `uv.lock` has 227 insertions and 0 deletions: no package that existed
before Day 16 changed version.

### Corrected results

```text
$ .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_api.py
40 passed in 0.34s

$ env -u DATABASE_URL -u TEST_DATABASE_URL -u TEST_DATABASE_NAME .venv/bin/python -m pytest -q -p no:cacheprovider \
    tests/test_database_config.py tests/test_hardening_public.py tests/test_api.py
82 passed in 0.36s

# The same smoke set in a fresh copy (no .env, no dataset) after `uv sync --locked`, as in CI:
# httpx not installed, httpx2 installed
82 passed in 0.87s

$ .venv/bin/python -m pytest -q -p no:cacheprovider
1151 passed in 132.20s (0:02:12)                # 1111 baseline + 40 API tests; 0 failed, 0 skipped, 0 warnings

$ uv lock --check                               # Resolved 40 packages
$ uv run --no-sync ruff check .                 # All checks passed!
$ uv run --no-sync ruff format --check src tests alembic   # 85 files already formatted

$ .venv/bin/python -m rfq_copilot.integrity --target development ...   # exit 0; 34 "PASS"; "records": 1090
```

Uvicorn (`--factory rfq_copilot.api.app:create_app`, `127.0.0.1:8765`, no database
variables) gave the same responses as before. Development: `/health/live` 200,
`/api/v1/system/info` 200, `/openapi.json` 200, `/docs` 200. Production: 200, 200, 404,
404. Both runs ended with "Application shutdown complete", and the port was free afterwards.

The "Remaining limitations" line about the `httpx` deprecation warning no longer applies.
The others still stand.

Git status after corrections: nothing staged. Modified: `.github/workflows/smoke.yml`,
`README.md`, `pyproject.toml`, `uv.lock`. Untracked: `docs/dev_log/day_16.md`,
`docs/development/day_16_fastapi_backend.md`, `src/rfq_copilot/api/`, `tests/test_api.py`,
and `day16_review_bundle.zip` (not created by this work, left untouched).
