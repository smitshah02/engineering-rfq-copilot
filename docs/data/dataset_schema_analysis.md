# Dataset Schema and Relationship Analysis

**Project:** Engineering RFQ Multi-Agent Copilot  
**Roadmap:** Day 3 of 73  
**Baseline:** Synthetic RFQ dataset v1.1.0  
**Authoritative source:** `data/rfq_synthetic_data/contracts/entity_schemas.json`

## Schema policy

The dataset defines 18 entities in `entity_relationships` and 18 schemas in `entity_json_schemas`. Each entity schema has a declared primary key and required fields; some have declared foreign keys. The CSV-oriented wire records are represented as string fields (including dates, amounts, integer-like fields, and empty-value placeholders), whereas JSON records also contain arrays. **Do not assume those CSV strings are suitable final PostgreSQL types.** The dataset's `docs/database_schema.md` provides its separate future SQL design.

All properties printed from the 18 entity JSON schemas were marked required. Required presence **does not** imply a nonempty value; for example, 50 of 80 historical quote records have an empty `approver_id`, which must be interpreted using the business workflow state and SQL design, not automatically reported as an integrity failure.

## Entities, primary keys and foreign keys

| Entity | Primary key | Foreign keys | Source |
|---|---|---|---|
| `customer` | `customer_id` | None | `master_data/customers.csv` |
| `contact` | `contact_id` | `customer_id` -> `customer.customer_id` | `master_data/contacts.csv` |
| `finish` | `finish_id` | None | `master_data/finishes.csv` |
| `material` | `material_id` | None | `master_data/materials.csv` |
| `operation` | `operation_id` | None | `master_data/operations.csv` |
| `supplier` | `supplier_id` | None | `master_data/suppliers.csv` |
| `user` | `user_id` | None | `master_data/users.csv` |
| `workcentre` | `workcentre_id` | `operation_id` -> `operation.operation_id` | `master_data/workcentres.csv` |
| `inventory` | `inventory_id` | `material_id` -> `material.material_id`; `supplier_id` -> `supplier.supplier_id` | `operations_data/material_inventory.csv` |
| `capacity` | `capacity_id` | `workcentre_id` -> `workcentre.workcentre_id` | `operations_data/production_capacity.csv` |
| `routing` | `routing_id` | `operation_id` -> `operation.operation_id`; `workcentre_id` -> `workcentre.workcentre_id` | `operations_data/operation_routing.csv` |
| `procurement` | `procurement_id` | `inventory_id` -> `inventory.inventory_id`; `supplier_id` -> `supplier.supplier_id` | `operations_data/procurement_records.csv` |
| `historical_rfq` | `rfq_id` | `customer_id` -> `customer.customer_id` | `transactional_data/historical_rfqs.json` |
| `quote` | `quote_id` | `rfq_id` -> `historical_rfq.rfq_id`; `created_by`, `approver_id` -> `user.user_id` | `transactional_data/historical_quotes.csv` |
| `quote_line` | `quote_line_id` | `quote_id` -> `quote.quote_id` | `transactional_data/quote_line_items.csv` |
| `quote_version` | `quote_version_id` | `quote_id` -> `quote.quote_id` | `transactional_data/quote_versions.csv` |
| `approval` | `approval_id` | `quote_id` -> `quote.quote_id`; `actor_id` -> `user.user_id` | `transactional_data/approval_history.csv` |
| `rfq_scenario` | `rfq_id` | `customer_id` -> `customer.customer_id` | `rfq_scenarios/*/*.json` |

Only `customer` expressly listed an additional `unique` declaration in the relationship metadata dump (`customer_id`); the declared primary keys still require uniqueness independently. Do not interpret absent `unique` arrays as authorization for duplicate primary keys.

## Field inventory: formal required fields

These are **schema property names**, not inferred business values. The official files remain authoritative for types, formats, patterns, and nested constraints.

| Entity | Required properties |
|---|---|
| `approval` | `action`, `action_at`, `actor_id`, `approval_id`, `quote_id`, `quote_version`, `reason` |
| `capacity` | `available_hours`, `capacity_id`, `downtime_hours`, `nominal_hours`, `remaining_hours`, `scheduled_hours`, `utilization_fraction`, `week_start`, `workcentre_id` |
| `contact` | `contact_id`, `contact_name`, `customer_id`, `email`, `is_primary`, `phone` |
| `customer` | `account_status`, `approval_requirement`, `city`, `company_name`, `country`, `created_date`, `credit_exposure_usd`, `credit_limit_usd`, `customer_id`, `industry`, `payment_terms`, `preferred_shipping`, `region`, `restrictions`, `state` |
| `finish` | `display_name`, `finish_id`, `material_categories`, `pricing_rule_version` |
| `historical_rfq` | `customer_id`, `part_ids`, `raw_request`, `rfq_id`, `source_type`, `submitted_at` |
| `inventory` | `available_kg`, `expected_replenishment_date`, `inventory_id`, `material_id`, `quantity_kg`, `reorder_threshold_kg`, `reserved_kg`, `snapshot_date`, `stock_form`, `supplier_id`, `supplier_lead_business_days`, `thickness_mm`, `unit`, `warehouse` |
| `material` | `availability_class`, `category`, `compatible_finishes`, `cost_usd_per_kg`, `density_kg_m3`, `grade`, `material_id`, `min_bend_radius_factor`, `pricing_rule_version`, `supported_operations`, `supported_thickness_mm` |
| `operation` | `display_name`, `is_quoted`, `notes`, `operation_id` |
| `procurement` | `expected_receipt_date`, `inventory_id`, `ordered_date`, `procurement_id`, `quantity_kg`, `status`, `supplier_id` |
| `quote` | `approval_timestamp`, `approved_version`, `approver_id`, `created_at`, `created_by`, `currency`, `estimated_lead_business_days`, `expiry_date`, `idempotency_key`, `pricing_rule_version`, `quote_id`, `rejection_reason`, `rfq_id`, `status`, `submitted_by`, `total_usd`, `version` |
| `quote_line` | `cost_breakdown_json`, `line_number`, `line_total_usd`, `part_id`, `part_json`, `quantity`, `quote_id`, `quote_line_id`, `quote_version` |
| `quote_version` | `approval_valid`, `created_at`, `created_by`, `is_current`, `quote_id`, `quote_version_id`, `version` |
| `rfq_scenario` | `attachments`, `customer_id`, `data_classification`, `raw_customer_request`, `request_channel`, `rfq_id`, `submitted_at` |
| `routing` | `operation_id`, `part_type`, `required_when`, `routing_id`, `sequence`, `workcentre_id` |
| `supplier` | `company_name`, `email`, `normal_lead_business_days`, `supplier_id` |
| `user` | `active`, `assigned_region`, `authentication_subject`, `display_name`, `email`, `role`, `user_id` |
| `workcentre` | `downtime_hours_per_week`, `operation_id`, `planning_horizon_weeks`, `shift_calendar`, `weekly_nominal_hours`, `workcentre_id` |

## Nested and serialized structures

- `historical_rfq.part_ids`: JSON array.
- `rfq_scenario.attachments`: JSON array; attachment contents can be separate BOMs, drawings, PDFs, or other files. The scenario JSON itself has seven top-level fields.
- `quote_line.part_json` and `quote_line.cost_breakdown_json`: JSON payloads serialized in CSV string columns; parse and validate on future import instead of treating them as ordinary prose.
- The scenario manifest has 240 entries with `rfq_id`, `scenario_file`, `label_file`, `split`, `primary_category`, `family_id`, and `attachment_count`.
- Hidden ground-truth label objects contain `expected_*`, `missing_fields`, `contradiction_findings`, `tool_dependencies`, and other benchmark fields. They are **not** part of the runtime agent input contract.
- Supplementary `master_data/roles_permissions.csv` contains 27 entries and requires separate future import/data-model treatment; it is not one of the 18 formal entity entries.

## Relationships and cardinality considerations

1. One customer can have multiple contacts and RFQs; a contact's `customer_id` references one customer.
2. A historical RFQ links to a customer; a quote references the historical RFQ and creator/approver users.
3. A quote can have multiple lines, versions, and approval history events. Repeated foreign keys in these child tables are normal.
4. Inventory relates material and supplier; capacity attaches to workcentre; routing links operations with workcentres.
5. A scenario RFQ links to a customer, but is a **separate scenario fixture** from historical RFQs; do not merge the two ID spaces without reviewing dataset semantics.
6. Bill-of-materials attachments can contain multiple rows for the same `rfq_id`; this alone does not violate primary-key uniqueness.

## Proposed future import rules (not yet implemented)

- Apply exact primary- and foreign-key constraints from the authoritative `entity_relationships` metadata.
- Convert money, hours, quantities, thicknesses, and ratios from CSV strings to appropriate fixed-precision numeric SQL values; do not use binary floating point for price calculations.
- Parse timestamp/date fields; make optional business values nullable in SQL only when the actual workflow and design permit.
- Pin pricing, snapshot and approval versions. Enforce quote current-version checks and idempotency in the future application layer.
- Validate nested/serialized JSON using the declared schema before storing as JSONB.
- Preserve split membership and hard-isolate hidden `evaluation/ground_truth/` files from production tools and agent prompts.
- Keep client-visible/customer text separate from authenticated actor identity and customer authorization scope.

## Day 3 validation evidence and limits

The official validator returned `referential_integrity: pass`; entity-schema Pytest coverage and all 38 non-regenerating tests passed; the isolated reproducibility test passed. No live SQL database was created or validated. See [`data_integrity_report.md`](data_integrity_report.md) for the confirmed manufacturing rule gap `MFG-001`.
