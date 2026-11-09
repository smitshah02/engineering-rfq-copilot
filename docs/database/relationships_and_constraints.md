# Day 4 — Relationships, constraints and query indexes

**Proposed design only; no DDL or query execution against PostgreSQL occurred.** Column-level authority is [table specifications](table_schemas.md). Relationships here and in the [ER diagrams](er_diagram.md) derive from the same logical catalog. Source-to-target/import authority is [mapping](dataset_to_database_mapping.md).

## 1. Primary and alternate keys

All PKs are unique, non-null and immutable. Business strings preserve source identifiers. Junction PKs prevent duplicate memberships. Names below denote `business` tables except `orchestration.workflow_runs`.

| Table | Primary key | Additional nonpartial UNIQUE keys |
| --- | --- | --- |
| `business_rule_versions` | `(rule_version)` | `(rule_kind, sha256)` |
| `data_snapshots` | `(snapshot_id)` | `(snapshot_kind, sha256)` |
| `roles` | `(role)` | — |
| `permissions` | `(permission)` | — |
| `role_permissions` | `(role, permission)` | — |
| `users` | `(user_id)` | `(authentication_subject)` |
| `customers` | `(customer_id)` | — |
| `contacts` | `(contact_id)` | — |
| `materials` | `(material_id)` | — |
| `finishes` | `(finish_id)` | — |
| `operations` | `(operation_id)` | — |
| `workcentres` | `(workcentre_id)` | `(workcentre_id, operation_id)` |
| `suppliers` | `(supplier_id)` | — |
| `material_thickness_options` | `(material_id, thickness_mm)` | — |
| `material_finish_compatibility` | `(material_id, finish_id)` | — |
| `material_supported_operations` | `(material_id, operation_id)` | — |
| `finish_material_categories` | `(finish_id, material_category)` | — |
| `operation_routing` | `(routing_id)` | `(part_type, sequence)` |
| `material_inventory` | `(inventory_id)` | — |
| `procurement_records` | `(procurement_id)` | — |
| `production_capacity` | `(capacity_id)` | `(workcentre_id, week_start)` |
| `rfqs` | `(rfq_id)` | — |
| `rfq_line_items` | `(rfq_line_id)` | `(rfq_id, line_number)`; `(rfq_id, part_id)` |
| `rfq_attachments` | `(attachment_id)` | — |
| `pricing_calculations` | `(calculation_id)` | — |
| `quotes` | `(quote_id)` | `(rfq_id)` |
| `quote_versions` | `(quote_version_id)` | `(quote_id, version)` |
| `quote_line_items` | `(quote_line_id)` | `(quote_id, quote_version, line_number)` |
| `approval_requests` | `(approval_request_id)` | `(approval_request_id, quote_id, quote_version)` |
| `approval_history` | `(approval_id)` | — |
| `idempotency_records` | `(receipt_id)` | `(actor_id, customer_id, operation, idempotency_key)` |
| `audit_events` | `(event_id)` | — |
| `workflow_runs` | `(workflow_run_id)` | `(correlation_id)` |

The workcentres(workcentre_id,operation_id) and approval_requests(approval_request_id,quote_id,quote_version) unique keys contain a source PK but are required nonpartial composite FK targets. They are intentional extra indexes; no further equivalent indexes should be added. UNIQUE(quotes.rfq_id) is the explicit one-family proposal, not a claimed source business rule.

## 2. Complete FK and cardinality catalog

Notation is parent per child; every parent may have zero children unless a separate service/current-pointer invariant requires more. “0..1 child” means the child FK contains a complete unique key. All FKs are RESTRICT on update/delete and MATCH SIMPLE; nullability is column-specific. A composite is a single relationship. Optional references with some mandatory components skip FK matching only when at least one component is NULL.

| Child FK | Parent key | Target support | Parents per child | Children per parent | Timing |
| --- | --- | --- | --- | --- | --- |
| `role_permissions(role)` | `roles(role)` | PK | 1 | 0..N | Immediate |
| `role_permissions(permission)` | `permissions(permission)` | PK | 1 | 0..N | Immediate |
| `users(role)` | `roles(role)` | PK | 1 | 0..N | Immediate |
| `contacts(customer_id)` | `customers(customer_id)` | PK | 1 | 0..N | Immediate |
| `materials(pricing_rule_version)` | `business_rule_versions(rule_version)` | PK | 1 | 0..N | Immediate |
| `finishes(pricing_rule_version)` | `business_rule_versions(rule_version)` | PK | 1 | 0..N | Immediate |
| `workcentres(operation_id)` | `operations(operation_id)` | PK | 1 | 0..N | Immediate |
| `material_thickness_options(material_id)` | `materials(material_id)` | PK | 1 | 0..N | Immediate |
| `material_finish_compatibility(material_id)` | `materials(material_id)` | PK | 1 | 0..N | Immediate |
| `material_finish_compatibility(finish_id)` | `finishes(finish_id)` | PK | 1 | 0..N | Immediate |
| `material_supported_operations(material_id)` | `materials(material_id)` | PK | 1 | 0..N | Immediate |
| `material_supported_operations(operation_id)` | `operations(operation_id)` | PK | 1 | 0..N | Immediate |
| `finish_material_categories(finish_id)` | `finishes(finish_id)` | PK | 1 | 0..N | Immediate |
| `operation_routing(workcentre_id, operation_id)` | `workcentres(workcentre_id, operation_id)` | nonpartial UNIQUE | 1 | 0..N | Immediate |
| `material_inventory(material_id, thickness_mm)` | `material_thickness_options(material_id, thickness_mm)` | PK | 1 | 0..N | Immediate |
| `material_inventory(supplier_id)` | `suppliers(supplier_id)` | PK | 1 | 0..N | Immediate |
| `material_inventory(snapshot_id)` | `data_snapshots(snapshot_id)` | PK | 1 | 0..N | Immediate |
| `procurement_records(inventory_id)` | `material_inventory(inventory_id)` | PK | 1 | 0..N | Immediate |
| `procurement_records(supplier_id)` | `suppliers(supplier_id)` | PK | 1 | 0..N | Immediate |
| `production_capacity(workcentre_id)` | `workcentres(workcentre_id)` | PK | 1 | 0..N | Immediate |
| `production_capacity(snapshot_id)` | `data_snapshots(snapshot_id)` | PK | 1 | 0..N | Immediate |
| `rfqs(customer_id)` | `customers(customer_id)` | PK | 1 | 0..N | Immediate |
| `rfqs(created_by)` | `users(user_id)` | PK | 0..1 | 0..N | Immediate |
| `rfq_line_items(rfq_id)` | `rfqs(rfq_id)` | PK | 1 | 0..N | Immediate |
| `rfq_line_items(material_id)` | `materials(material_id)` | PK | 0..1 | 0..N | Immediate |
| `rfq_line_items(finish_id)` | `finishes(finish_id)` | PK | 0..1 | 0..N | Immediate |
| `rfq_attachments(rfq_id)` | `rfqs(rfq_id)` | PK | 1 | 0..N | Immediate |
| `pricing_calculations(rfq_id)` | `rfqs(rfq_id)` | PK | 1 | 0..N | Immediate |
| `pricing_calculations(created_by)` | `users(user_id)` | PK | 1 | 0..N | Immediate |
| `pricing_calculations(pricing_rule_version)` | `business_rule_versions(rule_version)` | PK | 1 | 0..N | Immediate |
| `pricing_calculations(manufacturing_rule_version)` | `business_rule_versions(rule_version)` | PK | 1 | 0..N | Immediate |
| `quotes(rfq_id)` | `rfqs(rfq_id)` | PK | 1 | 0..1 | Immediate |
| `quotes(created_by)` | `users(user_id)` | PK | 1 | 0..N | Immediate |
| `quotes(submitted_by)` | `users(user_id)` | PK | 0..1 | 0..N | Immediate |
| `quotes(approver_id)` | `users(user_id)` | PK | 0..1 | 0..N | Immediate |
| `quotes(released_by)` | `users(user_id)` | PK | 0..1 | 0..N | Immediate |
| `quotes(quote_id, current_version)` | `quote_versions(quote_id, version)` | nonpartial UNIQUE | 1 | 0..1 | Initially deferred |
| `quotes(quote_id, approved_version)` | `quote_versions(quote_id, version)` | nonpartial UNIQUE | 0..1 | 0..1 | Initially deferred |
| `quote_versions(quote_id)` | `quotes(quote_id)` | PK | 1 | 0..N | Immediate |
| `quote_versions(created_by)` | `users(user_id)` | PK | 1 | 0..N | Immediate |
| `quote_versions(pricing_rule_version)` | `business_rule_versions(rule_version)` | PK | 0..1 | 0..N | Immediate |
| `quote_versions(manufacturing_rule_version)` | `business_rule_versions(rule_version)` | PK | 0..1 | 0..N | Immediate |
| `quote_versions(approval_rule_version)` | `business_rule_versions(rule_version)` | PK | 0..1 | 0..N | Immediate |
| `quote_versions(workflow_rule_version)` | `business_rule_versions(rule_version)` | PK | 0..1 | 0..N | Immediate |
| `quote_versions(calculation_id)` | `pricing_calculations(calculation_id)` | PK | 0..1 | 0..N | Immediate |
| `quote_line_items(quote_id, quote_version)` | `quote_versions(quote_id, version)` | nonpartial UNIQUE | 1 | 0..N | Immediate |
| `approval_requests(quote_id, quote_version)` | `quote_versions(quote_id, version)` | nonpartial UNIQUE | 1 | 0..N | Immediate |
| `approval_requests(requested_by)` | `users(user_id)` | PK | 1 | 0..N | Immediate |
| `approval_requests(resolved_by)` | `users(user_id)` | PK | 0..1 | 0..N | Immediate |
| `approval_requests(required_role)` | `roles(role)` | PK | 1 | 0..N | Immediate |
| `approval_requests(approval_rule_version)` | `business_rule_versions(rule_version)` | PK | 1 | 0..N | Immediate |
| `approval_history(quote_id, quote_version)` | `quote_versions(quote_id, version)` | nonpartial UNIQUE | 1 | 0..N | Immediate |
| `approval_history(actor_id)` | `users(user_id)` | PK | 1 | 0..N | Immediate |
| `approval_history(approval_request_id, quote_id, quote_version)` | `approval_requests(approval_request_id, quote_id, quote_version)` | nonpartial UNIQUE | 0..1 | 0..N | Immediate |
| `idempotency_records(actor_id)` | `users(user_id)` | PK | 1 | 0..N | Immediate |
| `idempotency_records(customer_id)` | `customers(customer_id)` | PK | 1 | 0..N | Immediate |
| `audit_events(actor_id)` | `users(user_id)` | PK | 0..1 | 0..N | Immediate |
| `audit_events(customer_id)` | `customers(customer_id)` | PK | 0..1 | 0..N | Immediate |
| `audit_events(rfq_id)` | `rfqs(rfq_id)` | PK | 0..1 | 0..N | Immediate |
| `audit_events(quote_id)` | `quotes(quote_id)` | PK | 0..1 | 0..N | Immediate |
| `audit_events(quote_id, quote_version)` | `quote_versions(quote_id, version)` | nonpartial UNIQUE | 0..1 | 0..N | Immediate |
| `workflow_runs(customer_id)` | `customers(customer_id)` | PK | 1 | 0..N | Immediate |
| `workflow_runs(rfq_id)` | `rfqs(rfq_id)` | PK | 0..1 | 0..N | Immediate |
| `workflow_runs(initiated_by)` | `users(user_id)` | PK | 1 | 0..N | Immediate |

Important composite targets: inventory(material_id,thickness_mm)→material_thickness_options PK; route(workcentre_id,operation_id)→workcentres unique pair; quote pointers and versioned lines/requests/events/audit→quote_versions unique pair; optional event request reference→approval_requests unique triple. A quote line's part_id has no global part-master FK. Source legacy RFQ part_ids remain opaque JSONB references.

## 3. Four enforcement responsibilities

**A — PostgreSQL declarative:** row-local type, NOT NULL, PK/UNIQUE/FK/CHECK, generated expressions and partial unique index. **B — transactional backend service:** multi-row/cross-table invariants and state/side-effect coordination. **C — secure MCP/authentication gateway:** actor/delegation/capability, contract validation, scope and human/agent boundary; services recheck. **D — immutable versioned configuration:** authoritative supported manufacturing limits, prices, approval thresholds and transition graph, evaluated by B. A row CHECK cannot query another table or prove a real human authorized an action. [PostgreSQL constraint documentation](https://www.postgresql.org/docs/18/ddl-constraints.html) supports this separation and the nonpartial unique target requirement.

| Integrity rule | A: declarative | B: business transaction | C: identity/gateway | D: pinned rule |
| --- | --- | --- | --- | --- |
| Primary IDs / duplicate associations | PKs; complete unique keys; nonblank identity checks | Reject unknown/blank IDs; preserve legacy spelling | Never accept claimed actor identity from payload | Entity contract version |
| Material/supplier/RFQ references | Listed FKs, including supported inventory thickness | Resolve all referenced scopes/kinds; unknown raw input remains evidence | Customer/region isolation before revealing objects | Catalog/config snapshot version |
| Positive dimensions and quantities | Typed dimensions >0; quantity >0; holes >=0; nullable extraction allowed | Require complete applicable fields before validating/pricing; nested Part validation | Argument schema/size/unit limits | mfg-v1.0 bounds, required height/weld, max holes/bends |
| Finish/category/operation support | Junction FKs and composite PKs | Check explicit finish pair AND category; operation path eligibility | Engineering tool role allow-list | Manufacturing/routing/catalog versions; catalog-only punch/machine not activated |
| Money / total agreement | Finite NUMERIC; totals/rates >=0; USD currency | Use trusted Decimal calculator; sum rounded line totals; validate nested IDs/quantities/totals | Reject output tampering/price override | price-v1.0 exact rounding/margin; no tax/freight/shipping |
| Inventory reservation arithmetic | 0 <= reserved_kg <= quantity_kg; available generated | Future authorized stock update locks/CAS affected rows and publishes a new verified snapshot; no booking today | No exposed tool reserves/updates stock | Pinned inventory snapshot; policy defines future booking |
| Capacity scheduling arithmetic | 0 <= scheduled <= nominal-downtime; generated remainder/utilization; Monday week | Future authorized booking locks rows in stable order; current tools only estimate | No capacity write tool | Pinned capacity series and scheduling assumptions |
| Routing consistency | Composite FK workcentre+operation; unique part_type+sequence | Select required_when from trusted input; rework/parallel alternatives require new design | Catalog change authority separate from agents | Pinned serial route semantics |
| RFQ/quotation creation | UNIQUE(quotes.rfq_id); scoped receipt UNIQUE | Lock RFQ; create exactly one header/version/lines/receipt; no half-created drafts | Authenticated actor + matching write grant/tool role + customer | Draft requires supported complete spec and feasible requested delivery |
| Version integrity/current state | UNIQUE(quote_id,version); exact-version FKs; partial unique current flag; header current FK | Exactly one current flag equals pointer at commit; flags changed atomically; nonempty complete lines for live versions | Expected version cannot grant authority | Version bump resets approval; workflow transition graph |
| Stale approval prevention | approved_version NULL or equals current_version; complete approved summary; creator differs | Lock header/current version/request; compare submitted version and row_version; reject obsolete queue events | Authenticated independent human sales_manager; no AI approval/release tool | approval-v1.1 + pinned workflow; threshold >=7500 additional review |
| Historical approval references | Composite event/version FK; nullable request FK triple | Preserve historical facts; do not generate missing past requests or infer past access | Historical actor reference is not current authorization | Historical policy unknown stays unknown |
| Current approval and release | Valid status vocabulary; release actor/time pair; exact approved-version FK | Read current version/event evidence; require approved state, complete live snapshot, active customer and verified commercial clearance; record release atomically | Human session + grant; independent approver; admin no bypass | human_release_approval_required=true for every release |
| Duplicate pending approvals | Partial UNIQUE(quote_id,quote_version) WHERE pending | Lock header and request; return/reconcile stable operation outcome, never duplicate queue | Authorized request_approval grant; scoped operation key | Quote and approval policy pin |
| Idempotency replay | UNIQUE(actor_id,customer_id,operation,idempotency_key) | Digest compare; result/receipt/business/audit same commit; no earlier committed receipt | Stable server logical key across delegation/resume; internal reconciliation only | max_autonomous_write_retry_count=0 |
| Statuses and transitions | CHECK vocabulary in every state-bearing table | Check old→new transition under row lock and CAS; invalid transitions fail | No direct agent status updates | workflow-v1.0 exact graph; RFQ/run statuses are separate application vocabularies |
| Rule/snapshot versions | FK version/snapshot existence; unique kind/hash | Verify FK kind, snapshot applicability and hashes; never update pinned evidence | Backend-only artifact refs/credentials | Immutable config and contracts; no thresholds duplicated in CHECKs |
| Evaluation and cross-customer isolation | No evaluation tables in proposed schema; normal FKs do not provide tenant isolation | Customer-chain validation for every nested object/calculation/handoff | Separate storage/credentials; agent cannot query hidden labels | Harness controls split exposure; labels scorer-only |

Common finite/type/nonempty and local CHECKs are exhaustively listed with each table. No CHECK claims to validate cross-table price aggregates, role membership, current-flag/pointer agreement, actual commercial clearance or authenticated humanity. Cross-table concurrency/security depends on the specified backend credential boundary and transaction protocol, not unrestricted SQL clients. CHECKs are necessary defense in depth, not standalone approval authorization.

### Configured quote transition graph (exact source)

| From | Allowed next states |
| --- | --- |
| draft | revised_draft, pending_approval, clarification_required, blocked |
| revised_draft | pending_approval, clarification_required, blocked |
| pending_approval | approved, rejected, revised_draft |
| approved | released, expired, revised_draft |
| rejected | revised_draft |
| clarification_required | revised_draft |
| blocked | revised_draft |
| released | expired |
| expired | (terminal) |

`released` is supported by config although no historical header currently has that state. RFQ intake statuses and orchestration statuses are explicitly application proposals and do not reuse evaluation expected_outcome labels. Membership in the quotation CHECK includes all configured states, but a service must also permit the specific edge.

## 4. Transaction protocols and concurrency

Use a normal PostgreSQL transaction and row locks, with narrowly scoped optimistic checks. Lock known customer/identity authorization context as needed, then RFQ, quote header, exact version and review request in that order; future material/capacity mutations sort rows by immutable ID. Every service taking these locks follows the same order. [PostgreSQL row-lock semantics](https://www.postgresql.org/docs/18/explicit-locking.html) support serialization of competing writers; deadlocks/serialization failures require receipt reconciliation and policy-governed recovery, not agent-controlled retries.

**Create RFQ:** authenticate a valid scoped actor and create_rfq grant; validate the logical operation key and canonical hash. Within one transaction, claim the unique receipt tuple, create a draft RFQ shell with backend-selected ID/customer/source_type and server time, append success audit and record the exact minimal reply. No raw_request, parts or attachments are invented: the two-field tool input cannot supply them. Rollback removes all effects. A concurrent equal receipt tuple waits for the winner, compares the committed request digest, rechecks access and returns its original reply; hash mismatch is CONFLICT.

**Create quotation draft:** the documented create-only interpretation accepts expected_version=0. Under an RFQ row lock, reject any existing quotation family unless replaying a committed matching receipt. Reload the scoped trusted pricing_calculations row, compare RFQ/customer/validated requirement fingerprint and immutable engineering/pricing/snapshot pins, and recalculate/revalidate schedule feasibility if stale. Create header current_version=1, complete revision and nonempty immutable lines; verify total equals sum of rounded lines. Set is_current=true/approval_valid=false and unapproved header fields. Save receipt/audit with all business effects in one transaction. The deferred header pointer resolves at commit. UNIQUE(rfq_id) also blocks duplicate creation with different actor/key. Expired/missing/mismatched calculations fail without writing a partial draft. A calculation caches evidence but does not authorize creation.

**Edit/revise (later authenticated service, not one of the 14 tools):** lock customer/RFQ/header/current revision. Compare both expected quote version and expected BIGINT row_version. A status-only update increments row_version too, so same-version concurrent approval/review changes cannot be silently lost. Check the configured transition before creating a new revision; create its lines/snapshots atomically, switch header pointer/current flags, clear approved_version/approver_id/approval_timestamp/rejection summary, invalidate prior approval_valid, supersede pending requests with actor/time, increment row_version and audit/receipt. Never mutate past priced fields or reuse the old human approval. The frozen create_quote_draft output constant draft does not authorize a revised_draft edit operation.

**Request approval:** authenticate role/grant; lock and compare requested quote_version to header.current_version. Reject version=0, incomplete/noncurrent/invalid draft or stale source evidence; require configured transition into pending_approval. Insert one pending queue row with pinned approval policy and required_role=sales_manager, append submitted event, set submitted_by/status, increment row_version, and commit receipt/audit in the same transaction. Partial uniqueness prevents duplicate pending items across callers. A prior identical receipt replays its original response; a separate command with an already-pending request is a scoped CONFLICT, not another item.

**Human approve/reject:** outside the agent tool set, authenticate an active human sales_manager with the matching permission and customer/region scope. Lock header/current version/request and compare expected version + row_version again. Approval requires pending_approval, exact pending request version, independent approver (`actor_id <> quotes.created_by`) and validated policy/price/spec evidence. Set exact approved summary and approval_valid, resolve request, append immutable event, update state/row_version and commit human-operation receipt/success audit. Rejection clears any current approval and preserves reason. A UI decision from an old version fails CONFLICT, even if it was loaded before a concurrent revision. An expired or superseded request cannot approve a newer version.

**Human release:** again outside MCP, lock customer/RFQ/header/current version and verify authenticated human permission; do not trust a cached UI/agent clearance boolean. Reevaluate customer_active, current approved state/version, independent recorded approver, absence of unresolved commercial/credit restrictions, expiry, complete validated current snapshot, and authorization under the pinned policy. Derive commercial clearance from current account/credit evidence and authorized review events in protected audit/event_data (or obtain a new review); no source clearance record is fabricated. Append released event, released_by/released_at and protected clearance evidence; change state/row_version and commit receipt/audit atomically. Actual email/document dispatch is outside Day 4; a later outbox would be required only when such an external side effect is designed.

**Unknown outcomes/resume:** a business commit and graph checkpoint cannot be one transaction across stores. Before repeating any write after timeout, gateway internally queries the scoped durable receipt and verifies digest/result under current authorization. Committed receipt→return original result; confirmed rollback→the trusted recovery controller decides next action; unknown→escalate. The contract permits zero autonomous write retries. Read-only transient retries are bounded by recovery policy and backoff. No receipt or checkpoint means “unknown,” never proof of zero effects. Resume reloads current business version and source pins; changed snapshots require engineering/supply/pricing revalidation.

**Inventory/capacity:** current tools neither reserve stock nor schedule persistent hours. For future authorized mutating services, lock/CAS each affected row, validate bounds against the same locked values, increment row_version and audit atomically. Publish a new immutable approved snapshot archive/token for changed current rows before serving subsequent planning reads; the selected snapshot series must be internally consistent. Aggregate per-quote simulated planning consumption is not persisted in these tables. Do not introduce a reservation ledger without a real booking requirement.

Success audit is atomic with its mutation. A denied/rolled-back attempt is logged separately after rollback and must not carry FK references to nonexistent rows. Minimal error classes follow the contract: UNAUTHENTICATED, FORBIDDEN, NOT_FOUND, VALIDATION_ERROR, CONFLICT, UNAVAILABLE, TIMEOUT; internal integrity errors map to a contract-valid failure and protected audit reason rather than invented public reply fields.

## 5. Index catalog

All proposed indexes are B-tree. PostgreSQL automatically supplies indexes for each PK and nonpartial UNIQUE; these are listed first and **must not be duplicated**. FK declarations alone do not create child indexes. Indexes improve the stated access path in a future realistic workload; 731 source rows do not justify benchmark claims. Every additional index consumes storage/WAL and mutation work. No GIN is proposed: no required JSON containment/path query is established. Quote JSON is retrieved by relational version keys.

### 5.1 Constraint-backed indexes (automatic)

| Table | Columns | Type | Unique | Predicate | Supported query / expected benefit | Maintenance tradeoff |
| --- | --- | --- | --- | --- | --- | --- |
| business_rule_versions | rule_version | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| business_rule_versions | rule_kind, sha256 | B-tree | Yes | none | UNIQUE: Enforce documented alternate/business key and exact lookup | Entry per row; maintain on insert/key change; no duplicate explicit index |
| data_snapshots | snapshot_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| data_snapshots | snapshot_kind, sha256 | B-tree | Yes | none | UNIQUE: Enforce documented alternate/business key and exact lookup | Entry per row; maintain on insert/key change; no duplicate explicit index |
| roles | role | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| permissions | permission | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| role_permissions | role, permission | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| users | user_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| users | authentication_subject | B-tree | Yes | none | UNIQUE: Enforce documented alternate/business key and exact lookup | Entry per row; maintain on insert/key change; no duplicate explicit index |
| customers | customer_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| contacts | contact_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| materials | material_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| finishes | finish_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| operations | operation_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| workcentres | workcentre_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| workcentres | workcentre_id, operation_id | B-tree | Yes | none | UNIQUE: Enforce documented alternate/business key and exact lookup | Entry per row; maintain on insert/key change; no duplicate explicit index |
| suppliers | supplier_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| material_thickness_options | material_id, thickness_mm | B-tree | Yes | none | PK: Material eligibility/support lookup with leading material_id | Entry per row; maintain on insert/key change; no duplicate explicit index |
| material_finish_compatibility | material_id, finish_id | B-tree | Yes | none | PK: Material eligibility/support lookup with leading material_id | Entry per row; maintain on insert/key change; no duplicate explicit index |
| material_supported_operations | material_id, operation_id | B-tree | Yes | none | PK: Material eligibility/support lookup with leading material_id | Entry per row; maintain on insert/key change; no duplicate explicit index |
| finish_material_categories | finish_id, material_category | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| operation_routing | routing_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| operation_routing | part_type, sequence | B-tree | Yes | none | UNIQUE: Enforce documented alternate/business key and exact lookup | Entry per row; maintain on insert/key change; no duplicate explicit index |
| material_inventory | inventory_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| procurement_records | procurement_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| production_capacity | capacity_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| production_capacity | workcentre_id, week_start | B-tree | Yes | none | UNIQUE: Capacity by workcentre/week range | Entry per row; maintain on insert/key change; no duplicate explicit index |
| rfqs | rfq_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| rfq_line_items | rfq_line_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| rfq_line_items | rfq_id, line_number | B-tree | Yes | none | UNIQUE: Enforce documented alternate/business key and exact lookup | Entry per row; maintain on insert/key change; no duplicate explicit index |
| rfq_line_items | rfq_id, part_id | B-tree | Yes | none | UNIQUE: Enforce documented alternate/business key and exact lookup | Entry per row; maintain on insert/key change; no duplicate explicit index |
| rfq_attachments | attachment_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| pricing_calculations | calculation_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| quotes | quote_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| quotes | rfq_id | B-tree | Yes | none | UNIQUE: Quotes by RFQ; cross-actor duplicate-family prevention | Entry per row; maintain on insert/key change; no duplicate explicit index |
| quote_versions | quote_version_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| quote_versions | quote_id, version | B-tree | Yes | none | UNIQUE: Version history, reverse scan latest first, exact current pointer/approval join | Entry per row; maintain on insert/key change; no duplicate explicit index |
| quote_line_items | quote_line_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| quote_line_items | quote_id, quote_version, line_number | B-tree | Yes | none | UNIQUE: Ordered exact-version quote lines | Entry per row; maintain on insert/key change; no duplicate explicit index |
| approval_requests | approval_request_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| approval_requests | approval_request_id, quote_id, quote_version | B-tree | Yes | none | UNIQUE: Enforce documented alternate/business key and exact lookup | Entry per row; maintain on insert/key change; no duplicate explicit index |
| approval_history | approval_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| idempotency_records | receipt_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| idempotency_records | actor_id, customer_id, operation, idempotency_key | B-tree | Yes | none | UNIQUE: Authenticated actor/customer/operation/key receipt lookup | Entry per row; maintain on insert/key change; no duplicate explicit index |
| audit_events | event_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| workflow_runs | workflow_run_id | B-tree | Yes | none | PK: Identity lookup / referenced-key validation | Entry per row; maintain on insert/key change; no duplicate explicit index |
| workflow_runs | correlation_id | B-tree | Yes | none | UNIQUE: Enforce documented alternate/business key and exact lookup | Entry per row; maintain on insert/key change; no duplicate explicit index |

### 5.2 Additional indexes

| Name | Table | Columns | Type | Unique | Partial predicate | Query | Expected benefit | Maintenance tradeoff |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| uq_contacts_primary | contacts | customer_id | B-tree | Yes | is_primary | One primary contact per customer | Rejects competing primary designation | Extra index only for primary rows; updating primary status changes membership |
| uq_quote_versions_current | quote_versions | quote_id | B-tree | Yes | is_current | Current-version lookup and at-most-one current flag | Protects concurrent current-flag changes | Version switch removes/adds one entry; not an FK target |
| uq_approval_requests_pending | approval_requests | quote_id, quote_version | B-tree | Yes | status = 'pending' | One pending review per quote/version | Rejects duplicate queue items even across actors/keys | Resolving/superseding changes index membership |
| ix_rfqs_customer_submitted | rfqs | customer_id, submitted_at DESC, rfq_id | B-tree | No | — | Customer inquiry timeline with stable pagination | Limits customer timeline scan | One entry per RFQ; no raw-text index |
| ix_contacts_customer_name | contacts | customer_id, contact_name, contact_id | B-tree | No | — | List all customer contacts ordered by name | Supports full contact list, unlike primary-only partial index | Entry per contact; names update rarely |
| ix_users_reviewers | users | role, assigned_region, user_id | B-tree | No | active | Find active regional sales_manager reviewers | Selective role/region queue assignment | Role/region/active changes maintain index |
| ix_inventory_material_thickness | material_inventory | material_id, thickness_mm, warehouse | B-tree | No | — | Available kg by material/thickness, optionally warehouse | Supports aggregation and inventory composite-FK lookup | Entry per stock row; quantity-only updates do not change index keys |
| ix_procurement_supplier_status_eta | procurement_records | supplier_id, status, expected_receipt_date | B-tree | No | — | Supplier open/delayed orders ordered by ETA | Matches supplier and status filtering | Status/ETA changes update entries |
| ix_procurement_inventory_status | procurement_records | inventory_id, status | B-tree | No | — | Orders supporting a selected stock record | Supports inventory join and FK lookup | Second order index justified by different leading key |
| ix_approval_history_version_time | approval_history | quote_id, quote_version, action_at, approval_id | B-tree | No | — | Chronological events for exact version | Avoids global approval history scan | Append-only entry per event |
| ix_approval_requests_queue | approval_requests | required_role, requested_at, quote_id, quote_version | B-tree | No | status = 'pending' | Oldest pending reviews for authenticated role | Ordered pending queue; service also joins customer scope | Distinct queue ordering from partial uniqueness index; resolution removes entries |
| ix_audit_correlation_time | audit_events | correlation_id, occurred_at, event_id | B-tree | No | — | Workflow/request audit trail ordered by event time | Supports correlation trace across business operations | Append-only write/storage cost; no full payload indexing |
| ix_audit_rfq_time | audit_events | rfq_id, occurred_at, event_id | B-tree | No | rfq_id IS NOT NULL | Scoped RFQ audit timeline across retries/runs | Filters known RFQ histories | Additional append-only entries only for known RFQs |
| ix_workflow_runs_rfq_time | workflow_runs | rfq_id, created_at, workflow_run_id | B-tree | No | rfq_id IS NOT NULL | Find orchestration runs for an RFQ | Supports resumability/status screen join | Small run metadata index; excludes pre-RFQ shells |

A primary-contact partial index supports at most one, not at least one. The current-version partial index is not an FK target and does not ensure the current flag matches the header. The pending-approval partial index enforces uniqueness; the separate queue index supports requested_at ordering. Partial predicates must be present/implied in the query; see [PostgreSQL partial indexes](https://www.postgresql.org/docs/18/indexes-partial.html). No redundant quote-by-RFQ, quote-version, role-grant, workcentre-week or receipt indexes are added over existing leading constraint keys. Small role/permission and rare actor-FK scans can remain unindexed until measured workloads justify them. No arbitrary reverse junction/JSON/text indexes are preemptively added.

## 6. Representative backend-only query shapes

These are illustrative parameterized SELECTs inside documentation, not executed SQL. `:customer_id` is a scope already verified against the authenticated actor/run, not a trusted LLM-provided filter. Services must apply that scope for every object. Joined projections return only contract-authorized fields; the application never exposes raw SELECT access.

```sql
-- Customer RFQs / contacts: composite indexes lead with customer_id.
SELECT rfq_id, submitted_at, status FROM business.rfqs
WHERE customer_id = :customer_id ORDER BY submitted_at DESC, rfq_id;
SELECT contact_id, contact_name FROM business.contacts
WHERE customer_id = :customer_id ORDER BY contact_name, contact_id;

-- Quote by RFQ and history/current version: UNIQUE RFQ and version keys.
SELECT q.quote_id, q.status, v.version, v.total_usd
FROM business.quotes q JOIN business.rfqs r ON r.rfq_id = q.rfq_id
JOIN business.quote_versions v
  ON v.quote_id = q.quote_id AND v.version = q.current_version
WHERE r.customer_id = :customer_id AND q.rfq_id = :rfq_id;
SELECT v.version, v.snapshot_completeness FROM business.quote_versions v
JOIN business.quotes q ON q.quote_id = v.quote_id
JOIN business.rfqs r ON r.rfq_id = q.rfq_id
WHERE r.customer_id = :customer_id AND q.quote_id = :quote_id
ORDER BY v.version DESC;

-- Pending approvals, scoped via quote/RFQ and actor's role/region check.
SELECT a.approval_request_id, a.quote_version, a.requested_at
FROM business.approval_requests a
JOIN business.quotes q ON q.quote_id = a.quote_id
JOIN business.rfqs r ON r.rfq_id = q.rfq_id
WHERE a.status = 'pending' AND a.required_role = :authorized_role
  AND r.customer_id = :customer_id AND a.quote_version = q.current_version
ORDER BY a.requested_at, a.quote_id, a.quote_version;

-- Relational compatibility and current stock: leading composite keys.
SELECT finish_id FROM business.material_finish_compatibility
WHERE material_id = :material_id;
SELECT sum(available_kg) FROM business.material_inventory
WHERE material_id = :material_id AND thickness_mm = :thickness_mm
  AND snapshot_id = :verified_inventory_snapshot;
SELECT procurement_id, expected_receipt_date FROM business.procurement_records
WHERE supplier_id = :supplier_id AND status = :status
ORDER BY expected_receipt_date;
SELECT week_start, remaining_hours FROM business.production_capacity
WHERE workcentre_id = :workcentre_id AND week_start >= :first_week
  AND snapshot_id = :verified_capacity_snapshot ORDER BY week_start;

-- Protected audit and receipt reconciliation (backend only).
SELECT event_id, operation, outcome FROM business.audit_events
WHERE correlation_id = :verified_correlation AND customer_id = :customer_id
ORDER BY occurred_at, event_id;
SELECT request_sha256, result_json FROM business.idempotency_records
WHERE actor_id = :authenticated_actor AND customer_id = :customer_id
  AND operation = :operation AND idempotency_key = :key;
```

Quote history/approval-event queries use the exact composite quote/version join and customer ancestry above. Compatibility reads additionally validate finish categories and material-supported operations; stock and capacity snapshots must be verified by kind/hash. A planning service combines scoped, pinned snapshot reads into a consistent computation, without reserving anything. Day 5 may collect real EXPLAIN/EXPLAIN ANALYZE only after a database and representative query workload exist. **No plans, latency, throughput, or benchmark improvements have been fabricated.**
