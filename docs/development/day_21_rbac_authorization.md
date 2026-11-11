# Day 21: Role-Based Access Control and Authorization

Status: complete (2026-11-10). Builds on Day 20 ([day_20_authentication.md](day_20_authentication.md)).

## Three independent checks

| Question | Source of truth | Failure |
| --- | --- | --- |
| Who is calling? (authentication) | A JWT verified against the configured issuer and key (`api/tokens.py`), whose `sub` names an active `business.users.authentication_subject` | 401 |
| Which customer do they represent? (binding) | Exactly one unrevoked `business.user_customer_bindings` row, to a customer whose status is not `inactive` | 403 |
| What may they do? (authorization) | `business.role_permissions` rows with `allowed = true` for the user's `users.role` | 403 |
| Does the resource belong to that customer? (ownership) | Each route's customer-scoped SQL | 404 |

Each check is separate. A valid token grants nothing on its own. A binding without the permission gets 403. A permission without a binding also gets 403. Neither replaces the ownership check.

## Trusted roles and permissions

The vocabulary comes from dataset v1.1.0 (`master_data/roles_permissions.csv`), seeded on Day 5 into `business.roles` (6), `business.permissions` (12) and `business.role_permissions` (27 grants, all `allowed = true`). Nothing was added or changed.

- **Roles** (all internal staff): `sales_representative`, `estimator`, `manufacturing_engineer`, `sales_manager`, `administrator`, `auditor`. The FK `users.role → roles.role` and a CHECK on `roles.role` make any other role impossible.
- **Permissions:** `view_assigned_rfqs`, `view_customer`, `create_rfq`, `create_quote_draft`, `edit_quote_draft`, `request_approval`, `engineering_review`, `approve_quote`, `reject_quote`, `release_quote`, `view_audit`, `manage_catalog`.

The code names only permissions, never roles. Which role holds a permission is database data, so changing a grant needs no code change.

## Permission-to-endpoint matrix

| Method | Path | Required permission | Roles holding it today (from the grants) |
| --- | --- | --- | --- |
| GET | `/api/v1/customers/me` | `view_customer` | all six |
| GET | `/api/v1/rfqs` | `view_assigned_rfqs` | sales representative, estimator, manufacturing engineer, sales manager |
| GET | `/api/v1/rfqs/{rfq_id}` | `view_assigned_rfqs` | same |
| GET | `/api/v1/materials` | `view_assigned_rfqs` (TD-014) | same |
| GET | `/api/v1/quotes/{quote_id}` | `view_assigned_rfqs` (TD-014) | same |
| GET | `/api/v1/rfqs/{rfq_id}/attachments` | `view_assigned_rfqs` | same |
| POST | `/api/v1/rfqs/{rfq_id}/attachments` | `create_rfq` (TD-016) | sales representative, estimator |
| GET | `/health/live`, `/api/v1/system/info` | none (public) | — |

**Why these mappings:**
- `view_customer` covers the customer profile.
- The RFQ list, RFQ detail and attachment metadata are RFQ reads. "Assigned" means the bound customer's RFQs, because the schema has no per-RFQ staff assignment (TD-015).
- The vocabulary has no read permission for the catalog or for released quotes. Both serve work on the customer's RFQs, so they use `view_assigned_rfqs` rather than an invented permission (TD-014).
- Attaching an intake document is part of RFQ intake. `create_rfq` is the only intake permission; no RFQ-edit permission exists (TD-016).
- The approval permissions (`request_approval`, `approve_quote`, `reject_quote`, `release_quote`) are required by no route, and no route performs those actions.

`tests/test_rbac.py::MATRIX` is this table in code. A test enumerates the operations in the OpenAPI schema, so a new route without a matrix entry fails.

## Principal and dependency architecture

```
route handler
  └─ require_permission(P)             api/auth.py; 403 unless P in principal.permissions
       └─ get_principal                 api/auth.py; cached per request by FastAPI
            ├─ bearer (HTTPBearer)      no credentials: 401
            ├─ TokenVerifier.subject    bad token: 401; no trust configured: 503
            └─ one read-only session (get_database):
                 find_active_user       unknown or inactive: 401
                 customer_binding       none, revoked or ineligible: 403
                 granted_permissions    role grants with allowed = true
  └─ get_read_session / get_storage_dir / get_write_session   only after the above
```

- `Principal` is a frozen dataclass: `customer_id` plus `permissions: frozenset[str]`, which defaults to empty, so access is denied by default. It holds no role, so nothing can compare role names.
- `require_permission(name)` is a FastAPI dependency factory. The handler's first parameter is `Depends(require_permission(...))`. FastAPI resolves dependencies in declaration order, so authorization finishes before the session, storage directory or request body is touched. `get_principal` is cached per request, so the other dependencies that use it reuse the same principal without new lookups.
- Grants are read only after a binding is found. An unbound user costs 2 queries, as on Day 20. A bound user costs 3: user, binding, grants.
- No new module and no circular import: the repositories are imported inside `get_principal`, as on Day 20.

## Lookup lifecycle and how changes take effect

All three lookups run in one short read-only session per request, opened by `get_principal`. There is no cache or global authorization state. A grant change (`allowed`, a row deleted), a role change (`users.role`), a deactivation (`users.active`) or a binding revocation applies to the very next request, without a restart. The database tests prove each case.

## 401 vs 403 vs 404

- **401** (`WWW-Authenticate: Bearer`, "Authentication required."): no token, a bad token, or an unknown or inactive user. These are indistinguishable.
- **403** ("Forbidden."): verified, but no eligible binding or no required permission. Both give the same body, so callers cannot tell which check failed.
- **404** ("Not found."): the resource is another customer's or does not exist. These are indistinguishable, so ownership is concealed. A permitted user never gets 403 for another customer's RFQ.
- **503:** trust not configured, or the identity database is unavailable. This is unchanged from Day 20.

## Internal staff and customer representation

All roles are staff. A user acts for at most one customer (the Day 20 single-active-binding invariant, unchanged). There is no implicit all-customer access for any role, administrators included, and no impersonation or delegation. Cross-customer staff workflows are TD-017.

## RBAC does not replace the quote approval policy

- Day 21 adds no approval, rejection, release or draft routes, and no route requires an approval permission.
- Holding `approve_quote` or `release_quote` opens nothing. When a test removes the approver's `view_assigned_rfqs`, the approver gets 403 on quotes.
- Viewing a released quote runs only SELECTs. A test shows that every quote's status is unchanged afterwards.
- A future approval route will need both the permission and the approval policy engine (`approval-v1.2` for new evaluations, historical `approval-v1.1`): version match, approver ≠ creator, a human actor, and the release gate. A permission is necessary but never sufficient.
- No agent identity exists yet. The reference policy forbids agents from approving or releasing. Day 22+ must keep agent identities out of `users` roles that hold approval grants, or check the actor kind explicitly.
- Business engines, rule loading, evaluation fingerprints, `snapshot_session()`, REPEATABLE READ evaluation and Decimal arithmetic are untouched.

## Attacks tested

**Public, `tests/test_rbac.py` (30 tests; real ES256 tokens, fake identity store):**
- every OpenAPI operation is in the matrix;
- each route opens only for its permission: all other permissions give 403, its own permission alone gets past authorization;
- a bound user with no grants gets 403 everywhere, with no route session opened;
- each of the 9 unrelated permissions opens nothing;
- forged `role`, `roles`, `permissions`, `scope`, `scp` and `groups` claims are ignored;
- spoofed `X-Role` and `X-Permissions` headers, cookies and query parameters are ignored;
- grant changes apply between requests;
- an upload without `create_rfq` gets 403: no storage directory, no write database, body not parsed;
- grants without a binding give 403, and an unknown user with grants gets 401;
- 20 concurrent requests from two users with different grants are isolated;
- `Principal` validation, and near-miss permission names.

**PostgreSQL, `tests/test_day21_rbac_database.py` (13 tests; real tokens, seeded grants, guarded test database):**
- **matrix check per role:** every seeded role, each with one real user, gets exactly the routes its database grants allow. Expectations are derived from `role_permissions`. Only permitted uploads write files and rows;
- the route permissions are real, granted permission records;
- a denied request runs exactly 3 SELECTs (user, binding, grants) and no RFQ query;
- a valid role with the wrong customer gets 404, identical to a missing RFQ;
- the correct customer with an insufficient permission gets 403 and no data;
- grants revoked, restored or deleted, and role changes, apply on the next request;
- a role with no grants gets 403 everywhere; an unknown role is refused by the schema;
- forged privilege claims on a real token change nothing;
- a disabled user (401) or a revoked binding (403) loses access despite holding grants;
- approval authority neither implies viewing nor changes quotes;
- two users interleaved keep their own permissions.

**Mutations (each applied to a temporary copy and restored; failing tests out of 43):**

| Mutation | Failed |
| --- | --- |
| Permission check bypassed | 35 |
| Permissions read from the token's `permissions` claim | 23 |
| Customer scoping removed from RFQ detail | 1 |
| `allowed` flag ignored | 2 |
| Grants of any role used | 9 |
| Approval permission treated as full access | 12 |
| Upload route back on bare `get_principal` | 20 |

## Test results (actual)

| Command | Result |
| --- | --- |
| `pytest -q tests/test_rbac.py tests/test_day21_rbac_database.py` | 43 passed |
| Days 16–20 API tests (8 files) | 469 passed |
| Public smoke (8 files, `env -u DATABASE_URL`) | 501 passed |
| `pytest -q` | 1631 passed in 152.85 s; 0 failed, 0 skipped, 0 warnings |
| `ruff check .` / `ruff format --check src tests alembic` / `uv lock --check` | pass / 112 files formatted / pass |
| `integrity --target development` (read only) | PASS, 1090 records, 0 violations |

**Uvicorn, production mode, real ES256 tokens, development database read only:**
- live and info: 200;
- no token, bad signature, unknown subject, or spoofed headers without a token: 401 on all 7 routes;
- a valid active user: 403 on all 7, because the development database has no bindings (none were created);
- forged role and permission claims: 403;
- no tokens, subjects or errors in the log.

The allowed path behind real tokens is proven by the PostgreSQL tests, not live, because no binding may be provisioned in development.

## Limitations and Day 22 requirements

- TD-014 to TD-017 (see `technical_debt.md`): no catalog or quote read permission, assignment means the bound customer, upload mapped to `create_rfq`, no multi-customer staff model.
- Each authenticated request makes 3 small indexed queries. There is no cache, by design. Revisit only with measurements and an invalidation story.
- `view_audit`, `manage_catalog`, `engineering_review` and the draft and approval permissions have no routes yet.
- **Day 22+:**
  - every new route must use `require_permission` and get a `MATRIX` entry; the OpenAPI test enforces the entry;
  - write routes must still pass the business engines and the approval policy;
  - agent or tool identities must not reuse human approval authority.

## Addendum — Phase 3.5 completion (2026-11-11)

- Route permissions now come from one table, `api/auth.py::ROUTE_PERMISSIONS`, through `require_route_permission(operation)`. Policy `v1` is exactly the matrix above and stays active.
- A proposed policy `v2` (`view_catalog`, `view_quote`, `attach_rfq_document`, derived from the reference policy) is implemented and tested. It is not active: it awaits approval of `alembic/proposed/20261111_01`.
- `Principal` also carries `user_id` and the assignment flag.
- See [phase_3_technical_debt_resolution.md](phase_3_technical_debt_resolution.md).
