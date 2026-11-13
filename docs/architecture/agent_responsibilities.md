# Engineering RFQ Multi-Agent Copilot — Agent Responsibilities

**Document path (requested):** `docs/architecture/agent_resposibilities.md`  
**Project:** Engineering RFQ Multi-Agent Copilot  
**Roadmap:** Day 1 of 73 — Component 2: Multi-Agent System Architecture  
**Design status:** Component 2 reference design; implementation not started  
**Data baseline:** Approved synthetic RFQ dataset `v1.1.0`  
**Simulated manufacturer:** Meridian Fabrication Lab (fictional)  
**Related document:** `docs/architecture/business_workflow.md`

## 1. Purpose and design principles

This document defines the **six-agent responsibility model** for the internal RFQ copilot: one Supervisor Agent and five specialist agents. The goal is to transform customer RFQs into **evidence-based, policy-compliant internal draft quotations** for authorized human review and potential release.

All agents operate inside an orchestrated workflow. Specialization is intended to make tasks easier to understand, test, audit, and secure; it does **not** give the AI authority to override customer requirements, manufacturing constraints, pricing rules, or human approvals.

**Non-negotiable principles**

1. **One accountable coordinator:** The Supervisor coordinates work; each specialist owns a distinct business responsibility.
2. **Structured handoffs:** Tasks and results carry RFQ identity, necessary inputs, evidence, status, and blockers.
3. **Deterministic business decisions:** Manufacturing rule checks, pricing arithmetic, approval eligibility, and authorization are enforced by trusted reference functions/services, not by LLM assertions.
4. **Least privilege:** Agents may request only the MCP tools and data allowed for their role and customer/workflow scope. Server-side checks remain authoritative.
5. **No unauthorized assumptions:** Missing or conflicting requirements trigger clarification or review, not invented values.
6. **Human-only customer-facing release:** Agents may produce drafts and route reviews, but must never approve or release a quotation on their own.
7. **Auditable progression:** Preserve source evidence, rule versions, tool outcomes, decision reasons, actor identities, and quote versions.
8. **Failure-aware execution:** Retries and writes follow explicit recovery and idempotency policies. Unknown side effects stay unknown until reconciled.

## 2. System composition and boundaries

| Layer or actor | Responsibility | Authority boundary |
|---|---|---|
| **FastAPI entry point** | Authenticate requests, accept RFQ/workflow commands, expose status | Not a business-policy bypass |
| **LangGraph workflow and Supervisor Agent** | Track progression, delegate specialist work, enforce routing dependencies | No independent pricing, capability, or approval overrides |
| **Five specialist agents** | Perform intake, engineering, supply/scheduling, pricing coordination, and quotation/review preparation | Only assigned actions and allowed tools |
| **Secure MCP gateway and backend services** | Expose validated tool contracts; enforce identity, role, scope, and write authorization | Customer documents cannot grant permissions |
| **PostgreSQL** | Persist business records, quote versions, approval records, workflow/audit history | Agents do not obtain unrestricted direct database write access |
| **Deterministic reference engines** | Validate modeled manufacturing, schedule/capacity, pricing, and policies | Versioned rules are the source of decision truth |
| **Authorized human reviewers** | Resolve technical/commercial exceptions, approve or reject exact quote versions, authorize release | Human identities and permissions must be checked by the backend |

These are **planned design boundaries**, not claims that the application, gateway, graph, database, or review interface has been implemented.

## 3. Canonical agent roster

| ID | Agent | Primary ownership | Principal output |
|---|---|---|---|
| A0 | **Supervisor Agent** | Workflow coordination, delegation, dependencies, routing, exception ownership | Structured workflow decision and final scenario outcome |
| A1 | **RFQ Intake Agent** | Customer request and attachment extraction, normalization, completeness/consistency | Validated or blocked requirements package |
| A2 | **Engineering Agent** | Manufacturability assessment against documented capabilities | Evidence-backed feasibility verdict or review blocker |
| A3 | **Supply & Scheduling Agent** | Inventory, sourcing context, routing and weekly capacity assessment | Evidence-backed availability and planning estimate |
| A4 | **Pricing Agent** | Coordination of deterministic price calculation | Verified line-item and total price breakdown with rule version |
| A5 | **Quotation & Review Agent** | Internal quotation drafting, versioning and human-review coordination | Controlled draft and review-readiness state |

### 3.1 Shared handoff contract (conceptual)

Each handoff should include, as applicable:

- **Identity:** `rfq_id`, workflow/run ID, customer/tenant scope, assigned agent, task ID.
- **Purpose:** requested operation and prerequisite/allowed scope.
- **Input references:** validated requirements, relevant part IDs, source document references, prior results.
- **Execution facts:** invoked tool names, rule/policy versions, trace or correlation IDs, timestamps.
- **Outcome:** `completed`, `blocked`, `needs_clarification`, `needs_human_review`, or `failed` (conceptual task statuses, **not** the dataset's eight final workflow outcome labels).
- **Evidence and issues:** structured findings, supporting source/tool references, unresolved conflicts, risk flags.
- **Next-step suggestion:** only an allowed recommendation; the Supervisor and backend policies determine permitted workflow transitions.

Avoid shipping entire customer databases, hidden benchmark labels, or arbitrary tool credentials between agents. Concrete schemas must later align with the dataset's `contracts/` definitions; this section is an architectural description, not a finalized API schema.

## 4. A0 — Supervisor Agent

**Purpose:** Own the coordination of a single RFQ workflow from validated entry through an applicable business outcome.

**Inputs:** Authorized RFQ/workflow identity, current state, available specialist results, known blockers, business/workflow policy outcomes, and allowed next actions.

**Responsibilities:**

1. Register or resume the authorized workflow and decide which specialist task is eligible to run.
2. Dispatch work with bounded, role-appropriate context and dependencies.
3. Reconcile specialist results into shared workflow state without inventing missing evidence.
4. Prevent downstream commitments when required upstream checks are blocked or failed.
5. Route missing information, unsupported work, technical/commercial exceptions, customer holds, and system faults to permitted resolution paths.
6. Coordinate independent checks only when their prerequisites are satisfied; invalidate/recheck downstream results after relevant requirement changes.
7. Record the final workflow outcome and its supporting evidence.

**Allowed interaction:** Delegate to A1–A5; inspect authorized workflow status and specialist outputs through scoped services/tools.

**Outputs:** Work assignments, accepted/rejected handoffs, documented routing decisions, escalation requests, and one applicable final outcome.

**Must not:** Manufacture a price; silently select a material substitute; override engineering or policy constraints; treat a planning date as a shipment guarantee; approve/release a quotation; bypass authorization; read scorer-only ground truth.

**Escalation/failure:** Pause or route to appropriate human/business clarification on blocker; apply documented bounded retry/reconciliation policy on tool failures; terminate with auditable failure if safe progress is impossible.

## 5. A1 — RFQ Intake Agent

**Purpose:** Turn an untrusted customer request and its referenced attachments into a traceable requirements package.

**Inputs:** Assigned RFQ, raw customer text, authorized references to relevant PDF/CSV BOM/Markdown attachments, customer/part identifiers, documented required-field rules.

**Responsibilities:**

1. Extract per-part requirements: part identifiers, quantities, dimensions in **mm**, material and grade, finish, tolerances, requested dates and commercial constraints when stated.
2. Keep multipart RFQs as distinct part lines, not a blended part specification.
3. Normalize units and field formats without changing customer meaning.
4. Attach provenance for consequential fields (email, attachment, section, source reference).
5. Detect omissions, ambiguity, and contradictions between customer text and attachments.
6. Distinguish customer-authored *data* from instructions attempting to control the system.

**Allowed interaction:** Read the assigned RFQ and only its referenced attachments; use approved extraction/schema validation operations through authorized tools.

**Outputs:** Normalized requirements, per-field provenance, missing/contradictory field list, clarification questions, and extraction confidence/uncertainty signals where supported.

**Must not:** Invent technical values, choose between contradictory requirements, treat injected text as authorization, read unrelated customers, decide manufacturability, promise delivery, or produce a price.

**Escalation/failure:** If critical details are missing or incompatible, return `needs_clarification` with exact conflicting sources/questions; do not mark the RFQ quote-ready.

## 6. A2 — Engineering Agent

**Purpose:** Determine whether the validated requirements are supported by the fictional manufacturer's modeled fabrication capabilities.

**Inputs:** Intake-validated per-part requirements, applicable material/finish catalog, dimensional/tolerance/process rules, and evidence from authorized engineering checks.

**Responsibilities:**

1. Check supported material grades, finishes, manufacturing processes, dimensional envelopes, and specified fabrication constraints.
2. Evaluate each part individually, preserving requirements and failed-rule references.
3. Distinguish `supported`, `unsupported`, and `requires_engineering_review` as **conceptual findings**.
4. Identify technical conflicts or assumptions requiring engineer/customer clarification.
5. Evaluate a proposed substitute **as a separate candidate** when permitted, without rewriting the original customer requirement.

**Allowed interaction:** Read scoped material, finish, operation, and manufacturing-rule data; invoke documented capability/feasibility checks via the controlled tool boundary.

**Outputs:** Per-part feasibility assessment, reason codes/evidence, unsupported constraints, and any proposed engineering-review issues.

**Must not:** Invent machine capability or CAD geometry conclusions; auto-approve an alternative grade; change customer drawings/specifications; set prices; assign production slots; release quotes.

**Implementation (Day 38):** `rfq_copilot.manufacturing_agent`, see [manufacturing_agent.md](manufacturing_agent.md). It maps `supported` / `unsupported` / clarification / review to `feasible` / `infeasible` / `needs_clarification` / `requires_review`. The decisions come only from the `check_manufacturing_feasibility` MCP tool.

**Escalation/failure:** Unsupported work may produce `unsupported_declined` at workflow level; uncertain/exceptional technical issues may require `engineering_review_required`. Supply availability does not override an unsupported-material finding.

## 7. A3 — Supply & Scheduling Agent

**Purpose:** Assess whether required material and modeled production capacity support a realistic *planning estimate*.

**Inputs:** Approved/validated per-part requirements, material mass or verified mass inputs, production routing, inventory in **kg**, suppliers/procurement context, weekly workcentre capacity in hours, requested delivery date.

**Responsibilities:**

1. Query relevant material availability and shortages, respecting inventory units (kg, never unit counts unless explicitly converted through validated data).
2. Examine replenishment and documented sourcing information when present.
3. Check required routing and capacity constraints for the simulated workcentres.
4. Report bottlenecks, supported planning window, and whether the requested date is plausible under the reference model.
5. Surface possible alternatives for engineering/customer consideration without authorizing any substitution.
6. Mark findings stale if upstream engineering/quantity/material assumptions change.

**Allowed interaction:** Scoped reads of inventory, supplier/procurement, routing and weekly-capacity data; approved reference scheduling/availability operations.

**Outputs:** Stock sufficiency, sourcing constraints, capacity assessment, estimated planning week/lead-time basis, risks, and unmet delivery target warning.

**Must not:** Reserve inventory or capacity (not part of dataset baseline); promise a ship date; unilaterally substitute materials; override engineering findings; directly alter production records; approve release.

**Implementation (Day 39):** `rfq_copilot.supply_agent`, see [supply_scheduling_agent.md](supply_scheduling_agent.md). The read-only MCP tools `check_inventory`, `check_capacity` and `estimate_lead_time` make the decisions. The statuses are `available`, `constrained`, `needs_clarification`, `requires_review`, `not_evaluated` and `error`. A3 never reserves stock or commits a date.

**Escalation/failure:** Shortage or overloaded workcentre results in an evidence-based blocker or revised planning estimate; unresolved replacement materials must return to engineering and obtain customer authorization before substitution can be accepted.

**Important baseline limit:** Dataset v1.1.0 models weekly capacity and Friday-of-week schedule estimates. This is **not** an operational manufacturing scheduler or a confirmed shipment commitment.

## 8. A4 — Pricing Agent

**Purpose:** Coordinate trusted calculation of price using verified inputs and the dataset's pinned pricing rules.

**Inputs:** Validated part/quantity and technical data, engineering/supply findings, authorized commercial context, and pricing policy version `price-v1.0`.

**Responsibilities:**

1. Verify that all required pricing inputs and prerequisite findings exist.
2. Call the approved versioned deterministic pricing calculator; do not compute final amounts by LLM improvisation.
3. Preserve per-part and overall totals, currency **USD**, calculator/rule version, and assumptions.
4. Distinguish missing-input or commercial-policy exceptions from successful calculated prices.
5. Deliver an auditable breakdown suitable for quotation drafting.

**Allowed interaction:** Authorized reads of required input/rule data and invocation of the deterministic reference pricing operation.

**Outputs:** Pricing breakdown, totals and quote-line references; source/tool evidence, `price-v1.0` provenance, and any required commercial-review flags.

**Must not:** Invent prices, discounts, margin overrides or taxes/freight estimates; accept natural-language numbers as final cost calculations; silently use a different pricing version; approve/release a quote.

**Escalation/failure:** If pricing inputs are insufficient or a rule cannot be applied, stop that pricing path and request correction, clarification, or commercial review rather than presenting a fabricated figure.

**Implementation (Day 40):** `rfq_copilot.pricing_agent`, see [pricing_agent.md](pricing_agent.md). The read-only MCP tools `estimate_costs` and `calculate_price` (`view_quote`) are the only source of amounts. A4 checks them against the trusted `price-v1.0` rule and A2/A3 evidence. The statuses are `priced`, `requires_review`, `not_evaluated` and `error`. A constrained supply is priced and flagged for review. A4 never quotes, approves or releases.

**Important baseline limit:** Synthetic prices are internally consistent and deterministic, not industrially calibrated. The dataset model excludes **tax, shipping and freight**.

## 9. A5 — Quotation & Review Agent

**Purpose:** Produce an accurate **internal, versioned draft quotation** and coordinate appropriate review without exercising human authority.

**Inputs:** Validated RFQ scope, engineering findings, supply/schedule planning estimate, verified pricing breakdown, customer/commercial eligibility, and approval-policy flags.

**Responsibilities:**

1. Assemble parts, quantities, verified prices, estimated planning times, assumptions, exceptions, and exclusions into a draft.
2. Verify `draft_creation_allowed` before requesting any draft write.
3. Create a versioned internal draft through an authorized, idempotent service operation.
4. Distinguish `manager_review_required` from `human_release_approval_required`.
5. Present the exact quote version and evidence to the authorized human review process.
6. Track review outcome, rejection, revision request, customer hold, and approval-readiness state.
7. Ensure changed versions are re-reviewed/re-approved as required by policy.

**Allowed interaction:** Scoped reads of validated RFQ/quote data; authenticated, authorized draft-creation operations; review-request/status tools permitted by policy.

**Outputs:** Draft quotation identifier and version, quote content, remaining blockers, review routing, and approval-readiness information.

**Must not:** Forge or record a human approval as the agent; approve its own draft; release a quote; reuse approval from an earlier quote version; ignore customer holds or blocked releases; fabricate missing evidence.

**Escalation/failure:** If a human rejects or requests revisions, preserve the outcome and return the affected issue to the appropriate owner. On uncertain draft-write status, reconcile by idempotency key before any retry.

**Implementation (Day 41):** `rfq_copilot.quotation_agent`, see [quotation_review_agent.md](quotation_review_agent.md). A5 re-verifies A4 by replaying it over A4's own evidence. It reads the quotation service once through the read-only `evaluate_quotation` tool (`view_quote`) and applies the `services.evaluation` policy helpers under the trusted `approval-v1.2`. It returns a **non-persisted** draft: `draft_ready_for_review`, `requires_review`, `clarification_required`, `not_evaluated` or `error`. Responsibilities 3, 6 and 7 (versioned draft write, review tracking, re-approval) are not implemented, because no authorized write or review-status tool exists. A5 never approves, releases or persists anything.

## 10. Required workflow order and dependencies

The **business dependency graph** is:

```text
Authorized RFQ submission
          |
          v
A0 Supervisor -> A1 Intake
          |
          v
   Requirements valid? ---- no ---> Clarification / blocked path
          |
         yes
          v
     A2 Engineering -------- unsupported/uncertain ---> Decline or engineering review
          |
     supported
          v
 A3 Supply & Scheduling ---- shortage/date conflict ---> Review / alternative / revised estimate
          |
    validated basis
          v
      A4 Pricing ----------- missing input/policy issue ---> Clarification / commercial review
          |
    verified price
          v
  A5 Quotation & Review ---- draft_creation_allowed=false ---> No draft write
          |
       draft
          v
 Authorized human review/approval of exact quote version
          |
          v
 Eligible for customer-facing release only after backend checks
```

The arrows represent **required information and decision dependencies**, not a blanket requirement to execute every tool sequentially. Independent safe reads may be concurrent, but downstream results must not be relied upon when their required inputs are unverified or have changed.

**Rework rule:** A change to material, dimensions, quantity, finish or other materially relevant requirements can invalidate prior engineering, supply, pricing and draft results. The workflow must re-run affected checks and use a new quote version as appropriate.

## 11. Decision ownership, escalation and approval

| Event | Primary evaluator | Routing decision | Required external authority |
|---|---|---|---|
| Missing/contradictory RFQ field | A1 Intake | A0 Supervisor | Customer or authorized RFQ coordinator provides clarification |
| Unsupported process, material or design | A2 Engineering | A0 Supervisor | Engineer for exceptions; customer for any specification change |
| Inventory shortage / date not achievable | A3 Supply & Scheduling | A0 Supervisor | Customer acceptance if requirements or timeline are changed; relevant internal review |
| Commercial/pricing exception | A4 Pricing | A0 Supervisor | Authorized commercial/manager reviewer where required |
| Draft creation eligibility | A5 Quotation & Review + backend policy | A0 Supervisor coordinates | Authenticated service policy, not LLM discretion |
| Additional managerial review | A5 + policy | A0 Supervisor coordinates | Authorized human manager when required |
| Customer-facing release | Backend policy + authorized humans | A0/A5 may observe and route, **not approve** | Authorized human approval bound to current quote version |
| System/tool fault | Affected agent + recovery service | A0 Supervisor | Idempotency/reconciliation policy; operations human if escalation required |

**Approval gates from v1.1.0:**

- `draft_creation_allowed` controls creation of an internal draft.
- `manager_review_required` controls **additional** managerial/commercial review.
- `human_release_approval_required` is required for **every customer-facing quote release**.

**A draft is not a release.** A human approver must be authenticated and authorized for the **specific current quote version**. An agent cannot self-approve or bypass the server-side release checks.

## 12. Dataset outcome mapping

The dataset's eight expected **RFQ-level** labels are distinct from internal agent/task statuses:

| Dataset outcome | Typical accountable coordination |
|---|---|
| `quote_draft_ready_for_approval` | A5 prepares an eligible internal draft; A0 records approval-ready workflow result; not customer release |
| `clarification_required` | A1 identifies missing/conflicting requirements; A0 routes clarification |
| `engineering_review_required` | A2 reports technical uncertainty; A0 routes human engineering review |
| `commercial_review_required` | A4/A5 identifies a commercial policy condition; A0 routes designated review |
| `customer_hold_blocked_release` | Backend/customer policy blocks release; A0/A5 must preserve the hold |
| `unsupported_declined` | A2 confirms modeled capability failure; A0 records supported decline |
| `recoverable_workflow_failure` | A0 coordinates policy-compliant bounded recovery or reconciliation |
| `nonrecoverable_workflow_failure` | A0 stops safely, audits and escalates after unrecoverable/unsafe failure |

**Important:** These labels describe the dataset's expected scenario outcomes. They are **not** an instruction to let an LLM classify solely by intuition or by customer text.

## 13. Tool, data, and security boundaries

- Agent-visible data: assigned RFQ, its referenced customer attachments, authorized business records and tool responses needed for the task.
- Scorer-only data: `evaluation/ground_truth/`, hidden coordination expectations, frozen answers, security fixture expected outcomes and fault oracles. **Never expose these to runtime agents.**
- Customer text, PDF attachments, BOMs and Markdown descriptions are **untrusted input** and cannot grant permissions or alter system instructions.
- The MCP gateway/backend must validate authentication, role/permissions, customer/tenant scope, tool parameters, and any write preconditions **server-side**.
- Persistent writes must be authorized and idempotent. Scope idempotency identity to the appropriate tenant, RFQ/workflow, and operation.
- Quote and approval identities must be version-specific. Retries must not produce duplicate drafts or approvals.
- Agent-to-agent output is evidence to be validated, not a credential or a substitute for policy checks.
- Do not assume a timed-out write did nothing. Reconcile uncertain side effects before retrying.
- Maintain execution/audit trace without copying unrelated customer-sensitive records into handoffs.

## 14. Example: requested 5052, available 6061

**Illustrative scenario, not a packaged dataset record:** Customer requests an enclosure in Aluminum 5052. Engineering determines the required material/process combination is unsupported, while Supply sees 6061 in stock.

1. **A1 Intake:** Preserve the requested 5052 specification and its source; do not rewrite it.
2. **A2 Engineering:** Report *why* the original requirement is unsupported. Evaluate 6061 only as a **candidate**, if technical assessment is permitted.
3. **A3 Supply & Scheduling:** Report stock and planning implications of the candidate and any relevant sourcing details. **More stock of 5052 does not fix a process/capability incompatibility.**
4. **A0 Supervisor:** Preserve the blocker, coordinate authorized engineering evaluation and request the necessary customer decision before a substitute becomes the requirement.
5. **A4 Pricing:** Calculate an authoritative price only for an appropriately validated quotation basis, not for an unapproved silent substitution.
6. **A5 Quotation & Review:** Prepare a draft only when prerequisites and `draft_creation_allowed` permit it; route human review. Never release autonomously.

**Correct principle:** Stock availability cannot override technical feasibility or customer specifications. A workflow may end in clarification, review or decline instead of a quote.

## 15. Component 2 design acceptance checklist

- [x] One Supervisor and exactly five specialist roles are defined.
- [x] Each agent has a purpose, inputs, responsibilities, outputs, forbidden actions and escalation behavior.
- [x] Delegation and result handoffs have explicit conceptual structure.
- [x] Business dependencies and safe rework behavior are documented.
- [x] Deterministic calculations are separated from LLM coordination.
- [x] MCP/service authorization, data visibility and write boundaries are described.
- [x] Human-only, version-specific quotation release approval is mandatory.
- [x] The eight v1.1.0 evaluation outcomes are mapped without conflating them with task statuses.
- [x] The document is consistent with `business_workflow.md` and the approved synthetic baseline.

## 16. Authoritative source references and status

Implementation should later be reconciled with the approved dataset's `README.md`, `docs/business_rules.md`, `docs/multi_agent_evaluation.md`, `docs/mcp_mapping.md`, `docs/recovery_policy.md`, `contracts/`, and `config/`. When these contain a more precise field, role, or tool contract, **the verified dataset contract takes precedence** over this conceptual summary.

**Component 2 is closed as a documentation/learning milestone at the project owner's direction.** No additional written assessment is required. This document does not claim that the agent code, orchestration, MCP gateway, PostgreSQL services, or end-to-end approval controls exist yet.
