# API foundation (Day 16): FastAPI application and backend setup

This document describes the code in `src/rfq_copilot/api/` as implemented. It exposes
**no business endpoint**: no RFQ submission, evaluation, pricing, quotation, approval or
release. It has no authentication and reads no database.

## 1. Purpose and place in the architecture

The deterministic services (`src/rfq_copilot/services/`) decide; later layers call them.
The FastAPI application is the future HTTP boundary for authenticated RFQ submission and
status, as planned in [the supervisor architecture](../architecture/supervisor_langgraph_architecture.md).
Day 16 builds only the parts every later endpoint needs: the application factory,
routing, response contracts, dependency injection, settings, lifespan, OpenAPI and tests.

```
 HTTP client ─► Uvicorn (ASGI server) ─► FastAPI app (create_app)
                                            ├─ /health/live          health.router   (no I/O)
                                            ├─ /api/v1/system/info   system.router   (app metadata)
                                            └─ /docs /redoc /openapi.json  (development only)

 Not connected: services/, repositories/, database.py, config.py, seeding/, data/
```

## 2. Module structure

```
src/rfq_copilot/api/
    __init__.py        package docstring only; importing it creates nothing
    app.py             create_app(), lifespan, OpenAPI metadata and tags
    config.py          ApiSettings: typed RFQ_API_* settings
    routers/
        __init__.py
        health.py      GET /health/live, Liveness response model
        system.py      GET /api/v1/system/info, SystemInfo model, get_system_info dependency
```

Deviations from the proposed layout: there is no `schemas.py` or `dependencies.py`. Each
response model and its dependency sit in the router module that owns the contract, which
keeps the two contracts next to the code that produces them. A shared module becomes
worthwhile when a model or dependency is used by more than one router.

## 3. Entry point and application factory

`create_app(settings: ApiSettings | None = None) -> FastAPI` builds a new, independent
application each time it is called:

- `settings=None` reads `RFQ_API_*` from the process environment. Tests pass an explicit
  `ApiSettings`.
- Metadata: title `Engineering RFQ Copilot API`; version from the installed package
  metadata (`importlib.metadata.version("engineering-rfq-copilot")`, currently `0.1.0`), so
  it cannot drift from `pyproject.toml`; `debug` stays `False`.
- It registers `health.router` at the root and `system.router` under `/api/v1`.

There is **no module-level `app`**. Uvicorn calls the factory:

```bash
RFQ_API_ENVIRONMENT=development uv run uvicorn --factory rfq_copilot.api.app:create_app --host 127.0.0.1 --port 8000
```

So importing `rfq_copilot.api.app` reads no environment, cannot fail on configuration and
creates no application. A test asserts that no database module (`sqlalchemy`,
`asyncpg`, `dotenv`, `rfq_copilot.database`, `rfq_copilot.config`, `rfq_copilot.seeding`)
is imported even after the application has served requests.

## 4. HTTP request lifecycle (`GET /api/v1/system/info`)

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

## 5. Routers

| Router | Prefix (after include) | Tag | Module |
| --- | --- | --- | --- |
| `health.router` | `/health` | `health` | `routers/health.py` |
| `system.router` | `/api/v1/system` | `system` | `routers/system.py` |

Liveness stays outside the versioned prefix because infrastructure probes target a
stable path that does not change with the API contract. Business endpoints will use
`/api/v1/...` routers, one module per tag.

## 6. Response contracts (Pydantic v2)

Both models are `frozen=True, extra="forbid"`:

- `Liveness`: `status: Literal["alive"]`.
- `SystemInfo`: `name: str`, `version: str`, `api_version: Literal["v1"]`.

Endpoints declare the model as their return type, so FastAPI uses it both as the
OpenAPI schema and as an output filter. Nothing is serialized from ORM objects or
domain entities, and no domain type is duplicated.

## 7. Dependency injection

`get_system_info` is a FastAPI dependency (`Annotated[SystemInfo, Depends(get_system_info)]`).
Tests replace it with `app.dependency_overrides[get_system_info] = ...`. Overrides are
per application instance (a test proves that a second instance is unaffected). They are
also how later endpoints will receive a caller-owned `AsyncSession` or a verified
identity.

There is deliberately **no** database-session dependency, no identity dependency and no
placeholder authentication: a dependency that silently grants access would later be
mistaken for a real one.

## 8. Configuration

`ApiSettings` (`api/config.py`) is a frozen Pydantic model:

| Variable | Values | Default | Effect |
| --- | --- | --- | --- |
| `RFQ_API_ENVIRONMENT` | `development`, `production` | `production` | `development` serves `/docs`, `/redoc`, `/openapi.json`; `production` returns 404 for all three |

- `ApiSettings.from_env(environ=None)` reads only variables that start with `RFQ_API_`,
  from the process environment or from an explicit mapping (tests).
- Invalid values (`staging`, empty, wrong case) and **unknown** `RFQ_API_*` variables
  raise `pydantic.ValidationError`, so a typo stops startup instead of being ignored.
- No dotenv file is read. The database configuration (`rfq_copilot/config.py`,
  `DATABASE_URL`, explicit `--env-file`) is untouched and is not imported by the API.
- Settings are never returned by an endpoint.
- `pydantic-settings` was not added: the API has one setting, and a short
  `from_env` keeps the existing explicit-mapping pattern of `DatabaseConfig.from_env`
  without adding a second dotenv-aware loader.

Secure defaults: docs off unless `development`, `debug=False`, no CORS middleware, no
custom exception handlers (standard FastAPI 404/405/422/500 semantics).

## 9. Lifespan

`lifespan(app)` is an `asynccontextmanager` passed to `FastAPI(lifespan=...)`. It logs
`API startup: <title> <version>` before serving and `API shutdown: <title> <version>`
after the last request on the `rfq_copilot.api.app` logger. The shutdown step sits in a
`try/finally` around `yield`, so it also runs when the context exits with an exception. There are no long-lived
resources yet, so it opens nothing: no engine or pool, no migration, no seeding, no rule
registration. Database resources will be added here, with cleanup in the `finally` block, when an
endpoint needs them. Uvicorn's default logging configuration does not show this
logger's `INFO` lines; tests check them with `caplog`.

## 10. Uvicorn startup and shutdown

Verified on 2026-11-10 from an empty working directory with no database variables (see
the [dev log](../dev_log/day_16.md)): startup, the four routes in both environments,
and graceful shutdown on SIGTERM ("Waiting for application shutdown. / Application
shutdown complete."). Always bind to `127.0.0.1` during development.

## 11. OpenAPI

In `development`, `/openapi.json` (OpenAPI 3.1) lists exactly the two routes, with tags
`health` and `system`, tag descriptions, and the `Liveness` and `SystemInfo` component
schemas. `/docs` (Swagger UI) and `/redoc` serve HTML. In `production`, the schema
is not served, but `app.openapi()` still generates it, which tests use to prove that no
other route is registered.

## 12. Tests

`tests/test_api.py` has 40 tests and needs no PostgreSQL, credentials, dataset or
network. It is part of the public smoke workflow. It covers settings defaults,
overrides, invalid values and immutability; factory metadata and instance isolation;
import side effects; lifespan logging over two start/stop cycles and shutdown after an exception inside the lifespan context; both endpoints
(status, content type, exact body, schema); 405 for mutating methods; 404 for unknown
paths; dependency overrides; contract violations giving an opaque 500; route registration;
OpenAPI content; docs in development; no docs in production; no environment value
in any response; and a fresh interpreter in an empty directory without database variables
that serves all routes and loads no database module.

## 13. Security boundaries today

- No endpoint reads customer, RFQ, quotation, rule or evaluation data.
- No endpoint mutates anything, and no write method is registered.
- No authentication exists, so nothing may be added that needs it until it does.
- Responses contain only the constant title, package version and API version.

## 14. Excluded from Day 16

Business endpoints, authentication and authorization, a database-backed readiness
check, DB-session dependencies, CORS, MCP, LangGraph, quote persistence, approval or
release execution, background tasks, WebSockets, Docker, and a frontend.

## 15. Integrating future endpoints safely

- Put each endpoint in a router under `/api/v1`, with an explicit Pydantic request and
  response contract, and never return ORM objects or service results directly.
- Derive customer identity from verified authentication in a dependency, never from a
  request field or header. Pass it as `customer_id` to the customer-scoped repositories.
- Create the `Database` in `lifespan` and yield one caller-owned `AsyncSession` per
  request through a dependency. Evaluations use `snapshot_session()` (read only,
  REPEATABLE READ) and explicit snapshot selection, as the services require.
- Load rules by pinned version (`approval-v1.2` for new evaluations) through the trusted
  manifest; never accept a rule version or document from the client.
- Keep calculation and approval separate: an endpoint may return
  `draft_creation_allowed`, but release always requires a human decision.
- Keep money values as `Decimal`, serialized as strings, never floats.
