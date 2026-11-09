# Day 4 — Dataset-to-database mapping

**Design only. No imports, seeding code, source modifications or database execution.** Every path below is relative to `data/rfq_synthetic_data/` unless otherwise stated; filename links resolve to the frozen source. Future seeding should run only in an explicitly selected synthetic development/test environment. Hidden expected outcomes never populate production-facing PostgreSQL tables.

## 1. Shared transformation and identity contract

1. Read exact UTF-8 CSV headers and JSON contracts; reject unexpected/missing fields or duplicate keys. All CSV values are wire strings. Preserve source IDs exactly, including prefixes/case/leading zeros; resolve FKs after parents are validated. No UUID replacement, email matching, fuzzy FK lookup, or placeholder parent creation.
2. `TEXT`: preserve business content; trim only for validation, not silently for identity conversion. Literal `none` is a real finish value or restrictions prose, not SQL NULL. Source IDs must be nonblank and ≤64 characters. No global part master exists.
3. `NUMERIC(p,s)`: parse decimal strings with Decimal, reject nonfinite/overflow and nonzero precision beyond the target scale; preserve exact JSON Decimal strings. Quantize only by declared policy, never via float. Final USD line/quote cents use ROUND_HALF_UP at the calculator boundary. Scales/types are defined in [table conventions](table_schemas.md).
4. `INTEGER`/`BIGINT`: strict integer parsing, bounds and per-column positive/nonnegative checks. Stored quote versions begin at 1; zero is only the tool's creation sentinel. `BOOLEAN`: explicitly map source `True`→true, `False`→false; reject other tokens. Never use truthiness of a nonempty string.
5. `DATE`: parse/round-trip ISO YYYY-MM-DD; preserve calendar date. `TIMESTAMPTZ`: require ISO timezone (`Z` in source), parse as aware UTC; reject naive times. Do not turn a missing approval time into creation/import time.
6. Empty CSV string becomes NULL **only where the target is business-optional**. Source column-required means field presence, not NOT NULL semantics. Missing mandatory keys/quantities/monetary facts fail validation, rather than becoming zero. Other optional historical facts not supplied at all remain NULL.
7. `JSONB`: parse serialized JSON exactly once; validate top-level shape and entity/Part schema where available. Preserve nested Decimal strings and source references. Explode justified catalog arrays into keyed rows; reject duplicates and unknown targets rather than silently dropping them. Do not relationalize every config or calculation detail.
8. Derived inventory/capacity fields are validated against source arithmetic, then omitted from insert input and recomputed as generated attributes. Retain original source bytes in immutable approved snapshot archives. Generated values are not seeded independently.
9. Source `pricing_rule_version` resolves to immutable business_rule_versions with kind=pricing. Future live revisions require additional rules/snapshot provenance; historical records cannot be assigned an unproved historical pin merely because today's config is available.

## 2. Source inventory and dependency stages

| Exact CSV path | Rows | Columns | Primary target | Import stage |
| --- | --- | --- | --- | --- |
| [master_data/contacts.csv](../../data/rfq_synthetic_data/master_data/contacts.csv) | 40 | 6 | contacts | 2–3 |
| [master_data/customers.csv](../../data/rfq_synthetic_data/master_data/customers.csv) | 40 | 15 | customers | 2–3 |
| [master_data/finishes.csv](../../data/rfq_synthetic_data/master_data/finishes.csv) | 7 | 4 | finishes | 2–3 |
| [master_data/materials.csv](../../data/rfq_synthetic_data/master_data/materials.csv) | 15 | 11 | materials | 2–3 |
| [master_data/operations.csv](../../data/rfq_synthetic_data/master_data/operations.csv) | 8 | 4 | operations | 2–3 |
| [master_data/roles_permissions.csv](../../data/rfq_synthetic_data/master_data/roles_permissions.csv) | 27 | 3 | role_permissions | 2–3 |
| [master_data/suppliers.csv](../../data/rfq_synthetic_data/master_data/suppliers.csv) | 6 | 4 | suppliers | 2–3 |
| [master_data/users.csv](../../data/rfq_synthetic_data/master_data/users.csv) | 24 | 7 | users | 2–3 |
| [master_data/workcentres.csv](../../data/rfq_synthetic_data/master_data/workcentres.csv) | 8 | 6 | workcentres | 2–3 |
| [operations_data/material_inventory.csv](../../data/rfq_synthetic_data/operations_data/material_inventory.csv) | 90 | 14 | material_inventory | 4 |
| [operations_data/operation_routing.csv](../../data/rfq_synthetic_data/operations_data/operation_routing.csv) | 28 | 6 | operation_routing | 4 |
| [operations_data/procurement_records.csv](../../data/rfq_synthetic_data/operations_data/procurement_records.csv) | 12 | 7 | procurement_records | 4 |
| [operations_data/production_capacity.csv](../../data/rfq_synthetic_data/operations_data/production_capacity.csv) | 80 | 9 | production_capacity | 4 |
| [transactional_data/approval_history.csv](../../data/rfq_synthetic_data/transactional_data/approval_history.csv) | 70 | 7 | approval_history | 6 |
| [transactional_data/historical_quotes.csv](../../data/rfq_synthetic_data/transactional_data/historical_quotes.csv) | 80 | 17 | quotes | 6 |
| [transactional_data/quote_line_items.csv](../../data/rfq_synthetic_data/transactional_data/quote_line_items.csv) | 100 | 9 | quote_line_items | 6 |
| [transactional_data/quote_versions.csv](../../data/rfq_synthetic_data/transactional_data/quote_versions.csv) | 96 | 7 | quote_versions | 6 |

All **17 designated business CSV files / 731 rows** are mapped below, including roles_permissions. The 16 formally contracted CSVs contribute 704 rows. Attachment BOM CSVs are document evidence and are **not** part of this 731-row business-table inventory. Additional normalized rows are projections, not extra source files: 6 roles, 12 permissions, 90 thickness options, 50 material-finish pairs, 90 material-operation pairs, and 19 finish-category memberships.

Import stages: 1 immutable rule/contract/snapshot registries and preflight; 2 roles/permissions/users/customers/suppliers/operations/materials/finishes; 3 contacts/workcentres/compatibility junctions/grants; 4 inventory/capacity/routing then procurement; 5 historical RFQs; 6 atomic quote headers+versions+lines+approval events. References and partial business uniqueness are validated before commit. New application-support records remain absent unless generated by a real authenticated future operation. Detailed atomic ordering appears in Section 6.

## 3. Complete business CSV field mappings

For each row, resolve the full FK/composite as specified in the target schema and [FK catalog](relationships_and_constraints.md). Empty counts below were measured in the actual source. “Required value” means preflight rejects blank source; a target column nullable for live business reasons does not create missing baseline values. A source may map to a generated target only for validation, not insertion.

### `master_data/contacts.csv` — 40 rows

Source: [master_data/contacts.csv](../../data/rfq_synthetic_data/master_data/contacts.csv). Primary target: `contacts`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `contact_id` | `contacts.contact_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `customer_id` | `contacts.customer_id` | TEXT via shared strict parser.  | Required value; zero empty | customers(customer_id) |
| `contact_name` | `contacts.contact_name` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `email` | `contacts.email` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `phone` | `contacts.phone` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `is_primary` | `contacts.is_primary` | BOOLEAN via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |

### `master_data/customers.csv` — 40 rows

Source: [master_data/customers.csv](../../data/rfq_synthetic_data/master_data/customers.csv). Primary target: `customers`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `customer_id` | `customers.customer_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `company_name` | `customers.company_name` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `industry` | `customers.industry` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `city` | `customers.city` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `state` | `customers.state` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `country` | `customers.country` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `region` | `customers.region` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `account_status` | `customers.account_status` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `payment_terms` | `customers.payment_terms` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `credit_limit_usd` | `customers.credit_limit_usd` | NUMERIC(18,2) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `credit_exposure_usd` | `customers.credit_exposure_usd` | NUMERIC(18,2) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `preferred_shipping` | `customers.preferred_shipping` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `restrictions` | `customers.restrictions` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `created_date` | `customers.created_date` | DATE via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `approval_requirement` | `customers.approval_requirement` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |

### `master_data/finishes.csv` — 7 rows

Source: [master_data/finishes.csv](../../data/rfq_synthetic_data/master_data/finishes.csv). Primary target: `finishes`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `finish_id` | `finishes.finish_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `display_name` | `finishes.display_name` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `material_categories` | `finish_material_categories.material_category` | JSON array → one (finish_id,material_category) row each; owner finish_id copied; validate controlled category vocabulary. TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `pricing_rule_version` | `finishes.pricing_rule_version` | TEXT via shared strict parser.  | Required value; zero empty | business_rule_versions(rule_version) |

### `master_data/materials.csv` — 15 rows

Source: [master_data/materials.csv](../../data/rfq_synthetic_data/master_data/materials.csv). Primary target: `materials`. Import scalar catalog first, then explode three arrays after finish/operation parents exist.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `material_id` | `materials.material_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `category` | `materials.category` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `grade` | `materials.grade` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `density_kg_m3` | `materials.density_kg_m3` | NUMERIC(18,6) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `cost_usd_per_kg` | `materials.cost_usd_per_kg` | NUMERIC(18,4) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `supported_thickness_mm` | `material_thickness_options.thickness_mm` | JSON array → one (material_id,value) row each; owner material_id copied; exact Decimal values for thickness; reject duplicates/unresolved targets. NUMERIC(12,3) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `compatible_finishes` | `material_finish_compatibility.finish_id` | JSON array → one (material_id,value) row each; owner material_id copied; exact Decimal values for thickness; reject duplicates/unresolved targets. TEXT via shared strict parser.  | Required value; zero empty | finishes(finish_id) |
| `supported_operations` | `material_supported_operations.operation_id` | JSON array → one (material_id,value) row each; owner material_id copied; exact Decimal values for thickness; reject duplicates/unresolved targets. TEXT via shared strict parser.  | Required value; zero empty | operations(operation_id) |
| `min_bend_radius_factor` | `materials.min_bend_radius_factor` | NUMERIC(12,3) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `availability_class` | `materials.availability_class` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `pricing_rule_version` | `materials.pricing_rule_version` | TEXT via shared strict parser.  | Required value; zero empty | business_rule_versions(rule_version) |

### `master_data/operations.csv` — 8 rows

Source: [master_data/operations.csv](../../data/rfq_synthetic_data/master_data/operations.csv). Primary target: `operations`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `operation_id` | `operations.operation_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `display_name` | `operations.display_name` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `is_quoted` | `operations.is_quoted` | BOOLEAN via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `notes` | `operations.notes` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |

### `master_data/roles_permissions.csv` — 27 rows

Source: [master_data/roles_permissions.csv](../../data/rfq_synthetic_data/master_data/roles_permissions.csv). Primary target: `role_permissions`. Derive roles and permissions from distinct values before grant insertion.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `role` | `role_permissions.role` | TEXT via shared strict parser.  | Required value; zero empty | roles(role) |
| `permission` | `role_permissions.permission` | TEXT via shared strict parser.  | Required value; zero empty | permissions(permission) |
| `allowed` | `role_permissions.allowed` | BOOLEAN via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |

### `master_data/suppliers.csv` — 6 rows

Source: [master_data/suppliers.csv](../../data/rfq_synthetic_data/master_data/suppliers.csv). Primary target: `suppliers`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `supplier_id` | `suppliers.supplier_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `company_name` | `suppliers.company_name` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `email` | `suppliers.email` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `normal_lead_business_days` | `suppliers.normal_lead_business_days` | INTEGER via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |

### `master_data/users.csv` — 24 rows

Source: [master_data/users.csv](../../data/rfq_synthetic_data/master_data/users.csv). Primary target: `users`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `user_id` | `users.user_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `display_name` | `users.display_name` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `role` | `users.role` | TEXT via shared strict parser.  | Required value; zero empty | roles(role) |
| `email` | `users.email` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `active` | `users.active` | BOOLEAN via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `assigned_region` | `users.assigned_region` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `authentication_subject` | `users.authentication_subject` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |

### `master_data/workcentres.csv` — 8 rows

Source: [master_data/workcentres.csv](../../data/rfq_synthetic_data/master_data/workcentres.csv). Primary target: `workcentres`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `workcentre_id` | `workcentres.workcentre_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `operation_id` | `workcentres.operation_id` | TEXT via shared strict parser.  | Required value; zero empty | operations(operation_id) |
| `shift_calendar` | `workcentres.shift_calendar` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `weekly_nominal_hours` | `workcentres.weekly_nominal_hours` | NUMERIC(12,4) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `downtime_hours_per_week` | `workcentres.downtime_hours_per_week` | NUMERIC(12,4) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `planning_horizon_weeks` | `workcentres.planning_horizon_weeks` | INTEGER via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |

### `operations_data/material_inventory.csv` — 90 rows

Source: [operations_data/material_inventory.csv](../../data/rfq_synthetic_data/operations_data/material_inventory.csv). Primary target: `material_inventory`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `inventory_id` | `material_inventory.inventory_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `material_id` | `material_inventory.material_id` | TEXT via shared strict parser.  | Required value; zero empty | material_thickness_options(material_id,thickness_mm) as complete composite |
| `thickness_mm` | `material_inventory.thickness_mm` | NUMERIC(12,3) via shared strict parser.  | Required value; zero empty | material_thickness_options(material_id,thickness_mm) as complete composite |
| `stock_form` | `material_inventory.stock_form` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `quantity_kg` | `material_inventory.quantity_kg` | NUMERIC(18,6) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `reserved_kg` | `material_inventory.reserved_kg` | NUMERIC(18,6) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `available_kg` | `material_inventory.available_kg` | Validate original arithmetic then omit from insert; target is generated. NUMERIC(18,6) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `unit` | `material_inventory.unit` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `warehouse` | `material_inventory.warehouse` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `reorder_threshold_kg` | `material_inventory.reorder_threshold_kg` | NUMERIC(18,6) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `supplier_id` | `material_inventory.supplier_id` | TEXT via shared strict parser.  | Required value; zero empty | suppliers(supplier_id) |
| `supplier_lead_business_days` | `material_inventory.supplier_lead_business_days` | INTEGER via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `expected_replenishment_date` | `material_inventory.expected_replenishment_date` | DATE via shared strict parser.  | 75 empty → NULL | No FK; validate local key/value rules |
| `snapshot_date` | `material_inventory.snapshot_date` | DATE via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |

### `operations_data/operation_routing.csv` — 28 rows

Source: [operations_data/operation_routing.csv](../../data/rfq_synthetic_data/operations_data/operation_routing.csv). Primary target: `operation_routing`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `routing_id` | `operation_routing.routing_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `part_type` | `operation_routing.part_type` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `operation_id` | `operation_routing.operation_id` | TEXT via shared strict parser.  | Required value; zero empty | workcentres(workcentre_id,operation_id) as complete composite |
| `sequence` | `operation_routing.sequence` | INTEGER via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `required_when` | `operation_routing.required_when` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `workcentre_id` | `operation_routing.workcentre_id` | TEXT via shared strict parser.  | Required value; zero empty | workcentres(workcentre_id,operation_id) as complete composite |

### `operations_data/procurement_records.csv` — 12 rows

Source: [operations_data/procurement_records.csv](../../data/rfq_synthetic_data/operations_data/procurement_records.csv). Primary target: `procurement_records`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `procurement_id` | `procurement_records.procurement_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `inventory_id` | `procurement_records.inventory_id` | TEXT via shared strict parser.  | Required value; zero empty | material_inventory(inventory_id) |
| `supplier_id` | `procurement_records.supplier_id` | TEXT via shared strict parser.  | Required value; zero empty | suppliers(supplier_id) |
| `quantity_kg` | `procurement_records.quantity_kg` | NUMERIC(18,6) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `ordered_date` | `procurement_records.ordered_date` | DATE via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `expected_receipt_date` | `procurement_records.expected_receipt_date` | DATE via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `status` | `procurement_records.status` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |

### `operations_data/production_capacity.csv` — 80 rows

Source: [operations_data/production_capacity.csv](../../data/rfq_synthetic_data/operations_data/production_capacity.csv). Primary target: `production_capacity`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `capacity_id` | `production_capacity.capacity_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `workcentre_id` | `production_capacity.workcentre_id` | TEXT via shared strict parser.  | Required value; zero empty | workcentres(workcentre_id) |
| `week_start` | `production_capacity.week_start` | DATE via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `nominal_hours` | `production_capacity.nominal_hours` | NUMERIC(12,4) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `downtime_hours` | `production_capacity.downtime_hours` | NUMERIC(12,4) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `available_hours` | `production_capacity.available_hours` | Validate original arithmetic then omit from insert; target is generated. NUMERIC(12,4) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `scheduled_hours` | `production_capacity.scheduled_hours` | NUMERIC(12,4) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `remaining_hours` | `production_capacity.remaining_hours` | Validate original arithmetic then omit from insert; target is generated. NUMERIC(12,4) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `utilization_fraction` | `production_capacity.utilization_fraction` | Validate original arithmetic then omit from insert; target is generated. NUMERIC(9,4) via shared strict parser.  | No empty values; retain source values | No FK; validate local key/value rules |

### `transactional_data/approval_history.csv` — 70 rows

Source: [transactional_data/approval_history.csv](../../data/rfq_synthetic_data/transactional_data/approval_history.csv). Primary target: `approval_history`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `approval_id` | `approval_history.approval_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `quote_id` | `approval_history.quote_id` | TEXT via shared strict parser.  | Required value; zero empty | quote_versions(quote_id,version) as complete composite; approval_requests(approval_request_id,quote_id,quote_version) as complete composite |
| `quote_version` | `approval_history.quote_version` | INTEGER via shared strict parser.  | Required value; zero empty | quote_versions(quote_id,version) as complete composite; approval_requests(approval_request_id,quote_id,quote_version) as complete composite |
| `action` | `approval_history.action` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `actor_id` | `approval_history.actor_id` | TEXT via shared strict parser.  | Required value; zero empty | users(user_id) |
| `action_at` | `approval_history.action_at` | TIMESTAMPTZ via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `reason` | `approval_history.reason` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |

### `transactional_data/historical_quotes.csv` — 80 rows

Source: [transactional_data/historical_quotes.csv](../../data/rfq_synthetic_data/transactional_data/historical_quotes.csv). Primary target: `quotes`. Join only the header current version to version financial columns; circular header pointers are deferred within one atomic transaction.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `quote_id` | `quotes.quote_id` | TEXT via shared strict parser.  | Required value; zero empty | quote_versions(quote_id,version) as complete composite; quote_versions(quote_id,version) as complete composite |
| `rfq_id` | `quotes.rfq_id` | TEXT via shared strict parser.  | Required value; zero empty | rfqs(rfq_id) |
| `version` | `quotes.current_version` | INTEGER via shared strict parser.  | Required value; zero empty | quote_versions(quote_id,version) as complete composite |
| `status` | `quotes.status` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `currency` | `quote_versions.currency` | Only row (quote_id,version=header.version); older revision value unavailable. TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `total_usd` | `quote_versions.total_usd` | Only row (quote_id,version=header.version); older revision value unavailable. NUMERIC(18,2) via shared strict parser.  | No empty values; retain source values | No FK; validate local key/value rules |
| `pricing_rule_version` | `quote_versions.pricing_rule_version` | Only row (quote_id,version=header.version); older revision value unavailable. TEXT via shared strict parser.  | No empty values; retain source values | business_rule_versions(rule_version) |
| `created_by` | `quotes.created_by` | TEXT via shared strict parser.  | Required value; zero empty | users(user_id) |
| `created_at` | `quotes.created_at` | TIMESTAMPTZ via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `submitted_by` | `quotes.submitted_by` | TEXT via shared strict parser.  | 40 empty → NULL | users(user_id) |
| `approved_version` | `quotes.approved_version` | INTEGER via shared strict parser.  | 60 empty → NULL | quote_versions(quote_id,version) as complete composite |
| `approver_id` | `quotes.approver_id` | TEXT via shared strict parser.  | 50 empty → NULL | users(user_id) |
| `approval_timestamp` | `quotes.approval_timestamp` | TIMESTAMPTZ via shared strict parser.  | 60 empty → NULL | No FK; validate local key/value rules |
| `rejection_reason` | `quotes.rejection_reason` | TEXT via shared strict parser.  | 70 empty → NULL | No FK; validate local key/value rules |
| `expiry_date` | `quote_versions.expiry_date` | Only row (quote_id,version=header.version); older revision value unavailable. DATE via shared strict parser.  | No empty values; retain source values | No FK; validate local key/value rules |
| `estimated_lead_business_days` | `quote_versions.estimated_lead_business_days` | Only row (quote_id,version=header.version); older revision value unavailable. INTEGER via shared strict parser.  | No empty values; retain source values | No FK; validate local key/value rules |
| `idempotency_key` | `quotes.legacy_idempotency_key` | Preserve provenance only; do not fabricate authenticated receipt scope. TEXT via shared strict parser.  | No empty values; retain source values | No FK; validate local key/value rules |

### `transactional_data/quote_line_items.csv` — 100 rows

Source: [transactional_data/quote_line_items.csv](../../data/rfq_synthetic_data/transactional_data/quote_line_items.csv). Primary target: `quote_line_items`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `quote_line_id` | `quote_line_items.quote_line_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `quote_id` | `quote_line_items.quote_id` | TEXT via shared strict parser.  | Required value; zero empty | quote_versions(quote_id,version) as complete composite |
| `quote_version` | `quote_line_items.quote_version` | INTEGER via shared strict parser.  | Required value; zero empty | quote_versions(quote_id,version) as complete composite |
| `line_number` | `quote_line_items.line_number` | INTEGER via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `part_id` | `quote_line_items.part_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `part_json` | `quote_line_items.part_json` | JSONB via shared strict parser. Validate JSON object, nested Part/material/finish references, quantity/ID/total agreement; retain Decimal strings.  | Required value; zero empty | No FK; validate local key/value rules |
| `cost_breakdown_json` | `quote_line_items.cost_breakdown_json` | JSONB via shared strict parser. Validate JSON object, nested Part/material/finish references, quantity/ID/total agreement; retain Decimal strings.  | Required value; zero empty | No FK; validate local key/value rules |
| `quantity` | `quote_line_items.quantity` | INTEGER via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `line_total_usd` | `quote_line_items.line_total_usd` | NUMERIC(18,2) via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |

### `transactional_data/quote_versions.csv` — 96 rows

Source: [transactional_data/quote_versions.csv](../../data/rfq_synthetic_data/transactional_data/quote_versions.csv). Primary target: `quote_versions`.

| Exact source field | Exact target column | Transformation / validation | Null handling | FK resolution / dependency |
| --- | --- | --- | --- | --- |
| `quote_version_id` | `quote_versions.quote_version_id` | TEXT via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `quote_id` | `quote_versions.quote_id` | TEXT via shared strict parser.  | Required value; zero empty | quotes(quote_id) |
| `version` | `quote_versions.version` | INTEGER via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `created_by` | `quote_versions.created_by` | TEXT via shared strict parser.  | Required value; zero empty | users(user_id) |
| `created_at` | `quote_versions.created_at` | TIMESTAMPTZ via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `is_current` | `quote_versions.is_current` | BOOLEAN via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |
| `approval_valid` | `quote_versions.approval_valid` | BOOLEAN via shared strict parser.  | Required value; zero empty | No FK; validate local key/value rules |


### Required-field/empty-string findings

Inventory.expected_replenishment_date has **75** blanks. Historical quotes have **40** blank submitted_by, **60** approved_version, **50** approver_id, **60** approval_timestamp and **70** rejection_reason. Those cells become NULL, not empty FK strings. All ten rejected headers have an approver_id but no approved_version/timestamp; preserve the reviewer identity without inventing approval. All 20 approved/expired headers carrying an approved_version refer to their current version and independent creator/approver IDs.

No blanket transformation fills all blank source cells or all NULL target columns. The 16 earlier version rows lack financial payloads rather than containing an empty source financial column. Their new nullable financial columns stay NULL and snapshot_completeness=metadata_only. Current 80 get only their supplied header/line facts with snapshot_completeness=priced_partial. Source is_current and approval_valid flags are imported as historical metadata after consistency checks; they are not proof of live release eligibility.

## 4. JSON, attachments, configuration and contract mapping

### Historical RFQs — 80 records

Source: [transactional_data/historical_rfqs.json](../../data/rfq_synthetic_data/transactional_data/historical_rfqs.json). Load at stage 5 after customers. This source is business history, not a scenario fixture.

| Source field | Target | Transformation / validation |
| --- | --- | --- |
| rfq_id | rfqs.rfq_id | Preserve HRFQ string PK; exact quote RFQ FK must resolve. |
| customer_id | rfqs.customer_id | Resolve customers.customer_id before child insertion. |
| submitted_at | rfqs.submitted_at | Aware UTC TIMESTAMPTZ; no current-time replacement. |
| raw_request | rfqs.raw_request | Preserve condensed untrusted source prose, not a parsed specification. |
| source_type | rfqs.source_type | Preserve historical_internal. |
| part_ids | rfqs.part_ids | Validate array of opaque string part references; retain JSONB; compare against supplied quote-line part_ids as preflight where present; no part master FK. |

Additional RFQ application columns: status=archived marks imported history, created_by=NULL (not supplied), row_version=1 is an importer-assigned mutation baseline. No historical RFQ editable lines, attachments or actor IDs are inferred from labels. Source quote lines remain immutable priced snapshots rather than falsely reconstructed extraction revisions.

### RFQ scenarios — 120 development / 40 validation / 80 frozen

Sources: `rfq_scenarios/development/*.json`, `rfq_scenarios/validation/*.json`, `rfq_scenarios/frozen_evaluation/*.json`. They remain harness inputs, with **no automatic production seeding**. The following describes deliberate ingestion of an assigned visible scenario into an isolated test environment through authorized interfaces, not a direct merge with historical RFQs.

| Exact scenario field | Controlled test target/location | Transformation / limit |
| --- | --- | --- |
| rfq_id | Harness fixture key; workflow_runs.external_workflow_id in an intentional test run | Keep RFQ-xxxx; production business ID assigned by authenticated intake and explicitly correlated. Never infer label access from shared ID. |
| customer_id | rfqs.customer_id when an authorized test inquiry is registered | Verify seeded customer and run/actor scope; fixture itself grants no account access. |
| submitted_at | rfqs.submitted_at through later controlled ingestion | UTC parsing; no rewriting historical HRFQ records. |
| raw_customer_request | rfqs.raw_request through later controlled ingestion | Rename only; preserve untrusted raw text. create_rfq wire schema cannot accept this field. |
| request_channel | rfqs.source_type through later ingestion | Preserve intake channel; shell creation uses server source_type=mcp_shell until authorized content ingestion. |
| data_classification | Harness boundary / trusted intake validation metadata | No dedicated production column or evaluation-label import; classification stays source/harness metadata. Treat all customer content as untrusted. |
| attachments | rfq_attachments only for assigned controlled test run | Process only referenced files; do not mount archive root or manifest for agents. |

The **offline harness** controls validation and frozen input assignment; their visibility differs from open development inputs. Development scenarios may be agent-visible test inputs in an appropriately controlled test environment. Validation and frozen-evaluation scenarios require separately controlled access. Every split's hidden ground-truth label remains inaccessible to agents and business MCP services, including during development.

### Attachment objects and manifest — 124 referenced files

Source [attachments/attachment_manifest.json](../../data/rfq_synthetic_data/attachments/attachment_manifest.json) is a harness list with **rfq_id, path, split, type**; it has no attachment_id, revision, trust flag or filename field. Those fields come from the actual scenario attachment object, not a fabricated manifest column. Manifest rfq_id joins the harness fixture only; split controls exposure, type cross-checks document_type and path must exist/hash-match. No HRFQ attachment relationship is assumed.

| Exact scenario attachment field | Operational target if deliberately ingested | Transformation / validation |
| --- | --- | --- |
| attachment_id | rfq_attachments.attachment_id | Preserve scoped test ID or explicit intake mapping to assigned business ATT-UUID; reject duplicate PK. |
| filename | rfq_attachments.filename | Preserve actual field; do not claim it is a manifest field. |
| path | rfq_attachments.storage_ref | Allow-listed relative path → backend-only immutable object key; reject traversal; hash file bytes to sha256. |
| revision | rfq_attachments.revision | Preserve nonblank revision; NULL only if future business attachment genuinely lacks one. |
| document_type | rfq_attachments.document_type | Validate pdf/csv/md and file content; compare manifest type. |
| source_role | rfq_attachments.source_role | Preserve customer_supplied_simulated; does not confer business RBAC. |
| is_trusted_instruction | rfq_attachments.is_trusted_instruction | JSON boolean false; CHECK forbids true. Never treat document instructions as gateway authority. |

The 124 files comprise 79 PDF, 44 CSV and 1 Markdown attachment. The owning rfq_id is supplied by the authorized test intake correlation, not a new field invented inside the attachment object. sha256 is computed from referenced file bytes. PDF/Markdown/BOM contents are untrusted evidence read by intake; BOM rows are not seeded straight into priced lines, and missing/contradictory specifications require clarification. No hidden extraction labels populate rfq_line_items. Attachment bytes remain object/filesystem data outside PostgreSQL.

All 44 `attachments/bills_of_materials/*.csv` files have the same 11 source columns and contain **72 document rows** total, separate from the 731 business rows. Their exact filenames/owner split are enumerated by attachment_manifest.path, not guessed from IDs. Treat these columns as candidate extraction evidence only:

| Exact BOM field | Controlled extraction destination / validation |
| --- | --- |
| rfq_id | Compare to assigned harness fixture/authorized inquiry correlation, not a direct production FK. |
| revision | Compare to attachment revision and authoritative customer update; preserve evidence. |
| line | Candidate rfq_line_items.line_number after contradiction/ordinal validation. |
| part_type | Candidate rfq_line_items.part_type under configured vocabulary. |
| length_mm, width_mm, height_mm | Candidate typed dimensions; blank height means unavailable/inapplicable, never zero default. |
| material_id | Resolve known material only after reconciling request/doc authority; unknown stays evidence. |
| quantity | Candidate positive integer; conflicts trigger clarification. |
| tolerance_mm | Candidate Decimal millimetres, checked against manufacturing rules. |
| finish_id | Resolve explicit compatible finish; literal none remains a real finish. |

BOM CSVs have **no thickness_mm, part_id or cost column**. Thickness/part identity must come from other authorized source evidence or explicit application assignment; never from hidden labels. PDF/Markdown attachments have no universal tabular column schema; preserve bytes and source evidence, then validate extraction through intake/engineering.

### Policy/configuration JSON — immutable documents, not tables per key

Each complete object is stored in business_rule_versions.document with exact source_path and SHA-256, not rewritten. The registry's application registration timestamp is not a historical policy effective date. Source root keys below were read from the actual files; all remaining nested keys remain in the document at their original JSON paths.

| Source | Declared version | Registry kind | Exact root keys | Target / transformation |
| --- | --- | --- | --- | --- |
| [config/manufacturing_rules.json](../../data/rfq_synthetic_data/config/manufacturing_rules.json) | mfg-v1.0 | manufacturing | `version`, `units`, `allowed_part_types`, `mandatory_fields`, `height_required`, `weld_length_required`, `limits`, `defaults`, `bend_counts`, `unsupported_operations`, `interpretation` | Full object → document; version → rule_version; kind assigned; byte hash → sha256; immutable FK target |
| [config/pricing_rules.json](../../data/rfq_synthetic_data/config/pricing_rules.json) | price-v1.0 | pricing | `version`, `currency`, `rounding`, `calculation_precision`, `line_total_scale`, `unit_display_scale`, `gross_margin`, `scrap_fraction`, `inspection_minutes_per_unit`, `packaging_usd_per_unit`, `hole_cut_extra_mm`, `workcentre_hourly_rates_usd`, `cutting_mm_per_min`, `bend_minutes_per_bend`, `weld_mm_per_min`, `finishing_usd_per_m2`, `setup_minutes`, `tax_included`, `freight_included`, `shipping_included`, `policy` | Full object → document; version → rule_version; kind assigned; byte hash → sha256; immutable FK target |
| [config/approval_policies.json](../../data/rfq_synthetic_data/config/approval_policies.json) | approval-v1.1 | approval | `admin_does_not_bypass_approval`, `agent_forbidden_actions`, `all_writes_require_authenticated_actor`, `blocked_customer_statuses`, `commercial_review_customer_statuses`, `currency`, `draft_creation_policy`, `engineering_review_conditions`, `human_release_approval_required`, `manager_review_policy`, `manager_review_threshold_usd`, `quote_release_requires`, `release_policy`, `version` | Full object → document; version → rule_version; kind assigned; byte hash → sha256; immutable FK target |
| [config/workflow_state_rules.json](../../data/rfq_synthetic_data/config/workflow_state_rules.json) | workflow-v1.0 | workflow | `version`, `transitions`, `version_bump_requires_approval_reset`, `idempotent_writes`, `immutable_approval_version` | Full object → document; version → rule_version; kind assigned; byte hash → sha256; immutable FK target |

`config/generation_config.yaml` remains an offline source provenance document: version=1.1.0 and reference_date=2026-11-02 populate data_snapshots.dataset_version/as_of_date; inventory's measured snapshot_date must agree. Capacity has **no source snapshot_date or snapshot_version column**: its registry as_of_date is explicitly derived from generation metadata, with week_start left unchanged. schedule_start=2026-11-09 and planning_weeks=10 validate the baseline. Suggested stable tokens are inventory-v1.1.0-2026-11-02 and capacity-v1.1.0-2026-11-02; the service maps these to exact approved CSV byte hashes and immutable archive references. Do not archive the evaluation ZIP for business runtime access.

`config/v1_0_0_preservation_manifest.json` has exact root fields description, sha256, source_archive, version. Keep the complete document offline as compatibility/checksum provenance, not production business rows or new pricing/approval policy. Preserve it unchanged. It has no unverified effect on current rule versions.

### Machine-readable contracts — five documents

| Source | Registry kind | Source version field/value | Exact root keys | Mapping |
| --- | --- | --- | --- | --- |
| [contracts/entity_schemas.json](../../data/rfq_synthetic_data/contracts/entity_schemas.json) | entity_contract | 1.0 | `entity_json_schemas`, `entity_relationships`, `money_type`, `primary_key_format`, `schema_version`, `timestamp_type`, `value_types` | Full object → document; namespaced/content-addressed rule_version; source_path + exact-byte sha256; no fixture rows |
| [contracts/tool_schemas.json](../../data/rfq_synthetic_data/contracts/tool_schemas.json) | tool_contract | 1.1 | `approval_tool_exposed_to_agent`, `contract_version`, `notes`, `tools` | Full object → document; namespaced/content-addressed rule_version; source_path + exact-byte sha256; no fixture rows |
| [contracts/business_constraints.json](../../data/rfq_synthetic_data/contracts/business_constraints.json) | business_constraints | properties.schema_version.const=1.1; content addressed | `agent_can_finalize_approval`, `currency`, `customer_scope_checked_server_side`, `document_conflict_policy`, `estimate_is_nonbinding`, `max_autonomous_write_retry_count`, `price_and_approval_version_pinning`, `state_transition_configuration` | Full object → document; namespaced/content-addressed rule_version; source_path + exact-byte sha256; no fixture rows |
| [contracts/agent_coordination_policy.json](../../data/rfq_synthetic_data/contracts/agent_coordination_policy.json) | coordination | 1.1 | `agents`, `all_business_tools_via_secure_mcp_gateway`, `allowed_parallel_group`, `architecture`, `dependencies_are_partial_order_not_exact_trace`, `handoff_schema`, `human_approval_for_release`, `llm_cannot_override_business_tool_results`, `must_have_engineering_validation_before_pricing`, `no_approve_or_release_tool_for_ai_agents`, `no_duplicate_write_on_redelegation`, `required_handoff_fields`, `supervisor_escalation_on`, `version` | Full object → document; namespaced/content-addressed rule_version; source_path + exact-byte sha256; no fixture rows |
| [contracts/agent_handoff_schema.json](../../data/rfq_synthetic_data/contracts/agent_handoff_schema.json) | handoff | properties.schema_version.const=1.1; content addressed | `$id`, `$schema`, `additionalProperties`, `properties`, `required`, `title`, `type` | Full object → document; namespaced/content-addressed rule_version; source_path + exact-byte sha256; no fixture rows |

The handoff schema's properties.schema_version.const=1.1 and $id describe its contract version (no root version field); preserve the complete schema and identify it by namespaced/content hash. A version string repeated between entity/tool/coordination/handoff contracts is **not** a globally unique registry key: namespace it by document kind or use content addressing. Tool/state contract metadata lives in immutable document JSONB, not fabricated tool/agent SQL master tables.

Entity source mapping: the contract's 16 formal CSV paths resolve to the fields above; historical_rfq→rfqs; rfq_scenario→harness only. Tool schemas define 14 operations, three idempotent writes; coordination/handoff define six agent identities and pinned evidence. business_constraints.max_autonomous_write_retry_count=0 and human-approval restrictions must remain enforced, not imported as agent-facing answer labels.

### Evaluation-only sources — offline harness, no business target

| Exact source | Measured shape/count | Source field names (metadata only) | Target / access |
| --- | --- | --- | --- |
| [evaluation/agent_coordination_cases.json](../../data/rfq_synthetic_data/evaluation/agent_coordination_cases.json) | 240 list records | `acceptable_supervisor_disposition`, `allowed_parallel_groups`, `clarification_propagation_required`, `conditional_short_circuit_allowed`, `fault_instance_id`, `forbidden_agent_actions`, `required_agents`, `required_dependency_edges`, `required_handoffs`, `required_specialist_disagreement_handling`, `rfq_id`, `schema_version`, `split`, `tool_call_order_is_not_fixed`, `unnecessary_specialist_invocation_penalty` | Harness-only schema/manifest/scoring/fault/security/coordination provenance; no business-table field mapping |
| [evaluation/agent_exception_cases.json](../../data/rfq_synthetic_data/evaluation/agent_exception_cases.json) | 12 list records | `agent_visible_input_must_exclude_expected_result`, `coordination_case_id`, `expected_agent_release_write_count`, `expected_handoff_status`, `expected_supervisor_action`, `injected_event`, `source_rfq_id` | Harness-only schema/manifest/scoring/fault/security/coordination provenance; no business-table field mapping |
| [evaluation/checksums.sha256.json](../../data/rfq_synthetic_data/evaluation/checksums.sha256.json) | 637 root keys (not necessarily records) | relative asset path → SHA-256 (dynamic map) | Harness-only schema/manifest/scoring/fault/security/coordination provenance; no business-table field mapping |
| [evaluation/evaluation_schema.json](../../data/rfq_synthetic_data/evaluation/evaluation_schema.json) | 5 root keys (not necessarily records) | `evaluation_protocol`, `label_storage`, `metrics`, `score_requires_independent_executor`, `version` | Harness-only schema/manifest/scoring/fault/security/coordination provenance; no business-table field mapping |
| [evaluation/expected_tool_behavior.json](../../data/rfq_synthetic_data/evaluation/expected_tool_behavior.json) | 2 root keys (not necessarily records) | `policy`, `version` | Harness-only schema/manifest/scoring/fault/security/coordination provenance; no business-table field mapping |
| [evaluation/failure_scenarios.json](../../data/rfq_synthetic_data/evaluation/failure_scenarios.json) | 36 list records | `attempt_sequence`, `checkpoint_available`, `expected_audit_events`, `expected_final_workflow_state`, `expected_outcome`, `expected_recovery`, `expected_write_count_after_recovery`, `expected_write_count_first_attempt`, `failure_case_id`, `failure_type`, `fallback_available`, `fault_injection`, `fault_instance_id`, `fault_template_id`, `idempotency_key`, `must_not_assume_rollback`, `observed_backoff_seconds`, `operation_class`, `operation_id`, `outcome_uncertain`, `prior_result_available`, `receipt_status`, `reconciliation_status`, `recovered`, `required_backoff_seconds`, `retry_after_window_elapsed`, `retry_attempt_idempotency_keys`, `retry_limit`, `rfq_id`, `safe_to_retry`, `snapshot_revalidated`, `tenant_id`, `trigger_after_operation` | Harness-only schema/manifest/scoring/fault/security/coordination provenance; no business-table field mapping |
| [evaluation/fault_templates.json](../../data/rfq_synthetic_data/evaluation/fault_templates.json) | 19 list records | `direct_retry_safe`, `failure_type`, `fault_template_id`, `operation_class`, `recovery_policy` | Harness-only schema/manifest/scoring/fault/security/coordination provenance; no business-table field mapping |
| [evaluation/scenario_families.json](../../data/rfq_synthetic_data/evaluation/scenario_families.json) | 26 list records | `count`, `family`, `primary_category`, `split` | Harness-only schema/manifest/scoring/fault/security/coordination provenance; no business-table field mapping |
| [evaluation/scenario_manifest.json](../../data/rfq_synthetic_data/evaluation/scenario_manifest.json) | 240 list records | `attachment_count`, `family_id`, `label_file`, `primary_category`, `rfq_id`, `scenario_file`, `split` | Harness-only schema/manifest/scoring/fault/security/coordination provenance; no business-table field mapping |
| [evaluation/security_scenarios.json](../../data/rfq_synthetic_data/evaluation/security_scenarios.json) | 60 list records | `actor_id`, `actor_kind`, `attack_type`, `authenticated_role`, `block_downstream`, `claimed_role`, `customer_scope`, `expected_audit_event`, `expected_decision`, `expected_downstream_write_count`, `expected_error_class`, `input_condition`, `reason`, `requested_action`, `security_case_id`, `target_customer_id` | Harness-only schema/manifest/scoring/fault/security/coordination provenance; no business-table field mapping |

`evaluation/ground_truth/development/*.json` (120), `validation/*.json` (40), `frozen_evaluation/*.json` (80) remain separate scorer-only labels. Their expected_extraction/expected_pricing/expected_schedule/expected_outcome, required_tools, tool_dependencies, human-approval labels, fault answers and coordination decisions never map to production business columns, prompts, vector indexes, checkpoint metadata or business-service logs. Scenario_manifest's exact fields rfq_id, scenario_file, label_file, split, primary_category, family_id, attachment_count remain harness metadata; primary_category and label_file must not become agent-visible intake context. checksums.sha256.json validates 637 immutable asset paths, not business rows. A hash manifest also contains hidden paths and is not mounted into agent retrieval.

The offline evaluation harness alone accesses hidden expected outcomes and scores observed behavior with distinct credentials. Split access is enforced by that harness; business tool roles do not grant hidden-label access. Evaluation metadata must never accidentally become production-facing RFQ/quote records. These are planned runtime controls, not evidence of deployed isolation.

Source/docs/tests/reference engines and the dataset ZIP are not relational records. Preserve them; do not run generation/import/build/export commands as part of this mapping. Source rows and contracts take precedence over illustrative SQL. The narrative MCP document's omitted create_rfq does not remove the fourteenth tool.

## 5. Additional application fields and non-seeded tables

Every column whose table specification says application is an explicit proposal. Technical IDs, row_version=1, registered_at/created_at defaults, current completeness labels and snapshot hashes/tokens are assigned by the future importer/service as described; they are not falsely claimed source columns. Rule registry copies current immutable documents; new version pins on historical revisions remain absent where not provable.

Start rfq_line_items, rfq_attachments, pricing_calculations, approval_requests, idempotency_records, audit_events and workflow_runs **empty** in the baseline seed. roles, permissions, four compatibility projections, business_rule_versions and data_snapshots are justified derived/support records. Do not create a user_roles table, historical hidden-label database, fake approval requests, fake calculation IDs, or fabricated old audit events. A future actual import may log its own present-time import action; that is distinct from reconstructing historical operations.

## 6. Future seeding dependency and atomicity specification

1. Preflight outside the database: verify approved archive/source checksums, all 18 schemas/relationships, all 17 exact CSV headers and duplicate IDs, semantic parsers/nulls, JSON shapes, FK candidates, nested line agreement, current quote totals, version pairs, route consistency and constraints. Select an explicitly synthetic development/test target, never an accidental production database.
2. Prepare immutable backend-only copies of approved inventory/capacity CSV bytes; register their kind/as_of/hash metadata. Register four rule and five contract documents, rejecting version/hash conflicts. Derive six roles and 12 permissions; import users after roles, then grants after both reference parents. Import customers/suppliers/operations and pricing-linked materials/finishes. These are planned operations only.
3. Load contacts/workcentres and four junction projections after their parents; reject duplicate composite pairs. Workcentres' composite unique key must exist for routes. Load selected current inventory and capacity (including application snapshot_id/row_version), validate generated source arithmetic, then routing and procurement. Enforce (workcentre_id,week_start) and proposed (part_type,sequence) uniqueness. Parent records never materialize from unmatched text.
4. Load 80 historical RFQs after customers, preserving part_ids and setting archived/created_by=NULL. Verify each of 80 quote.rfq_id resolves, with no duplicate family. Group the quote import into a **single transaction**: insert header pointers with current/approved FKs initially deferred; insert all 96 version rows with their true metadata; join only the matching 80 current header financial payloads; insert 100 exact-version lines; insert 70 approval events with approval_request_id=NULL. Never load a child before an immediate FK parent; only documented cyclic header pointers are deferred.
5. Before the commit, verify every header current pointer/flag, exactly one current flag per header, all version line ordinals, current sums, source approval summaries and optional actor references. Set historical older versions metadata_only and current versions priced_partial; no historical snapshot is promoted to complete. Entire business-baseline import (small dataset) should be one atomic transaction, including its referenced config/catalog parents, so any failure rolls back all newly imported rows. Checkpoint/evaluation imports are not part of it.
6. Make future baseline seeding safely repeatable by verifying existing source-ID rows and immutable digests are identical; a conflicting or live-mutated row aborts the import. Do not blanket ON CONFLICT DO UPDATE and overwrite business history. Compare compound-key projections as complete tuples; record the source dataset version/hash. A resumed seeding attempt either observes the committed identical baseline or reloads after confirmed rollback, never a half-import.

No source file is changed to satisfy the target design. MFG-001 remains an unresolved reference-validator dependency; future seed preflight must explicitly enforce configured scalar limits on nested parts instead of assuming the reference validator does so. Required source strings are not a database-nullability specification. Missing old pricing snapshots and absent historical policy/clearance evidence are known limitations, not errors to repair by guessing.
