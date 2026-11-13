# Manufacturing Feasibility Agent (A2, Day 38)

Module: `src/rfq_copilot/manufacturing_agent.py`. Tests: `tests/test_manufacturing_agent.py`
(offline) and `tests/test_manufacturing_agent_live_mcp.py` (opt-in live MCP).

A2 answers one question per part: can the fictional manufacturer make this part under the
pinned manufacturing rules (`mfg-v1.0`), and through which operations? It does not cover
inventory, capacity, lead time, cost, price, approval or orchestration.

## Interface (for the future Supervisor)

```python
await evaluate_manufacturing_feasibility(
    intake: IntakeValidation,          # Day 37 output
    execute: ToolExecutor | None,      # trusted, principal-bound: graph.mcp_executor(...)
    *, timeout_seconds: float = 60.0,
) -> ManufacturingAssessment
```

- **Input gate.** A2 calls no tool unless `intake.readiness` and every part are
  `ready_for_manufacturing_evaluation` and every part has a built `PartSpecification`.
  Otherwise the result is `not_evaluated`, with `not_evaluated_reason` set to the blocking
  Intake readiness, `specification_missing` or `no_parts`.
- **Executor.** The executor is the existing Day 34 `ToolExecutor`. `graph.mcp_executor` binds
  it to a verified `Principal`. Each call goes through `McpIntegration`, which uses the
  delegated MCP credential and the authenticated gateway, and re-checks the user, the customer
  and the RBAC v2 `view_catalog` permission. If `execute` is `None`, the result is
  `error / unauthenticated`.

## Tool permissions

| Tool | Server | Permission | Service it runs |
|---|---|---|---|
| `check_manufacturing_feasibility` | manufacturing | `view_catalog` | `services.manufacturing.check_manufacturing_capability` (runs `services.materials.check_part_material` inside) |

The allow-list is `ALLOWED_TOOLS`. A test asserts that it is a subset of
`graph.READ_ONLY_TOOLS`. The module does not import the graph, because application modules
must not (`test_conversations.py`). A2 does not need these tools:

- `check_material_suitability`: its result is already embedded as `output.material`.
- `get_material`: the spec is in `output.material.material`.
- `search_materials`: catalog mapping is Intake's job.

No MCP contract was changed.

## Execution flow

1. Gate the input (see above). A2 never edits the specification.
2. For each part, sequentially, once each, with no retries, inside one `asyncio.timeout`:
   build `PartCheckInput(part=spec)` and record the sha256 of its JSON. Then call the executor
   and re-validate the result against `ManufacturingFeasibilityOutput`.
3. Check the evidence (fail closed). These results become `invalid_evidence`, and their
   evidence is not kept:
   - the material check's rule version or fingerprint differs from the outer result's
   - a finding code is not one the services produce. `MISSING_<field>` and
     `OUT_OF_RANGE_<field>` are accepted only for real `PartSpecification` fields (plus
     `OUT_OF_RANGE_BEND_COUNT`). The material check may carry only material codes.
   - the outer result or the material check breaks the services' status rule: `valid` has no
     findings, `needs_clarification` has only `MISSING_*` findings, and `unsupported` has
     findings but no `MISSING_*`
   - a `MISSING_*` code names a field the part states
   - the catalog entry is another material, is present alongside `UNSUPPORTED_MATERIAL`, or
     is absent without `UNSUPPORTED_MATERIAL` or a clarification
   - the material findings are not carried into the outer result unchanged (a clarification
     keeps only the material asks)
   - `needs_clarification` has a route
   - `valid` lacks a route or catalog entry, or the catalog entry does not list the part's
     thickness, finish and every routed operation (D38-01)
   - `valid` has a `bend` route without `min_bend_radius_mm`, or a minimum below
     `thickness x` the catalog's `min_bend_radius_factor` (the service's floor), or above the
     part's stated bend radius. A5's borderline-bend review reads this value (D42-A2-01)

   The services' own exception is accepted: when a non-material field is missing, they report
   only what to ask for.
4. Check the rule pins. The tool's `rule_version` and `rule_fingerprint` must equal `mfg-v1.0`
   and the fingerprint Intake validated against. Otherwise the part is `requires_review`, with
   the evidence kept for the reviewer.
5. Map the result. `valid` becomes `feasible`, `unsupported` becomes `infeasible`, and
   `needs_clarification` keeps its name, with `clarifications` set to the `PartSpecification`
   fields named by `MISSING_*`.
6. Write the explanation from finding codes using fixed templates, naming the part by its index.
   The explanation never includes customer text such as `part_id` or model prose.
7. Set the RFQ-level status to the most blocking part status:
   `error > infeasible > needs_clarification > requires_review > feasible`.

Tool failures map to `error`, using the `McpCallError` code (`unavailable`, `timeout`, `busy`,
`forbidden`, `not_found`, `unauthenticated`, `conflict`, `tool_error`, `invalid_input`). They
also map to `invalid_output` for an off-contract result, `unavailable` for an executor fault
(its text is never surfaced) and `timeout` for every part still pending when the deadline hits.

## Output: `ManufacturingAssessment` (frozen, `extra="forbid"`, JSON round-trips)

| Field | Meaning |
|---|---|
| `source_id`, `rfq_id` | From the trusted `IntakeValidation` |
| `status` | `feasible`, `infeasible`, `needs_clarification`, `requires_review`, `not_evaluated` or `error` |
| `manufacturing_rule_version`, `manufacturing_rule_fingerprint` | The pin Intake validated against |
| `parts[]` | One `PartFeasibility` per part (fields below) |
| `not_evaluated_reason`, `error` | Why nothing was concluded |

Each `PartFeasibility` has these fields:

- `index`, `part_id`
- `status`
- `specification`: unchanged
- `clarifications`
- `explanation`
- `evidence`: a `ToolEvidence`, or `None` when no conclusion was reached
- `error`

`ToolEvidence` has these fields:

- `tool`, `server`
- `arguments_sha256`
- `output`: the exact `ManufacturingFeasibilityOutput`, which holds:
  - `result`, `findings`, `operations` (the route), `min_bend_radius_mm`
  - `material`: the material and finish assessment with the catalog spec
  - `rule_version`, `rule_fingerprint`

A part is `feasible` only when `evidence` holds a validated, consistent tool result.

## Authority and security

- There is no LLM. The deterministic services decide feasibility, findings and routing.
  Phase 5 already decided that model prose is never shown, so a model would only have chosen
  the one fixed tool. A2 therefore cannot calculate, override or invent feasibility, and
  prompt injection has nothing to steer. The tests make every model `ainvoke` fail.
- MFG-001 is preserved through the service: 300 holes pass and 301 give
  `OUT_OF_RANGE_HOLE_COUNT`. This is tested offline with the real pure services and in the
  live test.
- Tool arguments are just `{"part": PartSpecification}` (`extra="forbid"`). They have no
  customer, role, permission or rule-version field. The customer comes only from the
  executor's verified principal.
- Each call carries only its own part. A failure stays with its own part, and the RFQ status
  is the most blocking status.
- A2 makes no writes. The tool runs in a `READ ONLY` snapshot transaction on the server.

## Testing

- `tests/test_manufacturing_agent.py` (offline). A fake executor runs the real
  `evaluate_material` and `evaluate_manufacturing` over the frozen catalog and maps the result
  as the MCP server does. It covers:
  - feasible parts, unsupported material, finish and thickness
  - envelope and tolerance violations
  - MFG-001 (0, 300 and 301 holes)
  - missing height and routing per part type and finish
  - every tool failure code, executor faults, timeouts and a missing executor
  - off-contract and forged results, and rule drift
  - injection in `part_id`
  - argument scope, per-part isolation and principal binding through `graph.mcp_executor`
  - not-ready input, the part bound and serialization
  - the real Intake → A2 handoff
- `tests/test_manufacturing_agent_live_mcp.py` (`RFQ_AGENT_LIVE_MCP=1`). It runs through the
  real uvicorn MCP host and the seeded test database. It covers:
  - the Intake → A2 handoff
  - MFG-001 300/301
  - unsupported thickness
  - a revoked subject

## Known limitations

- The explanation has no rule limits or numbers in it, only codes and fixed text. The MCP
  output does not carry the rule document, and A2 does not re-read it.
- The services have no engineering-review outcome. A2 uses `requires_review` only for rule
  drift between Intake and evaluation.
- The offline routing table in the tests stands in for `operation_routing`. The live test reads
  the stored routing.
- A2 does not evaluate substitute materials. That would be a separate candidate evaluation and
  is not implemented.
- A2 is a plain async function rather than a LangGraph `StateGraph`. One deterministic tool
  call per part has no branching for a graph to add. The future Supervisor can wrap it as a
  node.
