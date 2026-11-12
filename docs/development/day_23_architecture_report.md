# Day 23: MCP fundamentals and architecture report (2026-11-11)

Design-only day. No runtime code, dependency, schema, migration, test or dataset change.
Related docs: [MCP architecture](../architecture/mcp_architecture.md),
[ADR-001](../architecture/mcp_architecture_decision.md),
[findings tracker](phase_04_findings_tracker.md).

## 1. Repository audit (verified in the local tree)

- **Package:** `src/rfq_copilot` (uv build backend, Python `>=3.12,<3.13`). Areas: `api/`
  (FastAPI), `services/` (deterministic engines), `repositories/` (queries, identity,
  idempotency, rule manifest, frozen results), `models/` (ORM), `seeding/`, admin CLIs.
- **App factory:** `api/app.py:create_app` with `lifespan`. Databases are built lazily.
  Read-only and writable session dependencies live in `api/db.py` (`get_read_session`,
  `get_write_session`).
- **Auth:** `api/tokens.py:TokenVerifier` (asymmetric only: RS256/ES256/EdDSA; one issuer
  and audience; `kid` rotation for 1–4 keys; max lifetime 900 s default).
  `api/auth.py:get_principal` resolves user → revocation (if enforced) → customer binding
  → granted permissions in one DB session. `require_route_permission` maps operations
  through `ROUTE_PERMISSIONS[policy]`.
- **Flags (`api/config.py:ApiSettings`, all default to the conservative Phase 3
  behaviour):** `environment="production"`, `permission_policy="v1"`,
  `rfq_assignments="off"`, `token_revocation="off"`, `customer_context="single"`,
  `upload_idempotency="off"`.
- **Provisioning CLIs:** `identity_admin.py`, `operator_admin.py`, `assignment_admin.py`,
  `reconcile_attachments.py` (dry run by default, audited).
- **Sessions:** `database.py:Database.session()` always rolls back on exit.
  `snapshot_session()` = `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`.
  Services never create sessions.
- **Rules:** `repositories/rule_manifest.py` pins versions with sha256.
  `queries.get_rule_version(..., expected_sha256=...)` raises `RuleIntegrityError`.
  `approval-v1.2` (`rules/approval-v1.2.json`, supersedes v1.1) is registered by
  `seeding/rules.py`.
- **Finding:** no code path in `src/` picks `approval-v1.2` automatically. Every
  evaluation caller passes `approval_rules`, and the "v1.2 for new evaluations" rule exists
  only in docs (Day 16/17/21). The MCP adapter must pin it server-side (Day 24 contract
  item).
- **REST does not call the evaluation services yet.** `api/routers/*` import no
  `services.*`, so MCP will be their first networked caller.
- **Fingerprints:** `services/evaluation.py:evaluation_fingerprint`. Human release:
  `require_human_release`.
- **Debt register:** `docs/development/technical_debt.md` (TD-001, TD-006, TD-007 open;
  TD-015/017/022/023/024 blocked or pending).
- **Docs conventions:** `docs/architecture/`, `docs/development/day_NN_*.md`,
  `docs/dev_log/day_NN.md` (append-only).
- **Tests:** `tests/`, `pytest -q`. DB tests use the guarded `*_test` DB. Baseline at the end
  of Day 22: 1839 passed.

## 2. Service-to-MCP mapping

"Scope": C = the customer comes from the principal; RFQ = ownership is checked through
`get_rfq(..., customer_id=...)`; G = global catalog data (still authenticated). All entries
are read-only, run in one `snapshot_session()`, and depend on pinned manufacturing rules
unless noted.

| Python entry point | Server | Consumer (future) | Scope | R/W | Rule / snapshot deps | Phase 4 safe? |
|---|---|---|---|---|---|---|
| `repositories.queries.find_materials`, `list_materials` | Manufacturing | Material/engineering agent | G | R | none | Yes |
| `services.materials.get_material_spec` | Manufacturing | Material agent | G | R | none | Yes |
| `services.materials.check_part_material(session, part, rules)` | Manufacturing | Material agent | G (part from args or RFQ) | R | manufacturing | Yes |
| `services.manufacturing.check_manufacturing_capability(session, part, rules)` | Manufacturing | Manufacturing agent | G / RFQ | R | manufacturing | Yes |
| `services.inventory.check_inventory(session, parts, *, manufacturing_rules, pricing_rules, snapshot)` | Supply | Supply agent | G / RFQ | R | mfg + pricing; inventory snapshot | Yes (server picks snapshot) |
| `services.capacity.check_capacity(session, parts, inventory, *, ..., horizon_start, snapshot)` | Supply | Supply agent | RFQ | R | mfg + pricing; capacity snapshot | Yes, if inventory is recomputed server-side |
| `services.lead_time.select_snapshots` / `estimate_lead_time` | Supply | Supply agent | RFQ | R | snapshots, horizon | Yes |
| `services.costing.estimate_costs(session, parts, inventory, *, ...)` | Pricing | Pricing agent | RFQ | R | mfg + pricing | Yes, if inventory is recomputed |
| `services.pricing.calculate_quote(session, parts, costs, *, pricing_rules)` | Pricing | Pricing agent | RFQ | R | pricing | Yes, if costs are recomputed |
| `services.quotation.quote_parts` / `quote_rfq` | Pricing | Pricing agent, Supervisor | RFQ | R | mfg + pricing + snapshots | Yes |
| `services.evaluation.evaluate_stored_rfq(session, rfq_id, *, customer_id, ..., approval_rules, ...)` | Pricing (commercial policy) | Supervisor / commercial agent | C + RFQ (returns None if not the customer's) | R | mfg + pricing + **approval-v1.2** + snapshots | Yes, with a dedicated permission |
| `queries.get_customer` (credit/status) | not exposed alone | — | C | R | — | No: only inside evaluation |
| `queries.list_quotation_history`, `get_released_quotation`, `list_pending_approvals` | not exposed in Phase 4 | — | C | R | — | Defer; approval queue is Phase 8 |
| `*_admin` CLIs, seeding, migrations, `reconcile_attachments`, uploads | never | — | — | W | — | **No** |

## 3. MCP technology verification (official sources, 2026-11-11)

Sources: py.sdk.modelcontextprotocol.io (home, migration, run/asgi, run/authorization,
run/deploy, protocol-versions, handling-errors, structured-output, low-level-server), the
GitHub README, and modelcontextprotocol.io/specification/2026-07-28 (basic, versioning,
authorization, streamable-http, server/discover). PyPI JSON: `mcp` **2.3.0** latest
(2.0.1, 2.1.x, 2.2.0 before it). Requires Python ≥3.10 per PyPI metadata (docs say 3.11+).
Both fit our 3.12.

| # | Topic | Verified fact | Decision |
|---|---|---|---|
| 1 | SDK v2 | `mcp` 2.x is current. v1 users are told to pin `mcp<2` | Use `mcp>=2.3,<3` |
| 2 | `MCPServer` vs `FastMCP` | "`FastMCP` has been renamed to `MCPServer`". `mcp.server.fastmcp` raises `ModuleNotFoundError` | `from mcp.server import MCPServer`; never FastMCP |
| 3 | High vs low level | Low-level `Server(name, on_call_tool=...)` skips argument validation and maps handler exceptions to `-32603` | High-level |
| 4 | Streamable HTTP | `mcp.streamable_http_app()` returns a Starlette app, endpoint `/mcp`, `streamable_http_path` configurable | Use it |
| 5 | stdio | Supported. Spec: HTTP authorization SHOULD NOT apply to stdio | Local inspection only |
| 6 | JSON-RPC | Handled by the SDK | No custom parser |
| 7 | `server/discover` | Spec: servers MUST implement it. Clients probe with it, then fall back to `initialize` on legacy servers | SDK provides it |
| 8 | Stateless / sessions | 2026-07-28 removes protocol sessions and the GET stream. `Mcp-Session-Id` is never set. Per-request `_meta` protocol version + `MCP-Protocol-Version` header (mismatch → 400 `-32020`). `stateless_http` affects only the legacy leg | Serve modern only (ADR D-3) |
| 9 | Authorization | `token_verifier=` (`mcp.server.auth.provider.TokenVerifier`, `verify_token(token) -> AccessToken \| None`) + `auth=AuthSettings(issuer_url, resource_server_url, required_scopes, validate_token_resource)`. Must be passed together. RFC 9728 metadata served; 401 with `WWW-Authenticate resource_metadata=`. `get_access_token()` in handlers. Spec: audience validation per RFC 8707 MUST; "MUST NOT accept or transit any other tokens" | Adapter over our verifier; `validate_token_resource=True` |
| 10 | ASGI / FastAPI | Mount the Starlette app. A mounted app's lifespan never runs: the host lifespan must enter `mcp.session_manager.run()` (per server, `AsyncExitStack`). `session_manager` exists only after `streamable_http_app()` is called | Separate Starlette host process |
| 11 | Lifecycle | Without `session_manager.run()`: `RuntimeError: Task group is not initialized` | Day 28 lifespan test |
| 12 | Errors | `ToolError` (`mcp.server.mcpserver.exceptions`) → `is_error=True`. `MCPError(code=INVALID_PARAMS, ...)` → JSON-RPC error. Other exceptions → generic tool error, traceback logged only. Structured output from the return annotation, validated | Domain outcomes = structured success |
| — | Host/Origin | `TransportSecuritySettings(allowed_hosts, allowed_origins, enable_dns_rebinding_protection)`. Default allowlist is loopback only. 421 bad Host, 403 bad Origin | Keep the default until Day 28; explicit allowlist in deployment |

Not read on Day 23: the spec's stdio and security-considerations pages, and the rest of the
migration guide. Read them before Day 27.

### Dependency strategy

Add `mcp>=2.3,<3` in `pyproject.toml` on Day 24, the first day with code, after approval.
Run `uv lock`, commit `uv.lock`, and gate on `uv lock --check`. Do not add `mcp[cli]` (dev
tooling only). Before any minor bump, read the migration notes: 2.x is still changing
defaults (e.g. `validate_token_resource` → default in 3.0). No dependency was added on Day 23.

## 4. Decisions

See the ADR. Option C (separate MCP ASGI process, shared core, three logical servers);
high-level `MCPServer`; Streamable HTTP 2026-07-28; SDK auth hook over our verifier;
loopback-only until the F1/F2/F4 controls are verified.

## 5. Current limitations

- Principal resolution is coupled to FastAPI. It needs extraction (Day 27) with REST
  behaviour unchanged.
- The `approval-v1.2` default for new evaluations is a documented rule, not code. The MCP
  layer must pin it.
- Composite service inputs (`InventoryCheck`, `CostEstimate`) cannot be taken from agents.
  Tools must recompute them.
- Agent identity model undecided (Phase 5).

## 6. Day 24 handoff (MCP tool contracts). Not started

- **Structure:** `src/rfq_copilot/mcp/{contracts,adapters,servers}`, `settings.py`,
  `security.py`, `permissions.py`, `gateway.py` (ADR-001). Day 24 touches `contracts/` only.
- **Candidate tools:** manufacturing: `search_materials`, `get_material`,
  `check_material_suitability`, `check_manufacturing_feasibility`. Supply:
  `check_inventory`, `check_capacity`, `estimate_lead_time`. Pricing: `estimate_costs`,
  `calculate_price`, `evaluate_quotation`, `evaluate_rfq_commercial_policy`. Each maps to
  §2.
- **Auth per category:** catalog → `view_catalog`. RFQ-scoped feasibility, supply and
  pricing → `view_assigned_rfqs` + RFQ ownership (+ assignment when required). Commercial
  policy → a dedicated permission. That needs a new grant, which is a policy-overlay change
  requiring approval: flag it, do not add it.
- **Schemas:** Pydantic v2 strict models, bounded strings and lists, `Decimal` serialized
  as string, no customer or role fields in inputs, enums for part types from the rules.
- **Outputs:** structured results carrying rule versions, snapshot ids, fingerprint and
  evidence. Errors: `-32602` for invalid input, generic tool error for internal failures,
  not-found indistinguishable from not-yours.
- **Compatibility:** `mcp>=2.3,<3`, protocol `2026-07-28` only, Python 3.12.
- **Tests before any exposure:** contract schema snapshot; parity of tool output with
  direct service calls on dev/validation scenarios (never frozen data); permission-matrix
  coverage; cross-customer denial; no write possible (READ ONLY transaction).
- **Open until implemented:** tool granularity, the commercial-policy permission name,
  the rate-limit mechanism, legacy-revision support.
