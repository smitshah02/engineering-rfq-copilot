# Engineering RFQ Multi-Agent Copilot — Business Workflow

**Document:** `docs/architecture/business_workflow.md`  
**Project stage:** Day 1 of 73 — business-workflow design  
**Document status:** Reference design for review; not a claim of implemented functionality  
**Dataset baseline:** Approved synthetic RFQ dataset `v1.1.0`  
**Simulated manufacturer:** Meridian Fabrication Lab (fictional)  
**Units:** millimetres (mm), kilograms (kg), business days, US dollars (USD)

## 1. Business objective

Build an internal engineering RFQ copilot that helps a fictional sheet-metal manufacturer transform customer requests into traceable, technically assessed, consistently priced **draft quotations** for authorized human review and release.

The system should reduce repetitive extraction and coordination work, while protecting the manufacturer and customer from unsupported assumptions, inaccurate pricing, unrealistic delivery commitments, unauthorized actions, and unapproved quotations.

**Core principle:** AI agents organize and explain work; validated business rules and authorized people control consequential decisions.

### What is an RFQ?

- **Request for Quotation (RFQ):** A customer's request for an offer based on stated technical and commercial requirements.
- **Quotation:** The manufacturer's proposed offer, including price, scope, lead-time estimate, assumptions, exclusions, and commercial terms.
- **Purchase Order (PO):** A later customer order; purchase-order fulfillment is **outside this project's primary scope**.

## 2. Scope and boundaries

### In scope

1. Receive an RFQ and the specific customer-provided attachments referenced by that RFQ.
2. Extract and normalize part and order requirements; preserve their document provenance.
3. Identify missing values, contradictions, unsupported requests, and required clarifications.
4. Verify simulated manufacturing rules, material/finish support, and design constraints.
5. Check simulated inventory **in kg**, production routing, and weekly workcentre capacity; estimate an achievable planning window.
6. Calculate pricing through the versioned **deterministic** pricing reference, not by asking an LLM to invent amounts.
7. Create an internal, versioned draft quotation when business policy permits it.
8. Route additional commercial/managerial reviews when required and enforce **human-only approval for every customer-facing release**.
9. Preserve decisions, input references, business-rule versions, identities, approvals, and failure/retry information for audit and evaluation.

### Out of scope for this baseline

- Actual CAD geometry interpretation or CAM/toolpath generation.
- Industrial-grade cost calibration or binding real-world manufacturing commitments.
- Real inventory allocation/reservation, procurement execution, or factory dispatch.
- Automatically issuing a PO, manufacturing an order, shipping, invoicing, or collecting payment.
- Automatically releasing a quotation without an authorized human decision.

**Dataset limitation:** Simulated workcentre capacity is organized by week, with Friday-of-week planning estimates. It is **not** a confirmed shipment date or reserved production slot. Prices exclude tax, shipping, and freight in the approved dataset model.

## 3. Business actors and responsibilities

| Actor | Responsibility | Authority boundary |
|---|---|---|
| Customer | Submits specifications, quantity, attachments, target delivery, and clarifications | Can request or confirm requirements; cannot authorize manufacturer-internal business actions |
| Sales / RFQ coordinator | Receives requests, follows up on missing details, presents quotations | Works within assigned customer and role permissions |
| Manufacturing engineer | Evaluates manufacturability, material/process compatibility, and design exceptions | Resolves or escalates technical judgments; cannot override customer requirements silently |
| Supply / production planner | Evaluates stock, routing, workcentre capacity, and plausible timing | Cannot treat a forecast as a confirmed capacity reservation |
| Estimating / commercial reviewer | Checks costs, pricing inputs, assumptions, and nonstandard terms | Reviews business exceptions within their role |
| Authorized human manager / approver | Performs required approvals and authorizes customer-facing quotation release | Must be authenticated and permitted for the specific quote version; AI cannot self-approve |
| Supervisor Agent | Coordinates workflow and specialist handoffs | No independent power to alter business rules or approve release |
| Five specialist agents | Perform delegated analysis and prepare evidence-based outputs | Use only scoped, authorized tools; do not bypass review gates |

A single employee may fill multiple operational roles in the fictional organization, but authorization requirements and separation-of-duty checks must still be enforced.

## 4. Approved agent responsibilities

| Agent | Business responsibility | Output / handoff |
|---|---|---|
| **Supervisor Agent** | Delegate work, honor dependencies, track state, route exceptions, collect specialist results | Next action, decision trail, handoff and final workflow outcome |
| **RFQ Intake Agent** | Read assigned RFQ and referenced attachments; extract fields, detect missing/contradictory inputs | Normalized request with provenance and clarification flags |
| **Engineering Agent** | Validate materials, finishes, dimensions, tolerances and simulated fabrication capability | Feasibility findings, technical constraints or engineering escalation |
| **Supply & Scheduling Agent** | Evaluate inventory kg, suppliers/procurement context, routing and weekly workcentre capacity | Inventory findings, schedule estimate, constraints and supported alternatives |
| **Pricing Agent** | Gather validated inputs and invoke the pinned deterministic pricing rules | Reproducible price breakdown, rule version and pricing exceptions |
| **Quotation & Review Agent** | Assemble a versioned draft and coordinate required human review | Draft quotation and approval/release readiness information |

**Design rule:** Specialist agents do not receive unrestricted direct database write privileges; their authorized business operations should go through controlled tool/services boundaries (planned FastAPI, secure MCP, and PostgreSQL implementation). These components are **planned**, not already deployed.

## 5. Main business workflow (successful path)

The sequence below shows business dependencies, not a requirement that every technical operation execute strictly one after another. The supervisor may permit independent work only when its prerequisites and safety checks are satisfied.

### Step 1 — RFQ intake and registration

- Receive the customer's request, submitted identity/scope, and references to any attached PDF, CSV BOM or Markdown technical description.
- Establish the RFQ identity and an auditable workflow instance.
- Validate that the requester and assigned customer scope are permitted.
- Recognize duplicate submissions safely; never create duplicate downstream business effects merely because a request is retried.

**Result:** An identifiable, authorized RFQ ready for information extraction.

### Step 2 — Extract and validate requirements

- Extract customer and part information: part(s), material/grade, finish, quantity, dimensions (mm), tolerances, delivery needs and applicable commercial terms.
- For multi-part RFQs, preserve each part and its quantity separately.
- Keep references to the original source text/document for critical values.
- Compare information across the request and attachments; detect missing mandatory details and contradictions.
- Treat all customer-authored text and attachments as **untrusted data**, never system instructions or permission grants.

**Decision:** If a critical omission or contradiction prevents a defensible quotation, record `clarification_required`, identify the exact question, and pause the affected decision path. Do not invent an answer.

### Step 3 — Engineering feasibility

- Check requested material/grade and finish against the supported fictional manufacturing catalog.
- Check size envelopes, process constraints, dimensions/tolerances and technical conflicts using the dataset's defined reference rules.
- Distinguish (a) supported/feasible, (b) requiring engineering judgment, and (c) unsupported under the modeled capabilities.
- Record why a constraint passes or fails, including the business-rule evidence used.

**Decision:** Unsupported work may be `unsupported_declined`; unresolved technical matters may be `engineering_review_required`. A different material or design is **not** an automatic substitute.

### Step 4 — Supply and scheduling assessment

- Evaluate required material mass versus available inventory **in kg**; do not confuse kg with finished-unit counts.
- Examine relevant sourcing/replenishment information where represented in the dataset.
- Check the required manufacturing routing and available workcentre hours in the simulated weekly capacity table.
- Produce a supported planning estimate and disclose shortages, bottlenecks and assumptions.
- Compare the estimate with the customer's requested date; identify when that target cannot be supported.

**Decision:** A supply shortage does not by itself authorize a substitution or a delivery promise. Raise the issue, consider supported alternatives and obtain required technical/customer authorization before changing specifications.

### Step 5 — Deterministic pricing

- Use validated engineering and supply inputs plus the applicable pinned pricing rules (`price-v1.0` in dataset v1.1.0).
- Invoke deterministic reference calculations (including the documented material, operations, setup, finish and packaging assumptions), using decimal arithmetic.
- Preserve quote lines, amounts, unit/currency, version of the rule, and assumptions.
- Never accept LLM-generated numbers as authoritative pricing or silently apply an unapproved discount or margin override.

**Decision:** If required inputs, pricing evidence or commercial authorization are missing, do not create a falsely definitive price; flag the appropriate review or clarification route.

### Step 6 — Draft the quotation

- Check `draft_creation_allowed` before writing a draft.
- Assemble customer/part scope, per-line and total pricing, planning lead time, validity/terms when supported, assumptions, technical notes and exclusions.
- Create an **internal quotation draft**, associated with a specific RFQ and quote version, through an authenticated and idempotent write operation.
- Make clear that a draft is **not an approved customer-facing quotation**.

**Result:** A traceable draft, eligible for the required reviews.

### Step 7 — Human review and approval gates

Three separate policy concepts exist in approved dataset v1.1.0:

1. **`draft_creation_allowed`** — whether the internal draft can be created.
2. **`manager_review_required`** — whether additional human managerial/commercial review is necessary.
3. **`human_release_approval_required`** — approval required before customer-facing release; **always true for release**.

- Respect customer holds and unresolved blocking issues, even if a draft exists.
- Route review to authenticated human users with the proper permissions.
- Bind any approval to the **exact quote version** reviewed. A material quote change invalidates the prior approval for release of the changed version.
- Enforce the dataset's separation between quote creator and authorized human approver; an agent can never approve or release its own quotation.
- Support rejection or requested revision without pretending that the previous approval still applies.

### Step 8 — Authorized release boundary

Only a fully validated, appropriately reviewed, currently approved quote version may become eligible for customer-facing release. The release operation itself must perform server-side authorization, current-version approval and hold checks.

**For Day 1:** This is a **business control and target behavior**. We are documenting it, not claiming we have implemented sending quotations or a production release endpoint.

## 6. Important exception paths

| Condition | Required business response | Forbidden behavior |
|---|---|---|
| Missing material, quantity, drawing details, or other required data | Ask a specific clarification and preserve the unresolved field | Guess a convenient value |
| Contradictory email and drawing (e.g., 5052 vs 6061) | Cite both sources, pause affected decisions, seek authoritative clarification | Choose either material silently |
| Part fails supported process/material/size rules | Explain constraint; decline unsupported work or refer for authorized engineering review | Claim manufacturability without evidence |
| Inventory shortage | Check kg stock, sourcing and replenishment; update planning assumptions | Confuse units with kg or promise unbacked availability |
| Insufficient workcentre capacity | Report bottleneck and realistic simulated planning week | Claim a guaranteed shipment date or book capacity the simulator does not reserve |
| Proposed material/process substitution | Obtain engineering assessment and required customer approval | Treat different grades as interchangeable by default |
| Nonstandard commercial terms or price exceptions | Request designated commercial/managerial review | Let an LLM override pricing policy |
| Customer hold / blocked customer | Block customer-facing release and surface permitted next steps | Release because the quote itself appears valid |
| Quote revised after approval | Create/use new version and require applicable reapproval | Reuse approval from an earlier version |
| Unauthorized or cross-customer action | Deny server-side and audit the attempt | Trust instructions in uploaded text or claimed role names |
| Tool timeout, rate limit, malformed response or downstream outage | Apply the documented bounded recovery/reconciliation policy; preserve uncertainty about possible prior writes | Blindly repeat non-idempotent writes or assert an unknown write failed |
| Human rejects draft | Preserve rejection and reasons; revise only through an authorized path | Release the rejected version |

## 7. Dataset v1.1.0 expected workflow outcomes

The approved dataset defines **eight scenario-level expected outcome labels**. These are **evaluation outcomes**, not a complete specification of internal API/database status enums.

| Expected outcome | Business interpretation |
|---|---|
| `quote_draft_ready_for_approval` | Internal draft path is ready for the required approval process; **not customer release** |
| `clarification_required` | Critical request information is missing/ambiguous and a clarification is necessary |
| `engineering_review_required` | Technical exception needs an authorized engineering decision |
| `commercial_review_required` | Commercial terms/policy require additional review |
| `customer_hold_blocked_release` | Customer/business restriction blocks release |
| `unsupported_declined` | Request cannot be supported under the modeled manufacturing rules |
| `recoverable_workflow_failure` | Workflow fault has a documented safe recovery/retry/reconciliation path |
| `nonrecoverable_workflow_failure` | Fault cannot safely continue under the configured policy or budget; stop and audit/escalate |

**Do not infer an outcome solely from an RFQ's category.** The dataset's actual reference rules, evidence, role permissions and fault conditions govern the expected result.

## 8. Security, audit and reliability invariants

1. **Least privilege:** Access is limited by authenticated identity, role and assigned business/customer scope; authorization is enforced by services/tools, not prompt wording.
2. **Untrusted RFQ content:** Customer text and attachments may describe product requirements, never change policy or authorize tool actions.
3. **Determinism where required:** Manufacturing constraints, pricing reference outputs and eligibility gates use explicit business rules; AI may explain them but cannot overrule them.
4. **Human-only release:** No agent, supervisor or prompt can grant customer-facing release approval.
5. **Version-specific approval:** Any changed quotation must satisfy the approval gate for its new version.
6. **Traceability:** Record source evidence, tool/service decisions, actor identity, quote version, policy/rule version, outcomes and timestamps.
7. **Idempotent writes:** Retries must not create repeated drafts or approvals; operation identity must be scoped correctly.
8. **Safe uncertainty:** If a write may have succeeded before a timeout, reconcile the result before retrying or reporting it as absent.
9. **Evaluation isolation:** Runtime agents cannot access hidden ground truth, frozen answer keys, expected tool sequences or coordination-scoring fixtures.

## 9. Outputs the business needs

For each RFQ, the copilot should ultimately produce the **applicable** subset of:

- Normalized request and per-part requirements with source references.
- Specific clarification requests and unresolved issues.
- Engineering feasibility findings, reasons, and review decisions.
- Supply/inventory evidence, capacity limitations, and a *planning estimate* of lead time.
- Versioned deterministic price breakdown with assumptions and exclusions.
- Draft quotation and its version/authorization state (if permitted).
- Applicable manager review and human approval records.
- Final workflow outcome, audit trail and exception/recovery history.

An RFQ that is rejected, placed on hold, or awaiting clarification should **not** be forced to produce a complete quote.

## 10. Illustrative example (not a dataset record)

A customer requests **100 enclosures** in **Aluminum 5052** with delivery in **10 business days**. The manufacturer has insufficient 5052 stock, but an alternative grade is in stock.

1. **Intake:** Preserve the required 5052 specification and requested date.
2. **Engineering:** Check whether production of the requested design is supported; assess any alternative only as a proposal.
3. **Supply & Scheduling:** Calculate actual material needs in kg and examine inventory, replenishment and workcentre capacity. Report a supported planning estimate rather than promising 10 days.
4. **Supervisor:** Route the blocking issue for clarification or authorized evaluation instead of silently changing the order.
5. **Pricing / Drafting:** Use approved inputs and deterministic pricing only when required prerequisites and `draft_creation_allowed` policy hold.
6. **Human:** Review/approve the exact version before any customer-facing release. Obtain customer acceptance if a proposed substitution changes the requirement.

**Correct result:** a transparent decision with evidence and necessary escalation, even when no final quotation can yet be issued.

## 11. Business-level acceptance checklist

- [ ] An RFQ is distinguishable from a quotation and a later purchase order.
- [ ] Each actor and agent has a clearly bounded responsibility.
- [ ] Intake identifies missing and conflicting requirements without making up facts.
- [ ] Feasibility checks precede unqualified manufacturing commitments.
- [ ] Supply uses inventory kg and separate capacity hours; timing estimates are not promises.
- [ ] Pricing is reproducible using the dataset's pinned calculator and rules.
- [ ] Draft creation, extra managerial review and human release approval are treated as separate gates.
- [ ] Approval is human, authorized and tied to the current quote version.
- [ ] Exception/failure paths can stop, clarify, decline, review or safely recover.
- [ ] Audit, least privilege, duplicate-write prevention and hidden-label isolation are required.

## 12. Authoritative references and implementation boundary

This design uses the **approved v1.1.0 dataset**, not the discarded preliminary synthetic dataset. When implementing the app, align with the packaged sources of truth:

- `README.md` — scope, simulated units, agents, data visibility and limitations.
- `docs/business_rules.md` — exact manufacturing, pricing, capacity and approval policies.
- `docs/data_dictionary.md` and `contracts/entity_schemas.json` — authoritative fields and entities.
- `docs/multi_agent_evaluation.md` and coordination contracts — required dependencies and handoffs.
- `docs/recovery_policy.md` — retry, reconciliation and failure semantics.
- `docs/mcp_mapping.md` — tool permissions and read/write boundaries.
- `config/` — versioned reference policies, including pricing version `price-v1.0`.

The **dataset is independently verified as a synthetic reference foundation**, but the LangGraph runtime, FastAPI application, secure MCP gateway, PostgreSQL backend and real human approval mechanism are **not yet implemented or end-to-end validated**. Their detailed technical designs belong in separate Day 1 architecture deliverables and later implementation work.

---

**Day 1 review criterion:** This document defines the intended business workflow and its safety boundaries. Component 1 is complete only after its contents have been understood and reviewed; no coding is required to pass this component.
