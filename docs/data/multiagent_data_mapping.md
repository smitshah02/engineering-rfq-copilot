# Multi-Agent Data, Tool, and Authorization Mapping

**Project:** Engineering RFQ Multi-Agent Copilot  
**Roadmap:** Day 3 of 73  
**Dataset baseline:** Synthetic RFQ v1.1.0  
**Status:** Verified **contracts** and proposed future application mapping. MCP endpoints and LangGraph agents have **not** been implemented.

## Contract sources and semantics

Primary machine-readable sources:

- `data/rfq_synthetic_data/contracts/tool_schemas.json` (contract v1.1; 14 tools).
- `data/rfq_synthetic_data/contracts/agent_coordination_policy.json`.
- `data/rfq_synthetic_data/contracts/agent_handoff_schema.json`.
- `data/rfq_synthetic_data/docs/mcp_mapping.md`.
- `data/rfq_synthetic_data/evaluation/agent_coordination_cases.json` and `agent_exception_cases.json`.

**Important distinction:** `allowed_roles` are authenticated **business actor roles** enforced by a backend. `rfq_intake`, `engineering`, etc. are **agent responsibilities**, not identity roles. Assigning a tool to an agent in this document does **not** grant that agent its own privileged identity or bypass the tool's listed roles, customer scope, or state checks. The future secure MCP gateway must enforce actual end-user/service delegation rules.

## Six agents: proposed operational mapping

| Agent | Relevant data and contract input | Proposed tools / actions | Handoff and boundaries |
|---|---|---|---|
| **Supervisor** | Scenario state, handoff packets, tool outcomes, escalation and exception policy | Orchestrate, delegate, reconcile, re-plan, escalate; avoid unrestricted direct business writes | Oversees all stages, enforces dependency order, stops forbidden actions, routes for human decision |
| **RFQ Intake** | Agent-visible RFQ JSON, attachments, customer and contacts, missing/contradictory specifications | `lookup_customer`, `get_customer_status`; conditional `create_rfq` (write) | To Engineering: `parts`, `missing_fields`, `contradiction_findings`, `source_evidence`; customer scoping required |
| **Engineering** | Material/finish catalogs, operations/routing, manufacturing rules | `get_material_specs`, `get_supported_operations`, `check_manufacturing_capability` | To Supply: validated parts, capability result, material IDs; to Pricing: validated parts, capability result, pricing rule version; reject or escalate invalid geometry, including `MFG-001` risk |
| **Supply & Scheduling** | Material inventory, suppliers, capacity, routing, snapshot versions | `check_material_availability`, `check_production_capacity`, `estimate_lead_time` | To Quotation & Review: inventory status, capacity status, lead business days, snapshot versions; estimates do **not** reserve stock/capacity |
| **Pricing** | Material costs, pricing rules, engineering-validated parts, calculation identifiers | `calculate_quote`, `get_pricing_breakdown` | To Quotation & Review: quote total, line breakdown, pricing rule version; no autonomous price override |
| **Quotation & Review** | Verified price and schedule outputs, quote/version/history, approval policy | `create_quote_draft` (write), `get_quote_status`, `request_approval` (write) | To Supervisor: draft status, quote version, manager-review requirement, human-release approval requirement; never self-approve/release |

The tool assignment column is **architectural intent only**. More than one specialist may need a value provided through a verified handoff rather than a fresh direct tool call.

## All 14 MCP tool contracts and business role requirements

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

## Tool-to-data dependencies

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
| `create_rfq` | Future authenticated RFQ persistence and idempotency (in machine-readable tool contract) |
| `create_quote_draft` | Future quote, quote-line, quote-version, audit, idempotency persistence |
| `get_quote_status` | Quotes and approvals |
| `request_approval` | Approval policies, quote versions, future audit history |

## Handoff envelope and dependencies

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

## Verified evaluation coverage

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

## Security and isolation requirements for future implementation

1. All tools invoked through secure MCP gateway, with authenticated actor context from the backend, not customer-supplied text or LLM instructions.
2. Verify customer ownership/assigned-region scope and least-privilege role access server-side.
3. Enforce idempotency keys and optimistic version checks on state-changing writes; durable audit events are required.
4. Reject tampered business-tool output before state changes. Make disagreement and failed reconciliation visible to the Supervisor.
5. Preserve snapshot and pricing rule versions across handoffs, caching, review and approval.
6. Expose **no** `approve_quote` or `release_quote` MCP tools to AI agents; human manager release is a separate authenticated interface with active customer, commercial clearance, current approval version and independent approver.
7. Never mount `evaluation/ground_truth/`, hidden expected results, security outcomes, or evaluation coordination labels into agent-visible retrieval contexts.
8. Treat `MFG-001` as a required engineering-validation guardrail for future untrusted inputs; the frozen reference engine currently does not enforce the scalar hole maximum.

## Verification limits

All 240 scenario-manifest pairs had matching scenario and label IDs and valid distinct file paths. No top-level `expected_*` keys appeared in the agent-visible RFQ objects, based on the targeted inspection. **This does not prove future runtime access isolation.** The dataset contains contracts and reference code, not deployed MCP endpoints, an RBAC gateway, or running agents.
