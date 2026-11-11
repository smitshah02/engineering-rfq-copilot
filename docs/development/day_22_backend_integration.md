# Day 22: Backend Integration and Phase 3 Closure

Day 22 checked the FastAPI backend built in Days 16–21 and hardened in Phase 3.5 as one
system. It added only the tests that were missing and introduced no new features.

## Request lifecycle under test

Request → FastAPI routing → Pydantic path/query validation → bearer JWT (`TokenVerifier`:
algorithm allowlist, issuer, audience, `exp`/`iat`, max lifetime, `kid` rotation) →
`get_principal` (active user, optional revocation check, customer binding, DB-granted
permissions, all in one session) → `require_route_permission` (policy v1/v2 table) →
route dependencies (read-only or write session, storage, body) → repository SQL with the
customer filter (plus the assignment EXISTS filter when enforcement is on) → PostgreSQL →
frozen response model.

Failure ordering (tested in `test_auth.py`, `test_rbac.py`, `test_day21_rbac_database.py`):
no or invalid token gives 401, an unknown, inactive or revoked user gives 401, no binding
gives 403, a missing permission gives 403. All of these happen before any session,
storage or body access. Another customer's resource gives 404. When the trust boundary
or the database is unavailable, or revocation status is unknown, the answer is an opaque
503.

## Coverage already in place (verified, not duplicated)

| Area | Where |
| --- | --- |
| Startup/shutdown, lifespan disposal, settings, docs per environment | `test_api.py`, `test_phase3_hardening.py` |
| OpenAPI route set and the per-route permission matrix | `test_api.py`, `test_rbac.py` |
| JWT validation, rotation, key symlink/substitution race, ancestor checks | `test_auth.py`, `test_phase3_hardening.py` |
| Per-role DB grants, cross-customer 404, interleaved users, forged claims | `test_day21_rbac_database.py`, `test_day20_auth_database.py` |
| PostgreSQL reads, SQL scoping, read-only transactions, concurrency | `test_day18_api_database.py`, `test_phase3_hardening_database.py` |
| Uploads, PDF subprocess limits, quarantine, reconcile, no-overwrite restore | `test_attachments.py`, `test_day19_attachments_database.py`, `test_phase3_reconcile.py` |
| Revocation fail-closed, idempotency/race/lost-ack, assignments, multi-customer | `test_phase35_completion*.py`, `test_multi_customer_lifecycle.py`, `test_operator_assignment_admin_database.py` |
| Fresh-DB migrate/seed/overlay/downgrade lifecycle (guarded `*_test` DB) | `test_policy_overlay_lifecycle.py`, `test_database_migrations.py` |

## Gaps closed on Day 22: `tests/test_day22_integration.py`

1. **Hidden-route bypass.** The matrix test enumerates the OpenAPI schema, so it cannot
   see a route registered with `include_in_schema=False`. The new test walks the live
   router tree, recursing into FastAPI's nested `_IncludedRouter`. It requires every
   operation to be either public by name or in the permission matrix, and it requires
   every matrix route to answer 401 to anonymous ASGI calls. A mutation check (adding a
   hidden `/system/hidden` route) was caught by the new test and passed the existing
   OpenAPI test, which confirms the gap was real.
2. **Real Uvicorn process.** The test runs `uvicorn --factory` with no `RFQ_*` or
   `DATABASE*` variables and checks five things:
   - liveness returns 200;
   - production hides `/openapi.json` (404);
   - an anonymous request gets 401;
   - a bearer request with no trust configuration gets 503;
   - SIGTERM leads to "Application shutdown complete" with no traceback.

   Uvicorn re-raises the handled SIGTERM, so exit code `-15` counts as a clean exit.

## Audit result

No reproducible authentication, authorization, isolation, SQL, filesystem, transaction or
error-leakage defect was found. One documentation defect was fixed: the debt register
and README still described the development database at `20261110_01` with 1,090 records.
Exact results: `docs/dev_log/day_22.md`. Final report: `phase_3_final_backend_report.md`.
