# ADR-001: MCP deployment architecture

- Status: **Accepted**
- Context doc: [mcp_architecture.md](mcp_architecture.md)

## Context

The repository has a FastAPI REST app (`rfq_copilot.api.app.create_app`) whose security
lives in FastAPI dependencies (`api.auth.get_principal`, `require_route_permission`).
Those read `Request` and `app.state`. The deterministic services (`rfq_copilot.services.*`)
are transport-neutral `async` functions over a caller-owned `AsyncSession`. Agents arrive
and need read/evaluation tools only. The official Python SDK is `mcp` 2.x
(2.3.0 on PyPI at decision time), with `MCPServer`, Streamable HTTP and protocol `2026-07-28`.

## Options

| Criterion | A: mount in FastAPI app | B: separate services per server | C: hybrid (chosen) |
|---|---|---|---|
| Security boundary | Weak: shares port and process with REST; FastAPI deps do **not** apply to a mounted Starlette sub-app, so it is easy to believe it is protected when it is not | Strong | Strong: separate process and audience, shared verified core |
| Deployment complexity | Lowest | Highest (3 deployables) | Medium (1 extra deployable) |
| Code reuse | High | Low unless a shared library is extracted anyway | High: one package, one principal resolver |
| Auth integration | Must re-implement in middleware anyway | Per service | Once, through the SDK `TokenVerifier` hook |
| Horizontal scaling | Coupled to REST | Independent per server | Independent of REST; servers scale together |
| Runtime isolation | None (agent load hurts REST) | Full | REST vs MCP isolated |
| Observability | Mixed logs | Three streams | One MCP stream, tagged per server |
| Independent testing | Mixed | Yes | Yes (each server's ASGI app tested alone) |
| Latency | In-process | In-process per service | In-process (services called directly) |
| LangGraph fit | OK | Three endpoints to configure | One gateway, three paths |
| Operational load | Low | High | Medium |

## Decision

**Option C.** New package `rfq_copilot.mcp` in the same distribution:

```
src/rfq_copilot/mcp/
  settings.py      # McpSettings; refuses unsafe flag combinations
  security.py      # SDK TokenVerifier adapter -> shared resolve_principal()
  permissions.py   # tool -> permission, per policy version
  adapters/        # one module per server; snapshot_session + pinned rules + DTO mapping
  contracts/       # MCP contract Pydantic input/output models
  servers/         # manufacturing.py, supply.py, pricing.py: MCPServer instances
  gateway.py       # Starlette host app, lifespan enters each session_manager.run()
```

Run it as its own ASGI process (`uvicorn --factory rfq_copilot.mcp.host:create_app_from_env`), separate from the
REST app. Each server is mounted at `/mcp/<name>` with `streamable_http_path="/"`. The host
lifespan enters every `session_manager.run()` in an `AsyncExitStack`, as the SDK ASGI guide
requires, because mounted sub-app lifespans never run.

Supporting decisions:

- **D-1 SDK:** high-level `from mcp.server import MCPServer`. The v1 `mcp.server.fastmcp` /
  `FastMCP` import no longer exists in v2 and must not be used. The low-level `Server` is
  rejected: it skips argument validation and maps exceptions to `-32603`.
- **D-2 Transport:** Streamable HTTP, `2026-07-28`, sessionless; stdio for local inspection.
- **D-3 Legacy revisions:** not served initially. `stateless_http` applies only to the
  legacy leg, so it is irrelevant until we serve that leg.
- **D-4 Errors:** domain outcomes are structured results. Bad input is `-32602` /
  `MCPError`. Unexpected failure is a tool error with generic text.
- **D-5 Auth:** SDK `token_verifier` with `validate_token_resource=False`. The adapter
  reuses the REST JWT verifier and shared database identity checks, and verifies `aud`
  against the dedicated MCP audience. REST and MCP audiences remain distinct. See
  [resource scope](mcp_architecture.md#11-backend-mcp-client-and-rest-delegation).
- **D-6 Dependency:** `mcp>=2.3,<3` is declared and locked. Keep `uv.lock` committed,
  check it with `uv lock --check`, and review SDK upgrade notes before changing versions.

## Consequences

- Positive: REST security stays untouched. MCP gets a boundary that does not depend on
  FastAPI and is enforced inside each server. No business logic is duplicated.
- Negative: one more process to deploy and monitor. Shared principal
  resolution must keep REST behavior consistent (covered by the existing RBAC and JWT suites).
- Limitation: the three servers share one process, so a crash or overload hits all three.
  Splitting them later is a deployment change (same package, three entry points).

## Deployment decisions

- Configure the identity provider and dedicated MCP audience values.
- Retain the in-process gateway limits and apply reviewed proxy limits where needed.
- Serve legacy transports only if a real client requires them.
- Supply on-behalf-of user credentials through the provider interface. Tools must never
  substitute a universal privileged service identity for the verified user.
