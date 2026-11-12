# Phase 4 final verification report (Days 23–30)

Date: 2026-11-11. Scope: MCP architecture, contracts, three logical servers, authentication,
gateway, client and REST integration. Evidence: the Day 30 run recorded in
`docs/dev_log/day_30.md`; findings in `phase_04_findings_tracker.md`.

**Verdict: PASS WITH CONDITIONS.** The system is ready for Phase 5 development. It is not
ready for production; the conditions are §5 and §6.

## 1. Implemented and verified

| Area | Evidence (Day 30 run unless noted) |
|---|---|
| Ten read-only tools, Pydantic contracts, exact Decimal strings | `test_mcp_contracts.py`, `test_mcp_manufacturing.py`, `test_mcp_supply.py`, `test_mcp_pricing.py` (full suite) |
| Three endpoints through one gateway, Host/Origin, modern-only protocol, size/rate/concurrency/timeout | `test_mcp_host.py`; live smoke `test_live_uvicorn_host_smoke` |
| Independent MCP authentication (signature, `iss`, MCP `aud`, exp/nbf/iat, revocation, active user, binding) | `test_mcp_auth.py`; live: six invalid credential kinds → `unauthenticated` on all three servers |
| RBAC v2 from the database, per tool | `test_every_tool_enforces_its_declared_v2_permission`; live: restricted and operator identities, per server |
| Stored-RFQ customer isolation and assignments | `test_mcp_auth.py`; live: customer B and forged-claim tokens → `not_found`, 8 concurrent calls |
| REST → MCP delegation without token passthrough | `test_mcp_client.py`; live: REST token never in the issued set |
| **Authenticated live end to end** | `test_authenticated_live_end_to_end`: REST (hardened, real `Database`) → test provider → TCP → separate uvicorn MCP host → tools → PostgreSQL; quote equals `quote_rfq` exactly |
| Deterministic results unchanged | Day 6–15 service, parity and evaluation suites in the full run; live quote parity |
| Hardened REST profile | `test_mcp_host.py` (configuration); `test_hardened_profile_enforces_revocation_and_upload_idempotency`; `test_hardened_profile_never_mounts_rest_to_mcp_delegation` |

Day 30 corrections (tracker D30-1, D30-2):
- `repositories.find_active_user` (behind the shared `api.auth.resolve_identity`) now returns the user detached. Before this, every
  authenticated request through a real `Database` failed (REST 500; MCP `unavailable`).
- The MCP verifier reports the audience it actually verified, not a URL copied from its
  own settings. The binding is the shared MCP audience (§3).

## 2. Local-development security guarantees (verified)

- REST and MCP tokens are separate audiences, and each side rejects the other's tokens.
- MCP re-resolves user, revocation, binding and v2 grants from PostgreSQL on every
  request. Token claims for role, customer, permissions or scope have no effect (live, forged-claim test).
- Unauthenticated or unauthorized calls open no business snapshot (`test_rejected_credentials_never_open_a_snapshot`, `test_every_tool_requires_a_principal_before_any_snapshot`).
- Each evaluation runs in one REPEATABLE READ, READ ONLY transaction on a read-only engine (Day 26/28 tests; the live host runs `Database(read_only=True)`).
- Errors are fixed texts and codes. The live host log contained no token, no configured secret and no traceback.

## 3. Resource-binding model (Day 30 audit)

The three endpoints form one protected resource. A token is accepted when its `aud`
exactly equals `RFQ_MCP_JWT_AUDIENCE`; the same token works on all three endpoints. This is
safe because authorization is per tool, from database grants. The token carries no
authority of its own. The RFC 9728 metadata still names each endpoint URL as its
`resource`, so the issuer must:
- mint `aud = RFQ_MCP_JWT_AUDIENCE` for every MCP endpoint (a per-endpoint RFC 8707 `aud`
  is rejected);
- sign with the configured algorithm and key (`kid` from the configured set);
- keep `exp − iat` ≤ `RFQ_MCP_JWT_MAX_LIFETIME_SECONDS`;
- mint MCP tokens only for the authenticated subject, through a token exchange, never by
  copying a REST token.

Per-endpoint restriction (for example, a pricing-only agent token) needs issuer-side
per-resource audiences plus a verifier change. It is not required by any current design document.

## 4. Test-only demonstration boundaries

- The live test signs tokens with a throwaway key in a pytest temp directory
  (`TestIssuer`, `trust.mint`). No signing key is in any application setting or repository file.
- The REST app runs in process (ASGI). The MCP host is a separate OS process. REST was
  not run under its own uvicorn process.
- Live data is committed only to `rfq_copilot_test` for the duration of the test, then
  truncated. The guarded fixture downgrades to base and asserts no objects remain;
  a read-only check after the run showed 0 relations.
- The demonstration route mounts only with `create_app(mcp=...)`. Production startup,
  including `hardened`, never mounts it.

## 5. Production deployment prerequisites

| Item | State | Owner / closure evidence |
|---|---|---|
| Token delegation (IdP token exchange → `McpCredentialProvider`) | **BLOCKED**: no approved IdP or exchange | Identity/deployment owner. Evidence: a provider integration test with the real IdP that rejects passthrough and other subjects |
| `rfq_mcp_reader` role | Documented, reviewed (below), not created | DBA. Evidence: allowed/denied privilege test on an isolated DB, then the host connecting with it |
| TLS | Required by configuration for non-loopback; not deployed | Deployment owner. Evidence below |
| Activation | REST `hardened` and the MCP host not deployed | Deployment owner. Evidence: deployed settings plus smoke checks against the deployed URLs |

**Reader role review** (`mcp_architecture.md` §10 SQL):
- Correct: no superuser/createdb/createrole/replication/bypassrls; only `CONNECT`, `USAGE` on
  `business` and `SELECT`; no INSERT/UPDATE/DELETE/TRUNCATE or DDL privileges. The host also
  refuses privileged roles (`Database.verify_connection`).
- `default_transaction_read_only` is a session default a client can override. It is not
  the boundary; the missing write privileges are.
- No `ALTER DEFAULT PRIVILEGES`: tables added by later migrations stay unreadable until
  `GRANT SELECT` is re-run. This fails closed; make the re-grant a post-migration deploy step.
- PostgreSQL 18 (verified): PUBLIC has no CREATE on `public`. PUBLIC still has TEMP and CONNECT by default; revoking them is a DBA, cluster-wide decision.
- `SELECT ON ALL TABLES` also exposes tables MCP does not read (`audit_events`,
  `idempotency_records`, `rfq_attachments`). Optional narrowing: `REVOKE SELECT` on those.
- There is no row-level security. Customer isolation lives in the application
  (`get_rfq(customer_id=...)`). The role limits a compromised MCP process to reads, but
  those reads cover every customer.

**TLS requirements:** terminate HTTPS at a reverse proxy, with the host bound to loopback
or a private interface. Configure `RFQ_MCP_HOST_PUBLIC_BASE_URL=https://…`,
`RFQ_MCP_HOST_TLS_TERMINATING_PROXY=true` and `RFQ_MCP_HOST_TRUSTED_PROXIES=<proxy IP>`
(startup refuses otherwise). Run uvicorn with `--no-proxy-headers` (the gateway reads
`X-Forwarded-For` only from trusted proxies). Deployment checks: plain HTTP refused or
redirected at the proxy, a valid certificate chain for the public host, a direct
connection to the host port unreachable from outside, and a 421 for a foreign Host
through the proxy. Localhost HTTP tests prove none of this.

## 6. Unresolved blockers and open items

- BLOCKED: production credential provider (no approved IdP token exchange).
  Production REST→MCP stays disabled (`create_app(mcp=None)`).
- Awaiting approval: development demo identities (F3); `rfq_mcp_reader` creation and test.
- OPEN: D30-3 (`httpx2` runtime import declared only as dev dependency).
- Limits are per process (in-memory rate/concurrency); multiple workers need a shared limiter.

## 7. Future-phase dependencies

- Phase 5 (LangGraph): agent identity model (on behalf of a user versus a service
  identity) determines the credential provider. Agents must use `McpToolClient` and never
  hold signing keys.
- Phase 8: quotation persistence and human approval/release stay out of MCP (no such tools exist).

## 8. Acceptance matrix

| # | Requirement | Result |
|---|---|---|
| 1 | Ten tools functional and contract-compliant | MET |
| 2 | Three authenticated endpoints through the gateway | MET (live) |
| 3 | Trusted identities, DB-derived permissions | MET (live) |
| 4 | Stored-RFQ customer isolation | MET (live) |
| 5 | Authenticated live end-to-end verification | MET for development (separate MCP host process, guarded test DB); production path BLOCKED on IdP |
| 6 | Invalid requests fail closed, safe errors | MET |
| 7 | Deterministic calculations unchanged | MET |
| 8 | Regression suites pass | MET: full suite 2263 passed, 0 failed, 0 skipped; Ruff clean (`day_30.md`) |
| 9 | F1–F5 defensible statuses | MET (F3 pending approval) |
| 10 | Production limitations documented | MET (§5, §6) |
