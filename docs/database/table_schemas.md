# Day 4 — Proposed PostgreSQL table specifications

Design baseline: synthetic dataset v1.1.0, verified 2026-11-07. **Documentation only: no database, DDL, models, migrations, imports, or seeding executed.** These 33 logical tables supersede the provisional Component 2 registry. Source facts and application proposals are identified per table and column. Read [architecture](database_architecture.md), [relationships and indexes](relationships_and_constraints.md), and [field mapping](dataset_to_database_mapping.md) together.

## 1. Conventions applying to every table

- Tables are plural snake_case. All tables are in proposed `business` schema except `orchestration.workflow_runs`; short names below are unambiguous. No hidden-label tables exist in either schema.
- `NULL? No` means NOT NULL, including every PK component. `Default none` means the caller/importer must supply the value; it does not mean an implicit empty string or zero. Explicit source values always override proposed live defaults. No default is inferred for a missing historical fact.
- Established IDs remain exact case-sensitive `TEXT`, checked nonblank and length 1–64 for business entity identifiers. Choose TEXT consistently rather than mixing VARCHAR and UUID references. New business IDs use a prefix plus application-generated UUIDv4 text, e.g. `RFQ-<UUID>`; technical IDs use native UUIDv4. Neither strategy changes legacy identifiers. Version/permission/role/correlation tokens are nonblank TEXT; opaque identity subjects are not limited to 64 characters.
- Every NUMERIC value must be finite: add `column <> 'NaN'::numeric` to each numeric column's CHECK; fixed precision disallows infinity. Nullable numeric checks allow NULL. Reject excessive source scale/overflow in preflight rather than relying on PostgreSQL's implicit rounding. Source Decimal strings never pass through binary floats.
- Money: USD final line/quote totals `NUMERIC(18,2)`; rates/intermediate catalog money `NUMERIC(18,4)`. Calculators use Decimal with configured ROUND_HALF_UP; sum rounded line totals. Full precision nested cost/mass evidence remains JSONB strings. No floating-point monetary column exists.
- Millimetres `NUMERIC(12,3)`; kilograms and density `NUMERIC(18,6)`; workcentre hours `NUMERIC(12,4)`; utilization `NUMERIC(9,4)` rounded to four places. Integers represent quantities, versions, days, ordinals. BIGINT row_version is a mutation counter, distinct from quote version.
- DATE is a calendar date. TIMESTAMPTZ is an instant; use UTC sessions and explicit timezone input. Never convert a date to a guessed event time. Generated attributes are read-only expressions, with no independent value/default inserted.
- Every JSONB column has the stated top-level object/array type CHECK; validate schemas, required elements, lengths, units, and allowed properties in the backend. JSONB is reserved for immutable documents, evidence, payload snapshots, receipts and opaque legacy references. It is not a grant of trust or authority.
- All FKs use ON DELETE RESTRICT and ON UPDATE RESTRICT, MATCH SIMPLE. Business keys never change. No cascading deletion of history. Only the two specified quote-header pointer FKs are DEFERRABLE INITIALLY DEFERRED to permit atomic parent/version creation. Partial unique indexes are not FK targets.
- Roles/states use TEXT with CHECKs, not native PostgreSQL enums, to support explicit later migration changes. All state changes also need the configured transition check, authenticated authority and transaction protocol; membership CHECKs alone are insufficient.
- `application` provenance means a new application requirement, **not an existing dataset column**. New support tables start empty; no fictional historical audit/approval requests/calculations are manufactured.

The source entity contract suggests `numeric(18,4)` generically for money; the dataset database documentation explicitly distinguishes four-place intermediate values from two-place final totals. This design applies that distinction. See [PostgreSQL numeric types](https://www.postgresql.org/docs/18/datatype-numeric.html).

## 2. `business_rule_versions`

**Domain:** Rules. **Purpose:** Immutable rule and contract registry; the version key may be source-declared or content-addressed.

**Source or requirement:** `config/*.json and contracts/*.json`. **Primary key:** `(rule_version)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `rule_version` | `TEXT` | No | `none` | PK | config.version / contract version or kind + SHA-256 |
| `rule_kind` | `TEXT` | No | `none` | — | classification of exact source document |
| `sha256` | `TEXT` | No | `none` | — | SHA-256 of source bytes; 64 lowercase hexadecimal characters |
| `document` | `JSONB` | No | `none` | — | parsed source document; object |
| `source_path` | `TEXT` | No | `none` | — | exact repository-relative path |
| `registered_at` | `TIMESTAMPTZ` | No | `CURRENT_TIMESTAMP` | — | application |

**Unique constraints:** `(rule_kind, sha256)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:** none.

**Additional CHECKs:** `rule_kind IN ('manufacturing','pricing','approval','workflow','entity_contract','tool_contract','business_constraints','coordination','handoff')`.

**Design notes:** Version names are globally namespaced. Existing rule versions remain mfg-v1.0, price-v1.0, approval-v1.1, workflow-v1.0. A service verifies each FK points to the expected rule_kind; a plain version FK does not enforce the kind. Never UPDATE document/hash or reuse a version for changed bytes.

## 3. `data_snapshots`

**Domain:** Rules. **Purpose:** Immutable backend-readable inventory/capacity archives for replay and snapshot pinning.

**Source or requirement:** `operations CSV bytes + config/generation_config.yaml`. **Primary key:** `(snapshot_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `snapshot_id` | `TEXT` | No | `none` | PK | application stable inventory/capacity snapshot token |
| `snapshot_kind` | `TEXT` | No | `none` | — | inventory or capacity |
| `dataset_version` | `TEXT` | No | `none` | — | generation_config.yaml.version |
| `as_of_date` | `DATE` | No | `none` | — | inventory.snapshot_date; capacity generation_config.reference_date |
| `sha256` | `TEXT` | No | `none` | — | source artifact byte hash; 64 lowercase hexadecimal characters |
| `artifact_ref` | `TEXT` | No | `none` | — | backend-only immutable archive URI/path |
| `registered_at` | `TIMESTAMPTZ` | No | `CURRENT_TIMESTAMP` | — | application |

**Unique constraints:** `(snapshot_kind, sha256)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:** none.

**Additional CHECKs:** `snapshot_kind IN ('inventory','capacity')`.

**Design notes:** Proposed registry, not a stock ledger. Archive only approved operational CSVs, never the entire dataset/evaluation ZIP. Current-state tables hold one selected baseline; previous snapshots are immutable external artifacts, not additional rows with reused inventory_id/capacity_id.

## 4. `roles`

**Domain:** Identity. **Purpose:** Business role vocabulary; one role per user.

**Source or requirement:** `distinct roles in users.csv and roles_permissions.csv`. **Primary key:** `(role)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `role` | `TEXT` | No | `none` | PK | users.role / roles_permissions.role |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:** none.

**Additional CHECKs:** `role IN ('sales_representative','estimator','manufacturing_engineer','sales_manager','administrator','auditor')`.

## 5. `permissions`

**Domain:** Identity. **Purpose:** Granted business action vocabulary.

**Source or requirement:** `distinct roles_permissions.csv.permission`. **Primary key:** `(permission)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `permission` | `TEXT` | No | `none` | PK | roles_permissions.permission |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:** none.

**Additional CHECKs:** common key/type rules only.

**Design notes:** The 12 observed permissions are vocabulary, not 14 tool names. Read-tool allow-lists remain in the immutable tool contract; do not fabricate one permission per tool.

## 6. `role_permissions`

**Domain:** Identity. **Purpose:** One explicit allowed/denied grant per role/action pair.

**Source or requirement:** `master_data/roles_permissions.csv`. **Primary key:** `(role, permission)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `role` | `TEXT` | No | `none` | PK, FK | roles_permissions.role |
| `permission` | `TEXT` | No | `none` | PK, FK | roles_permissions.permission |
| `allowed` | `BOOLEAN` | No | `none` | — | roles_permissions.allowed |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(role) → roles(role)`.
- `(permission) → permissions(permission)`.

**Additional CHECKs:** common key/type rules only.

**Design notes:** 27 source rows, all True. Absence or allowed=false denies. No user_roles table is proposed.

## 7. `users`

**Domain:** Identity. **Purpose:** Authenticated internal business actors.

**Source or requirement:** `master_data/users.csv`. **Primary key:** `(user_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `user_id` | `TEXT` | No | `none` | PK | users.user_id |
| `display_name` | `TEXT` | No | `none` | — | users.display_name |
| `role` | `TEXT` | No | `none` | FK | users.role |
| `email` | `TEXT` | No | `none` | — | users.email |
| `active` | `BOOLEAN` | No | `none` | — | users.active |
| `assigned_region` | `TEXT` | No | `none` | — | users.assigned_region |
| `authentication_subject` | `TEXT` | No | `none` | — | users.authentication_subject |

**Unique constraints:** `(authentication_subject)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(role) → roles(role)`.

**Additional CHECKs:** common key/type rules only.

**Design notes:** authentication_subject is opaque and unique within the single configured identity issuer; multiple issuers require an explicit future (issuer, subject) key. Email is not an identity key. Inactive users fail runtime authorization; historical FKs remain valid.

## 8. `customers`

**Domain:** Customers. **Purpose:** Customer account, commercial context, and regional access boundary.

**Source or requirement:** `master_data/customers.csv`. **Primary key:** `(customer_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `customer_id` | `TEXT` | No | `none` | PK | customers.customer_id |
| `company_name` | `TEXT` | No | `none` | — | customers.company_name |
| `industry` | `TEXT` | No | `none` | — | customers.industry |
| `city` | `TEXT` | No | `none` | — | customers.city |
| `state` | `TEXT` | No | `none` | — | customers.state |
| `country` | `TEXT` | No | `none` | — | customers.country |
| `region` | `TEXT` | No | `none` | — | customers.region |
| `account_status` | `TEXT` | No | `none` | — | customers.account_status |
| `payment_terms` | `TEXT` | No | `none` | — | customers.payment_terms |
| `credit_limit_usd` | `NUMERIC(18,2)` | No | `none` | — | customers.credit_limit_usd |
| `credit_exposure_usd` | `NUMERIC(18,2)` | No | `none` | — | customers.credit_exposure_usd |
| `preferred_shipping` | `TEXT` | No | `none` | — | customers.preferred_shipping |
| `restrictions` | `TEXT` | No | `none` | — | customers.restrictions |
| `created_date` | `DATE` | No | `none` | — | customers.created_date |
| `approval_requirement` | `TEXT` | No | `none` | — | customers.approval_requirement |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:** none.

**Additional CHECKs:** `account_status IN ('active','pending_verification','on_hold','credit_restricted','inactive')`; `credit_limit_usd >= 0 AND credit_exposure_usd >= 0`; `approval_requirement IN ('threshold_based','commercial_review')`.

**Design notes:** restrictions is prose (including literal none), not JSON. Current credit exposure is separate from an immutable quote price. No tenant table: this project is one fictional manufacturer serving scoped customer accounts.

## 9. `contacts`

**Domain:** Customers. **Purpose:** Customer contacts, permitting more than one per customer.

**Source or requirement:** `master_data/contacts.csv`. **Primary key:** `(contact_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `contact_id` | `TEXT` | No | `none` | PK | contacts.contact_id |
| `customer_id` | `TEXT` | No | `none` | FK | contacts.customer_id |
| `contact_name` | `TEXT` | No | `none` | — | contacts.contact_name |
| `email` | `TEXT` | No | `none` | — | contacts.email |
| `phone` | `TEXT` | No | `none` | — | contacts.phone |
| `is_primary` | `BOOLEAN` | No | `none` | — | contacts.is_primary |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(customer_id) → customers(customer_id)`.

**Additional CHECKs:** common key/type rules only.

**Design notes:** At most one primary contact per customer via partial unique index; zero contacts is allowed. Forty observed one-to-one pairs do not establish a permanent one-to-one rule.

## 10. `materials`

**Domain:** Manufacturing. **Purpose:** Material catalog without repeating compatibility arrays.

**Source or requirement:** `master_data/materials.csv`. **Primary key:** `(material_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `material_id` | `TEXT` | No | `none` | PK | materials.material_id |
| `category` | `TEXT` | No | `none` | — | materials.category |
| `grade` | `TEXT` | No | `none` | — | materials.grade |
| `density_kg_m3` | `NUMERIC(18,6)` | No | `none` | — | materials.density_kg_m3 |
| `cost_usd_per_kg` | `NUMERIC(18,4)` | No | `none` | — | materials.cost_usd_per_kg |
| `min_bend_radius_factor` | `NUMERIC(12,3)` | No | `none` | — | materials.min_bend_radius_factor |
| `availability_class` | `TEXT` | No | `none` | — | materials.availability_class |
| `pricing_rule_version` | `TEXT` | No | `none` | FK | materials.pricing_rule_version |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(pricing_rule_version) → business_rule_versions(rule_version)`.

**Additional CHECKs:** `density_kg_m3 > 0 AND cost_usd_per_kg >= 0 AND min_bend_radius_factor > 0`; `availability_class IN ('stocked','procure')`; `category IN ('aluminum','mild_steel','stainless_steel','galvanized_steel','copper','brass')`.

**Design notes:** Catalog updates require authorized catalog administration and new calculation provenance; old quote snapshots remain immutable. Do not infer UNIQUE(category,grade) from this sample.

## 11. `finishes`

**Domain:** Manufacturing. **Purpose:** Finish catalog; none is a real finish identifier.

**Source or requirement:** `master_data/finishes.csv`. **Primary key:** `(finish_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `finish_id` | `TEXT` | No | `none` | PK | finishes.finish_id |
| `display_name` | `TEXT` | No | `none` | — | finishes.display_name |
| `pricing_rule_version` | `TEXT` | No | `none` | FK | finishes.pricing_rule_version |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(pricing_rule_version) → business_rule_versions(rule_version)`.

**Additional CHECKs:** common key/type rules only.

## 12. `operations`

**Domain:** Manufacturing. **Purpose:** Supported and catalog-only operation definitions.

**Source or requirement:** `master_data/operations.csv`. **Primary key:** `(operation_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `operation_id` | `TEXT` | No | `none` | PK | operations.operation_id |
| `display_name` | `TEXT` | No | `none` | — | operations.display_name |
| `is_quoted` | `BOOLEAN` | No | `none` | — | operations.is_quoted |
| `notes` | `TEXT` | No | `none` | — | operations.notes |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:** none.

**Additional CHECKs:** common key/type rules only.

**Design notes:** punch and machine exist but are outside reference quotation routing; catalog presence does not activate pricing support.

## 13. `workcentres`

**Domain:** Manufacturing. **Purpose:** Workcentre operation and nominal planning parameters.

**Source or requirement:** `master_data/workcentres.csv`. **Primary key:** `(workcentre_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `workcentre_id` | `TEXT` | No | `none` | PK | workcentres.workcentre_id |
| `operation_id` | `TEXT` | No | `none` | FK | workcentres.operation_id |
| `shift_calendar` | `TEXT` | No | `none` | — | workcentres.shift_calendar |
| `weekly_nominal_hours` | `NUMERIC(12,4)` | No | `none` | — | workcentres.weekly_nominal_hours |
| `downtime_hours_per_week` | `NUMERIC(12,4)` | No | `none` | — | workcentres.downtime_hours_per_week |
| `planning_horizon_weeks` | `INTEGER` | No | `none` | — | workcentres.planning_horizon_weeks |

**Unique constraints:** `(workcentre_id, operation_id)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(operation_id) → operations(operation_id)`.

**Additional CHECKs:** `weekly_nominal_hours >= 0 AND downtime_hours_per_week >= 0 AND downtime_hours_per_week <= weekly_nominal_hours`; `planning_horizon_weeks > 0`.

**Design notes:** The additional unique pair supports the routing composite FK; workcentre_id alone is already the PK. One operation may have many workcentres.

## 14. `suppliers`

**Domain:** Supply. **Purpose:** Supplier contact and default lead time.

**Source or requirement:** `master_data/suppliers.csv`. **Primary key:** `(supplier_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `supplier_id` | `TEXT` | No | `none` | PK | suppliers.supplier_id |
| `company_name` | `TEXT` | No | `none` | — | suppliers.company_name |
| `email` | `TEXT` | No | `none` | — | suppliers.email |
| `normal_lead_business_days` | `INTEGER` | No | `none` | — | suppliers.normal_lead_business_days |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:** none.

**Additional CHECKs:** `normal_lead_business_days >= 0`.

## 15. `material_thickness_options`

**Domain:** Manufacturing. **Purpose:** Atomic supported material/thickness pair.

**Source or requirement:** `master_data/materials.csv.supported_thickness_mm`. **Primary key:** `(material_id, thickness_mm)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `material_id` | `TEXT` | No | `none` | PK, FK | materials.material_id |
| `thickness_mm` | `NUMERIC(12,3)` | No | `none` | PK | each supported_thickness_mm array value |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(material_id) → materials(material_id)`.

**Additional CHECKs:** `thickness_mm > 0`.

## 16. `material_finish_compatibility`

**Domain:** Manufacturing. **Purpose:** Material-specific finish eligibility.

**Source or requirement:** `master_data/materials.csv.compatible_finishes`. **Primary key:** `(material_id, finish_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `material_id` | `TEXT` | No | `none` | PK, FK | materials.material_id |
| `finish_id` | `TEXT` | No | `none` | PK, FK | each compatible_finishes array value |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(material_id) → materials(material_id)`.
- `(finish_id) → finishes(finish_id)`.

**Additional CHECKs:** common key/type rules only.

**Design notes:** Also validate finish category support transactionally; that cross-table condition is not a CHECK.

## 17. `material_supported_operations`

**Domain:** Manufacturing. **Purpose:** Material-specific operation support.

**Source or requirement:** `master_data/materials.csv.supported_operations`. **Primary key:** `(material_id, operation_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `material_id` | `TEXT` | No | `none` | PK, FK | materials.material_id |
| `operation_id` | `TEXT` | No | `none` | PK, FK | each supported_operations array value |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(material_id) → materials(material_id)`.
- `(operation_id) → operations(operation_id)`.

**Additional CHECKs:** common key/type rules only.

## 18. `finish_material_categories`

**Domain:** Manufacturing. **Purpose:** Finish eligibility by material category.

**Source or requirement:** `master_data/finishes.csv.material_categories`. **Primary key:** `(finish_id, material_category)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `finish_id` | `TEXT` | No | `none` | PK, FK | finishes.finish_id |
| `material_category` | `TEXT` | No | `none` | PK | each material_categories array value |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(finish_id) → finishes(finish_id)`.

**Additional CHECKs:** `material_category IN ('aluminum','mild_steel','stainless_steel','galvanized_steel','copper','brass')`.

**Design notes:** Controlled category vocabulary; no category master is justified yet. A material must satisfy both its explicit finish list and category eligibility.

## 19. `operation_routing`

**Domain:** Manufacturing. **Purpose:** Ordered route with predicate and assigned compatible workcentre.

**Source or requirement:** `operations_data/operation_routing.csv`. **Primary key:** `(routing_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `routing_id` | `TEXT` | No | `none` | PK | operation_routing.routing_id |
| `part_type` | `TEXT` | No | `none` | — | operation_routing.part_type |
| `operation_id` | `TEXT` | No | `none` | FK | operation_routing.operation_id |
| `sequence` | `INTEGER` | No | `none` | — | operation_routing.sequence |
| `required_when` | `TEXT` | No | `none` | — | operation_routing.required_when |
| `workcentre_id` | `TEXT` | No | `none` | FK | operation_routing.workcentre_id |

**Unique constraints:** `(part_type, sequence)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(workcentre_id, operation_id) → workcentres(workcentre_id, operation_id)`.

**Additional CHECKs:** `sequence > 0`; `required_when IN ('always','finish_not_none')`; `part_type IN ('flat_panel','mounting_plate','l_bracket','u_bracket','enclosure','fabricated_assembly')`.

**Design notes:** Composite FK enforces route.operation_id = workcentre.operation_id and transitively a valid operation. UNIQUE(part_type,sequence) is a proposal for the current serial routes, independently verified for 28 rows; parallel/alternative routing would require a changed key. part_type uses the six-value vocabulary in Section 1.

## 20. `material_inventory`

**Domain:** Supply. **Purpose:** Current approved material/thickness/warehouse stock baseline.

**Source or requirement:** `operations_data/material_inventory.csv`. **Primary key:** `(inventory_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `inventory_id` | `TEXT` | No | `none` | PK | material_inventory.inventory_id |
| `material_id` | `TEXT` | No | `none` | FK | material_inventory.material_id |
| `thickness_mm` | `NUMERIC(12,3)` | No | `none` | FK | material_inventory.thickness_mm |
| `stock_form` | `TEXT` | No | `none` | — | material_inventory.stock_form |
| `quantity_kg` | `NUMERIC(18,6)` | No | `none` | — | material_inventory.quantity_kg |
| `reserved_kg` | `NUMERIC(18,6)` | No | `none` | — | material_inventory.reserved_kg |
| `unit` | `TEXT` | No | `none` | — | material_inventory.unit |
| `warehouse` | `TEXT` | No | `none` | — | material_inventory.warehouse |
| `reorder_threshold_kg` | `NUMERIC(18,6)` | No | `none` | — | material_inventory.reorder_threshold_kg |
| `supplier_id` | `TEXT` | No | `none` | FK | material_inventory.supplier_id |
| `supplier_lead_business_days` | `INTEGER` | No | `none` | — | material_inventory.supplier_lead_business_days |
| `expected_replenishment_date` | `DATE` | Yes | `none` | — | material_inventory.expected_replenishment_date |
| `snapshot_date` | `DATE` | No | `none` | — | material_inventory.snapshot_date |
| `available_kg` | `NUMERIC(18,6)` | No | `generated: quantity_kg - reserved_kg` | — | validated source available_kg |
| `snapshot_id` | `TEXT` | No | `none` | FK | registered inventory snapshot |
| `row_version` | `BIGINT` | No | `1` | — | application |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(material_id, thickness_mm) → material_thickness_options(material_id, thickness_mm)`.
- `(supplier_id) → suppliers(supplier_id)`.
- `(snapshot_id) → data_snapshots(snapshot_id)`.

**Additional CHECKs:** `quantity_kg >= 0 AND reserved_kg >= 0 AND reserved_kg <= quantity_kg`; `reorder_threshold_kg >= 0 AND supplier_lead_business_days >= 0`; `unit = 'kg'`; `stock_form = 'sheet'`; `row_version >= 1`.

**Design notes:** No inferred lot or stock-movement ledger. Snapshot date is not a repeated-history key. No uniqueness on material/thickness/warehouse: future lots are not ruled out. Supplier lead is a per-stock sourcing snapshot/override, not a duplicate forced equal to supplier default.

## 21. `procurement_records`

**Domain:** Supply. **Purpose:** Existing material procurement and expected receipt records.

**Source or requirement:** `operations_data/procurement_records.csv`. **Primary key:** `(procurement_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `procurement_id` | `TEXT` | No | `none` | PK | procurement_records.procurement_id |
| `inventory_id` | `TEXT` | No | `none` | FK | procurement_records.inventory_id |
| `supplier_id` | `TEXT` | No | `none` | FK | procurement_records.supplier_id |
| `quantity_kg` | `NUMERIC(18,6)` | No | `none` | — | procurement_records.quantity_kg |
| `ordered_date` | `DATE` | No | `none` | — | procurement_records.ordered_date |
| `expected_receipt_date` | `DATE` | No | `none` | — | procurement_records.expected_receipt_date |
| `status` | `TEXT` | No | `none` | — | procurement_records.status |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(inventory_id) → material_inventory(inventory_id)`.
- `(supplier_id) → suppliers(supplier_id)`.

**Additional CHECKs:** `quantity_kg > 0 AND expected_receipt_date >= ordered_date`; `status IN ('ordered','delayed')`.

**Design notes:** Only ordered/delayed are evidenced; receiving/cancellation needs a versioned service contract before widening this vocabulary. Procurement supplier need not equal stock preferred supplier.

## 22. `production_capacity`

**Domain:** Scheduling. **Purpose:** Current selected weekly workcentre capacity baseline.

**Source or requirement:** `operations_data/production_capacity.csv`. **Primary key:** `(capacity_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `capacity_id` | `TEXT` | No | `none` | PK | production_capacity.capacity_id |
| `workcentre_id` | `TEXT` | No | `none` | FK | production_capacity.workcentre_id |
| `week_start` | `DATE` | No | `none` | — | production_capacity.week_start |
| `nominal_hours` | `NUMERIC(12,4)` | No | `none` | — | production_capacity.nominal_hours |
| `downtime_hours` | `NUMERIC(12,4)` | No | `none` | — | production_capacity.downtime_hours |
| `scheduled_hours` | `NUMERIC(12,4)` | No | `none` | — | production_capacity.scheduled_hours |
| `available_hours` | `NUMERIC(12,4)` | No | `generated: nominal_hours - downtime_hours` | — | validated source available_hours |
| `remaining_hours` | `NUMERIC(12,4)` | No | `generated: nominal_hours - downtime_hours - scheduled_hours` | — | validated source remaining_hours |
| `utilization_fraction` | `NUMERIC(9,4)` | Yes | `generated: CASE WHEN nominal_hours > downtime_hours THEN round(scheduled_hours / (nominal_hours - downtime_hours),4) ELSE NULL END` | — | validated source utilization_fraction |
| `snapshot_id` | `TEXT` | No | `none` | FK | registered capacity snapshot |
| `row_version` | `BIGINT` | No | `1` | — | application |

**Unique constraints:** `(workcentre_id, week_start)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(workcentre_id) → workcentres(workcentre_id)`.
- `(snapshot_id) → data_snapshots(snapshot_id)`.

**Additional CHECKs:** `nominal_hours >= 0 AND downtime_hours >= 0 AND downtime_hours <= nominal_hours`; `scheduled_hours >= 0 AND scheduled_hours <= nominal_hours - downtime_hours`; `EXTRACT(ISODOW FROM week_start) = 1`; `row_version >= 1`.

**Design notes:** Generated expressions reference base columns, not other generated columns. Utilization is NULL for zero available hours; the baseline has no zero denominator. This is one current planning series, not multiple snapshot rows per workcentre/week. Future refresh archives old CSV bytes before updating current state. Any future authorized quantity/scheduling mutation must publish a new consistent archived snapshot token before planning can read changed rows; never serve altered values under an old immutable token.

## 23. `rfqs`

**Domain:** RFQ. **Purpose:** Operational inquiry identity and raw intake, distinct from evaluation scenarios.

**Source or requirement:** `transactional_data/historical_rfqs.json + authenticated intake requirement`. **Primary key:** `(rfq_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `rfq_id` | `TEXT` | No | `none` | PK | historical_rfq.rfq_id / application RFQ-<UUID> |
| `customer_id` | `TEXT` | No | `none` | FK | historical_rfq.customer_id / authenticated scope |
| `submitted_at` | `TIMESTAMPTZ` | No | `CURRENT_TIMESTAMP` | — | historical_rfq.submitted_at / server intake time |
| `raw_request` | `TEXT` | Yes | `none` | — | historical_rfq.raw_request / later intake ingestion |
| `source_type` | `TEXT` | No | `none` | — | historical_rfq.source_type / application intake channel |
| `part_ids` | `JSONB` | No | `'[]'::jsonb` | — | historical_rfq.part_ids; opaque legacy references; array of string IDs |
| `status` | `TEXT` | No | `'draft'` | — | application intake state; historical rows assigned archived |
| `created_by` | `TEXT` | Yes | `none` | FK | authenticated intake actor; absent from historical source |
| `row_version` | `BIGINT` | No | `1` | — | application |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(customer_id) → customers(customer_id)`.
- `(created_by) → users(user_id)`.

**Additional CHECKs:** `status IN ('draft','intake','validated','blocked','closed','archived')`; `row_version >= 1`.

**Design notes:** create_rfq takes only customer_id + idempotency_key, so it creates a draft shell; raw_request is nullable until separately authenticated ingestion. That ingestion interface is a later requirement, not an invented fifteenth MCP tool. Historical part_ids are references to quote-line snapshots, not a new part master or synthesized live RFQ lines.

## 24. `rfq_line_items`

**Domain:** RFQ. **Purpose:** Editable structured requirements; incomplete extraction is retained without invented defaults.

**Source or requirement:** `new application intake requirement; Part value contract`. **Primary key:** `(rfq_line_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `rfq_line_id` | `UUID` | No | `application UUIDv4` | PK | application |
| `rfq_id` | `TEXT` | No | `none` | FK | application |
| `line_number` | `INTEGER` | No | `none` | — | application |
| `part_id` | `TEXT` | No | `none` | — | application preserved/generated part token |
| `part_type` | `TEXT` | Yes | `none` | — | application |
| `material_id` | `TEXT` | Yes | `none` | FK | application |
| `finish_id` | `TEXT` | Yes | `none` | FK | application |
| `length_mm` | `NUMERIC(12,3)` | Yes | `none` | — | application; positive when present |
| `width_mm` | `NUMERIC(12,3)` | Yes | `none` | — | application; positive when present |
| `height_mm` | `NUMERIC(12,3)` | Yes | `none` | — | application; positive when present |
| `thickness_mm` | `NUMERIC(12,3)` | Yes | `none` | — | application; positive when present |
| `tolerance_mm` | `NUMERIC(12,3)` | Yes | `none` | — | application; nonnegative when present |
| `bend_radius_mm` | `NUMERIC(12,3)` | Yes | `none` | — | application; positive when present |
| `weld_length_mm` | `NUMERIC(12,3)` | Yes | `none` | — | application; nonnegative when present |
| `quantity` | `INTEGER` | Yes | `none` | — | application; > 0 when present |
| `hole_count` | `INTEGER` | Yes | `none` | — | application; >= 0 when present |
| `source_evidence` | `JSONB` | No | `'[]'::jsonb` | — | application; array of source evidence references |
| `requirement_revision` | `INTEGER` | No | `1` | — | application |

**Unique constraints:** `(rfq_id, line_number)`; `(rfq_id, part_id)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(rfq_id) → rfqs(rfq_id)`.
- `(material_id) → materials(material_id)`.
- `(finish_id) → finishes(finish_id)`.

**Additional CHECKs:** `line_number > 0 AND requirement_revision > 0`; `length_mm > 0 AND width_mm > 0 AND height_mm > 0 AND thickness_mm > 0 AND bend_radius_mm > 0`; `tolerance_mm >= 0 AND weld_length_mm >= 0 AND quantity > 0 AND hole_count >= 0`; `part_type IN ('flat_panel','mounting_plate','l_bracket','u_bracket','enclosure','fabricated_assembly')`.

**Design notes:** Nullable fields permit missing requirements. PostgreSQL CHECK accepts NULL; manufacturing/pricing services require all applicable fields before validation. Known values must reference valid catalogs; unknown tokens remain raw evidence, not invalid FKs. Geometry maxima and required height/weld rules come from immutable manufacturing config. No Day 4 import populates this table from hidden labels.

## 25. `rfq_attachments`

**Domain:** RFQ. **Purpose:** Operational attachment metadata and immutable evidence references.

**Source or requirement:** `new application table; scenario attachment shape only`. **Primary key:** `(attachment_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `attachment_id` | `TEXT` | No | `none` | PK | assigned attachment.attachment_id / application ATT-<UUID> |
| `rfq_id` | `TEXT` | No | `none` | FK | authorized owning RFQ |
| `filename` | `TEXT` | No | `none` | — | attachment.filename |
| `storage_ref` | `TEXT` | No | `none` | — | attachment.path remapped to backend object key |
| `revision` | `TEXT` | Yes | `none` | — | attachment.revision |
| `document_type` | `TEXT` | No | `none` | — | attachment.document_type |
| `source_role` | `TEXT` | No | `none` | — | attachment.source_role |
| `is_trusted_instruction` | `BOOLEAN` | No | `FALSE` | — | attachment.is_trusted_instruction |
| `sha256` | `TEXT` | No | `none` | — | computed immutable file byte hash; 64 lowercase hexadecimal characters |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(rfq_id) → rfqs(rfq_id)`.

**Additional CHECKs:** `document_type IN ('pdf','csv','md')`; `is_trusted_instruction = FALSE`.

**Design notes:** Dataset attachment IDs belong to scenarios, not HRFQ records. No automatic production seed from attachment_manifest. Object bytes stay outside PostgreSQL; scan/allow-list paths, reject traversal, and verify authorized RFQ scope. Source document authority over specifications never grants instruction authority.

## 26. `pricing_calculations`

**Domain:** Quotation. **Purpose:** Immutable trusted calculation cache needed by calculation_id read and draft tools.

**Source or requirement:** `new backend cache requirement in calculate_quote/get_pricing_breakdown/create_quote_draft`. **Primary key:** `(calculation_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `calculation_id` | `TEXT` | No | `none` | PK | server CALC-<UUID> returned as calculation_id / quote_calculation_id / pricing_calculation_id |
| `rfq_id` | `TEXT` | No | `none` | FK | authenticated workflow context; not an extra tool payload field |
| `created_by` | `TEXT` | No | `none` | FK | authenticated estimator |
| `created_at` | `TIMESTAMPTZ` | No | `CURRENT_TIMESTAMP` | — | application |
| `expires_at` | `TIMESTAMPTZ` | No | `none` | — | application |
| `requirement_sha256` | `TEXT` | No | `none` | — | canonical validated input fingerprint; 64 lowercase hexadecimal characters |
| `parts` | `JSONB` | No | `none` | — | validated calculate_quote.parts; nonempty Part array |
| `lines` | `JSONB` | No | `none` | — | trusted calculator lines; array |
| `quote_total_usd` | `NUMERIC(18,2)` | No | `none` | — | calculate_quote.quote_total_usd |
| `pricing_rule_version` | `TEXT` | No | `none` | FK | calculate_quote.pricing_rule_version |
| `manufacturing_rule_version` | `TEXT` | No | `none` | FK | verified engineering rule pin |
| `source_snapshot_versions` | `JSONB` | No | `none` | — | verified inventory/capacity tokens; object |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(rfq_id) → rfqs(rfq_id)`.
- `(created_by) → users(user_id)`.
- `(pricing_rule_version) → business_rule_versions(rule_version)`.
- `(manufacturing_rule_version) → business_rule_versions(rule_version)`.

**Additional CHECKs:** `quote_total_usd >= 0 AND expires_at > created_at`.

**Design notes:** Server-side immutable cache, not a quotation or reservation: calculate_quote remains state_changing=false in the business contract. Draft creation reloads trusted cache and binds RFQ/customer/input fingerprint; an LLM-supplied total or calculation ID is not proof. No pricing cache rows are invented for historical quotes.

## 27. `quotes`

**Domain:** Quotation. **Purpose:** One quotation family per RFQ with mutable lifecycle and current-version pointer.

**Source or requirement:** `transactional_data/historical_quotes.csv`. **Primary key:** `(quote_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `quote_id` | `TEXT` | No | `none` | PK, FK | historical_quotes.quote_id / application QUOTE-<UUID> |
| `rfq_id` | `TEXT` | No | `none` | FK | historical_quotes.rfq_id |
| `current_version` | `INTEGER` | No | `none` | FK | historical_quotes.version |
| `status` | `TEXT` | No | `none` | — | historical_quotes.status |
| `created_by` | `TEXT` | No | `none` | FK | historical_quotes.created_by |
| `created_at` | `TIMESTAMPTZ` | No | `none` | — | historical_quotes.created_at |
| `submitted_by` | `TEXT` | Yes | `none` | FK | historical_quotes.submitted_by |
| `approved_version` | `INTEGER` | Yes | `none` | FK | historical_quotes.approved_version |
| `approver_id` | `TEXT` | Yes | `none` | FK | historical_quotes.approver_id |
| `approval_timestamp` | `TIMESTAMPTZ` | Yes | `none` | — | historical_quotes.approval_timestamp |
| `rejection_reason` | `TEXT` | Yes | `none` | — | historical_quotes.rejection_reason |
| `legacy_idempotency_key` | `TEXT` | Yes | `none` | — | historical_quotes.idempotency_key |
| `row_version` | `BIGINT` | No | `1` | — | application |
| `released_by` | `TEXT` | Yes | `none` | FK | application |
| `released_at` | `TIMESTAMPTZ` | Yes | `none` | — | application |

**Unique constraints:** `(rfq_id)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(rfq_id) → rfqs(rfq_id)`.
- `(created_by) → users(user_id)`.
- `(submitted_by) → users(user_id)`.
- `(approver_id) → users(user_id)`.
- `(released_by) → users(user_id)`.
- `(quote_id, current_version) → quote_versions(quote_id, version)` — DEFERRABLE INITIALLY DEFERRED.
- `(quote_id, approved_version) → quote_versions(quote_id, version)` — DEFERRABLE INITIALLY DEFERRED.

**Additional CHECKs:** `current_version >= 1 AND row_version >= 1`; `approved_version IS NULL OR approved_version = current_version`; `approved_version IS NULL OR (approver_id IS NOT NULL AND approval_timestamp IS NOT NULL AND approver_id <> created_by)`; `status NOT IN ('approved','released') OR (approved_version IS NOT NULL AND approver_id IS NOT NULL AND approval_timestamp IS NOT NULL)`; `(released_by IS NULL) = (released_at IS NULL)`; `status <> 'released' OR released_by IS NOT NULL`; `status IN ('draft','revised_draft','pending_approval','approved','rejected','expired','clarification_required','blocked','released')`; `(approved_version IS NULL) = (approval_timestamp IS NULL)`.

**Design notes:** UNIQUE(rfq_id) is an explicit application proposal resolving duplicate creation across actors/keys: the create tool identifies a family by RFQ, not quote_id. Alternative quote families would require a changed contract/key. No total cache on the header: total/currency/expiry/lead/pricing version belong to quote_versions. Rejected source rows have approver_id but no approval_timestamp; preserve them without inventing an approval. An expired approved quote may retain its exact historical approval metadata.

## 28. `quote_versions`

**Domain:** Quotation. **Purpose:** Immutable priced revision, with limited mutable current/approval flags.

**Source or requirement:** `transactional_data/quote_versions.csv + current header fields only`. **Primary key:** `(quote_version_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `quote_version_id` | `TEXT` | No | `none` | PK | quote_versions.quote_version_id |
| `quote_id` | `TEXT` | No | `none` | FK | quote_versions.quote_id |
| `version` | `INTEGER` | No | `none` | — | quote_versions.version |
| `created_by` | `TEXT` | No | `none` | FK | quote_versions.created_by |
| `created_at` | `TIMESTAMPTZ` | No | `none` | — | quote_versions.created_at |
| `is_current` | `BOOLEAN` | No | `none` | — | quote_versions.is_current |
| `approval_valid` | `BOOLEAN` | No | `none` | — | quote_versions.approval_valid |
| `currency` | `TEXT` | No | `'USD'` | — | historical current header.currency / configured USD |
| `total_usd` | `NUMERIC(18,2)` | Yes | `none` | — | matching current historical header.total_usd / trusted new calculation |
| `pricing_rule_version` | `TEXT` | Yes | `none` | FK | matching current historical header.pricing_rule_version / calculation |
| `manufacturing_rule_version` | `TEXT` | Yes | `none` | FK | new version verified rule pin; absent historically |
| `approval_rule_version` | `TEXT` | Yes | `none` | FK | new version verified approval policy pin; absent historically |
| `workflow_rule_version` | `TEXT` | Yes | `none` | FK | new version verified transition rule pin; absent historically |
| `expiry_date` | `DATE` | Yes | `none` | — | matching current historical header.expiry_date / new service |
| `estimated_lead_business_days` | `INTEGER` | Yes | `none` | — | matching current historical header.estimated_lead_business_days / schedule |
| `source_snapshot_versions` | `JSONB` | Yes | `none` | — | new version verified snapshot tokens; absent historically; object when present |
| `calculation_id` | `TEXT` | Yes | `none` | FK | new pricing calculation; absent historically |
| `snapshot_completeness` | `TEXT` | No | `none` | — | import evidence or live service; metadata_only / priced_partial / complete |

**Unique constraints:** `(quote_id, version)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(quote_id) → quotes(quote_id)`.
- `(created_by) → users(user_id)`.
- `(pricing_rule_version) → business_rule_versions(rule_version)`.
- `(manufacturing_rule_version) → business_rule_versions(rule_version)`.
- `(approval_rule_version) → business_rule_versions(rule_version)`.
- `(workflow_rule_version) → business_rule_versions(rule_version)`.
- `(calculation_id) → pricing_calculations(calculation_id)`.

**Additional CHECKs:** `version >= 1`; `currency = 'USD'`; `total_usd >= 0 AND estimated_lead_business_days >= 0`; `snapshot_completeness IN ('metadata_only','priced_partial','complete')`; `snapshot_completeness <> 'metadata_only' OR total_usd IS NULL`; `snapshot_completeness = 'metadata_only' OR (total_usd IS NOT NULL AND pricing_rule_version IS NOT NULL)`; `snapshot_completeness <> 'complete' OR (manufacturing_rule_version IS NOT NULL AND approval_rule_version IS NOT NULL AND workflow_rule_version IS NOT NULL AND source_snapshot_versions IS NOT NULL AND calculation_id IS NOT NULL AND expiry_date IS NOT NULL AND estimated_lead_business_days IS NOT NULL)`; `NOT approval_valid OR is_current`.

**Design notes:** 96 metadata rows: 80 current versions get only actually supplied current header prices/terms (priced_partial); 16 earlier versions remain metadata_only with absent amounts. USD on older rows is a declared dataset currency invariant, not an inferred past total. New live revisions require complete snapshots and associated line validation. Only is_current and approval_valid may change after insertion; immutable pricing/specification fields never do. Source flags are historical metadata, not proof of present release authorization.

## 29. `quote_line_items`

**Domain:** Quotation. **Purpose:** Exact version-specific priced part snapshot.

**Source or requirement:** `transactional_data/quote_line_items.csv`. **Primary key:** `(quote_line_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `quote_line_id` | `TEXT` | No | `none` | PK | quote_line_items.quote_line_id |
| `quote_id` | `TEXT` | No | `none` | FK | quote_line_items.quote_id |
| `quote_version` | `INTEGER` | No | `none` | FK | quote_line_items.quote_version |
| `line_number` | `INTEGER` | No | `none` | — | quote_line_items.line_number |
| `part_id` | `TEXT` | No | `none` | — | quote_line_items.part_id |
| `part_json` | `JSONB` | No | `none` | — | quote_line_items.part_json |
| `cost_breakdown_json` | `JSONB` | No | `none` | — | quote_line_items.cost_breakdown_json |
| `quantity` | `INTEGER` | No | `none` | — | quote_line_items.quantity |
| `line_total_usd` | `NUMERIC(18,2)` | No | `none` | — | quote_line_items.line_total_usd |

**Unique constraints:** `(quote_id, quote_version, line_number)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(quote_id, quote_version) → quote_versions(quote_id, version)`.

**Additional CHECKs:** `quote_version >= 1 AND line_number > 0 AND quantity > 0 AND line_total_usd >= 0`; `jsonb_typeof(part_json) = 'object' AND jsonb_typeof(cost_breakdown_json) = 'object'`.

**Design notes:** part_id is a version snapshot identity, not an FK to a nonexistent global part master. Validate Part schema, nested material/finish references, quantity/part-ID/line-total agreement, and rules in the service/import preflight. JSONB preserves exact Decimal strings, including mass values with more than six decimal places; do not round nested snapshot evidence into inventory columns. No earlier-version lines are synthesized.

## 30. `approval_requests`

**Domain:** Approval. **Purpose:** Actionable human review queue for an exact version.

**Source or requirement:** `new application requirement; request_approval output`. **Primary key:** `(approval_request_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `approval_request_id` | `TEXT` | No | `none` | PK | server APPREQ-<UUID> |
| `quote_id` | `TEXT` | No | `none` | FK | request_approval.quote_id |
| `quote_version` | `INTEGER` | No | `none` | FK | request_approval.quote_version |
| `requested_by` | `TEXT` | No | `none` | FK | authenticated actor |
| `requested_at` | `TIMESTAMPTZ` | No | `CURRENT_TIMESTAMP` | — | application |
| `required_role` | `TEXT` | No | `'sales_manager'` | FK | application |
| `status` | `TEXT` | No | `'pending'` | — | application |
| `approval_rule_version` | `TEXT` | No | `none` | FK | pinned policy at request time |
| `resolved_by` | `TEXT` | Yes | `none` | FK | application |
| `resolved_at` | `TIMESTAMPTZ` | Yes | `none` | — | application |

**Unique constraints:** `(approval_request_id, quote_id, quote_version)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(quote_id, quote_version) → quote_versions(quote_id, version)`.
- `(requested_by) → users(user_id)`.
- `(resolved_by) → users(user_id)`.
- `(required_role) → roles(role)`.
- `(approval_rule_version) → business_rule_versions(rule_version)`.

**Additional CHECKs:** `status IN ('pending','approved','rejected','superseded')`; `required_role = 'sales_manager'`; `(status = 'pending' AND resolved_by IS NULL AND resolved_at IS NULL) OR (status <> 'pending' AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)`.

**Design notes:** Partial unique (quote_id,quote_version) WHERE status=pending prevents parallel open requests. Supersession records the authenticated revising actor as resolved_by and appends audit evidence. Historical submitted events do not create fictional queue rows. Human decisions must validate current version and role regardless of required_role metadata.

## 31. `approval_history`

**Domain:** Approval. **Purpose:** Append-only factual approval/submission/release events.

**Source or requirement:** `transactional_data/approval_history.csv + future human service events`. **Primary key:** `(approval_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `approval_id` | `TEXT` | No | `none` | PK | approval_history.approval_id |
| `quote_id` | `TEXT` | No | `none` | FK | approval_history.quote_id |
| `quote_version` | `INTEGER` | No | `none` | FK | approval_history.quote_version |
| `action` | `TEXT` | No | `none` | — | approval_history.action |
| `actor_id` | `TEXT` | No | `none` | FK | approval_history.actor_id |
| `action_at` | `TIMESTAMPTZ` | No | `none` | — | approval_history.action_at |
| `reason` | `TEXT` | No | `none` | — | approval_history.reason |
| `approval_request_id` | `TEXT` | Yes | `none` | FK | future review queue; absent historically |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(quote_id, quote_version) → quote_versions(quote_id, version)`.
- `(actor_id) → users(user_id)`.
- `(approval_request_id, quote_id, quote_version) → approval_requests(approval_request_id, quote_id, quote_version)`.

**Additional CHECKs:** `action IN ('submitted','approved','rejected','released','superseded')`; `quote_version >= 1`.

**Design notes:** MATCH SIMPLE permits NULL approval_request_id on historical events while the separate version FK still applies. Historical identities are preserved without claiming their present role/region proves past authorization. No autonomous AI approval/release; protected service path supplies human evidence for new events.

## 32. `idempotency_records`

**Domain:** Reliability. **Purpose:** Atomic committed write receipts and replay protection.

**Source or requirement:** `new application reliability requirement`. **Primary key:** `(receipt_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `receipt_id` | `UUID` | No | `application UUIDv4` | PK | application |
| `actor_id` | `TEXT` | No | `none` | FK | authenticated actor |
| `customer_id` | `TEXT` | No | `none` | FK | authorized operation scope |
| `operation` | `TEXT` | No | `none` | — | server operation name |
| `idempotency_key` | `TEXT` | No | `none` | — | stable logical operation key; nonblank; gateway bounded to 256 characters |
| `request_sha256` | `TEXT` | No | `none` | — | canonical operation payload + immutable scope digest; 64 lowercase hexadecimal characters |
| `result_json` | `JSONB` | No | `none` | — | minimal validated successful tool/human reply; object |
| `correlation_id` | `TEXT` | No | `none` | — | verified workflow/request correlation |
| `committed_at` | `TIMESTAMPTZ` | No | `CURRENT_TIMESTAMP` | — | application |

**Unique constraints:** `(actor_id, customer_id, operation, idempotency_key)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(actor_id) → users(user_id)`.
- `(customer_id) → customers(customer_id)`.

**Additional CHECKs:** `operation IN ('create_rfq','create_quote_draft','request_approval','approve_quote','reject_quote','release_quote','edit_quote_draft')`.

**Design notes:** Only committed success receipts persist, in the same transaction as business rows and success audit. A rolled-back command leaves no success receipt. Do not persist a receipt in an earlier independent transaction. Unique-key contention waits for the winner; mismatched request hashes yield CONFLICT. Customer-level business uniqueness separately prevents duplicate quotation families across different actors/keys. Legacy quote keys remain provenance and are not fabricated authenticated receipts. Retain receipts for the supported operation replay lifetime; no automatic TTL deletion.

## 33. `audit_events`

**Domain:** Reliability. **Purpose:** Append-only business/security event evidence, separate from graph checkpoints.

**Source or requirement:** `new application audit requirement`. **Primary key:** `(event_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `event_id` | `UUID` | No | `application UUIDv4` | PK | application |
| `occurred_at` | `TIMESTAMPTZ` | No | `CURRENT_TIMESTAMP` | — | application |
| `actor_id` | `TEXT` | Yes | `none` | FK | verified actor; NULL for unauthenticated denials |
| `agent_name` | `TEXT` | Yes | `none` | — | verified orchestration identity; NULL for human/system |
| `customer_id` | `TEXT` | Yes | `none` | FK | resolved authorized scope; NULL if unknown |
| `rfq_id` | `TEXT` | Yes | `none` | FK | existing target RFQ |
| `quote_id` | `TEXT` | Yes | `none` | FK | existing target quote |
| `quote_version` | `INTEGER` | Yes | `none` | FK | known target revision |
| `operation` | `TEXT` | No | `none` | — | server action/tool name |
| `outcome` | `TEXT` | No | `none` | — | success / denied / error |
| `correlation_id` | `TEXT` | No | `none` | — | server trace/request identity |
| `event_data` | `JSONB` | No | `'{}'::jsonb` | — | allow-listed reason, policy versions, evidence refs, authenticated commercial-review/clearance evidence and safe change summary; object |

**Unique constraints:** PK only. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(actor_id) → users(user_id)`.
- `(customer_id) → customers(customer_id)`.
- `(rfq_id) → rfqs(rfq_id)`.
- `(quote_id) → quotes(quote_id)`.
- `(quote_id, quote_version) → quote_versions(quote_id, version)`.

**Additional CHECKs:** `outcome IN ('success','denied','error')`; `quote_version IS NULL OR (quote_id IS NOT NULL AND quote_version >= 1)`.

**Design notes:** Unknown/nonexistent attempted IDs belong in protected event_data, not invalid FKs. Known customer/RFQ/quote chains must agree under the service. No FK from authoritative business records into orchestration checkpoints. Redact full customer text/secrets and never log hidden labels. Committed successes audit atomically; denials/failures audit in an independent transaction after any business rollback.

## 34. `workflow_runs`

**Domain:** Workflow. **Purpose:** Orchestration run metadata and opaque checkpoint-store reference, not business truth.

**Source or requirement:** `new LangGraph orchestration requirement`. **Primary key:** `(workflow_run_id)`.

| Column | PostgreSQL type | NULL? | Default / generated expression | Key | Source / application purpose / local rule |
| --- | --- | --- | --- | --- | --- |
| `workflow_run_id` | `UUID` | No | `application UUIDv4` | PK | application |
| `customer_id` | `TEXT` | No | `none` | FK | authenticated run scope |
| `rfq_id` | `TEXT` | Yes | `none` | FK | created RFQ; nullable before create_rfq |
| `initiated_by` | `TEXT` | No | `none` | FK | authenticated delegation actor |
| `correlation_id` | `TEXT` | No | `none` | — | handoff correlation_id |
| `external_workflow_id` | `TEXT` | Yes | `none` | — | optional fixture handoff.workflow_id; separate from business rfq_id |
| `status` | `TEXT` | No | `'running'` | — | application |
| `graph_version` | `TEXT` | No | `none` | — | deployed graph definition identity |
| `rule_versions` | `JSONB` | No | `none` | — | immutable run-level policy/contract pins; object |
| `source_snapshot_versions` | `JSONB` | No | `none` | — | verified handoff snapshot pins; object |
| `checkpoint_ref` | `TEXT` | Yes | `none` | — | opaque saver-owned checkpoint/thread reference; no embedded credentials |
| `created_at` | `TIMESTAMPTZ` | No | `CURRENT_TIMESTAMP` | — | application |
| `updated_at` | `TIMESTAMPTZ` | No | `CURRENT_TIMESTAMP` | — | application |

**Unique constraints:** `(correlation_id)`. Partial uniqueness, where required, is listed in the index catalog.

**Foreign keys / dependencies:**

- `(customer_id) → customers(customer_id)`.
- `(rfq_id) → rfqs(rfq_id)`.
- `(initiated_by) → users(user_id)`.

**Additional CHECKs:** `status IN ('running','waiting_human','blocked','failed','completed')`; `updated_at >= created_at`.

**Design notes:** This table lives in orchestration; all other specified tables live in business. Saver-owned physical checkpoint tables are deliberately not invented or specified: select the LangGraph saver/version in its later roadmap phase and use a distinct credential/schema/store. checkpoint_ref is external, not an FK. On resume, reload business state, actor authorization and receipts; a checkpoint cannot authorize writes or human approval.

## 35. Physical implementation handoff

Day 5 should expand these common conventions into named constraints, create nonpartial unique keys before referencing FKs, and add the two deferred quote pointers only after both tables exist. Generated columns are a logical proposal; exercise their expressions against the selected PostgreSQL version in Day 5. Provision separate owner/migration, backend runtime, human-review, audit-writer and orchestration credentials in the later security implementation. Restrict mutable quote-version updates to current/approval flags; enforce immutable evidence through privileges and backend APIs, with triggers only if later threat modeling requires them.

Saver-owned LangGraph checkpoint tables are **outside this 33-table logical application catalog**. Their actual physical schema must come from the selected saver package and migration lifecycle, not invented Day 4 columns. Their payload can contain untrusted input and must be independently isolated and protected. The workflow metadata table only stores an opaque external checkpoint_ref. No business record depends on a successful checkpoint write.
