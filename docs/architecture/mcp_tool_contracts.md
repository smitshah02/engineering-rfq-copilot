# MCP tool contracts (Day 24)

Source of truth: `src/rfq_copilot/mcp/contracts.py` (`TOOLS`). Tests:
`tests/test_mcp_contracts.py`. SDK: `mcp` 2.3.0. Contracts only: no server, endpoint or
adapter exists yet (Days 25–26). Architecture: [mcp_architecture.md](mcp_architecture.md).

All tools are read-only (`ToolAnnotations(read_only_hint=True, destructive_hint=False,
idempotent_hint=True, open_world_hint=False)`). Permissions are rbac-policy-v2 names. MCP
runs under v2 only (F1). JSON schemas come from the Pydantic models through the SDK.
Nothing is maintained by hand.

| Tool | Server | Input | Output | Permission | Service (Day 25/26) |
|---|---|---|---|---|---|
| `search_materials` | manufacturing | `MaterialListQuery` (category, grade, thickness_mm, finish_id, limit ≤100, offset) | `MaterialPage` | `view_catalog` | `repositories.queries.find_materials` |
| `get_material` | manufacturing | `GetMaterialInput` (material_id) | `MaterialSpecOutput` | `view_catalog` | `services.materials.get_material_spec` |
| `check_material_suitability` | manufacturing | `PartCheckInput` (part) | `MaterialSuitabilityOutput` | `view_catalog` | `services.materials.check_part_material` |
| `check_manufacturing_feasibility` | manufacturing | `PartCheckInput` | `ManufacturingFeasibilityOutput` | `view_catalog` | `services.manufacturing.check_manufacturing_capability` |
| `check_inventory` | supply | `PlanningInput` | `InventoryOutput` | `view_assigned_rfqs` | `services.inventory.check_inventory` |
| `check_capacity` | supply | `PlanningInput` | `CapacityOutput` | `view_assigned_rfqs` | `services.capacity.check_capacity` |
| `estimate_lead_time` | supply | `DeliveryPlanningInput` | `LeadTimeOutput` | `view_assigned_rfqs` | `services.lead_time.estimate_lead_time` |
| `estimate_costs` | pricing | `PlanningInput` | `CostOutput` | `view_quote` | `services.costing.estimate_costs` |
| `calculate_price` | pricing | `PlanningInput` | `PriceOutput` | `view_quote` | `services.pricing.calculate_quote` |
| `evaluate_quotation` | pricing | `DeliveryPlanningInput` | `QuotationOutput` | `view_quote` | `services.quotation.quote_parts` / `quote_rfq` |

## Inputs

- `part` / `parts`: the existing `api.schemas.rfq.PartSpecification` (millimetres,
  structural only; missing fields become clarification findings), 1–50 parts.
- `PlanningInput`: exactly one of `rfq_id` or `parts`, plus `horizon_start` (ISO date, a
  Monday). `DeliveryPlanningInput` adds an optional `requested_delivery_date`.
- Exact numbers: an integer or a plain decimal string. JSON floats, exponents, signs and
  NaN are rejected.

## Outputs

Field names match the frozen service dataclasses. Each output keeps the provenance fields
(rule version and fingerprint, snapshot ids). Decimals serialize as strings at service
scale. A business rejection (`unsupported`, `needs_clarification`, `insufficient`,
`infeasible`, `not_manufacturable`) is a **successful** result whose `result` and
`findings` explain it.

## Restrictions

- **Identity:** no input has a customer, user, role, permission or tenant field
  (`extra="forbid"`, enforced by the SDK before a tool body runs). The customer comes only
  from the verified principal (Day 27). `rfq_id` is an untrusted key. The adapter loads it
  with `customer_id=principal.customer_id`, and a missing RFQ and someone else's RFQ both
  answer `NOT_FOUND`.
- **Upstream results:** no input accepts `InventoryCheck`, `CapacityCheck`, `CostEstimate`,
  `PriceCalculation`, rule documents or snapshot pins. Day 25/26 adapters must recompute
  the prerequisites (inventory → capacity/costs → price) in one `snapshot_session()`, and
  rules and snapshots must be selected server-side (`select_snapshots`, pinned
  `get_rule_version`).
- **Approval:** `NEW_EVALUATION_APPROVAL_RULE_VERSION = "approval-v1.2"` is a server-side
  constant, and no input can override it. `approval-v1.1` stays reachable only on trusted
  internal replay paths. The commercial-policy tool (`evaluate_stored_rfq`) is **deferred**:
  it needs a dedicated permission (a new grant in a policy overlay), which is not approved.
- **No writes:** no tool approves, releases, persists, submits or uploads. No output has
  an approval, release, draft or customer field.

## Errors

| Code | When | MCP form |
|---|---|---|
| `INVALID_INPUT` | arguments fail the schema (SDK) or a service `ValueError` on input | tool error (`is_error`) |
| `UNAUTHENTICATED` | principal unavailable mid-call (normally HTTP 401 before any tool runs) | tool error |
| `PERMISSION_DENIED` | principal lacks the tool's permission | tool error |
| `NOT_FOUND` | RFQ or material absent **or not visible** | tool error |
| `SNAPSHOT_CONFLICT` | `SnapshotMismatchError`, `RuleIntegrityError` | tool error |
| `INTERNAL` | anything else; traceback logged server-side only | tool error |

`tool_failure(code)` builds `ToolError("CODE: <fixed message>")`. The texts are fixed and
never echo exceptions, SQL, paths or identifiers.

**SDK note (verified):** in `mcp` 2.3.0 the high-level server reports argument-validation
failures as a tool error whose text includes the Pydantic message. That message contains
the caller's own input values, but no server data. It does not report them as JSON-RPC
`-32602`.

**Runtime (Day 25, `mcp/safety.py`):** the servers register the SDK `ServerMiddleware`
`sanitize_tool_errors`.
- **Error text:** it rewrites every `tools/call` error result to one of the six fixed
  texts (an allow-list), so SDK validation errors no longer echo input.
- **Unknown arguments:** it refuses unknown top-level arguments (only `request` is
  allowed); the SDK would otherwise drop them silently.
- **Malformed params:** these become `-32602` with a fixed message.
- **Coverage limit:** the middleware runs on the wire path (`Client`, stdio, HTTP), not on
  direct in-process `MCPServer.call_tool()` calls.

Failures inside a tool body go through `safe_call`:
- `RuleIntegrityError` and `SnapshotMismatchError` → `SNAPSHOT_CONFLICT`.
- `DatabaseError` → `INTERNAL`.
- Input conversion (`runtime.to_parts`: `Part.from_mapping` plus the existing
  `validate_lines`, e.g. duplicate part IDs) → `INVALID_INPUT`.
- Any other `ValueError` (a service or output-contract check after validation), or any
  other exception → `INTERNAL`, with details logged server-side only (Day 26 A1).

**Dispatch boundary:** call servers only through the SDK dispatch path (`mcp.Client` or a
transport). Direct `MCPServer.call_tool()` bypasses `sanitize_tool_errors` and must not
be used by application code.

**Authorization (Day 27):** every tool runs only with a verified, request-scoped
`McpPrincipal`. `McpTokenVerifier` checks the bearer token over HTTP:
- signature, issuer, the dedicated MCP audience, exp/nbf/iat, and `sub`;
- revocation, which is always enforced;
- an active user and a single customer binding.

Then `runtime._authorized` checks the tool's v2 permission before the handler runs.
Missing principal → `UNAUTHENTICATED`; missing permission → `PERMISSION_DENIED`. Neither
opens a business snapshot. An in-memory `Client` carries no principal, so it always gets
`UNAUTHENTICATED`.

**Stored RFQs:** an `rfq_id` is read inside the business snapshot with
`get_rfq(customer_id=principal.customer_id, assigned_user=...)`. Absent, foreign and
unassigned RFQs all give the same `NOT_FOUND`. A visible RFQ without runtime lines gives
`INVALID_INPUT`. Over MCP the customer comes only from the single binding: there is no
customer-selection header, so a user with several bindings is refused.

**Recomputation (Day 26):** every supply and pricing call runs in one snapshot session:
`select_snapshots` → `check_inventory` → (`estimate_costs` → `calculate_quote`), and
capacity/lead time via `estimate_lead_time`, quotation via `quote_parts`. The rule
versions `mfg-v1.0` and `price-v1.0` are pinned in `mcp/runtime.py`, sha256-checked. No
Day 26 tool needs approval rules.

**Protocol versions:** the SDK may negotiate legacy revisions. Restricting them is a runtime
responsibility (Day 28) and is not enforced by these contracts.
