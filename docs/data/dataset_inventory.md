# Synthetic RFQ Dataset Inventory

**Project:** Engineering RFQ Multi-Agent Copilot  
**Roadmap:** Day 3 of 73  
**Dataset baseline:** `data/rfq_synthetic_data/` (synthetic RFQ data v1.1.0)  
**Status:** Inventory documented from local terminal outputs; original dataset unchanged

## Purpose and source of evidence

This is an inventory of the pre-existing synthetic dataset. It does not regenerate data or claim that a future PostgreSQL import, MCP service, FastAPI API, or agent system exists. Findings are drawn from the file listings, formal contracts, official validator output, and independent tests executed locally during Day 3. For authoritative definitions, consult the dataset files rather than this summary.

## Package overview

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

## Physical directory layout

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

## Record counts by source

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

## Scenario partitions

| Split | Count | Intended handling |
|---|---:|---|
| Development | 120 | Development examples and controlled inspection |
| Validation | 40 | Validation holdout for development checks |
| Frozen evaluation | 80 | Holdout; do not feed labels to agents or train/tune against them |
| **Total** | **240** | Manifest: `evaluation/scenario_manifest.json` |

Each manifest entry identifies a `scenario_file` and corresponding `label_file`. `rfq_scenarios/` contains agent-visible request inputs. `evaluation/ground_truth/` contains hidden expected results and must not be mounted in the runtime agents' retrieval/tool context.

## Existing contracts and policies

**Contracts (`contracts/`):**

- `entity_schemas.json`: 18 entity schemas, primary/foreign key declarations, wire types.
- `tool_schemas.json`: 14 future MCP tool contracts with roles, input/output schemas, state-changing flags, and idempotency policy.
- `agent_handoff_schema.json`: required handoff envelope, JSON Schema Draft 2020-12.
- `agent_coordination_policy.json`: Supervisor and specialist coordination rules.
- `business_constraints.json`: business and approval constraints.

**Configuration (`config/`):** `generation_config.yaml`, `manufacturing_rules.json`, `pricing_rules.json`, `approval_policies.json`, `workflow_state_rules.json`, `v1_0_0_preservation_manifest.json`.

**Documentation (`docs/`):** `data_dictionary.md`, `database_schema.md`, `mcp_mapping.md`, `business_rules.md`, `evaluation_design.md`, `multi_agent_evaluation.md`, `scenario_coverage.md`, `security_fixture_design.md`, `recovery_policy.md`, `verification_quickstart.md`, `assumptions_and_limitations.md`.

**Testing (`tests/`):** `test_business_rules.py`, `test_capacity.py`, `test_entity_schemas.py`, `test_integrity.py`, `test_pricing.py`, `test_reproducibility.py`, `test_scenario_labels.py`, `test_tool_contracts.py`, `test_v110_recovery.py`, `test_v110_security_coordination.py`.

## Inventory decisions

1. Preserve `data/rfq_synthetic_data/` as the frozen v1.1.0 baseline.
2. Reference existing schemas and policies, rather than duplicate them as a second source of truth.
3. Treat `evaluation/ground_truth/` and coordination labels as evaluation-only material.
4. Keep the two operational views distinct: **table-like historical/master data** and **scenario/attachment fixtures**.
5. Do not infer database readiness from file-level validation. PostgreSQL migrations, import validation, isolation, authentication, and runtime retrieval controls remain future work.

## Evidence

- `python -B -m src.validate_data` run with exit code 0 from dataset directory.
- Independent checksum comparison: 637 expected, 637 actual, zero differences.
- Test suite: 38 non-regenerating tests and 1 reproducibility test in an isolated temporary copy, all passing.
- Original dataset preservation checks: 681 files before/after validator, no changes; clean project Git tree after testing.

See [`dataset_schema_analysis.md`](dataset_schema_analysis.md), [`data_integrity_report.md`](data_integrity_report.md), and [`multiagent_data_mapping.md`](multiagent_data_mapping.md).
