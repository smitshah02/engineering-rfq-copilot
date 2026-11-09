# Day 5, Component 3 — SQLAlchemy ORM models

**Status:** COMPONENT 3 COMPLETE, verified 2026-11-08. Component 3 itself defines and tests ORM metadata only. Subsequent Day 5 components deployed the 33-table schema and imported 1,089 approved records; see the [final verification report](day_05_final_verification_report.md) for the current state. Component 3 verification counts below remain the original milestone evidence.

## Authority and scope

The complete approved Day 4 [architecture](../database/database_architecture.md), [table specifications](../database/table_schemas.md), [relationships/constraints/indexes](../database/relationships_and_constraints.md), [ER diagrams](../database/er_diagram.md) and [source mapping](../database/dataset_to_database_mapping.md) were read before implementation. A table-by-table checklist extracted their 33 tables, 261 columns, all defaults, keys, checks, relationships and indexes. The dataset contracts and earlier illustrative PostgreSQL schema were supporting evidence; their older table names/types did not replace the approved design.

Day 4 documents and the frozen dataset remain unchanged. There is no ORM table for scenarios, hidden labels, standalone master parts, multiple user roles or saver-owned checkpoint internals. Every implemented table is approved by Day 4. Earlier Day 5 log sections remain historical records of Components 1–2; the new Component 3 section records this implementation.

## Declarative architecture and organization

[`models/base.py`](../../src/rfq_copilot/models/base.py) defines the only DeclarativeBase and MetaData, with default schema business and a naming convention for primary, foreign, unique, check and index names. Every class uses SQLAlchemy 2.x Mapped annotations and mapped_column. The existing [async connection/session layer](database_connection.md) is reused unchanged; no new engine/session factory or dependency was added.

Import [`rfq_copilot.models`](../../src/rfq_copilot/models/__init__.py) to register every model in Base.metadata. MODEL_CLASSES is a static inventory of the 33 mappings. Importing any domain submodule first also initializes the package registry. Definitions are static Python; production model import reads no Markdown, dataset, environment file or credentials and performs no connection or physical DDL.

| Module under src/rfq_copilot/models | Mapped classes |
| --- | --- |
| `rules.py` | BusinessRuleVersion, DataSnapshot |
| `identity.py` | Role, Permission, RolePermission, User |
| `customers.py` | Customer, Contact |
| `manufacturing.py` | Material, Finish, Operation, Workcentre, Supplier, MaterialThicknessOption, MaterialFinishCompatibility, MaterialSupportedOperation, FinishMaterialCategory, OperationRouting |
| `supply.py` | MaterialInventory, ProcurementRecord, ProductionCapacity |
| `rfqs.py` | RFQ, RFQLineItem, RFQAttachment |
| `quotations.py` | PricingCalculation, Quote, QuoteVersion, QuoteLineItem |
| `approvals.py` | ApprovalRequest, ApprovalHistory |
| `reliability.py` | IdempotencyRecord, AuditEvent, WorkflowRun |

base.py provides the foundation; __init__.py imports/exports the domain classes. business holds 32 tables; orchestration holds workflow_runs only. A mapped workflow run stores orchestration metadata and an opaque checkpoint reference, not a physical LangGraph checkpoint schema. No authoritative business FK points into checkpoint storage.

## Complete model inventory

The relationships below include both the FK-side association and its inverse back_populates navigations. Class attribute names are ORM API choices; the database columns/relationships are unchanged.

| ORM class | Qualified database table | Primary key | Relationship attributes | Status |
| --- | --- | --- | --- | --- |
| BusinessRuleVersion | business.business_rule_versions | rule_version | materials, finishes, pricing_calculations_pricing, pricing_calculations_manufacturing, quote_versions_pricing, quote_versions_manufacturing, quote_versions_approval, quote_versions_workflow, approval_requests | PASS |
| DataSnapshot | business.data_snapshots | snapshot_id | inventory_records, capacity_records | PASS |
| Role | business.roles | role | grants, users, approval_requests | PASS |
| Permission | business.permissions | permission | role_grants | PASS |
| RolePermission | business.role_permissions | role, permission | role_record, permission_record | PASS |
| User | business.users | user_id | role_record, created_rfqs, pricing_calculations, created_quotes, submitted_quotes, approved_quotes, released_quotes, created_quote_versions, requested_approvals, resolved_approvals, approval_events, idempotency_records, audit_events, workflow_runs | PASS |
| Customer | business.customers | customer_id | contacts, rfqs, idempotency_records, audit_events, workflow_runs | PASS |
| Contact | business.contacts | contact_id | customer | PASS |
| Material | business.materials | material_id | pricing_rule, thickness_options, finish_compatibilities, supported_operations, rfq_lines | PASS |
| Finish | business.finishes | finish_id | pricing_rule, material_compatibilities, material_categories, rfq_lines | PASS |
| Operation | business.operations | operation_id | workcentres, material_support | PASS |
| Workcentre | business.workcentres | workcentre_id | operation, routes, capacity_records | PASS |
| Supplier | business.suppliers | supplier_id | inventory_records, procurements | PASS |
| MaterialThicknessOption | business.material_thickness_options | material_id, thickness_mm | material, inventory_records | PASS |
| MaterialFinishCompatibility | business.material_finish_compatibility | material_id, finish_id | material, finish | PASS |
| MaterialSupportedOperation | business.material_supported_operations | material_id, operation_id | material, operation | PASS |
| FinishMaterialCategory | business.finish_material_categories | finish_id, material_category | finish | PASS |
| OperationRouting | business.operation_routing | routing_id | workcentre | PASS |
| MaterialInventory | business.material_inventory | inventory_id | thickness_option, supplier, snapshot, procurements | PASS |
| ProcurementRecord | business.procurement_records | procurement_id | inventory, supplier | PASS |
| ProductionCapacity | business.production_capacity | capacity_id | workcentre, snapshot | PASS |
| RFQ | business.rfqs | rfq_id | customer, creator, line_items, attachments, pricing_calculations, quote, audit_events, workflow_runs | PASS |
| RFQLineItem | business.rfq_line_items | rfq_line_id | rfq, material, finish | PASS |
| RFQAttachment | business.rfq_attachments | attachment_id | rfq | PASS |
| PricingCalculation | business.pricing_calculations | calculation_id | rfq, creator, pricing_rule, manufacturing_rule, quote_versions | PASS |
| Quote | business.quotes | quote_id | rfq, creator, submitter, approver, releaser, current_version_record, approved_version_record, versions, audit_events | PASS |
| QuoteVersion | business.quote_versions | quote_version_id | current_for_quote, approved_for_quote, quote, creator, pricing_rule, manufacturing_rule, approval_rule, workflow_rule, calculation, line_items, approval_requests, approval_events, audit_events | PASS |
| QuoteLineItem | business.quote_line_items | quote_line_id | version_record | PASS |
| ApprovalRequest | business.approval_requests | approval_request_id | version_record, requester, resolver, required_role_record, approval_rule, history_events | PASS |
| ApprovalHistory | business.approval_history | approval_id | version_record, actor, request | PASS |
| IdempotencyRecord | business.idempotency_records | receipt_id | actor, customer | PASS |
| AuditEvent | business.audit_events | event_id | actor, customer, rfq, quote, version_record | PASS |
| WorkflowRun | orchestration.workflow_runs | workflow_run_id | customer, rfq, initiator | PASS |

## Types, defaults and common conventions

- Legacy entity IDs remain TEXT with nonblank/1–64-character business-ID checks. User-FK aliases such as created_by/requested_by also obey that identity bound. Native UUID is used only for the four approved technical/RFQ-line keys, with application uuid4 callables. No legacy ID is converted and no default is invented for application-supplied TEXT IDs.
- Role, permission, rule-version, graph/dataset-version and correlation tokens remain nonblank TEXT. Opaque authentication subjects/correlation/external workflow references are not arbitrarily capped to 64 characters. Registry content-addressed rule-version keys can exceed that business-ID bound. Existing required hash fields use their documented lowercase 64-hex shape. The idempotency-key gateway limit of 256 remains a gateway rule rather than an invented SQL length CHECK.
- Money/dimensions/weights/hours use the exact approved Numeric precision/scale with asdecimal=True and Python Decimal. Every NUMERIC column has the approved finite-value CHECK. No floating-point currency type is present.
- Calendar dates use Date; events use DateTime(timezone=True). The existing connection layer supplies UTC sessions. Backend/import validation must still supply aware instants; mapped constructors do not certify timezone correctness or business chronology.
- PostgreSQL JSONB uses none_as_null=True so Python None means SQL NULL for missing nullable historical facts. Array/object shape CHECKs are explicit. Nested evidence/cost Decimal strings are preserved; Part contents, registry kinds, snapshot validity and authoritative results still require service validation.
- Documented SQL defaults are server_default expressions; Python uuid4 defaults run at insertion. Ordinary DeclarativeBase constructors do not automatically evaluate insertion defaults. No arbitrary empty, zero or current-time replacement is supplied for missing historical facts.
- Inventory available_kg and capacity available_hours/remaining_hours/utilization_fraction are Computed expressions over their documented base fields. STORED is the explicit SQLAlchemy physical-storage choice for the approved generated values; Day 4 specified the expressions but not stored versus virtual persistence. It changes no formula or business constraint. Expressions do not chain through another generated column.
- updated_at has only its documented insertion default. No automatic onupdate, implicit quote-version bump, mapper version generator, event listener, business-rule override or inferred historical snapshot was added. row_version remains the service-owned optimistic-concurrency counter.

Constructors accept transient typed values and relationships; they do **not** execute or enforce PostgreSQL CHECK/FK/NOT NULL constraints before tables exist. Tests distinguish typed/source fixture construction from database-level enforcement. Standard JSONB containers have no speculative mutable-tracking extension; immutable snapshots and authorized modifications remain backend responsibilities.

## Relationships and referential behavior

Every one of the 64 approved FKs has an explicit bidirectional relationship: **128 relationship properties** in total. Explicit primaryjoin and foreign_keys resolve multiple user roles, composite references and quote/version cycles without ambiguous paths or SQLAlchemy warnings. RFQ→quotation-family is optional one-to-one via the approved UNIQUE(rfq_id); quotation→versions and version→lines/events/requests are collections. Normalized membership models are association entities, not extra secondary tables.

All relationships use lazy=raise to prevent accidental implicit I/O in asynchronous code. A service must request eager loading, explicitly refresh a relationship, or use already-populated in-memory associations. See [SQLAlchemy relationship joins/overlaps](https://docs.sqlalchemy.org/en/21/orm/join_conditions.html) and [async loading guidance](https://docs.sqlalchemy.org/en/21/orm/extensions/asyncio.html).

Writable navigations use save-update/merge only; no delete/delete-orphan cascade is configured. Parent inverses use passive_deletes=all, so the ORM does not null child keys to circumvent database RESTRICT behavior. Every FK separately declares ON DELETE RESTRICT, ON UPDATE RESTRICT and MATCH SIMPLE. Relationship configuration never replaces a database FK.

Four edge pairs are deliberately viewonly on both directions:

| Association | Reason |
| --- | --- |
| quotes.current_version_record ↔ quote_versions.current_for_quote | Header pointer includes quote_id; navigation assignment must not rewrite the quotation PK or create a persistence cycle. |
| quotes.approved_version_record ↔ quote_versions.approved_for_quote | Exact approved revision remains an explicit service-owned header field. |
| approval_history.request ↔ approval_requests.history_events | The optional request triple shares quote_id/quote_version with the required exact-version FK. The request must not overwrite that version lineage. |
| audit_events.version_record ↔ quote_versions.audit_events | The optional version FK shares quote_id with the independent quote-header association. |

viewonly is a navigation choice, **not removal or relaxation of a database FK**. Set the documented primitive pointer/request fields in the trusted transaction service. Read-only associations do not synchronize in-memory inverse lists on assignment. All other FK associations have ordinary back_populates synchronization. Inventory material ownership is represented by inventory.thickness_option.material; routing operation ownership by routing.workcentre.operation. No unsupported extra direct material/workcentre/operation FK was added.

The two header→version FKs remain DEFERRABLE INITIALLY DEFERRED. They use use_alter=True for SQLAlchemy DDL ordering to break the structural cycle with quote_versions.quote_id→quotes. Named AddConstraint compilation includes both exact header pointers; ordinary CreateTable compilation intentionally omits those two until the separate constraint step. This is metadata ordering only, not an executed migration or post_update workaround.

## Constraint and index inventory

| Object category | Implemented metadata |
| --- | --- |
| Tables / columns | 33 / 261 |
| Primary keys | 33, including approved composite junction keys |
| Foreign keys | 64, including composites and the two deferred pointers |
| Additional nonpartial UNIQUE constraints | 14 |
| Table-specific approved CHECK expressions | 65 |
| Shared/column convention CHECK expressions | 142 |
| Total CHECK constraints | 207 |
| Explicit B-tree indexes | 14, including seven partial indexes |
| Bidirectional ORM relationship properties | 128 |

The three partial unique indexes enforce primary contacts, current-version flags and pending review uniqueness. The remaining explicit indexes preserve exact names, column order (including submitted_at DESC), predicates and uniqueness from Day 4. No GIN/redundant standalone PK/UNIQUE indexes were added. PostgreSQL will supply indexes backing the 33 PK and 14 UNIQUE constraints when future migrations apply them; none exist physically today for these model tables.

All constraints have stable naming-convention names. The 65 table-specific checks retain document order as rule_01, rule_02, etc.; common checks use column/purpose labels. SQLAlchemy's convention handles PostgreSQL identifier truncation. See [constraints/naming](https://docs.sqlalchemy.org/en/21/core/constraints.html) and [server/computed defaults](https://docs.sqlalchemy.org/en/21/core/defaults.html).

Cross-table totals, exact-one-current-flag consistency, valid policy/snapshot kinds, immutable historical business records, row locks/CAS, RBAC/customer isolation and human-only approval/release remain the approved service/gateway/configuration rules. ORM metadata does not certify a human decision, allow an agent to release a quote, fix MFG-001, or turn a checkpoint into a committed business operation.

## Source reconciliation and nonblocking details

The approved Day 4 table list, columns, key/check/index semantics, nullability and defaults matched across the table specifications, FK/index catalog and all seven ER views. Earlier dataset table names (historical_rfqs/inventory/capacity/quote_lines) and generic money precision did not replace the approved rfqs/material_inventory/production_capacity/quote_line_items and exact final/intermediate Numeric scales. Historical metadata-only versions keep nullable prices/rule/snapshot fields; no old prices or queue events are fabricated.

One nonblocking documentation metadata discrepancy was recorded: the business_constraints.json row in [Day 4 source mapping](../database/dataset_to_database_mapping.md) labels its source version as properties.schema_version.const=1.1. The [actual contract](../../data/rfq_synthetic_data/contracts/business_constraints.json) has neither that properties tree nor a standalone version. The Day 4 architecture already provides kind@sha256 content addressing for versionless contracts. No ORM column/default or seeding logic depends on the mistaken label, so no schema decision was required and neither approved documentation nor dataset was changed. Future importer work must use actual source metadata/content addressing and explicitly resolve that narrative label.

Other preexisting dependencies (MFG-001, fixture-only handoff ID format, ingestion/delegation and saver selection) remain as recorded in Day 4. They do not justify speculative ORM tables or business-policy changes. No blocking schema contradiction was found.

## Offline metadata verification and future Alembic discovery

Future migration configuration can import the already registered metadata:

```python
from rfq_copilot.models import Base

target_metadata = Base.metadata
```

That is an illustration for Component 4, **not Alembic infrastructure initialized here**. Future migration work must account for both named schemas (include_schemas=True with appropriate scope filtering) and create the schemas/tables/constraints in a reviewed migration. No Alembic dependency, configuration, revision or migration directory was added.

Offline inspection compiles CreateTable for all 33 tables, AddConstraint for the two use_alter FKs, and CreateIndex for all 14 indexes with the PostgreSQL dialect. Compilation produces strings only; no engine executes those statements. A guarded fresh-process import blocks engine construction, connections, socket connections and physical table creation and still registers/configures all models successfully.

## Executed verification

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m pytest tests/test_orm_models.py -q
.venv/bin/python -m pytest -q
```

- Dedicated ORM suite: **241 passed**, zero failures/errors/skips, 0.40 seconds.
- Full suite: **274 passed**, zero failures/errors/skips, 5.57 seconds; includes all **33 existing Component 2 tests**.
- Models/mappers: 33 classes and tables; all 261 columns, 64 FKs, 14 UNIQUE constraints, 207 checks, defaults, computed values, nullability and 14 indexes matched independent expectations parsed from the immutable Day 4 documents. SQLAlchemy warnings are errors during mapper configuration.
- Metadata-only construction covers every model plus actual CUST-001/HQUOTE-0001 source fixtures, Decimal values, JSON strings and in-memory relationship synchronization. Source fixtures were read, never imported into PostgreSQL.
- The unchanged async session factory accepts pending mapped objects and rolls them back without opening a connection. Import and compilation require no PostgreSQL access.
- Read-only PostgreSQL catalog snapshots before/after the full suite matched: both development and test return SELECT 1=1 as the restricted user, have only the public schema, and contain **zero user relations**. Neither business nor orchestration was created physically.

[Day 5 log](../dev_log/day_05.md) records actual commands, exit codes, test XML evidence, database preservation and final repository checks. Physical constraint enforcement and persisted ORM round trips deliberately wait for later migration/test components. Offline compilation alone is not a claim that PostgreSQL has enforced these constraints.
