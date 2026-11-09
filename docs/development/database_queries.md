# Day 5, Component 7 — Representative database reads

The internal `rfq_copilot.repositories` package implements seven async read operations over the existing ORM. It contains `queries.py` (bound SQLAlchemy queries and input validation), `results.py` (typed result wrappers), `__init__.py` (public exports), and `demo.py` (a read-only execution/verifier command). No API endpoint, MCP tool, agent, feasibility engine, pricing engine, table or migration is introduced.

## Public functions and results

All functions accept the caller's explicitly scoped `AsyncSession`. No function creates an engine/session, commits, reserves resources or modifies records.

| Function signature | Return type | Entities and behavior |
| --- | --- | --- |
| `get_customer(session, customer_id: str)` | `CustomerDetails \| None` | Customer plus only its associated contacts, sorted by contact_name/contact_id. |
| `get_rfq(session, rfq_id: str, *, customer_id: str)` | `RFQDetails \| None` | Customer-scoped RFQ/customer, opaque historical part references, separately loaded runtime RFQ lines. |
| `get_material_availability(session, material_id: str, *, thickness_mm: Decimal \| None = None, availability="all")` | `MaterialAvailability \| None` | Material and matching current inventory rows, with exact Decimal sum of generated available_kg. |
| `get_manufacturing_capabilities(session, material_id: str, *, part_type: str \| None = None)` | `ManufacturingCapabilities \| None` | Stored thickness options, compatible finishes, supported operations, their workcentres and optional part-type routing. |
| `get_workcentre_capacity(session, workcentre_id: str, *, week_start: date \| None = None, week_end: date \| None = None)` | `WorkcentreCapacity \| None` | Workcentre/operation and inclusive Monday-week capacity rows in hours, with Decimal sum of remaining_hours. |
| `list_quotation_history(session, customer_id: str, *, rfq_id: str \| None = None)` | `tuple[QuotationDetails, ...]` | One root per quote; all versions with their own supplied lines, preserving incomplete historical versions. |
| `list_pending_approvals(session, *, customer_id: str, required_role="sales_manager")` | `tuple[ApprovalRequest, ...]` | Actual pending request rows, scoped through exact quote version → quote → RFQ → customer. |

Result wrappers are frozen dataclasses containing ORM entities and sorted tuples; they do not make the contained entities immutable. Read/serialize the required fields while the caller's session is active. Unrequested relationships use raiseload, preventing implicit async lazy IO. The existing Database owner closes sessions/pools; queries never close a caller-owned session or introduce competing transaction management.

## Historical versus runtime data

The approved seed stores historical RFQ part identities in `RFQ.part_ids` JSONB and deliberately leaves `business.rfq_line_items` empty. `RFQDetails.historical_part_ids` returns those opaque references only for `source_type=historical_internal`. `runtime_lines` comes only from actual RFQLineItem records. No geometry or editable extraction line is invented from history or priced quotation snapshots.

QuotationDetails contains its Quote and a tuple of QuoteVersionDetails. Each version wrapper contains the exact QuoteVersion and its own line items. Older metadata-only versions retain NULL prices/pins and zero synthesized lines. Monetary NUMERIC columns remain Decimal, and nested monetary/mass JSON evidence remains the source's exact strings. No quote recalculation or historical-value repair occurs.

Pending approval requests and historical approval events are distinct. The development request table is empty; returning `()` is correct even though 70 historical events exist. Nonempty pending-request behavior is tested with permitted isolated test-only request records, never with invented development queue rows or reconstructed historical decisions.

## Filters, ordering and empty results

- Identifiers must be nonblank strings of at most 64 characters, with no NUL. They are preserved, never trimmed or fuzzily resolved. Malformed input raises a safe ValueError; well-formed missing IDs return None for single-object operations or an empty tuple for collection operations.
- RFQ retrieval requires customer_id. Quotation history and pending approval queries always require customer scope. A mismatched customer/RFQ returns empty without revealing another customer's records.
- Inventory thickness must be a finite positive Decimal with supported column precision/scale (NUMERIC(12,3)); binary floats and silently rounded inputs are rejected. `all` returns matching rows, `available` selects available_kg > 0, and `unavailable` selects available_kg = 0. These are stock-presence filters, not a promise that a requested job is feasible. A known material with no matching inventory returns an empty inventory tuple and Decimal zero.
- Capacity date filters must be Monday calendar dates; datetime inputs, non-Mondays and reversed bounds are rejected. Start/end are inclusive; a single start is a lower bound. To request exactly one week, supply the same start/end date. A known workcentre with no matching week returns empty weeks and Decimal zero hours.
- Manufacturing part-type filters use the six approved stored types. Operations/finishes come from the material's explicit junction records; workcentres/routing are limited to supported operations. Retrieval does not decide manufacturability.
- The approved approval_requests CHECK permits only required_role=sales_manager. Other role-filter inputs are rejected rather than implying a different queue policy.
- Inventory sorts by thickness/warehouse/ID; capabilities by stable catalog IDs and routing part_type/sequence/ID; capacity by week/ID. Quotes sort by created_at descending then quote_id, versions ascending by version/ID, lines by ordinal/ID, and requests by requested_at/ID. Sorting result tuples does not mutate ORM relationship collections.

## Session and authorization boundary

Use the existing application Database and one session per operation/task. Prefer a read-only Database for batches of reads. Functions use `session.no_autoflush`, including eager-loader execution, so a caller's pending ORM edits are not unexpectedly flushed even if autoflush=True. Caller-owned dirty objects can still affect identity-map representations; use a clean scoped session for authoritative reads. Functions never refresh/overwrite pending state or commit it.

```python
from rfq_copilot.repositories import get_customer, get_rfq


async def read_customer_inquiry(database, authorized_customer_id, rfq_id):
    async with database.session() as session:
        async with session.begin():
            customer = await get_customer(session, authorized_customer_id)
            inquiry = await get_rfq(
                session, rfq_id, customer_id=authorized_customer_id
            )
            return {
                "customer_found": customer is not None,
                "rfq_found": inquiry is not None,
                "historical_part_count": (
                    len(inquiry.historical_part_ids) if inquiry else 0
                ),
            }
```

The application supplies `database` through its existing infrastructure; there is no engine per query. Query values use SQLAlchemy bound parameters, with ORM joins and no caller SQL interpolation. Customer filters reduce accidental cross-customer reads, but do not authenticate or authorize the caller. Future backend services must verify actor identity, active role, permissions, assigned customer/region and safe response projection before exposing these internal results through FastAPI/MCP. A supplied sales_manager string does not grant approval authority. These repositories are not directly agent-facing tool contracts.

## Query-count verification and limits

Customer contacts use selectinload; RFQ/customer uses a scalar joinedload plus selectinload for runtime lines; manufacturing catalogs use batched eager loading; quotation versions and lines use nested selectinload. These strategies avoid multiplying root quotation rows and per-child queries. Only requested relationships are loaded; other navigation raises.

Real SQL instrumentation, including consumption of returned children, measured these SELECT counts for representative seeded results: customer 2, RFQ 2, inventory 2, manufacturing 6, capacity 2, quotation history 3, pending approvals 1. Tests verify these counts and parameter binding. All observed repository statements were SELECTs, with no INSERT/UPDATE/DELETE or commit. Local milliseconds in the execution evidence are observations on this small dataset, not production performance claims. Large-history pagination and deployment authorization belong to later requirements; no speculative cache/pagination/security service is added here.

## Executed development examples

Command:

```bash
.venv/bin/python -m rfq_copilot.repositories.demo --target development --env-file .env
```

The command reuses one Database/session, validates identity/head/schema, selects valid identifiers from current seeded records, runs all seven reads in a server-enforced read-only REPEATABLE READ transaction, tests missing IDs, compares full fingerprints, and closes the session/pool. Output contains only safe identifiers, counts and quantities/units, not customer names, contact details or credentials.

`repositories.demo.verify_query_examples(session, config)` exposes the same verification sequence to the integrated lifecycle test using its existing transaction. It performs no commit, write or session creation; `run_examples(config)` retains ownership of the read-only CLI resources. This allows real test seeding, audit and all seven queries to run together before the fixture rolls back. The [final Day 5 report](day_05_final_verification_report.md) records that complete lifecycle.

| Query | Actual input | Actual result | Status |
| --- | --- | --- | --- |
| Customer | CUST-001 | One customer, one associated contact | PASS |
| RFQ | HRFQ-0001, CUST-001 | Two historical part IDs, zero runtime lines | PASS |
| Material inventory | MAT-001, all availability | Six inventory rows, 385.300000 available kg | PASS |
| Manufacturing | MAT-001, flat_panel | Six operations, six workcentres, four routes, five finishes, six thicknesses | PASS |
| Capacity | WC-BND, 2026-11-09 through 2026-11-09 | One week, 15.0000 remaining hours | PASS |
| Quotation history | CUST-001, HRFQ-0001 | One quote, two versions, two priced lines; one older metadata-only version | PASS |
| Pending approvals | CUST-001, sales_manager | Zero pending requests | PASS |

All seven missing-ID checks passed. Read-only mode was on; the full database fingerprint was unchanged. The source-derived development state remained 1,089 rows across 33 tables at `20261108_01`.

## Tests and execution report

```bash
.venv/bin/python -m pytest tests/test_database_queries.py -q
.venv/bin/python -m pytest -q
```

The 45 focused query tests passed with zero failures/errors/skips in 8.03s. They cover existing/missing lookups, contacts and scope, historical/runtime distinction, inventory status/thickness/Decimal totals, stored compatibility/routing, capacity ranges/hours, historical quote versions/lines/ordering, empty and test-only nonempty pending queues, invalid inputs, parameter binding, no autoflush, bounded eager loading, read-only development preservation and session release.

The full regression result and final preservation evidence are recorded in [database_components_6_7_results.json](database_components_6_7_results.json) and appended below. Test setup reuses the existing guarded migration/seeding fixtures, with all new rows and mutations confined to rollback-only test transactions and the original test catalog restored.


## Component 7: Database Queries Verification Report

**Executed:** 2026-11-08 (America/New_York). **Status: COMPONENT 7 COMPLETE.** All seven operation/input/result/PASS rows above are actual development reads; all seven missing-ID checks passed.

- Module organization and all seven function signatures/return types are documented above. Async sessions come from the existing infrastructure; functions never create engines/sessions or commit.
- Eager-loading SELECT counts were verified as 2 / 2 / 2 / 6 / 2 / 3 / 1. Traversing requested child results caused no per-child queries. Root quotations remain unique and all historical versions remain present.
- Empty/missing results, customer mismatch, pending-role/status scoping, Decimal kg/hours/USD, deterministic ordering, bound parameters and no autoflush were tested. The real pending queue remains empty; nonempty behavior used only rollback-isolated test requests.
- Every observed read statement was SELECT. Server-enforced read-only mode was on for development. Full before/after SHA-256: `05f65a5515aae4c277f8e08a4437dcb7255772b69e19935fabb30902637aa6ee`. All 1,089 records and seven empty runtime tables were preserved.
- **Focused tests:** 45 passed, 0 failed/errors/skipped, 8.03s. **Full regression:** 593 passed, 0 failed/errors/skipped, 54.41s; all 468 prior tests still pass.
- **Files created/modified:** `src/rfq_copilot/repositories/{__init__,queries,results,demo}.py`, `tests/test_database_queries.py`, this guide, combined `database_components_6_7_results.json`, and the separate Component 7 entry in `docs/dev_log/day_05.md`.
- **Unresolved problems: none.** Internal filters are not a replacement for future authenticated service authorization. No endpoint/tool/agent/engine or Component 8 was implemented.

## Combined verification summary

| Suite | Passed | Failed | Errors | Skipped | Duration |
| --- | ---: | ---: | ---: | ---: | --- |
| Component 6 focused | 80 | 0 | 0 | 0 | 31.24s |
| Component 7 focused | 45 | 0 | 0 | 0 | 8.03s |
| Previous regression cases inside full run | 468 | 0 | 0 | 0 | 18.387s summed case time |
| Full regression | 593 | 0 | 0 | 0 | 54.41s elapsed pytest time |

Separate connections confirm **1,089 before / 1,089 after**, 33 tables, unchanged revision `20261108_01`, identical full fingerprints, unchanged seven empty runtime tables, no new migrations and no development reseeding/writes/deletes/duplicates. The test database is restored to its exact prior public-only catalog with zero user relations. All dataset/source/manifest checks pass without hidden-answer ingestion.

Ruff root lint, formatting of all 39 Python files, application/model imports, unchanged dependency lockfile and `git diff --check` passed. Exact initial/final Git statuses, full per-table fingerprints, query measurements and all final command exit codes are in [database_components_6_7_results.json](database_components_6_7_results.json). The initial dirty/untracked Components 1–5 are preserved. Credentials remain only in ignored/private settings; the final audit scans actual private URLs/passwords in-memory without displaying them. No staging, commit or push occurred.

**Final decision: COMPONENTS 6 AND 7 COMPLETE.** Stop before Component 8.
