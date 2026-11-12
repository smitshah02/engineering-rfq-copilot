# Phase 3 Final Backend Report (Days 16–22)

This report separates three states:

- **Implemented**: the code and tests exist.
- **Provisioned**: the data exists in the development database.
- **Activated**: the deployment configuration turns the feature on.

## Architecture

- **Application**: `rfq_copilot.api.app:create_app`, an application factory with no
  module-level app. Settings come from `RFQ_API_*` variables; unknown variables are
  rejected.
- **Startup**: loads and checks the JWT public keys. An unsafe key stops startup.
  Startup opens no database. A read-only `Database` and a separate writable `Database`
  (used for uploads only) are created lazily on the first authorized request and disposed
  in the lifespan.
- **Layers**: routers → `api/auth.py` (identity and RBAC) → `repositories/` (async
  SQLAlchemy, caller-owned sessions, `raiseload("*")`) → PostgreSQL schemas `business`
  and `orchestration`.
- **Deterministic business engines** (Phase 2): unchanged. Decimal arithmetic with
  ROUND_HALF_UP, pinned rule fingerprints, `approval-v1.2` current, `approval-v1.1`
  historical. Approval and release stay human-only, and no API route approves, rejects or
  releases.

## Routes and authorization

| Route | Policy v1 permission | Policy v2 permission |
| --- | --- | --- |
| `GET /health/live` | public | public |
| `GET /api/v1/system/info` | public (name and versions only) | public |
| `GET /api/v1/customers/me` | `view_customer` | `view_customer` |
| `GET /api/v1/rfqs`, `GET /api/v1/rfqs/{rfq_id}` | `view_assigned_rfqs` | `view_assigned_rfqs` |
| `GET /api/v1/rfqs/{rfq_id}/attachments` | `view_assigned_rfqs` | `view_assigned_rfqs` |
| `POST /api/v1/rfqs/{rfq_id}/attachments` | `create_rfq` | `attach_rfq_document` |
| `GET /api/v1/materials` | `view_assigned_rfqs` | `view_catalog` |
| `GET /api/v1/quotes/{quote_id}` (released only) | `view_assigned_rfqs` | `view_quote` |

`/openapi.json`, `/docs` and `/redoc` are served only when `environment=development`.

Two tests guard against an unlisted route:

- `test_rbac.py` checks every route in the OpenAPI schema against the matrix.
- `test_day22_integration.py` checks the live router tree, including routes hidden from
  the schema.

## Authentication and identity flow

1. **Token**: a bearer JWT signed with RS256, ES256 or EdDSA (one algorithm configured).
   - Issuer, audience, `exp` and `iat` are required, with a maximum lifetime.
   - `kid` selects among the rotation keys.
   - `none`, HMAC, embedded JWK and key-locating headers are refused.
2. **Key files**: owner-only permissions and no symlinks; the directory and its ancestors
   must not be writable by others. The key is opened safely against substitution races.
3. **Identity**: one session reads the active user, then the revocation status (when
   enforced), then the customer binding (one, or a selected one in `selectable` mode),
   then the permissions granted to the user's role in the database. Token claims never
   confer privilege.
4. **Scoping**: every customer filter is applied in SQL. Another customer's resource is a
   concealed 404. Quotes are visible only after a human release.
5. **Read routes**: `SET TRANSACTION READ ONLY`, rollback-only sessions.

## Database and migrations

- **Development**: revision `20261111_06` (head), `rbac-policy-v2` overlay applied
  (SHA-256 pinned).
  - 36 tables, 1,107 records, 0 integrity violations (re-verified Day 22).
  - Bindings: 0. RFQ assignments: 0. Token revocations: 0. Operators: none.
- **Migration `20261111_05`** (multi-customer index): deferred in
  `alembic/proposed/deferred/`. It is exercised only by the isolated test contract `NEXT`.

## Attachments

PDF and CSV uploads follow this order:

1. Authorization is checked before the body is read.
2. The multipart body is size-capped (also when streamed).
3. Content is validated. PDFs are parsed in a killable child process with time and
   resource limits.
4. The file is written without overwriting an existing one, under a checked storage
   root.
5. The database row is committed. A database marker binds the database to its storage.
6. If the COMMIT outcome is ambiguous, the file is kept for `reconcile_attachments`:
   - dry run by default;
   - quarantine with inode checks;
   - `--list-missing`;
   - restore without overwriting.

Upload idempotency (`Idempotency-Key`) replays the stored 201 response. A different body
gets a 409, and a concurrent race replays the winner's response.

## Feature flags (defaults are the safe or legacy behaviour)

| Variable | Default | State |
| --- | --- | --- |
| `RFQ_API_PERMISSION_POLICY` | `v1` | v2 implemented; its policy data is provisioned in development. The deployment must set `v2`. |
| `RFQ_API_TOKEN_REVOCATION` | `off` | Implemented; table present. Not activated. |
| `RFQ_API_UPLOAD_IDEMPOTENCY` | `off` | Implemented; CHECK constraint present. Not activated. |
| `RFQ_API_RFQ_ASSIGNMENTS` | `off` | Implemented; table present with 0 rows. **Must stay off** until assignments exist (otherwise every staff read would return empty or 404). |
| `RFQ_API_CUSTOMER_CONTEXT` | `single` | `selectable` is implemented and needs migration `_05`. Deferred. |

## Operator CLIs (implemented, not exercised against real accounts)

These commands are dry run by default, check authority against the database and write
audit rows:

- `python -m rfq_copilot.operator_admin`: provision or deactivate `identity_operator`
  accounts.
- `python -m rfq_copilot.identity_admin`: grant, revoke or inspect bindings, and revoke
  tokens.
- `python -m rfq_copilot.assignment_admin`: grant, revoke, list or verify RFQ
  assignments. The Day 22 `verify` against development returned `stale: []`.
- `python -m rfq_copilot.reconcile_attachments`: storage and database reconciliation.

## Tests

See `docs/dev_log/day_22.md` for the exact counts and durations. All tests that write to a
database use the guarded `*_test` database. Public tests need no credentials.

## Not done (deployment requirements)

- Provision real `identity_operator` accounts, customer bindings and, later, RFQ
  assignments, all through the CLIs (Decisions C and B).
- Choose the production flags: `v2`, `enforced` revocation and `enabled` idempotency are
  recommended.
- A DBA-provided restore test of the backup (TD-023).
- An identity-provider decision for revocation (TD-024).
- Decide on and apply `_05` before enabling multi-customer context (TD-017).
- Still open from Phase 2.5: TD-001, TD-006 and TD-007. TD-007 concerns quote persistence
  (Phase 4 scope).

## Phase 4 handoff

- Build agents or MCP tools on the repository and service layer with caller-owned
  sessions. Do not bypass `get_principal` or `require_route_permission`.
- Each new route must be added to the `MATRIX` in `test_rbac.py`. Both route-coverage
  tests fail otherwise.
- Do not add an API path to approve or release quotes. These actions stay human-only.
- Frozen evaluation data stays untouched until the final evaluation phase.

## Erratum (2026-11-11, Day 23)

The "Not done" list above says TD-007 (quote persistence) is "(Phase 4 scope)". That is
wrong. The approved roadmap is: Phase 4 MCP infrastructure, Phase 5 agent and LangGraph
fundamentals, Phase 6 the five specialist agents, Phase 7 supervisor orchestration,
**Phase 8 quotation persistence and human approval**. TD-007 belongs to Phase 8. Phase 4
exposes read and evaluation tools only. The original text is kept as historical evidence.
Tracked as F5 in `phase_04_findings_tracker.md`.
