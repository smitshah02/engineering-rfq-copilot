# Repository reads and query boundaries

The internal `rfq_copilot.repositories` package implements seven async read operations over the existing ORM. It contains `queries.py` (bound SQLAlchemy queries and input validation), `results.py` (typed result wrappers), `__init__.py` (public exports), and `demo.py` (a read-only execution/verifier command). These are repository-level reads; authenticated API/MCP adapters and business engines are separate layers. For current capabilities, see the [project overview](../../README.md).

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

## Read-only examples and verification

```bash
.venv/bin/python -m rfq_copilot.repositories.demo --target development --env-file .env
```

The example runner validates target identity/schema and runs representative queries inside
one read-only repeatable-read transaction, comparing state before/after. Its safe summary
contains identifiers, counts and quantities, not credentials or customer contact details.
`verify_query_examples` also supports the guarded integration fixture without committing.

`tests/test_database_queries.py` covers lookups, customer mismatch, ordering, exact decimals,
loading/query counts and rollback-isolated fixtures. Historical input/output tables and
execution receipts are retained privately. See the [current verification scope](../../README.md#verification).
No command was executed for this documentation edit.

## Parts and rule versions

Two reads extend the same pattern (bound identifiers, `raiseload("*")`, `no_autoflush`, caller-owned session, no commit):

- `get_rfq_parts(session, rfq_id, *, customer_id)` delegates to `get_rfq` and converts runtime `rfq_line_items` to immutable `rfq_copilot.entities.Part` objects in `line_number` order. Returns `None` for a missing or out-of-scope RFQ, `()` for a historical RFQ (no runtime lines). Two SELECTs, same as `get_rfq`.
- `get_rule_version(session, rule_kind, rule_version, *, expected_sha256=None)` reads one `business.business_rule_versions` row in one SELECT. It raises `RuleIntegrityError` when the stored kind differs, the stored sha256 is malformed, the caller's pin differs, a versioned document declares another version, or a hash-named rule's name no longer matches its stored hash. The document is parsed from PostgreSQL's jsonb text with `parse_float=Decimal` and returned deeply read-only. `fingerprint` is sha256 of that canonical jsonb text and can be recomputed at any time; `sha256` is the seeded source file's hash (provenance), which JSONB storage cannot reproduce.

`Part` checks types only (exact Decimal at the `Numeric(12, 3)` scale, strict int, nonblank ≤64-character identifiers, no unknown keys, no floats). Manufacturing ranges and missing-field findings belong to the manufacturing validator.

## Material lookup

- `find_materials(session, *, grade=None, category=None, thickness_mm=None, finish_id=None)` returns the material IDs matching every given filter, ordered by ID, in one SELECT. `grade` matches case-insensitively after trimming; `thickness_mm` (exact `Decimal`, compared numerically) and `finish_id` are `EXISTS` checks against `material_thickness_options` and `material_finish_compatibility`. Unknown values return `()`. Arguments are validated before any SQL; the thickness rule is the shared `_thickness` helper also used by `get_material_availability`.

The decisions built on it (supported thickness, finish compatibility, findings) live in `rfq_copilot.services.materials`, not in the repository layer.

## Part routing

- `list_part_routing(session, part_type)` returns the stored `business.operation_routing` steps for one part type, ordered by `sequence` then `routing_id`, in one SELECT. Unknown part types return `()`. Unlike the routing inside `get_manufacturing_capabilities`, it is not filtered by a material's supported operations, so the validator can detect a routed operation the material does not support.

## Material catalog rows for costing

- `list_materials(session, material_ids)` returns the `business.materials` rows for the distinct given IDs, ordered by `material_id`, in one SELECT (`raiseload("*")`, `no_autoflush`). An empty list issues no SQL; unknown IDs are simply absent. It is the only service read of `cost_usd_per_kg`; the material catalog `MaterialSpec` deliberately excludes cost. Costing checks each row's `pricing_rule_version` against the pinned pricing rule.

## Operation catalog rows for pricing

- `list_operations(session, operation_ids)` returns the `business.operations` rows for the distinct given IDs, ordered by `operation_id`, in one SELECT. An empty list issues no SQL. Pricing uses `is_quoted` to refuse any routed operation that is catalog-only (TD-001).
