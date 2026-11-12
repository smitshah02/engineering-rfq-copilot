# Phase 4 findings tracker

Updated after every Phase 4 day (Days 23–30). Statuses: OPEN, IN PROGRESS, FIXED, MITIGATED,
DEFERRED WITH JUSTIFICATION, BLOCKED. FIXED needs verification evidence (a test or command
result). A plan or a document alone is not enough.

## Summary

| ID | Title | Severity | Status | Target | Last updated |
|---|---|---|---|---|---|
| F1 | RBAC v2 exists but v1 is the default | High | MITIGATED (hardened-profile requirement verified Day 30; production REST activation OPEN) | Day 27; verify Days 28–30 | Day 30 |
| F2 | Token revocation and upload idempotency default to off | High | MITIGATED (both enforced and verified under the hardened profile; MCP revocation verified live; production activation OPEN) | Days 27–28; verify Day 30 | Day 30 |
| F3 | Customer bindings and operator accounts need provisioning | Medium | IN PROGRESS (process-level synthetic identities verified on the guarded test DB; development provisioning awaits approval) | Day 27; verify Days 29–30 | Day 30 |
| F4 | MCP needs an independent secure authorization boundary | Critical | IN PROGRESS (development boundary verified live, end to end; production delegation, TLS, reader role and deployment open) | Design Day 23; build and verify Days 27–30 | Day 30 |
| F5 | Phase 3 docs put quotation persistence in Phase 4 | Low | FIXED (no regression, Day 30 grep) | Day 23 | Day 30 |

## F1: RBAC v2 exists but v1 is the default

- **Evidence:** `src/rfq_copilot/api/config.py`: `permission_policy: Literal["v1", "v2"] = "v1"`.
  Under v1, catalog, quotes and uploads require `view_assigned_rfqs` / `create_rfq`. v2 uses
  `view_catalog` / `view_quote` / `attach_rfq_document` (rbac-policy-v2 overlay, applied on
  development per the Day 22 integrity run).
- **Required changes:** MCP settings accept only v2 and refuse to start otherwise. Add a
  tool → permission table for v2 (no v1 table for MCP). The REST default is a deployment
  decision and stays unchanged unless approved.
- **Regression tests:** MCP startup fails with v1. Every registered tool has a v2 permission
  (coverage test like `test_rbac.py` MATRIX). Missing permission → denied for each tool.
  Existing REST v1/v2 tests stay green.
- **Remaining risk:** REST deployments that keep v1 remain coarser than MCP.
- **Dependencies:** overlay applied on the target DB; F3 grants.
- **Verification evidence:** none yet.
- **Day 24:** every tool contract names a v2 permission (`view_catalog`, `view_assigned_rfqs`,
  `view_quote`); `test_inventory_is_complete_unique_and_read_only` checks they are v2 route
  permissions. Runtime enforcement not built: still OPEN.

## F2: Token revocation and upload idempotency default to off

- **Evidence:** `api/config.py`: `token_revocation: Literal["off","enforced"] = "off"`,
  `upload_idempotency: Literal["off","enabled"] = "off"`. `api/auth.py:get_principal` checks
  `repositories.token_revoked` only when enforced; unknown status → 503.
- **Required changes:** MCP requires `token_revocation="enforced"` (refuses to start
  otherwise). The MCP verifier reuses `token_revoked`. Upload idempotency does not apply
  to MCP, because MCP exposes no uploads. Record it here as REST-only and decide its
  production default on Day 28.
- **Regression tests:** revoked token → 401 on an MCP tool. Revocation lookup failure →
  503, never allow. Startup refusal when off.
- **Remaining risk:** revocation before `exp` depends on populating `token_revocations`
  (TD-024, IdP choice).
- **Dependencies:** revision `20261111_03` on the target DB; F4 verifier.
- **Verification evidence:** none yet.

## F3: Customer bindings and operator accounts need provisioning

- **Evidence:** the Day 22 integrity report shows empty binding, assignment and revocation
  tables on development. `identity_admin.py`, `operator_admin.py` and `assignment_admin.py`
  exist (dry run by default, audited). `get_principal` returns 403 when a user has no binding.
- **Required changes:** a provisioning runbook for demo identities on the **test** DB only
  (fixtures), and a documented operator procedure for development. No production
  identities or credentials.
- **Regression tests:** an MCP call with no binding → 403. A cross-customer RFQ id →
  not found (no existence leak). An operator without a grant → denied.
- **Remaining risk:** demos depend on manual provisioning.
- **Dependencies:** F4 resolver.
- **Verification evidence:** none yet.

## F4: MCP needs an independent secure authorization boundary

- **Evidence:** `api/auth.py:get_principal(request: Request, ...)` reads
  `request.app.state.token_verifier`, `request.app.state.settings` and `get_database(request)`.
  It is a FastAPI dependency and does not run for a mounted Starlette/MCP app or a separate
  process. `api.tokens.TokenVerifier.verify` and the repository checks are reusable.
- **Required changes (Days 27–28):** extract a transport-neutral `resolve_principal()` used
  by REST and MCP. Build an SDK `TokenVerifier` adapter with a distinct MCP audience and
  `validate_token_resource=True`. Add per-tool permission checks inside each server. Take
  the customer only from the principal. Add Host/Origin allowlist, size, timeout and rate
  limits, and a read-only DB role. Bind to loopback only until all of this exists.
- **Regression tests:** no token / bad signature / wrong `iss` / wrong `aud` (REST audience
  on MCP) / expired / revoked → 401. No binding → 403. Missing permission → denied.
  Customer id in arguments is ignored. Direct per-server path bypassing the gateway →
  still 401. Bad Host → 421, bad Origin → 403. Oversize → 413. The existing REST auth
  suite stays green after the extraction.
- **Remaining risk:** agent identity model (on-behalf-of vs service identity) undecided
  until Phase 5.
- **Dependencies:** F1, F2, F3; `mcp` dependency approval (Day 24).
- **Verification evidence:** Day 23 design in `docs/architecture/mcp_architecture.md` §6–7
  and the ADR. No implementation yet, so not FIXED.
- **Day 24 (contract level only):** `mcp/contracts.py` inputs forbid identity, role,
  permission, customer, upstream-result, rule, snapshot and approval fields
  (`extra="forbid"`, enforced by the SDK before a tool body runs). There are no
  approve/release/persist tools. Errors use fixed texts, and `NOT_FOUND` covers both
  "absent" and "not visible". Evidence: `tests/test_mcp_contracts.py`, 142 passed.
  Authentication, per-tool authorization, transport limits and protocol-version
  restriction are **not implemented**: still IN PROGRESS.
- **Day 25:** the Manufacturing server (`mcp/manufacturing.py`) is in-process only, with no
  listener and no auth bypass. `sanitize_tool_errors` middleware allow-lists error texts
  and refuses unknown arguments. A fake secret placed in values, keys and payloads is never
  echoed. Evidence: `tests/test_mcp_manufacturing.py`, 43 passed.
- **Still missing for Day 27:** bearer verification, `resolve_principal()`, and a
  per-tool `ToolContract.permission` check inside each handler (catalog tools →
  `view_catalog`). Catalog data is global (no customer rows; no cost or supply
  quantities), but it must still not be reachable unauthenticated over a network.
- **Day 26:** the Supply and Pricing servers are done.
  - Stored `rfq_id` requests fail closed (`UNAUTHENTICATED`, no session opened, no lookup).
  - A generic `ValueError` maps to `INTERNAL`.
  - The production snapshot was verified on the guarded test DB: one
    `repeatable read`, `read only` transaction per tool call.
  - Evidence: `tests/test_mcp_supply.py`, `tests/test_mcp_pricing.py`, 100 passed.
- **Day 27 must add:** verified principal injection into `runtime.prospective_parts`
  (principal-scoped RFQ loading via `quote_rfq` / `get_rfq_parts`), per-tool permission
  checks (`view_assigned_rfqs` for supply, `view_quote` for pricing), and the
  `rfq_assignments` check when required.

## F5: Phase 3 docs put quotation persistence in Phase 4

- **Evidence (original):** `docs/development/phase_3_final_backend_report.md` line 129–130:
  "TD-007 concerns quote persistence (Phase 4 scope)."
- **Changes made (Day 23):** appended a dated erratum to that historical report (original
  text preserved). Appended a Day 23 note to `technical_debt.md` mapping TD-007's
  "quote-persistence phase" to Phase 8.
- **Audit:** `grep -rniE "phase ?4|phase four" docs README.md` → the only hits outside the
  new MCP docs are the preserved line 130 and the "Phase 4 handoff" heading. The heading is
  correct: it concerns MCP/agents and says approval/release stay human-only. README lists
  persistence and approval as "Planned (not yet built)" without assigning a phase, which
  is correct. No other phase-to-persistence misassignment was found.
- **Remaining occurrences:** one, intentionally preserved and annotated.
- **Regression tests:** none (documentation).
- **Verification evidence:** grep result above, run on Day 23.

## Day 27 update (2026-11-11)

**F1: MITIGATED.**
- MCP has no v1 path: `McpAuthSettings.permission_policy` is `Literal["v2"]`, and
  `runtime._authorized` checks `ToolContract.permission` (v2 names) against grants read
  from `business.role_permissions` on every request.
- Evidence (`tests/test_mcp_auth.py`): `test_every_tool_enforces_its_declared_v2_permission`
  (10 tools), `test_v1_grants_never_substitute_for_v2_permissions`,
  `test_mcp_settings_fail_closed[v1 refused]`, and
  `test_database_roles_decide_and_operators_get_no_business_tools`.
- Remaining: the REST default stays `v1`, a deployment decision; switching REST to v2 is
  not done. Verify the deployed MCP host on Days 28–30.

**F2: IN PROGRESS.**
- MCP revocation is always enforced (`token_revocation: Literal["enforced"]`; "off" is
  refused). A revoked token gets 401 before any tool runs (`test_bearer_token_checks[revoked]`).
  A mutation that disabled it was caught.
- REST `token_revocation` and `upload_idempotency` still default to `off`. MCP tools are
  read-only and accept no uploads, so idempotency is not exercised by MCP.
- The enabled upload-idempotency behaviour is verified by the existing
  `test_phase35_completion_database.py` (passed in the Day 27 run).
- Remaining: the deployment configuration (`RFQ_API_TOKEN_REVOCATION=enforced`,
  `RFQ_API_UPLOAD_IDEMPOTENCY=enabled`) is not applied or verified. Day 28/30.

**F3: IN PROGRESS.**
- Synthetic identities exist only inside the seeded, rolled-back test transaction: two
  bound customers, a reduced-permission role (`manufacturing_engineer`), an
  `identity_operator`, and inactive, unbound, revoked and unknown subjects.
- Evidence: `test_bearer_token_checks`, `test_stored_rfq_is_customer_scoped_and_matches_quote_rfq`,
  `test_assignment_enforcement_for_stored_rfqs`.
- Remaining: provisioning real operators and bindings with the existing CLIs
  (deployment), plus integration on Days 29–30.

**F4: IN PROGRESS.** An MCP-native boundary is implemented:
- `mcp/security.py`: `McpTokenVerifier`, an SDK `TokenVerifier` over the existing
  `api.tokens.TokenVerifier`, with a dedicated MCP audience, so REST tokens fail.
- `api.auth.resolve_identity`: extracted unchanged from `get_principal` and shared by
  REST and MCP.
- A request-scoped `McpPrincipal` (an SDK `AccessToken`) reached via `get_access_token()`.
- One enforcement point in `runtime.build_server`.
- Stored RFQs are loaded with `get_rfq(customer_id=principal.customer_id,
  assigned_user=...)` inside the business snapshot. Absent, foreign and unassigned RFQs
  give an identical `NOT_FOUND`.
- Evidence: 50 tests in `test_mcp_auth.py`, including the real bearer path over the SDK
  ASGI app (in-memory transport, no listener), 16 concurrent identities with no leakage,
  and a mutation removing the permission check (18 failures).
- Remaining (Day 28): the host process and TLS, the Host/Origin allowlist beyond the
  loopback default, request-size, timeout and rate limits, a read-only DB role,
  protocol-version policy, and wiring `Database.snapshot_session` plus an identity
  session in the host.

## Day 28 update (2026-11-11)

**F1: MITIGATED (unchanged status).**
- The host accepts only MCP auth settings with `permission_policy="v2"` and checks this
  again at construction. MCP has no v1 table.
- The REST profile `RFQ_API_DEPLOYMENT_PROFILE=hardened` refuses to start unless
  `environment=production`, `permission_policy=v2`, `token_revocation=enforced`,
  `upload_idempotency=enabled` and JWT settings are all present. `standard` keeps the
  approved defaults.
- Evidence (`tests/test_mcp_host.py`): `test_rest_hardened_profile_refuses_insecure_settings`
  (6 cases), `test_rest_hardened_profile_requires_jwt`, and
  `test_mcp_auth_cannot_be_weakened_for_the_host`.
- Day 29–30 action: deploy REST with `hardened`, then verify.

**F2: IN PROGRESS.**
- Host: revoked, expired, REST-audience, bad-signature and missing tokens get 401 on all
  three endpoints, and no business snapshot opens (`test_rejected_credentials_never_open_a_snapshot`).
- REST: the `hardened` profile requires revocation and idempotency. Enabled idempotency
  behaviour is covered by the existing `test_phase35_completion_database.py` (passed).
- Not FIXED: no deployment runs `hardened` yet. Day 30 action: deploy and verify.

**F3: IN PROGRESS.**
- No new tooling. The existing CLIs are dry run by default; `--execute` applies, and each
  change is audited with a reason. Development sequence, run by an operator with their own
  credentials (nothing has been executed):
  1. `python -m rfq_copilot.operator_admin --target development provision --new-user <id> --display-name <name> --email <email> --region <region> --subject <idp-sub> --verification-ref <ref> --approved-by <approver> --bootstrap` (first operator; afterwards `--requested-by <operator>`), then the same with `--execute`.
  2. `python -m rfq_copilot.identity_admin --target development grant --user <staff-user> --customer <customer> --operator <operator> --reason onboarding [--execute]`.
  3. When assignments are required: `python -m rfq_copilot.assignment_admin --target development grant --user <staff-user> --rfq <rfq> --operator <operator> --reason onboarding [--execute]`.
  4. Revocation: `python -m rfq_copilot.identity_admin --target development revoke-token ...`.
- The IdP subject (`sub`) must equal `users.authentication_subject`. No credentials are
  stored in the repository.
- Day 29–30 action: provision demo identities and verify an end-to-end call.

**F4: IN PROGRESS.** The host is implemented (`mcp/host.py`) and the full chain is
tested through it in memory (`tests/test_mcp_host.py`, 54 tests):
- Host and Origin allowlists (421/403), on top of the SDK DNS-rebinding settings.
- Exact routes only (404 for REST/admin paths and slash variants).
- Modern-only protocol policy: legacy, handshake, unknown and missing versions,
  `initialize`, batches, malformed JSON, and header/body mismatches all get 400.
- Body limit (413, declared and streamed); rate limit (429; `X-Forwarded-For` honoured only
  from trusted proxies); concurrency limit (503); timeout (504).
- Per-server RFC 9728 metadata and 401 challenges.
- Ten tools, each server re-checking its own permission.
- Customer isolation and assignments through the host.
- 12 concurrent identities isolated.
- A read-only, repeatable-read snapshot through the host.
- Mutation checks: removing the protocol policy (11 failures), the Host check (2) and the
  rate limiter (2) are all caught.

Remaining:
- TLS is not tested; it is a deployment boundary, required by configuration.
- No live uvicorn run of the host yet.
- The `rfq_mcp_reader` role is documented but not created.
- Limits are per process.

Day 29–30: run the host under uvicorn on loopback, add REST-to-MCP client integration,
and verify end to end.

## Day 29 update (2026-11-11)

**F1: MITIGATED.** REST→MCP tests run the REST app under the `hardened` profile (v2).
- The demo route uses REST `catalog_read`: an `auditor` (v2 `view_quote` only) gets 403
  before any MCP call.
- MCP stays v2-only (Day 27–28 tests re-run: 104 passed).
- Day 30: deploy REST with `RFQ_API_DEPLOYMENT_PROFILE=hardened`.

**F2: IN PROGRESS.**
- A delegated MCP token revoked by its sha256 makes the REST→MCP call fail closed (503),
  and the REST token stays valid (`test_rest_rbac_and_revoked_delegated_token`).
- The hardened profile (revocation and idempotency) is exercised in the same tests.
- Day 30: verify a deployment running `hardened`.

**F3: IN PROGRESS.**
- The CLI workflow is verified on the guarded test database by the existing
  `tests/test_operator_assignment_admin_database.py` (in the 316-pass run).
- Nothing was provisioned on development; no real accounts or credentials.
- The live process cannot use rolled-back fixtures, so live authenticated execution needs
  provisioned identities. Day 30 action: run the documented CLI sequence with an operator.

**F4: IN PROGRESS.**
- Live `uvicorn --factory rfq_copilot.mcp.host:create_app_from_env` on 127.0.0.1 (one
  worker, `--no-proxy-headers`, guarded test DB config) verified:
  - startup and clean SIGTERM shutdown;
  - three metadata routes;
  - 401 on all three endpoints, for REST-audience, bad-signature and unknown-subject tokens;
  - 421 for a bad Host, 403 for a bad Origin;
  - 400 for malformed and legacy requests, 404 and 405;
  - no configured secret, path or trace in responses or logs.
- Live testing found and fixed a defect: `McpAuthSettings.from_env` refused the host's
  `RFQ_MCP_HOST_*` and `RFQ_MCP_DATABASE_TARGET` variables, so `create_app_from_env` could
  not start. A regression test was added.
- REST→MCP through the in-memory host:
  - delegated identity equals the REST principal;
  - the REST token is never forwarded;
  - spoofed customer and user headers are ignored;
  - all three servers are reachable;
  - another customer's RFQ gives `not_found`;
  - 9 concurrent identities stay isolated.

Remaining:
- No production credential provider (needs an IdP token exchange).
- Authenticated live execution is unverified.
- TLS and the `rfq_mcp_reader` role are not yet in place.

## Day 30 update (2026-11-11)

Evidence below comes from `tests/test_mcp_live_e2e.py` (Day 30, 6 tests) and the single full
regression run recorded in `docs/dev_log/day_30.md`.

**New Day 30 findings**

| ID | Finding | Severity | Status |
|---|---|---|---|
| D30-1 | Authenticated requests through a real `Database` failed: `resolve_identity` callers read `user.user_id` after `Database.session()` ended with a rollback, which expires instances (`DetachedInstanceError`). REST answered 500; the MCP host failed every authenticated call (`unavailable`). Present since Phase 3; hidden because every DB test used a savepoint-joined session. | High | FIXED |
| D30-2 | `McpTokenVerifier` copied each endpoint URL into `AccessToken.resource`, so `validate_token_resource=True` could never fail and only looked like per-endpoint binding. | Low | FIXED |
| D30-3 | `mcp/client.py` imports `httpx2` at runtime, but `pyproject.toml` declares it only in the dev group (it arrives transitively via `mcp>=2.3`, which requires `httpx2>=2.10`). | Low | RESOLVED Day 31: moved to `[project].dependencies` (`httpx2>=2.16,<3`), lock re-resolved with no version changes |
| D30-4 | The `20261111_02`–`_06` migration docstrings say "not applied to development"; a read-only inspection shows development at `20261111_06`. Migrations are frozen, so this is recorded, not edited. | Info | DEFERRED WITH JUSTIFICATION (frozen files) |

- **D30-1 fix:** `src/rfq_copilot/repositories/identity.py` `find_active_user`, the only loader
  behind `resolve_identity` (shared by REST and MCP), returns the user detached (`session.expunge`). Evidence: the live test passes with a real
  `Database` on both REST and the MCP host. Mutations (fix removed): the live REST call returned 500; an
  MCP-only call returned `unavailable` and the host log contained `DetachedInstanceError`.
- **D30-2 fix:** `src/rfq_copilot/mcp/security.py`: `resource` = the verified audience, and
  `validate_token_resource=False` (the SDK's documented setting when the verifier checks `aud`
  itself). The accepted-token set is unchanged. Evidence:
  `test_tokens_are_bound_to_the_mcp_audience_not_an_endpoint` (3 endpoints): the principal reports
  the audience, and an RFC 8707 per-endpoint `aud` is rejected. Architecture §7/§10/§11 and ADR D-5
  are corrected.

**F1: MITIGATED (unchanged status, narrower remainder).**
- Hardened configuration requirement: verified. `test_rest_hardened_profile_refuses_insecure_settings`
  (6) and `test_rest_hardened_profile_requires_jwt` passed. The live test ran the REST app under
  `hardened` against a real `Database`: `identity_operator` 403 (no `view_catalog`), customer A 200.
- Remaining (OPEN, deployment owner): no deployed REST runs `RFQ_API_DEPLOYMENT_PROFILE=hardened`.
  Closure evidence: the deployed process's settings plus a 403 for a v1-only grant against it.

**F2: IN PROGRESS → MITIGATED.**
- `test_hardened_profile_enforces_revocation_and_upload_idempotency`, under the full hardened
  profile: same key 201 then replay (`idempotent-replay: true`, one stored file), different body
  409; the token revoked through `identity_admin.revoke_tokens` 401; a fresh token 200.
- Live: a revoked identity gets REST 401 (hardened, real DB) and MCP `unauthenticated` on all three
  servers (separate uvicorn process).
- Not FIXED: no deployment runs `hardened` yet, and revocation before `exp` still depends on an
  IdP or operator feed (TD-024). Closure: the deployed configuration plus a revoked-token 401 against it.

**F3: IN PROGRESS (awaiting approval).**
- Process-level identities verified on the guarded test DB (committed for the test, then truncated):
  customer A and B users, restricted `manufacturing_engineer`, `identity_operator`, revoked and
  inactive users. Afterwards the test DB was back at rest (0 relations; `seed_database` asserts it).
- Development (read-only inspection): head `20261111_06`, `identity_operator` role present,
  0 bindings, 0 operators, 0 inactive users. The provisioning plan (existing CLIs, dry run first) is
  in `docs/dev_log/day_30.md` (Approval plan). Nothing executed.
- Closure for demo provisioning: approved commands executed, then
  `identity_admin inspect --user <id>` shows the expected binding/role for each demo identity.
  Production onboarding stays separate.

**F4: IN PROGRESS (development boundary verified live; production open).**
- Authenticated live end to end (`test_authenticated_live_end_to_end`): REST (in process, hardened,
  real `Database`) → `McpIntegration` with the TEST-ONLY provider → real TCP → separate
  `uvicorn --factory rfq_copilot.mcp.host:create_app_from_env` process (read-only engine, guarded
  test DB) → gateway → SDK bearer auth → tools → PostgreSQL. Verified:
  - REST token never forwarded;
  - all three servers reached with one MCP token;
  - stored-RFQ quote equals `quote_rfq` exactly (Decimal text);
  - customer B and forged claims → `not_found` (8 concurrent calls isolated);
  - DB-derived permissions per server (restricted and operator);
  - revoked, inactive, REST-audience, expired, bad-signature and unknown tokens → `unauthenticated`
    on all three servers;
  - no token or traceback in the host log, and a clean SIGTERM shutdown.
- Remaining (production, not development): IdP token exchange / credential provider (BLOCKED, no
  approved IdP); TLS termination evidence; `rfq_mcp_reader` provisioning and grant tests;
  deployment of both processes. Closure evidence for each: `phase_04_final_verification_report.md` §5.

**F5: FIXED (verified).** `grep -rniE "phase ?4|phase four" docs README.md` finds only the
preserved original line with its erratum and correct "not exposed in Phase 4" rows.
