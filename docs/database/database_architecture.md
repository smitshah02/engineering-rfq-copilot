# Day 4 — Database architecture

**Roadmap:** Day 4 of 73, Relational Data Modeling and PostgreSQL Fundamentals. **Status:** completed architecture/documentation; no deployed database or application. **Verified:** 2026-11-07 against frozen synthetic dataset v1.1.0. This document refines the existing provisional Component 2 draft using local source evidence, and completes the eight requested Day 4 components. Day 1–3 files and dataset bytes are preserved.

## 1. Architecture and evidence

The Supervisor and five specialist agents request typed operations through the secure MCP gateway. The gateway authenticates the delegated actor, validates the contract and scope, and calls backend business services. Only those services use database repositories and deterministic manufacturing, supply, pricing and review logic. Agents never receive database credentials or unrestricted SQL. An agent name is an orchestration responsibility, **not an authenticated human business role**.

Three separately controlled persistence domains are required:

1. **Authoritative business:** customers, catalogs, RFQs, quotation revisions, approval events, committed write receipts and business audit. Proposed `business` schema, backend-only credentials. Prices and human decisions originate in trusted services.
2. **Execution state:** `orchestration.workflow_runs` and a later selected LangGraph saver in a separate schema/store with distinct credentials. Checkpoints resume execution; they never prove a business operation succeeded or a human approved it. No business FK points into the checkpoint store.
3. **Offline evaluation:** scenario manifests, hidden labels and scoring/security/failure/coordination fixtures stay in harness-only filesystem/storage or a separate evaluation database. They are not production-facing business tables. Production services cannot mount or query them.

Source precedence is actual validated records and machine-readable entity/tool contracts, then versioned config, dataset dictionary/docs, Day 1–3 docs, then prior designs. The illustrative dataset SQL is not a complete schema. Primary sources inspected include all 17 business CSVs, historical RFQs, all scenario shapes/attachment metadata, five contracts, four policy JSONs, generation metadata, dataset database/dictionary/business/MCP/assumptions/evaluation/recovery/security docs, existing architecture/data docs, Day 3 log and checksum manifests. Detailed paths and fields are in [mapping](dataset_to_database_mapping.md); exact table/column/constraint authority is [table specifications](table_schemas.md).

## 2. Verified source inventory

| Exact source path relative to data/rfq_synthetic_data | Rows |
| --- | --- |
| `master_data/contacts.csv` | 40 |
| `master_data/customers.csv` | 40 |
| `master_data/finishes.csv` | 7 |
| `master_data/materials.csv` | 15 |
| `master_data/operations.csv` | 8 |
| `master_data/roles_permissions.csv` | 27 |
| `master_data/suppliers.csv` | 6 |
| `master_data/users.csv` | 24 |
| `master_data/workcentres.csv` | 8 |
| `operations_data/material_inventory.csv` | 90 |
| `operations_data/operation_routing.csv` | 28 |
| `operations_data/procurement_records.csv` | 12 |
| `operations_data/production_capacity.csv` | 80 |
| `transactional_data/approval_history.csv` | 70 |
| `transactional_data/historical_quotes.csv` | 80 |
| `transactional_data/quote_line_items.csv` | 100 |
| `transactional_data/quote_versions.csv` | 96 |

The 17 CSVs contain **731 rows**, including 27 supplementary role-permission grants. There are **18 formal entity schemas and 18 relationship definitions**: 16 CSV entities, historical RFQ JSON, and scenario JSON. Roles/permissions CSV is supplementary, not a nineteenth formal contract entity. `transactional_data/historical_rfqs.json` has **80** RFQs. Scenario inputs number **240**: development **120**, validation **40**, frozen evaluation **80**. Attachment metadata contains **124** files. Dataset tree: **681** files; existing generated-asset manifest: **637** hashes. These counts have different scopes.

All 18 declared FK checks passed, including historical quote→historical RFQ. Additional checks covered current/approved quote versions, line/approval version pairs, version creators and header submitters, supported thicknesses, category/finish/operation references, route/workcentre operation equality, line ordinals and capacity-week uniqueness. Current quotation totals equal sums of current lines in all 80 cases; no data establishes complete older snapshots.

## 3. Domain decomposition and table registry

| Domain | Proposed tables |
| --- | --- |
| Rules | `business_rule_versions`, `data_snapshots` |
| Identity | `roles`, `permissions`, `role_permissions`, `users` |
| Customers | `customers`, `contacts` |
| Manufacturing | `materials`, `finishes`, `operations`, `workcentres`, `material_thickness_options`, `material_finish_compatibility`, `material_supported_operations`, `finish_material_categories`, `operation_routing` |
| Supply | `suppliers`, `material_inventory`, `procurement_records` |
| Scheduling | `production_capacity` |
| RFQ | `rfqs`, `rfq_line_items`, `rfq_attachments` |
| Quotation | `pricing_calculations`, `quotes`, `quote_versions`, `quote_line_items` |
| Approval | `approval_requests`, `approval_history` |
| Reliability | `idempotency_records`, `audit_events` |
| Workflow | `workflow_runs` |

Classification matters: customers/catalogs/operations/history are **existing dataset entities**; RBAC reference tables and four material/finish junctions are **normalized projections**; editable RFQ lines/attachments, calculation cache, approval queue, receipts, audit and snapshot/rule registries are **new support proposals**. Four policy documents and five contract documents remain **versioned documents**, not one table per JSON key. `workflow_runs` is **orchestration metadata**. Scenario fixtures/answer keys remain **isolated evaluation artifacts**. Every proposed table has a complete column specification; no unspecified application table is needed for these diagrams.

No product/part master, tenant table, user_roles join, stock ledger, work-order system, or microservice fleet is introduced. Source `part_id` is a snapshot/reference identifier, not evidence of a standalone master entity. Stock/capacity estimates do not reserve resources.

## 4. Relational model and normalization

**First Normal Form:** replace catalog multivalued arrays with one association per row, and keep scalar typed keys/quantities. JSONB documents intentionally remain structured values where preserving exact versioned evidence is the requirement; their presence is not a claim of fully normalized internal JSON fields.

**Second Normal Form:** a fact on a compound key depends on the whole pair. For example, grant.allowed belongs to (role,permission); role definitions do not. Thickness/compatibility rows contain no partial-key descriptions. A route is an association with sequence/predicate attributes, not an unqualified material-operation join.

**Third Normal Form:** customer/account facts stay on customers, user role on users, supplier defaults on suppliers, and revision pricing on quote_versions. A stock-specific supplier lead is intentionally retained as snapshot/override evidence. Do not update old quote amounts because today's material catalog changed.

| Source field | Target structure | Reason / deliberate limit |
| --- | --- | --- |
| users.role; roles_permissions.role/permission/allowed | roles, permissions, role_permissions; users.role FK | One role per user preserved; composite grant PK prevents duplicate grants. No multi-role expansion. |
| materials.supported_thickness_mm | material_thickness_options(material_id,thickness_mm) | 90 atomic options; composite inventory FK can enforce supported thickness. |
| materials.compatible_finishes | material_finish_compatibility(material_id,finish_id) | 50 explicit material/finish pairs; category eligibility remains an additional check. |
| materials.supported_operations | material_supported_operations(material_id,operation_id) | 90 pairs; no implication that every listed catalog operation is quote-routed. |
| finishes.material_categories | finish_material_categories(finish_id,material_category) | 19 category memberships; a small controlled vocabulary avoids unnecessary category tables. |
| operation_routing.operation_id/workcentre_id/sequence | operation_routing + workcentres composite target | Preserve routing_id and predicates; composite FK prevents mismatched assigned operation. Serial route UNIQUE(part_type,sequence) verified and proposed. |
| historical_quotes version-sensitive fields; quote_versions | quotes current pointer + quote_versions financial facts | Exact (quote_id,version) key; source header details populate only its matching current version. |
| approval_history.quote_id/quote_version | approval_history exact-version FK; new approval_requests queue | Immutable events differ from actionable requests; no fake historical queue is reconstructed. |
| historical_rfq.part_ids | rfqs.part_ids JSONB | Opaque legacy references; do not invent standalone parts or editable extracted RFQ rows. |
| quote_line_items.part_json/cost_breakdown_json | Immutable JSONB snapshots plus typed quantity/line_total_usd | Preserve exact priced specification and high precision strings; no proven operation-cost reporting query justifies further tables. |

**Cardinalities:** customer→contacts/RFQs 1:N; operation→workcentres 1:N; quotation→versions and exact version→lines/events/requests 1:N; materials↔finishes/operations M:N via junctions. Composite keys implement association identity. No source pair proves a mandatory 1:1 relationship. The proposed UNIQUE(quotes.rfq_id) creates an **optional one-to-one** RFQ→quotation-family relationship to prevent duplicate families: the current create tool names only rfq_id. This is an explicit application choice, supported by all 80 observed unique RFQ/quote pairs, rather than a purported universal dataset rule. Alternatives/options would need a future family discriminator and revised contract.

Candidate keys are source business IDs; application alternate keys include users.authentication_subject, (quote_id,version), (workcentre_id,week_start), (part_type,sequence), and scoped idempotency tuples. Contact email, material grade, and supplier email are not assumed unique. See [seven ER diagrams](er_diagram.md) and [FK catalog](relationships_and_constraints.md).

## 5. Controlled denormalization and history

- `quotes.current_version`, approval summary and status are mutable lifecycle conveniences. `quote_versions.is_current`/approval_valid are source-derived flags; an atomic service update keeps flags and pointer consistent. One partial unique index ensures at most one current flag, while the service ensures exactly one and matching header pointer. Versioned financial payloads and line snapshots are immutable.
- Source coverage is 96 version metadata rows, **80 priced current versions**, and **16 older metadata-only versions**. All 100 lines belong to current versions. Preserve missing amounts/rule/snapshot provenance as NULL with explicit snapshot_completeness; never clone current amounts into earlier versions. Historical approval flags do not establish present commercial clearance. Historical versions are not eligible for live release without a new fully validated complete revision where the configured transition permits. An expired quotation is terminal under workflow-v1.0; a fresh inquiry is needed rather than reviving that version.
- Available stock and capacity are generated from base quantities. Raw source values are validated before discard; they cannot be independently updated. Snapshot registry hashes immutable operational CSV archives for replay; current inventory/capacity tables hold only the selected current series. No unrequested reservation ledger or full event sourcing is claimed.
- Calculation cache retains trusted immutable parts/results so a calculation ID can be resolved after a checkpoint resume. Technical caching does not make calculate_quote a business state-changing tool. Workflow state may cache progress; business services reread authoritative records before any write.

## 6. Identifier, unit and version conventions

Legacy IDs (`CUST-001`, `USER-001`, `MAT-001`, `HRFQ-0001`, `HQUOTE-0001`) remain exact TEXT keys/FKs. New business keys use collision-resistant prefix+UUID text within the contract's 64-character limit; source IDs are never converted. Native UUID is limited to new technical records and editable RFQ-line identity. Quote version is INTEGER ≥1; expected_version=0 is only a creation sentinel, never a stored version. BIGINT row_version detects same-version lifecycle conflicts.

USD cents are exact NUMERIC(18,2), rates NUMERIC(18,4), dimensions NUMERIC(12,3) mm, stock NUMERIC(18,6) kg, capacity NUMERIC(12,4) hours. DATE snapshots and Monday weeks are distinct from UTC TIMESTAMPTZ events. Currency, finite numeric validation, nullable business fields, JSON types and defaults are fully specified in [table conventions](table_schemas.md).

Rule documents keep exact source versions and byte hashes. The registry stores immutable JSONB+source path/hash; it does not relationalize every policy threshold. Contract identifiers without a standalone version use `kind@sha256:<full digest>` as a content-addressed TEXT rule_version (not subject to the business-ID 64-character bound). Version reuse with different bytes is rejected. Live quote versions pin manufacturing/pricing/approval/workflow policies and inventory/capacity tokens; graph runs additionally pin tool/entity/coordination/handoff contracts. Verify registry kind and snapshot kind in services. Configuration changes create new records and invalidate/recompute affected live results; they never mutate frozen source or past quote evidence.

## 7. Secure multi-agent data integration

The following tool assignments are proposed responsibilities, constrained by the authoritative tool contract and actual authenticated actor on **every call**. Supervisor primarily delegates, reads scoped status when authorized, reconciles and escalates. It has no privileged business role or independent write grant.

| Agent | Assigned tools / service boundary | Handoff obligation |
| --- | --- | --- |
| supervisor | Delegation; conditional get_quote_status for already-authorized caller; no arbitrary SQL | Validate correlation/customer, schema, dependencies and receipts; escalate unreconciled writes and conflicts. |
| rfq_intake | lookup_customer, get_customer_status, create_rfq → intake/customer services | parts, missing_fields, contradiction_findings, source_evidence → Engineering; incomplete specs block downstream writes. |
| engineering | get_material_specs, get_supported_operations, check_manufacturing_capability → engineering service | validated_parts, capability_result; pricing rule pin → Pricing; material_ids → Supply. MFG-001 guard enforced by the application validator since Day 8 (`services/manufacturing.py`). |
| supply_scheduling | check_material_availability, check_production_capacity, estimate_lead_time → planning service | inventory_status, capacity_status, lead_business_days, snapshot_versions → Review. No stock/capacity writes. |
| pricing | calculate_quote, get_pricing_breakdown → deterministic pricing/cache service | quote_total_usd, line_breakdown, pricing_rule_version → Review. Cannot override prices. |
| quotation_review | create_quote_draft, get_quote_status, request_approval → quotation/review services | draft_status, quote_version, manager_review_required, human_release_approval_required → Supervisor. No approve/release tool. |

### All 14 authoritative tools

| Tool | Authenticated business roles (exact allow-list) | Business write / idempotency | Exact input property names |
| --- | --- | --- | --- |
| `calculate_quote` | estimator | No / not required | `parts`, `pricing_rule_version` |
| `check_manufacturing_capability` | sales_representative, estimator, manufacturing_engineer | No / not required | `part` |
| `check_material_availability` | estimator, manufacturing_engineer | No / not required | `parts`, `snapshot_date` |
| `check_production_capacity` | estimator, manufacturing_engineer | No / not required | `parts`, `snapshot_version` |
| `create_quote_draft` | estimator, sales_representative | Yes / required | `expected_version`, `idempotency_key`, `pricing_calculation_id`, `rfq_id` |
| `create_rfq` | sales_representative, estimator | Yes / required | `customer_id`, `idempotency_key` |
| `estimate_lead_time` | estimator, manufacturing_engineer | No / not required | `capacity_snapshot`, `inventory_snapshot`, `parts` |
| `get_customer_status` | sales_representative, estimator, sales_manager | No / not required | `customer_id` |
| `get_material_specs` | sales_representative, estimator, manufacturing_engineer | No / not required | `material_id` |
| `get_pricing_breakdown` | estimator, sales_manager | No / not required | `quote_calculation_id` |
| `get_quote_status` | estimator, sales_representative, sales_manager, auditor | No / not required | `quote_id` |
| `get_supported_operations` | sales_representative, estimator, manufacturing_engineer | No / not required | `part_type` |
| `lookup_customer` | sales_representative, estimator, sales_manager, administrator, auditor, manufacturing_engineer | No / not required | `customer_id` |
| `request_approval` | estimator, sales_representative, sales_manager | Yes / required | `idempotency_key`, `quote_id`, `quote_version` |

These are **11 read-only business operations and 3 state-changing operations**. “Read-only” still requires authentication, customer scope, rule pins and protected audit/technical cache handling. Input/output schemas must be respected; missing contextual data is server session/job context, not silently added tool arguments. `calculation_id`, `quote_calculation_id` and `pricing_calculation_id` are three wire names for the same server calculation key.

Business permissions come from 27 source grants across six roles and 12 permissions. Where a write action has a matching permission (`create_rfq`, `create_quote_draft`, `request_approval`), require its true role_permissions grant **and** the contract role allow-list. Customer reads require view_customer; quote status reads for auditors use view_audit, whereas assigned operational users use view_assigned_rfqs. Manufacturing/pricing/planning read functions use their exact tool allow-list and authenticated assigned RFQ context; do not invent missing permission rows named after every tool. Human-only approve/reject/release require matching business grants and current-session human identity outside MCP. An administrator's catalog/audit permission does not confer quotation release authority.

Scope is a backend decision: resolve actor through authentication_subject, require active user, match customer to verified delegated account/assigned region, and rederive nested RFQ/quote/calculation/attachment ownership. The dataset has no explicit user_customer assignments. This design proposes ordinary access limited to the authenticated run's customer plus matching users.assigned_region/customers.region; any cross-region supervisory exception needs explicit future policy and cannot be inferred from a role. Historical actor/customer links are imported as facts, not retroactively certified region-authorized. A caller lacking estimator authorization cannot run Pricing simply because an agent is named pricing; route a task to an authorized estimator through a verified reassignment process or stop.

Gateway validates token, delegated identity, allowed agent capability, contract schema, argument bounds, object scope and tool-output authenticity. Backend reloads trusted outputs and checks business permissions, invariants and state transactionally; gateway authorization alone is insufficient. Customer prose, claimed role fields, handoff payload and arbitrary object IDs grant no authority. Prefer NOT_FOUND for out-of-scope objects where appropriate while auditing protected reasons. Business runtime has no evaluation storage credential and no access to migration ownership credentials.

Engineering must finish before Pricing and Supply; the two latter branches may run in parallel on the **same validated requirement revision**. Merge only matching RFQ/customer fingerprints, quote version, policy pins and source snapshots. All handoffs validate the actual schema's required fields: schema_version, workflow_id, correlation_id, sender, recipient, requested_action, payload, evidence_refs, decision_status, source_snapshot_versions. Optional findings/idempotency_key/quote_version retain their defined semantics. Ready/blocked/needs_clarification/failed/escalated are handoff decisions, separate from quote state and evaluation outcome taxonomies.

## 8. Transactions, approval and recovery

[Constraint and transaction design](relationships_and_constraints.md) defines one atomic commit for each business mutation, its success audit and committed receipt. Customer/RFQ/quote/queue rows are locked in stable order; row_version and quote version are checked again under lock. Human approve/release recheck exact current version, independent approver, active account, commercial clearance and policy. Managerial review at **≥ USD 7,500.00**, credit breach or account restriction is additional to the always-human release gate. No tool exposes approve_quote/release_quote to AI.

Checkpoint commits are separate from business commits. A timeout after a business commit is recovered by authenticated receipt lookup with the same logical key; do not infer rollback from an old checkpoint. Revalidate resumed identity, customer, current quote and snapshot/rule pins. Contract max_autonomous_write_retry_count=0 means no autonomous write retry; reconcile first and escalate unknown outcomes. Read retries are bounded by trusted recovery policy. A verified committed receipt can be replayed without a second business effect; mismatched payloads or stale versions fail closed. No new fifteenth receipt-lookup MCP tool is invented: reconciliation is an internal gateway/service responsibility.

## 9. Discrepancies, proposals and unresolved dependencies

| Finding | Decision / limitation |
| --- | --- |
| MFG-001: configured hole_count_max=300 is skipped by scalar-limit path in reference validate_part | **Fixed in the application (Day 8):** `check_manufacturing_capability` enforces the rule's `hole_count_max` (0–300, `OUT_OF_RANGE_HOLE_COUNT`) and raises on any rule limit it does not enforce. Regression tests prove 300 passes and 301 fails. The frozen reference `validate_part` still has the limitation and must remain unchanged (dataset v1.1.0 is frozen; it is a test oracle only). |
| Narrative MCP map has 13 tools; machine-readable contract has 14 | Use contract, including create_rfq. Frozen narrative unchanged. |
| Required CSV strings include empty values | Business-optional dates/approval fields become NULL. No empty string sentinel FK or bogus zero version. |
| Prior versions lack prices/lines and rule/snapshot provenance | Preserve 16 metadata-only versions; 80 current versions are priced_partial; no retrospective snapshots. |
| Source draft modeled 1:N quote families per RFQ | Final proposal is one family per RFQ with explicit UNIQUE, because create tool names RFQ and duplicate prevention is required; future alternatives need an explicit contract. |
| create_rfq input only customer_id/key; scenario has full request/attachments | Create draft shell only. Authorized content-ingestion interface remains a later design dependency. No fabricated tool fields. |
| create_quote_draft returns status constant draft; workflow revision state is revised_draft | Proposal: create-only tool with expected_version=0. Existing different commands return CONFLICT; exact receipt replays return original result. Revisions use a later authenticated edit service, not an invented MCP edit tool. |
| Tool schemas allow version zero; persisted/handoff versions start at one | Zero is creation sentinel only; request_approval(version=0) is rejected. |
| Handoff.workflow_id requires ^RFQ-[0-9]{4}$; legacy HRFQ/new RFQ-UUID do not match | Keep fixture external_workflow_id separate from business ID/run UUID. Production handoff needs a separately versioned contract extension before supporting those IDs; do not patch frozen contract or repurpose a fixture ID. |
| Dataset read-only planning does not define durable reservations or live refresh service | Keep current baseline + immutable snapshot archive; introduce reservation ledgers only with future authorized booking operations. |
| No LangGraph saver/version selected | Only application workflow metadata is specified. Saver physical checkpoint schema is a later workflow dependency, not guessed Day 5 business DDL. |

The business schema is ready to translate into SQLAlchemy/Alembic with these explicitly documented choices. Production agent integration still depends on the contract/scoping/ingestion/checkpoint decisions above (the MFG-001 guard was added in the application validator on Day 8). Nothing here asserts those services, permissions or database constraints already execute.

## 10. Documentation and reference authority

- [ER diagrams](er_diagram.md): seven views from one table/FK catalog.
- [Table specifications](table_schemas.md): every column, source/purpose, type, nullability, default, key and CHECK.
- [Relationships, constraints and indexes](relationships_and_constraints.md): authoritative enforcement categories, transaction protocol and query/index catalog.
- [Dataset mapping](dataset_to_database_mapping.md): exact fields/import dependencies/isolation.
- [Day 4 development log](../dev_log/day_04.md): executed verification, limitations and final Git evidence.

PostgreSQL references were checked for [declarative constraints and valid FK targets](https://www.postgresql.org/docs/18/ddl-constraints.html), [row locking](https://www.postgresql.org/docs/18/explicit-locking.html), and [partial indexes](https://www.postgresql.org/docs/18/indexes-partial.html). Cross-table policy checks remain transactional service rules; a row CHECK is not presented as a cross-table validator. These documentation references do not choose or install a PostgreSQL server version.
