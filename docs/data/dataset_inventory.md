# Protected synthetic data and reproducibility

Dataset v1.1.0 is privately distributed to authorized developers. A public checkout contains
code and source pins, not the archive, attachments, hidden evaluation labels or operational
snapshot payloads. The full regression requires the separately supplied frozen package.

`seeding/sources.py` validates the archive pin and source/checksum manifests; `seeding/mapping.py`
and transformations implement the import. `repositories/rule_manifest.py` anchors both source
SHA-256 and canonical rule fingerprints. Runtime registries use approved versioned overlays
rather than changing frozen package bytes. Inventory/capacity snapshots are content-addressed
and must remain available while database provenance references them.

Scenario and hidden ground-truth inputs belong to the evaluation harness. They are excluded
from baseline business seeding and runtime retrieval. The dataset's tool proposals and agent
coordination fixtures do not expand the application's executable MCP authority.



## Synthetic RFQ Dataset Inventory

<a id="dataset-inventory-purpose-and-source-of-evidence"></a>
### Purpose and source of evidence

This inventory describes the frozen synthetic dataset and its formal contracts.
It does not regenerate data or establish deployment readiness. Consult the private dataset
package for authoritative definitions and the [project overview](../../README.md) for runtime capabilities.

<a id="dataset-inventory-package-overview"></a>
### Package overview

| Measure | Verified result | Interpretation |
|---|---:|---|
| Files in dataset tree after verification | 681 | Entire `data/rfq_synthetic_data/` tree, including supporting files |
| Files listed in `evaluation/checksums.sha256.json` | 637 | Selected generated business assets in seven declared directories |
| Checksum matches | 637 of 637 | No missing, extra, or mismatched files in the covered set |
| Formal entity schemas | 18 | Seventeen table-like sources plus RFQ scenario records |
| RFQ scenarios | 240 | 120 development, 40 validation, 80 frozen evaluation |
| Scenario families | 26 | Count validated by original utility |
| Referenced attachments | 124 | Of which 79 are PDFs, per validator output |
| Security scenarios | 60 | 16 authorized, 44 denied |
| Fault instances | 36 | Eighteen linked to RFQs, per validator output |
| Agent coordination cases | 240 | One benchmark specification per scenario |
| Agent exception cases | 12 | Coordination edge cases |

The 681-file total and 637 checksum entries describe different scopes. Files not listed in the checksum manifest should not automatically be treated as corrupt or missing; for example, documentation, configuration, source code, and tests are outside its selected hash roots.

<a id="dataset-inventory-physical-directory-layout"></a>
### Physical directory layout

```text
data/rfq_synthetic_data/
├── attachments/          # Referenced engineering files, including drawings and BOM revisions
├── config/               # Generation, manufacturing, pricing, approval, workflow policies
├── contracts/            # Entity schemas, tool schemas, agent handoff and coordination policies
├── docs/                 # Existing data dictionary, database design, MCP and evaluation notes
├── evaluation/           # Scenario manifest, hidden labels, security and coordination benchmarks
├── master_data/          # Customers, contacts, catalogs, roles and users
├── operations_data/      # Inventory, routing, procurement, capacity snapshots
├── rfq_scenarios/        # Agent-visible request objects partitioned by split
├── src/                  # Standalone deterministic reference engines and validators
├── tests/                # Ten existing test modules
└── transactional_data/   # Historical RFQs, quotes, quote lines, versions and approvals
```

<a id="dataset-inventory-record-counts-by-source"></a>
### Record counts by source

| Logical entity or supporting file | Source path relative to dataset | Records |
|---|---|---:|
| Customer | `master_data/customers.csv` | 40 |
| Contact | `master_data/contacts.csv` | 40 |
| Finish | `master_data/finishes.csv` | 7 |
| Material | `master_data/materials.csv` | 15 |
| Operation | `master_data/operations.csv` | 8 |
| Supplier | `master_data/suppliers.csv` | 6 |
| User | `master_data/users.csv` | 24 |
| Workcentre | `master_data/workcentres.csv` | 8 |
| Roles/permissions (supporting table) | `master_data/roles_permissions.csv` | 27 |
| Material inventory | `operations_data/material_inventory.csv` | 90 |
| Operation routing | `operations_data/operation_routing.csv` | 28 |
| Procurement | `operations_data/procurement_records.csv` | 12 |
| Production capacity | `operations_data/production_capacity.csv` | 80 |
| Historical RFQ | `transactional_data/historical_rfqs.json` | 80 |
| Historical quote | `transactional_data/historical_quotes.csv` | 80 |
| Quote line | `transactional_data/quote_line_items.csv` | 100 |
| Quote version | `transactional_data/quote_versions.csv` | 96 |
| Approval history | `transactional_data/approval_history.csv` | 70 |
| RFQ scenario | `rfq_scenarios/*/*.json` | 240 |

The table lists record counts, not numbers of files. The dataset's original validation report reports 17 table-like entities and tracks `rfq_scenario` separately. `roles_permissions.csv` is an additional supporting CSV, not one of the 18 formal entity schemas.

<a id="dataset-inventory-scenario-partitions"></a>
### Scenario partitions

| Split | Count | Intended handling |
|---|---:|---|
| Development | 120 | Development examples and controlled inspection |
| Validation | 40 | Validation holdout for development checks |
| Frozen evaluation | 80 | Holdout; do not feed labels to agents or train/tune against them |
| **Total** | **240** | Manifest: `evaluation/scenario_manifest.json` |

Each manifest entry identifies a `scenario_file` and corresponding `label_file`. `rfq_scenarios/` contains agent-visible request inputs. `evaluation/ground_truth/` contains hidden expected results and must not be mounted in the runtime agents' retrieval/tool context.

<a id="dataset-inventory-existing-contracts-and-policies"></a>
### Existing contracts and policies

**Contracts (`contracts/`):**

- `entity_schemas.json`: 18 entity schemas, primary/foreign key declarations, wire types.
- `tool_schemas.json`: 14 future MCP tool contracts with roles, input/output schemas, state-changing flags, and idempotency policy.
- `agent_handoff_schema.json`: required handoff envelope, JSON Schema Draft 2020-12.
- `agent_coordination_policy.json`: Supervisor and specialist coordination rules.
- `business_constraints.json`: business and approval constraints.

**Configuration (`config/`):** `generation_config.yaml`, `manufacturing_rules.json`, `pricing_rules.json`, `approval_policies.json`, `workflow_state_rules.json`, `v1_0_0_preservation_manifest.json`.

**Documentation (`docs/`):** `data_dictionary.md`, `database_schema.md`, `mcp_mapping.md`, `business_rules.md`, `evaluation_design.md`, `multi_agent_evaluation.md`, `scenario_coverage.md`, `security_fixture_design.md`, `recovery_policy.md`, `verification_quickstart.md`, `assumptions_and_limitations.md`.

**Testing (`tests/`):** `test_business_rules.py`, `test_capacity.py`, `test_entity_schemas.py`, `test_integrity.py`, `test_pricing.py`, `test_reproducibility.py`, `test_scenario_labels.py`, `test_tool_contracts.py`, `test_v110_recovery.py`, `test_v110_security_coordination.py`.

<a id="dataset-inventory-inventory-decisions"></a>
### Inventory decisions

1. Preserve `data/rfq_synthetic_data/` as the frozen v1.1.0 baseline.
2. Reference existing schemas and policies, rather than duplicate them as a second source of truth.
3. Treat `evaluation/ground_truth/` and coordination labels as evaluation-only material.
4. Keep the two operational views distinct: **table-like historical/master data** and **scenario/attachment fixtures**.
5. Do not infer database readiness from file-level validation. PostgreSQL migrations, import validation, isolation, authentication and runtime retrieval controls are implemented separately from dataset-file validation.

<a id="dataset-inventory-evidence"></a>
### Evidence

- `python -B -m src.validate_data` run with exit code 0 from dataset directory.
- Independent checksum comparison: 637 expected, 637 actual, zero differences.
- Test suite: 38 non-regenerating tests and 1 reproducibility test in an isolated temporary copy, all passing.
- Original dataset preservation checks: 681 files before/after validator, no changes; clean project Git tree after testing.

See [`dataset_inventory.md`](dataset_inventory.md) and [`dataset_inventory.md`](dataset_inventory.md).


## Dataset Schema and Relationship Analysis

**Project:** Engineering RFQ Multi-Agent Copilot
**Baseline:** Synthetic RFQ dataset v1.1.0
**Authoritative source:** `data/rfq_synthetic_data/contracts/entity_schemas.json`

<a id="dataset-schema-analysis-schema-policy"></a>
### Schema policy

The dataset defines 18 entities in `entity_relationships` and 18 schemas in `entity_json_schemas`. Each entity schema has a declared primary key and required fields; some have declared foreign keys. The CSV-oriented wire records are represented as string fields (including dates, amounts, integer-like fields, and empty-value placeholders), whereas JSON records also contain arrays. **Do not assume those CSV strings are suitable final PostgreSQL types.** The dataset's `docs/database_schema.md` provides its separate future SQL design.

All properties printed from the 18 entity JSON schemas were marked required. Required presence **does not** imply a nonempty value; for example, 50 of 80 historical quote records have an empty `approver_id`, which must be interpreted using the business workflow state and SQL design, not automatically reported as an integrity failure.

<a id="dataset-schema-analysis-entities-primary-keys-and-foreign-keys"></a>
### Entities, primary keys and foreign keys

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

<a id="dataset-schema-analysis-field-inventory-formal-required-fields"></a>
### Field inventory: formal required fields

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

<a id="dataset-schema-analysis-nested-and-serialized-structures"></a>
### Nested and serialized structures

- `historical_rfq.part_ids`: JSON array.
- `rfq_scenario.attachments`: JSON array; attachment contents can be separate BOMs, drawings, PDFs, or other files. The scenario JSON itself has seven top-level fields.
- `quote_line.part_json` and `quote_line.cost_breakdown_json`: JSON payloads serialized in CSV string columns; parse and validate during import instead of treating them as ordinary prose.
- The scenario manifest has 240 entries with `rfq_id`, `scenario_file`, `label_file`, `split`, `primary_category`, `family_id`, and `attachment_count`.
- Hidden ground-truth label objects contain `expected_*`, `missing_fields`, `contradiction_findings`, `tool_dependencies`, and other benchmark fields. They are **not** part of the runtime agent input contract.
- Supplementary `master_data/roles_permissions.csv` contains 27 entries and requires separate future import/data-model treatment; it is not one of the 18 formal entity entries.

<a id="dataset-schema-analysis-relationships-and-cardinality-considerations"></a>
### Relationships and cardinality considerations

1. One customer can have multiple contacts and RFQs; a contact's `customer_id` references one customer.
2. A historical RFQ links to a customer; a quote references the historical RFQ and creator/approver users.
3. A quote can have multiple lines, versions, and approval history events. Repeated foreign keys in these child tables are normal.
4. Inventory relates material and supplier; capacity attaches to workcentre; routing links operations with workcentres.
5. A scenario RFQ links to a customer, but is a **separate scenario fixture** from historical RFQs; do not merge the two ID spaces without reviewing dataset semantics.
6. Bill-of-materials attachments can contain multiple rows for the same `rfq_id`; this alone does not violate primary-key uniqueness.

<a id="dataset-schema-analysis-import-interpretation-rules"></a>
### Import interpretation rules

- Apply exact primary- and foreign-key constraints from the authoritative `entity_relationships` metadata.
- Convert money, hours, quantities, thicknesses, and ratios from CSV strings to appropriate fixed-precision numeric SQL values; do not use binary floating point for price calculations.
- Parse timestamp/date fields; make optional business values nullable in SQL only when the actual workflow and design permit.
- Pin pricing, snapshot and approval versions. The application enforces current-version checks and idempotency.
- Validate nested/serialized JSON using the declared schema before storing as JSONB.
- Preserve split membership and hard-isolate hidden `evaluation/ground_truth/` files from production tools and agent prompts.
- Keep client-visible/customer text separate from authenticated actor identity and customer authorization scope.

<a id="dataset-schema-analysis-reference-engine-limits"></a>
### Reference-engine limits

The frozen reference manufacturing validator does not enforce its scalar hole-count maximum.
The application validator enforces `0 <= hole_count <= 300` and rejects unsupported rule
limits; see [manufacturing validation](../../src/rfq_copilot/services/manufacturing.py).
Keep the reference dataset unchanged and use the approved database mapping for imports.


## Frozen evaluation contracts and tool proposals

**Project:** Engineering RFQ Multi-Agent Copilot
**Dataset baseline:** Synthetic RFQ v1.1.0
**Scope:** Dataset contracts and their mapping to the implemented agents and MCP services.
Dataset fixture coverage does not establish live deployment readiness.

<a id="multiagent-data-mapping-contract-sources-and-semantics"></a>
### Contract sources and semantics

Primary machine-readable sources:

- `data/rfq_synthetic_data/contracts/tool_schemas.json` (contract v1.1; 14 tools).
- `data/rfq_synthetic_data/contracts/agent_coordination_policy.json`.
- `data/rfq_synthetic_data/contracts/agent_handoff_schema.json`.
- `data/rfq_synthetic_data/docs/mcp_mapping.md`.
- `data/rfq_synthetic_data/evaluation/agent_coordination_cases.json` and `agent_exception_cases.json`.

**Important distinction:** `allowed_roles` are authenticated **business actor roles** enforced by a backend. `rfq_intake`, `engineering`, etc. are **agent responsibilities**, not identity roles. Assigning a tool to an agent in this document does **not** grant that agent its own privileged identity or bypass the tool's listed roles, customer scope, or state checks. The future secure MCP gateway must enforce actual end-user/service delegation rules.

<a id="multiagent-data-mapping-six-agents-proposed-operational-mapping"></a>
### Frozen dataset agent responsibility mapping

| Agent | Relevant data and contract input | Proposed tools / actions | Handoff and boundaries |
|---|---|---|---|
| **Supervisor** | Scenario state, handoff packets, tool outcomes, escalation and exception policy | Orchestrate, delegate, reconcile, re-plan, escalate; avoid unrestricted direct business writes | Oversees all stages, enforces dependency order, stops forbidden actions, routes for human decision |
| **RFQ Intake** | Agent-visible RFQ JSON, attachments, customer and contacts, missing/contradictory specifications | `lookup_customer`, `get_customer_status`; conditional `create_rfq` (write) | To Engineering: `parts`, `missing_fields`, `contradiction_findings`, `source_evidence`; customer scoping required |
| **Engineering** | Material/finish catalogs, operations/routing, manufacturing rules | `get_material_specs`, `get_supported_operations`, `check_manufacturing_capability` | To Supply: validated parts, capability result, material IDs; to Pricing: validated parts, capability result, pricing rule version; reject or escalate invalid geometry, including `MFG-001` risk |
| **Supply & Scheduling** | Material inventory, suppliers, capacity, routing, snapshot versions | `check_material_availability`, `check_production_capacity`, `estimate_lead_time` | To Quotation & Review: inventory status, capacity status, lead business days, snapshot versions; estimates do **not** reserve stock/capacity |
| **Pricing** | Material costs, pricing rules, engineering-validated parts, calculation identifiers | `calculate_quote`, `get_pricing_breakdown` | To Quotation & Review: quote total, line breakdown, pricing rule version; no autonomous price override |
| **Quotation & Review** | Verified price and schedule outputs, quote/version/history, approval policy | `create_quote_draft` (write), `get_quote_status`, `request_approval` (write) | To Supervisor: draft status, quote version, manager-review requirement, human-release approval requirement; never self-approve/release |

The tool assignment column is **architectural intent only**. More than one specialist may need a value provided through a verified handoff rather than a fresh direct tool call.

<a id="multiagent-data-mapping-all-14-mcp-tool-contracts-and-business-role-requirements"></a>
### Frozen dataset tool contracts and business role requirements

| Tool | Allowed authenticated business roles from contract | State changing? | Idempotency required? |
|---|---|---|---|
| `lookup_customer` | sales_representative, estimator, sales_manager, administrator, auditor, manufacturing_engineer | No | No |
| `get_customer_status` | sales_representative, estimator, sales_manager | No | No |
| `get_material_specs` | sales_representative, estimator, manufacturing_engineer | No | No |
| `check_manufacturing_capability` | sales_representative, estimator, manufacturing_engineer | No | No |
| `get_supported_operations` | sales_representative, estimator, manufacturing_engineer | No | No |
| `check_material_availability` | estimator, manufacturing_engineer | No | No |
| `check_production_capacity` | estimator, manufacturing_engineer | No | No |
| `estimate_lead_time` | estimator, manufacturing_engineer | No | No |
| `calculate_quote` | estimator | No | No |
| `get_pricing_breakdown` | estimator, sales_manager | No | No |
| `create_rfq` | sales_representative, estimator | **Yes** | **Yes** |
| `create_quote_draft` | estimator, sales_representative | **Yes** | **Yes** |
| `get_quote_status` | estimator, sales_representative, sales_manager, auditor | No | No |
| `request_approval` | estimator, sales_representative, sales_manager | **Yes** | **Yes** |

The existing narrative `docs/mcp_mapping.md` enumerates 13 named rows and omits `create_rfq`, although this tool exists in the authoritative 14-tool `tool_schemas.json`. Treat the machine-readable contract as the correct tool inventory; note the narrative omission for future documentation cleanup, without modifying frozen dataset files.

<a id="multiagent-data-mapping-tool-to-data-dependencies"></a>
### Tool-to-data dependencies

| Tool | Contract-defined supporting records and business logic |
|---|---|
| `lookup_customer` | `master_data/customers.csv`, `master_data/contacts.csv` |
| `get_customer_status` | Customers and approval policy |
| `get_material_specs` | Materials and finishes catalogs |
| `check_manufacturing_capability` | Manufacturing rules and `src.business_rules.validate_part` |
| `get_supported_operations` | Operations and operation routing |
| `check_material_availability` | Material inventory, `src.capacity_engine.check_inventory` |
| `check_production_capacity` | Production capacity and workcentres |
| `estimate_lead_time` | Inventory/capacity snapshots, `src.capacity_engine.estimate_schedule` |
| `calculate_quote` | Materials, pricing rules, `src.pricing_engine` |
| `get_pricing_breakdown` | Calculation result or future verified calculation cache |
| `create_rfq` | Dataset proposal for RFQ persistence; actual writes use FastAPI (in machine-readable tool contract) |
| `create_quote_draft` | Dataset proposal for quotation writes; actual writes use trusted services |
| `get_quote_status` | Quotes and approvals |
| `request_approval` | Approval policies, quote versions, future audit history |

<a id="multiagent-data-mapping-handoff-envelope-and-dependencies"></a>
### Handoff envelope and dependencies

The JSON Schema Draft 2020-12 handoff envelope requires:

`schema_version`, `workflow_id`, `correlation_id`, `sender`, `recipient`, `requested_action`, `payload`, `evidence_refs`, `decision_status`, `source_snapshot_versions`.

It also defines `findings`, `idempotency_key`, and `quote_version` properties; whether they are populated depends on handoff/action. Every handoff must validate **schema, identities, source versions, RFQ/customer scope, and authorized requested action** before use.

Declared required handoff data:

| From -> To | Required semantic payload fields |
|---|---|
| `rfq_intake` -> `engineering` | `parts`, `missing_fields`, `contradiction_findings`, `source_evidence` |
| `engineering` -> `supply_scheduling` | `validated_parts`, `capability_result`, `material_ids` |
| `engineering` -> `pricing` | `validated_parts`, `capability_result`, `pricing_rule_version` |
| `supply_scheduling` -> `quotation_review` | `inventory_status`, `capacity_status`, `lead_business_days`, `snapshot_versions` |
| `pricing` -> `quotation_review` | `quote_total_usd`, `line_breakdown`, `pricing_rule_version` |
| `quotation_review` -> `supervisor` | `draft_status`, `quote_version`, `manager_review_required`, `human_release_approval_required` |

**Ordering:** Intake -> Engineering is required in all 240 benchmark cases. Engineering validation precedes Pricing and Supply/Scheduling. Pricing and Supply/Scheduling may run concurrently on eligible cases. Quotation & Review consumes both verified branches when needed. The Supervisor oversees the full workflow and need not appear as a terminal node in each dependency-edge list.

<a id="multiagent-data-mapping-verified-evaluation-coverage"></a>
### Verified evaluation coverage

| Agent | Required in 240 coordination fixtures |
|---|---:|
| Supervisor | 240 |
| RFQ Intake | 240 |
| Engineering | 240 |
| Supply & Scheduling | 156 |
| Pricing | 156 |
| Quotation & Review | 156 |

Required dependency-edge counts: Intake -> Engineering 240; Engineering -> Pricing 156; Engineering -> Supply/Scheduling 156; Pricing -> Quotation/Review 156; Supply/Scheduling -> Quotation/Review 156. A `supply_scheduling + pricing` parallel group is permitted in 156 cases. **Eighty-four** cases do not require invoking those three later specialists, illustrating necessary short-circuit routing.

Twelve exception fixtures cover: missing-field propagation; specialist disagreement; valid parallel merge; premature pricing; duplicate delegation; specialist timeout; malformed handoff; stale quote version; cross-customer handoff; unnecessary calls; inventory/pricing disagreement; attempted agent approval.

<a id="multiagent-data-mapping-security-and-isolation-requirements"></a>
### Security and isolation requirements

1. All tools invoked through secure MCP gateway, with authenticated actor context from the backend, not customer-supplied text or LLM instructions.
2. Verify customer ownership/assigned-region scope and least-privilege role access server-side.
3. Enforce idempotency keys and optimistic version checks on state-changing writes; durable audit events are required.
4. Reject tampered business-tool output before state changes. Make disagreement and failed reconciliation visible to the Supervisor.
5. Preserve snapshot and pricing rule versions across handoffs, caching, review and approval.
6. Expose **no** `approve_quote` or `release_quote` MCP tools to AI agents; human manager release is a separate authenticated interface with active customer, commercial clearance, current approval version and independent approver.
7. Never mount `evaluation/ground_truth/`, hidden expected results, security outcomes, or evaluation coordination labels into agent-visible retrieval contexts.
8. Keep the application manufacturing guard for `MFG-001`: the frozen reference engine does not enforce the scalar hole maximum and must remain unchanged.

<a id="multiagent-data-mapping-verification-limits"></a>
### Verification limits

All 240 scenario-manifest pairs had matching scenario and label IDs and valid distinct file paths. No top-level `expected_*` keys appeared in the agent-visible RFQ objects, based on the targeted inspection. **Dataset fixtures do not establish runtime access isolation.** Runtime authorization is
enforced by the application and secured MCP infrastructure, independently of these contracts.


## engineering Data Integrity and Deterministic Business Logic Report

<a id="data-integrity-report-finding-mfg-001-configured-hole-count-maximum-not-enforced"></a>
### Finding MFG-001: Configured hole-count maximum not enforced

**Status:** Fixed in the application manufacturing validator (manufacturing validation). Still present, by design, in the frozen v1.1.0 reference engine, which must remain unchanged.\
**Category:** Manufacturing capability validation / policy coverage.
**Location:** `src/business_rules.py` function `validate_part`; `config/manufacturing_rules.json` key `limits.hole_count_max`.
**Configured maximum:** 300 holes.
**Reproduction:** Use an otherwise valid flat panel and set `hole_count = 301`.

Observed:

```python
{'result': 'valid', 'findings': []}
```

**Root cause (from inspected implementation):** The generic loop in `validate_part` only checks `limits` values that are two-element lists. `hole_count_max` is a scalar value and has no separate validation branch, so it is skipped. This is a concrete failure to enforce the configured policy for otherwise valid input.

**Impact assessment:** An agent calling a future manufacturing-check tool backed by this unchanged function could mark an unsupported 301-hole request as valid. The engineering structured impact scan inspected 368 `hole_count` values from scenario JSON, hidden label JSON, and serialized `part_json` within historical quote lines: maximum observed 12; zero above 300, zero negative, zero nonnumeric. Thus **no affected record was observed in that scan**. The scan did not independently parse every attachment, every embedded prose requirement, or future API input, so it does not prove all possible inputs are safe.

**Recommended response:**

1. Keep v1.1.0 frozen and record this finding in project documentation.
2. The application validator enforces integer/nonnegative hole counts and the pinned maximum of 300 before accepting geometry.
3. Add regression cases for 300 (accept), 301 (reject), negative, non-integral, missing, and malformed hole counts.
4. If updating the reference `src.business_rules.validate_part` itself, make the change in an explicitly versioned subsequent dataset release with regenerated, verified checksums and test evidence; do not silently patch v1.1.0.
5. Revisit other scalar manufacturing limits as part of future hardening; do not claim they are defective without a separate reproduction.

**Severity:** Prospective moderate business-validation risk. **Current scanned dataset impact:** None observed.

<a id="data-integrity-report-manufacturing-validation-resolution-application-validator"></a>
#### manufacturing validation resolution (application validator)

- **Where it is fixed:** `src/rfq_copilot/services/manufacturing.py` (`evaluate_manufacturing`, called by `check_manufacturing_capability`). The configured `limits.hole_count_max` from the pinned `mfg-v1.0` rule version is enforced: a part passes only when `0 <= hole_count <= 300`, otherwise it gets `OUT_OF_RANGE_HOLE_COUNT` and result `unsupported`. An unstated hole count uses the rule default `defaults.hole_count = 0`.
- **Root cause closed, not just the symptom:** the validator raises an error if the rule document contains any limit it does not explicitly enforce. A future scalar limit therefore cannot be silently skipped the way `hole_count_max` was.
- **Regression tests** (recommended responses 2 and 3 above):
  - `tests/test_manufacturing_service.py`: 0, 300 and unstated → `valid`; 301, 10,000 and −1 → `OUT_OF_RANGE_HOLE_COUNT`; malformed values (`300.0`, `300.5`, `"300"`, `True`, `Decimal(300)`) are rejected by `Part.from_mapping` before validation; the maximum is read from the rule.
  - `tests/test_manufacturing_database.py::test_mfg_001_regression_against_the_reference`: against the seeded test database, **300 holes passes and 301 fails**, and the same test asserts that the frozen reference still returns `valid` for 301.
- **Frozen reference engine:** `data/rfq_synthetic_data/src/business_rules.py` (`validate_part`) still contains the original limitation and **must remain unchanged**. Dataset v1.1.0 is frozen; per recommended response 4, any reference fix belongs only in a separately versioned dataset release with regenerated, verified checksums. The reference is a test oracle only and is never imported by application code, so its limitation cannot reach runtime decisions.
- **Deliberate divergence:** for hole counts above 300 or below 0, application results intentionally differ from the reference. Parity tests cover only values within the limit.

<a id="data-integrity-report-reproducibility-and-source-safety"></a>
### Reproducibility and source safety

The original `tests/test_reproducibility.py` invokes `src.generate_data`; the dataset generator deletes and recreates generated directories. For engineering, that test was executed only inside a disposable temporary clone. The other 38 tests were run against the original tree with bytecode and pytest cache generation disabled. Hash snapshots verified preservation. Run future destructive regeneration only in disposable copies or a separately versioned dataset.

<a id="data-integrity-report-approval-security-and-leakage-boundaries"></a>
### Approval, security, and leakage boundaries

- Agent-visible requests are stored separately from hidden `evaluation/ground_truth` expected outcomes.
- Independent path/ID audit of all 240 scenario-manifest pairs found **zero** structural issues and zero top-level `expected_*` fields in visible scenario JSON.
- The dataset's 14 tool contracts include only three state-changing operations (`create_rfq`, `create_quote_draft`, `request_approval`), all requiring idempotency.
- No `approve_quote` or `release_quote` tool is exposed to AI agents. Release must be performed through a separately authenticated, human-authorized interface enforcing current-version approval, independent approver identity, active customer status, and commercial clearance.
- Existing checks demonstrate file separation and fixture behavior; they do not constitute a deployed access-control proof.
