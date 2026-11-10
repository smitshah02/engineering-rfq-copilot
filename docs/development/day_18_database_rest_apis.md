# Protected read APIs (Day 18): database-backed REST reads

This document describes the code as implemented. Day 18 adds **read-only**, customer-scoped
routes backed by the existing repositories. There is no authenticator yet, so a real
deployment answers **401** on every protected route. Integration tests inject a verified
principal through a FastAPI dependency override.

## 1. Endpoints

| Method and path | Response | Data source | Notes |
| --- | --- | --- | --- |
| `GET /api/v1/customers/me` | `CustomerProfile` | `get_customer(session, principal.customer_id)` | 404 if the account record is absent |
| `GET /api/v1/rfqs?limit=1..100&offset=0..10000` | `RfqPage` | new `list_rfqs(session, customer_id, limit, offset)` | newest `submitted_at` first, ties by `rfq_id`; `has_more` from a limit+1 fetch |
| `GET /api/v1/rfqs/{rfq_id}` | `RfqDetail` | `get_rfq(session, rfq_id, customer_id=…)` | runtime lines as `PartSpecification`; another customer's RFQ and a missing one give the same 404 |
| `GET /api/v1/materials?category&grade&thickness_mm&finish_id&limit&offset` | `MaterialPage` | `find_materials(...)`, then `list_materials(window)` | ordered by `material_id`; identity fields only |
| `GET /api/v1/quotes/{quote_id}` | `ReleasedQuote` | `list_quotation_history(session, customer_id)` | only `status == "released"` with `released_at`; returns the approved version |

**What each protected route returns:**
- no verified identity: `401 {"detail":"Authentication required."}` with `WWW-Authenticate: Bearer`
- invalid parameters: the Day 17 sanitized 422
- not visible to the caller: `404 {"detail":"Not found."}`
- database failure: `503 {"detail":"Service unavailable."}`
- unexpected error: the opaque `500 Internal Server Error`
- writes: every route is GET-only, so other methods give 405

`/health/live` and `/api/v1/system/info` are unchanged and database-independent.

## 2. Response contracts (`api/schemas/reads.py`)

Every model has `extra="forbid", frozen=True`, and each exposes only the fields listed:
- **`CustomerProfile`:** `customer_id`, `company_name`, `industry`, `city`, `state`, `country`,
  `payment_terms`, `preferred_shipping`, `contacts[contact_name, email, phone, is_primary]`.
- **`RfqSummary` / `RfqDetail`:** `rfq_id`, `status`, `submitted_at` (timezone-aware), plus
  `lines[line_number, part: PartSpecification]` in the detail. Measurements are exact strings.
- **`MaterialSummary`:** `material_id`, `category`, `grade`.
- **`ReleasedQuote`:** `quote_id`, `rfq_id`, `status: "released"`, `released_at`, `version`,
  `currency`, `total_usd` (`Money`), `expiry_date`, `estimated_lead_business_days`,
  `lines[line_number, part_id, quantity, line_total_usd]`.

**Deliberately not exposed:**
- credit limit and exposure, account status, restrictions, approval requirement, region
- contact IDs and internal user IDs (`created_by`, approver, releaser)
- row versions, source evidence, historical part references, `raw_request`
- material cost, density, bend factor, availability class, pricing version
- cost breakdowns, part JSON, rule versions, snapshots, other quote versions

**Query models:** `RfqListQuery` and `MaterialListQuery` forbid extra parameters, so
`?customer_id=…` is a 422. They are registered in `errors.REQUEST_MODELS`. The path names
`rfq_id` and `quote_id` are in `errors.PATH_PARAMETERS`. `int_parsing` was added to the
code allowlist, because query integers arrive as text.

## 3. Request-to-database lifecycle (`GET /api/v1/rfqs/{rfq_id}`)

1. FastAPI matches the route in `routers/rfqs.py::get_customer_rfq`.
2. It resolves `get_principal` (`api/auth.py`). Today that raises 401 before anything else
   runs, so `get_database` and the session are never reached (tested with a tripwire).
3. With a verified principal, FastAPI resolves `get_read_session` (`api/db.py`), which
   itself depends on `get_principal` and `get_database`:
   - `get_database` returns the injected `Database`, or builds one once, under an
     `asyncio.Lock`, from the process `DATABASE_URL` with `read_only=True`. That sets
     `default_transaction_read_only=on`, so PostgreSQL refuses writes. A missing
     configuration gives a 503.
   - `Database.session()` yields a new `AsyncSession` for this request.
4. FastAPI validates `rfq_id` as an `Identifier`. A failure is the sanitized 422. The
   session has been created but no query runs, because `AsyncSession` connects lazily
   (tested against an unreachable database: the result is 422, not 503).
5. The endpoint calls `get_rfq(session, rfq_id, customer_id=principal.customer_id)`. The
   SQL filters on both IDs and loads the customer and lines eagerly, with
   `raiseload("*")`.
6. The endpoint builds `RfqDetail` explicitly. It never returns ORM objects.
7. After the response, `Database.session()` rolls back and closes the session, also on
   errors and cancellation. A driver error inside the request becomes `DatabaseError`,
   which `get_read_session` turns into a 503 without SQL, URL or traceback.
8. At shutdown, the lifespan disposes a `Database` only if the application created it.
   An injected one belongs to its caller.

Database modules are imported lazily inside dependencies and endpoints. So the
application, `/health/live` and `/api/v1/system/info` start without SQLAlchemy, and the
Day 16 isolation test still holds.

## 4. Security

- **Fail closed.** `get_principal` takes no request input and always raises 401. There is
  no development bypass and no hard-coded customer. Headers (`X-Customer-Id`,
  `Authorization`, `X-Forwarded-User`), cookies and query parameters are ignored, as
  tested in both environments.
- **Customer scoping at the query.** Every customer read passes `principal.customer_id`
  into the repository's `WHERE` clause. Materials are not customer data but still require
  a verified caller.
- **Not visible means 404**, with an identical body whether a record is another
  customer's or missing.
- **Quotes need a human release.** Draft, revised, pending, approved-but-unreleased,
  rejected, expired, blocked and clarification quotes are never returned. The current
  dataset has no released quotes, so every real quote lookup is a 404 until a human
  release exists.
- **Errors:** the Day 17 sanitization is unchanged. 503 and 500 bodies are fixed text.

**Mutation checks.** Each was run once, then reverted and verified with `cmp`:
- removing the release filter: 8 tests fail
- ignoring the RFQ customer scope: 2 tests fail
- reading identity from `X-Customer-Id`: the spoofing test fails

## 5. What the authentication work must replace

- Replace the body of `api/auth.py::get_principal` with real verification. Keep the
  signature and the `Principal(customer_id)` result: it must come from the verified token
  or session, never from client fields.
- Map staff roles and account scope (`users`, roles, assigned customers) to the
  customer IDs a caller may read. If staff may read several customers, `Principal` needs an
  explicit scope, and every route keeps passing it to the repositories.
- Decide whether materials stay authenticated-only or become a public catalog.
- Keep 401 for missing or invalid credentials, and use 403 for authenticated callers
  outside their role. Today no route can produce 403, because no roles exist.

## 6. Tests

| Module | Needs | Count |
| --- | --- | --- |
| `tests/test_api_reads.py` | nothing (public smoke) | 55 |
| `tests/test_day18_api_database.py` | `.env`, PostgreSQL, dataset (guarded test database, rolled back) | 13 |

**Public module:**
- 401 on all 5 routes in both environments, with the database dependency never reached
- 6 spoofing variants
- 13 sanitized-422 cases (query and path)
- a real driver failure (port 1) giving an opaque 503 with no URL or SQL
- an unexpected error giving an opaque 500
- missing configuration giving a 503
- the lazy database: created once, read-only, owned and disposed; an injected one is not disposed
- field exclusion for profile, materials and quotes
- all 8 unreleased statuses giving 404
- the released quote returning the approved version only
- the OpenAPI contracts
- `list_rfqs` rejecting bad arguments before SQL

**Database module:**
- profile scope
- RFQ list order and pagination (each page against a direct SQL ordering), plus empty results
- cross-customer RFQ 404 identical to a missing RFQ
- runtime lines with exact decimals
- materials agreeing with `find_materials`
- an approved quote giving 404, then 200 after a release inside a savepoint, and 404 for another customer
- statement counts per route (2, 1, 2, 2, 3), all `SELECT`, with nothing pending in the session
- 9 concurrent requests on the real dependency chain getting 9 distinct sessions, all
  connections back to the pool baseline
- a lazily built database reporting `default_transaction_read_only = on`

## 7. Known limitations

- No authenticator, so the routes are unreachable outside tests (by design).
- `GET /quotes/{id}` reuses `list_quotation_history` and picks one quote. That is fine at
  the current scale (at most a handful of quotes per customer), and is marked `ponytail:`
  in `routers/quotes.py`.
- No total count in pages; the response has `has_more` only.
- For an authorized but invalid request, FastAPI creates the (lazy) session before
  reporting the 422; no connection or query happens.
- `list_rfqs` is the one additive repository function. No existing repository behaviour
  changed.
