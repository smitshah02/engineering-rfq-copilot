# API foundation: FastAPI application and backend setup

This document describes the shared FastAPI application foundation and its public liveness
and metadata routes. Protected reads, attachments, JWT authentication and RBAC are
documented separately; human approval and release run in a separate decision application.

## 1. Purpose and place in the architecture

The deterministic services (`src/rfq_copilot/services/`) decide; API adapters call them.
The FastAPI application supplies the HTTP boundary; RFQ creation/correction and
Supervisor job APIs remain integration work. See [the supervisor architecture](../architecture/supervisor_langgraph_architecture.md).
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
        ...            protected customer, RFQ, catalog, quotation and attachment routers
    auth.py            JWT verification and database-derived principal
    db.py              restricted application database/session dependencies
    schemas/           API contracts and exact-number types
    decision_app.py    separate human-decision application
```

Health and system response models stay beside their routers. Shared business contracts
live in `schemas/`; identity and session dependencies live in `auth.py` and `db.py`.

## 3. Entry point and application factory

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
stable path that does not change with the API contract. Business endpoints use
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
also how protected endpoints receive a caller-owned `AsyncSession` or a verified
identity.

Protected routes use verified-identity and restricted-session dependencies. Missing trust
configuration or database authority fails closed; no placeholder authenticator grants access.

## 8. Configuration

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
  policies have separate typed settings; see [authentication](day_20_authentication.md).
- Production defaults disable docs and debugging. Sanitized validation and error handlers
  prevent sensitive input and internal exception details from appearing in responses.

## 9. Lifespan

`lifespan(app)` is an `asynccontextmanager` passed to `FastAPI(lifespan=...)`. It logs
`API startup: <title> <version>` before serving and `API shutdown: <title> <version>`
after the last request on the `rfq_copilot.api.app` logger. The shutdown step sits in a
`try/finally` around `yield`, including exceptional shutdown. Application-owned lazy database
resources are disposed at shutdown; injected resources remain caller-owned. Startup does
not run migrations, seed data or register rules. Uvicorn's default logging configuration
may omit application `INFO` lines; tests inspect lifespan logs explicitly.

## 10. Uvicorn startup and shutdown

Use the factory command above and bind to `127.0.0.1` during local development. Public
liveness and metadata do not require database credentials; protected operations do.

## 11. OpenAPI

When documentation is enabled, `/openapi.json` describes the registered routers and their
response schemas; `/docs` and `/redoc` serve interactive documentation. Production defaults
hide these endpoints. `app.openapi()` remains available for offline contract inspection.

## 12. Tests

`tests/test_api.py` covers this foundation and needs no PostgreSQL, credentials, dataset or
network. It is part of the public smoke workflow. It covers settings defaults,
overrides, invalid values and immutability; factory metadata and instance isolation;
import side effects; lifespan logging over two start/stop cycles and shutdown after an exception inside the lifespan context; both endpoints
(status, content type, exact body, schema); 405 for mutating methods; 404 for unknown
paths; dependency overrides; contract violations giving an opaque 500; route registration;
OpenAPI content; docs in development; no docs in production; no environment value
in any response; and a fresh interpreter in an empty directory without database variables
that serves the public foundation routes without loading database infrastructure.

## 13. Public-route security boundaries

- Liveness and metadata routes expose only fixed health and package metadata.
- Protected routes derive identity, customer binding and grants from verified authentication.
- Ordinary API handlers cannot use decision authority; human decisions have a separate app.
- No model, request body or browser-supplied state grants approval or release authority.

## 14. Related API components

See [request validation](day_17_pydantic_api_validation.md),
[protected reads](day_18_database_rest_apis.md),
[attachments](day_19_file_uploads_extraction.md),
[authentication](day_20_authentication.md), and
[authorization](day_21_rbac_authorization.md). The [project overview](../../README.md)
distinguishes implemented backend services from remaining browser integration.

## 15. Adding endpoints safely

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
