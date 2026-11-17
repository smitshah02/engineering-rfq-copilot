"""Day 53 approval-policy assessment against the guarded, seeded PostgreSQL test database.

Every test runs in the seeded fixture transaction, which is rolled back; the assessment
itself never writes. Version 1 comes from the Day 51 draft service (RFQ-0041's lines:
total 998.98, no borderline bend under approval-v1.2), with the customer's account facts set
explicitly before drafting. Direct SQL changes are followed by ``expunge_all`` because
another transaction would not share this session's identity map. K2 triggers refuse every
change to recorded history, so corrupted history (pre-K2 rows, owner or superuser
tampering) is staged with ``history_corruption.corrupted``; that K2 refuses the same
changes is asserted in ``test_history_immutability_database.py``. Revision races are in
``test_quote_approval_policy_concurrency.py``. The development database is never touched.
"""

from decimal import Decimal

import pytest
from history_corruption import corrupted
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_database_queries import customer_id, other_customer
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day21_rbac_database import bound, grants, one_active_user_per_role, release_one
from test_quote_drafts_database import RFQ_ID, args, drafters, refused, setup
from test_quote_versions_database import LINES, VERSIONS, rargs, rows, state

import rfq_copilot.quote_drafts as quote_drafts
from rfq_copilot.api.auth import Principal
from rfq_copilot.models import (
    ApprovalHistory,
    ApprovalRequest,
    AuditEvent,
    BusinessRuleVersion,
    Customer,
    IdempotencyRecord,
    Quote,
    QuoteLineItem,
    QuoteVersion,
    User,
)
from rfq_copilot.quote_approval_policy import ReviewRequirement, assess_quote_approval
from rfq_copilot.quote_drafts import create_quote_draft
from rfq_copilot.quote_versions import revise_quote
from rfq_copilot.repositories import get_rule_version
from rfq_copilot.repositories.queries import RuleIntegrityError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

D = Decimal
PINS = ("mfg-v1.0", "price-v1.0", "approval-v1.2", "workflow-v1.2")
APPROVAL_ONLY = (ReviewRequirement("human_approval", "approve_quote", ("sales_manager",), ("HUMAN_APPROVAL_REQUIRED",)),)  # fmt: skip
# K3: a complete, unexpired, consistent version still is not approval-ready: the evaluation
# context is not authenticated (54C). A legacy (partial) version keeps every K3 blocker.
PROVENANCE = ("EVALUATION_CONTEXT_NOT_AUTHENTICATED",)
LEGACY_PROVENANCE = ("SNAPSHOT_NOT_COMPLETE", "NO_PERSISTED_CALCULATION", "NO_QUOTE_VALIDITY",
                     "FINGERPRINT_ONLY_IN_MUTABLE_AUDIT", "EVALUATION_CONTEXT_NOT_AUTHENTICATED")  # fmt: skip


async def drafted(session, plan, **facts):
    """A drafter, the runtime RFQ and version 1, drafted under explicit customer facts."""
    principal = await setup(session, plan)
    values = {"account_status": "active", "credit_limit_usd": D("25000"), "credit_exposure_usd": D("0"), **facts}  # fmt: skip
    await session.execute(update(Customer).where(Customer.customer_id == principal.customer_id).values(**values))  # fmt: skip
    session.expunge_all()
    draft = await create_quote_draft(session, principal, RFQ_ID, **args())
    return principal, draft


async def assess(session, principal, quote_id, version=1):
    return await assess_quote_approval(session, principal, quote_id, expected_version=version)


async def side_effects(session):
    """Every row an approval decision, request, receipt, audit or version would add."""
    counted = (ApprovalRequest, ApprovalHistory, AuditEvent, IdempotencyRecord, QuoteVersion, QuoteLineItem)  # fmt: skip
    return [await session.scalar(select(func.count()).select_from(m)) for m in counted]


# --- A sound assessment, and nothing else ---------------------------------------------------


async def test_a_fresh_draft_is_assessed_and_nothing_is_written(populated, seed_plan, monkeypatch):
    principal, draft = await drafted(populated, seed_plan)
    before, effects = await state(populated, draft.quote_id), await side_effects(populated)

    async def no_commit(self):
        raise AssertionError("the assessment must never commit")

    monkeypatch.setattr(AsyncSession, "commit", no_commit)
    result = await assess(populated, principal, draft.quote_id)
    again = await assess(populated, principal, draft.quote_id)
    monkeypatch.undo()

    assert (result.quote_id, result.rfq_id, result.customer_id) == (draft.quote_id, RFQ_ID, principal.customer_id)  # fmt: skip
    assert (result.assessed_version, result.quote_row_version, result.quote_status) == (1, 1, "draft")  # fmt: skip
    assert result.rule_versions == PINS
    approval = await get_rule_version(populated, "approval", "approval-v1.2")
    assert result.approval_rule_fingerprint == approval.fingerprint
    assert result.evaluation_fingerprint == result.recorded_evaluation_fingerprint == draft.evaluation_fingerprint  # fmt: skip
    assert result.calculation_id == draft.calculation_id
    assert result.integrity_findings == ()
    assert (result.policy.outcome, result.policy.escalation_reason) == ("quote_draft_ready_for_approval", None)  # fmt: skip
    assert result.policy.reviews == APPROVAL_ONLY
    assert result.policy.blockers == result.policy.unresolved == result.policy.commercial_conditions == ()  # fmt: skip
    assert (result.classification, result.human_review) == ("policy_conditions_met", "eligible")
    # A sound policy assessment is still not approval-ready evidence (K2/K3, L04).
    assert result.binding_approval_blockers == PROVENANCE
    assert (result.ready_for_binding_approval, result.approval_decision, result.released) == (False, "none", False)  # fmt: skip
    assert again == result  # deterministic, including assessment_fingerprint

    assert await state(populated, draft.quote_id) == before  # unapproved and unchanged
    assert await side_effects(populated) == effects
    assert not (populated.new or populated.dirty or populated.deleted)
    text_ = repr(result)
    assert "cost" not in text_ and "margin" not in text_ and "labor" not in text_


async def test_a_revised_version_is_assessed_and_its_predecessor_is_history(populated, seed_plan):
    principal, draft = await drafted(populated, seed_plan)
    v1 = await assess(populated, principal, draft.quote_id)
    revised = await revise_quote(populated, principal, draft.quote_id, **rargs())
    v2 = await assess(populated, principal, draft.quote_id, 2)
    assert (v2.assessed_version, v2.quote_row_version, v2.quote_status) == (2, 2, "revised_draft")
    assert v2.evaluation_fingerprint == revised.evaluation_fingerprint != v1.evaluation_fingerprint
    assert v2.assessment_fingerprint != v1.assessment_fingerprint  # bound to what it assessed
    assert v2.integrity_findings == () and v2.classification == "policy_conditions_met"
    # An assessment of version 1 can never stand for version 2.
    await refused("stale_version", assess(populated, principal, draft.quote_id, 1))
    await refused("stale_version", assess(populated, principal, draft.quote_id, 3))


async def test_a_workflow_v1_0_quotation_is_assessed_under_its_own_rule(populated, seed_plan, monkeypatch):  # fmt: skip
    monkeypatch.setattr(quote_drafts, "WORKFLOW_RULE_VERSION", "workflow-v1.0")
    principal, draft = await drafted(populated, seed_plan)
    monkeypatch.undo()
    result = await assess(populated, principal, draft.quote_id)
    assert result.rule_versions[-1] == "workflow-v1.0"  # recorded, never upgraded
    assert result.integrity_findings == () and result.classification == "policy_conditions_met"
    assert result.binding_approval_blockers == LEGACY_PROVENANCE  # partial: no K3 evidence
    assert (await rows(populated, VERSIONS, draft.quote_id))[0]["workflow_rule_version"] == "workflow-v1.0"  # fmt: skip


# --- approval-v1.2 commercial conditions on persisted versions --------------------------------


@pytest.mark.parametrize(
    "facts,outcome,classification,manager,commercial",
    [({"account_status": "credit_restricted"}, "commercial_review_required", "reviews_required",
      ("CUSTOMER_NOT_ACTIVE",), ("CUSTOMER_ACCOUNT_RESTRICTION",)),
     ({"account_status": "pending_verification"}, "commercial_review_required", "reviews_required",
      ("CUSTOMER_NOT_ACTIVE",), ("CUSTOMER_ACCOUNT_RESTRICTION",)),
     ({"credit_exposure_usd": D("24500")}, "commercial_review_required", "reviews_required",
      ("CREDIT_LIMIT_BREACH",), ("CREDIT_LIMIT_EXCEEDED",)),
     ({"account_status": "on_hold"}, "customer_hold_blocked_release", "commercially_blocked",
      ("CUSTOMER_NOT_ACTIVE",), ("CUSTOMER_ACCOUNT_RESTRICTION",))],
)  # fmt: skip
async def test_customer_account_and_credit_conditions(populated, seed_plan, facts, outcome, classification, manager, commercial):  # fmt: skip
    principal, draft = await drafted(populated, seed_plan, **facts)
    result = await assess(populated, principal, draft.quote_id)
    assert result.integrity_findings == ()  # drafted under these facts: reproduced
    assert (result.policy.outcome, result.classification) == (outcome, classification)
    reviews = {r.kind: r for r in result.policy.reviews}
    assert reviews["manager_review"].reasons == manager
    assert reviews["manager_review"].roles == ("sales_manager",)
    assert result.policy.commercial_conditions == commercial
    blocked = classification == "commercially_blocked"
    assert result.human_review == ("not_eligible" if blocked else "eligible")
    assert result.policy.blockers == (("CUSTOMER_ACCOUNT_RESTRICTION",) if blocked else ())
    assert result.ready_for_binding_approval is False


async def test_facts_changed_since_the_version_are_reported_not_hidden(populated, seed_plan):
    principal, draft = await drafted(populated, seed_plan)
    await populated.execute(update(Customer).where(Customer.customer_id == principal.customer_id).values(account_status="credit_restricted"))  # fmt: skip
    populated.expunge_all()
    result = await assess(populated, principal, draft.quote_id)
    assert result.integrity_findings == ("EVALUATION_CHANGED",)  # prices unchanged, policy not
    assert result.evaluation_fingerprint != result.recorded_evaluation_fingerprint
    assert result.policy.outcome == "commercial_review_required"  # today's requirements
    assert (result.classification, result.human_review) == ("evaluation_changed", "not_eligible")
    assert "EVALUATION_CHANGED" in result.binding_approval_blockers


# --- Stored version content against its reproducible evaluation -------------------------------


async def forge_audit(session, quote_id, **changes):
    where = (AuditEvent.quote_id == quote_id, AuditEvent.quote_version == 1)
    data = await session.scalar(select(AuditEvent.event_data).where(*where))
    async with corrupted(session, "audit_events"):
        await session.execute(update(AuditEvent).where(*where).values(event_data={**data, **changes}))  # fmt: skip


async def tamper_version_content(session, quote_id, version, tamper):
    """Version or line content K2 refuses to change: only their two triggers are disabled."""
    async with corrupted(session, "quote_versions", "quote_line_items"):
        if tamper == "total":
            await session.execute(update(QuoteVersion).where(*version).values(total_usd=QuoteVersion.total_usd + 1))  # fmt: skip
        elif tamper == "line":
            await session.execute(update(QuoteLineItem).where(QuoteLineItem.quote_id == quote_id, QuoteLineItem.line_number == 10).values(line_total_usd=QuoteLineItem.line_total_usd + D("0.01")))  # fmt: skip
        elif tamper == "lead":
            await session.execute(update(QuoteVersion).where(*version).values(estimated_lead_business_days=QuoteVersion.estimated_lead_business_days + 1))  # fmt: skip
        elif tamper == "snapshot":
            snapshots = await session.scalar(select(QuoteVersion.source_snapshot_versions).where(*version))  # fmt: skip
            await session.execute(update(QuoteVersion).where(*version).values(source_snapshot_versions={**snapshots, "dataset_version": "9.9.9"}))  # fmt: skip
        else:  # part, breakdown
            column = QuoteLineItem.part_json if tamper == "part" else QuoteLineItem.cost_breakdown_json  # fmt: skip
            line = (QuoteLineItem.quote_id == quote_id, QuoteLineItem.line_number == 10)
            stored = await session.scalar(select(column).where(*line))
            forged = {"hole_count": 99} if tamper == "part" else {"labor_cost_per_unit_usd": "0.0001"}  # fmt: skip
            await session.execute(update(QuoteLineItem).where(*line).values({column: {**stored, **forged}}))  # fmt: skip


@pytest.mark.parametrize(
    "tamper,finding",
    [("total", "TOTAL_MISMATCH"), ("line", "LINE_MISMATCH"), ("lead", "LEAD_TIME_MISMATCH"),
     # K3: a complete version's calculation id is its own column; an audit event that
     # disagrees with it is an evidence mismatch (CALCULATION_CHANGED: test_k3_*_database).
     ("snapshot", "SNAPSHOT_MISMATCH"), ("calculation", "EVIDENCE_MISMATCH"),
     ("part", "PART_MISMATCH"), ("breakdown", "COST_BREAKDOWN_MISMATCH")],
)  # fmt: skip
async def test_tampered_version_content_is_an_integrity_mismatch(populated, seed_plan, tamper, finding):  # fmt: skip
    principal, draft = await drafted(populated, seed_plan)
    quote_id = draft.quote_id
    version = (QuoteVersion.quote_id == quote_id, QuoteVersion.version == 1)
    if tamper == "calculation":  # audit content only: its trigger alone (forge_audit)
        await forge_audit(populated, quote_id, calculation_id="CALC-FORGED")
    else:
        await tamper_version_content(populated, quote_id, version, tamper)
    result = await assess(populated, principal, quote_id)
    assert result.integrity_findings == (finding,)
    assert (result.classification, result.human_review) == ("integrity_mismatch", "not_eligible")
    assert finding in result.binding_approval_blockers
    text_ = repr(result)  # codes only: no stored or recomputed cost value leaks
    assert "0.0001" not in text_ and "cost_per_unit" not in text_ and "hole_count" not in text_


async def test_a_changed_evaluation_with_changed_content_is_an_integrity_mismatch(populated, seed_plan):  # fmt: skip
    principal, draft = await drafted(populated, seed_plan)
    await populated.execute(update(Customer).where(Customer.customer_id == principal.customer_id).values(account_status="credit_restricted"))  # fmt: skip
    async with corrupted(populated, "quote_versions"):
        await populated.execute(update(QuoteVersion).where(QuoteVersion.quote_id == draft.quote_id).values(total_usd=QuoteVersion.total_usd + 1))  # fmt: skip
    populated.expunge_all()
    result = await assess(populated, principal, draft.quote_id)
    assert result.integrity_findings == ("EVALUATION_CHANGED", "TOTAL_MISMATCH")
    assert result.classification == "integrity_mismatch"  # never a mere "evaluation_changed"
    assert result.binding_approval_blockers[-2:] == ("EVALUATION_CHANGED", "TOTAL_MISMATCH")


# --- Fresh facts: rows already in the session's identity map are re-read (no expunge) ----------


@pytest.mark.parametrize(
    "facts,manager",
    [({"account_status": "credit_restricted"}, ("CUSTOMER_NOT_ACTIVE",)),
     ({"credit_exposure_usd": D("24500")}, ("CREDIT_LIMIT_BREACH",))],
)  # fmt: skip
async def test_a_preloaded_customer_is_never_stale(populated, seed_plan, facts, manager):
    principal, draft = await drafted(populated, seed_plan)
    customer = await populated.get(Customer, principal.customer_id)  # in the identity map
    assert (customer.account_status, customer.credit_exposure_usd) == ("active", D("0"))
    await populated.execute(update(Customer).where(Customer.customer_id == principal.customer_id).values(**facts))  # fmt: skip
    result = await assess(populated, principal, draft.quote_id)  # deliberately no expunge_all
    assert result.integrity_findings == ("EVALUATION_CHANGED",)
    assert result.policy.outcome == "commercial_review_required"
    assert {r.kind: r for r in result.policy.reviews}["manager_review"].reasons == manager
    for name, value in facts.items():
        assert getattr(customer, name) == value  # the preloaded object itself was refreshed


async def test_a_preloaded_user_with_a_changed_role_or_status_is_re_read(populated, seed_plan):
    principal, draft = await drafted(populated, seed_plan)
    user = await populated.get(User, principal.user_id)  # in the identity map
    assert user.role in {"sales_representative", "estimator"}
    await populated.execute(update(User).where(User.user_id == principal.user_id).values(role="auditor"))  # fmt: skip
    # A stale role would still carry view_assigned_rfqs; the auditor role does not.
    await refused("permission_denied", assess(populated, principal, draft.quote_id))
    assert user.role == "auditor"
    await populated.execute(update(User).where(User.user_id == principal.user_id).values(role="estimator", active=False))  # fmt: skip
    await refused("permission_denied", assess(populated, principal, draft.quote_id))


# --- Malformed stored provenance is a controlled refusal, never a Python exception -------------


@pytest.mark.parametrize(
    "changes",
    [{"rule_versions": 5}, {"rule_versions": "mfg-v1.0"}, {"rule_versions": [1, 2, 3, 4]},
     {"rule_versions": None}, {"evaluation_fingerprint": 12345},
     {"evaluation_fingerprint": "é" * 64}, {"evaluation_fingerprint": "ABC"},
     {"horizon_start": 20261109}, {"horizon_start": "next monday"},
     {"requested_delivery_date": ["2027-01-21"]}, {"contradiction_count": "0"},
     {"contradiction_count": False}, {"calculation_id": 7}, {"calculation_id": None}],
)  # fmt: skip
async def test_malformed_audit_fields_are_refused(populated, seed_plan, changes):
    principal, draft = await drafted(populated, seed_plan)
    await forge_audit(populated, draft.quote_id, **changes)
    await refused("provenance_missing", assess(populated, principal, draft.quote_id))


@pytest.mark.parametrize(
    "changes",
    [{"inventory": 1}, {"inventory": "x" * 65}, {"capacity": None}, {"capacity": ["SNAP"]},
     {"dataset_version": 1.5}],
)  # fmt: skip
async def test_malformed_snapshot_pins_are_refused(populated, seed_plan, changes):
    principal, draft = await drafted(populated, seed_plan)
    version = (QuoteVersion.quote_id == draft.quote_id, QuoteVersion.version == 1)
    snapshots = await populated.scalar(select(QuoteVersion.source_snapshot_versions).where(*version))  # fmt: skip
    async with corrupted(populated, "quote_versions"):
        await populated.execute(update(QuoteVersion).where(*version).values(source_snapshot_versions={**snapshots, **changes}))  # fmt: skip
    await refused("provenance_missing", assess(populated, principal, draft.quote_id))


async def test_a_forged_recorded_fingerprint_is_never_trusted(populated, seed_plan, monkeypatch):
    # Legacy (partial) version: the audit event is its only record of the fingerprint.
    monkeypatch.setattr(quote_drafts, "WORKFLOW_RULE_VERSION", "workflow-v1.1")
    principal, legacy = await drafted(populated, seed_plan)
    monkeypatch.undo()
    await forge_audit(populated, legacy.quote_id, evaluation_fingerprint="0" * 64)
    result = await assess(populated, principal, legacy.quote_id)
    assert result.recorded_evaluation_fingerprint == "0" * 64
    assert result.evaluation_fingerprint == legacy.evaluation_fingerprint  # always recomputed
    assert result.integrity_findings == ("EVALUATION_CHANGED",)
    assert result.classification == "evaluation_changed"


async def test_a_forged_audit_fingerprint_cannot_override_complete_evidence(populated, seed_plan):
    # K3: the complete version's own (K2-protected) fingerprint is the recorded one; the
    # forged audit event disagrees with it, which is an integrity mismatch, not a change.
    principal, draft = await drafted(populated, seed_plan)
    await forge_audit(populated, draft.quote_id, evaluation_fingerprint="0" * 64)
    result = await assess(populated, principal, draft.quote_id)
    assert result.recorded_evaluation_fingerprint == draft.evaluation_fingerprint
    assert result.integrity_findings == ("EVIDENCE_MISMATCH",)
    assert (result.classification, result.human_review) == ("integrity_mismatch", "not_eligible")


# --- Provenance, rules and version binding fail closed ------------------------------------------


@pytest.mark.parametrize(
    "tamper,reason",
    [("no_audit", "provenance_missing"), ("no_horizon", "provenance_missing"),
     ("contradictions", "provenance_missing"), ("no_snapshot", "provenance_missing"),
     ("audit_rules", "version_inconsistent"),
     ("approval_rule", "rule_version_changed"), ("not_current", "version_inconsistent")],
)  # fmt: skip
async def test_missing_or_inconsistent_provenance_is_refused(populated, seed_plan, tamper, reason):
    principal, draft = await drafted(populated, seed_plan)
    quote_id = draft.quote_id
    version = (QuoteVersion.quote_id == quote_id, QuoteVersion.version == 1)
    if tamper == "no_audit":
        async with corrupted(populated, "audit_events"):
            await populated.execute(delete(AuditEvent).where(AuditEvent.quote_id == quote_id))
    elif tamper == "no_horizon":
        await forge_audit(populated, quote_id, horizon_start=None)
    elif tamper == "contradictions":
        await forge_audit(populated, quote_id, contradiction_count=1)
    elif tamper == "audit_rules":
        await forge_audit(populated, quote_id, rule_versions=["mfg-v1.0", "price-v1.0", "approval-v1.1", "workflow-v1.1"])  # fmt: skip
    else:
        changes = {
            "no_snapshot": {"source_snapshot_versions": {"dataset_version": "1.1.0"}},
            "approval_rule": {"approval_rule_version": "approval-v1.1"},
            "not_current": {"is_current": False},  # history with no current version
        }[tamper]
        async with corrupted(populated, "quote_versions"):
            await populated.execute(update(QuoteVersion).where(*version).values(**changes))
    populated.expunge_all()
    before = await state(populated, quote_id)
    await refused(reason, assess(populated, principal, quote_id))
    assert await state(populated, quote_id) == before


async def test_untrusted_rules_raise(populated, seed_plan):
    principal, draft = await drafted(populated, seed_plan)
    version = (QuoteVersion.quote_id == draft.quote_id, QuoteVersion.version == 1)
    async with corrupted(populated, "quote_versions"):
        await populated.execute(update(QuoteVersion).where(*version).values(workflow_rule_version="approval-v1.2"))  # fmt: skip
    with pytest.raises(RuleIntegrityError, match="not an approved workflow rule"):
        await assess(populated, principal, draft.quote_id)
    async with corrupted(populated, "quote_versions"):
        await populated.execute(update(QuoteVersion).where(*version).values(workflow_rule_version="workflow-v1.1"))  # fmt: skip
    # A familiar version string over changed content is not the approved policy.
    v12 = BusinessRuleVersion.rule_version == "approval-v1.2"
    document = await populated.scalar(select(BusinessRuleVersion.document).where(v12))
    async with corrupted(populated, "business_rule_versions"):  # K3 refuses this tamper
        await populated.execute(update(BusinessRuleVersion).where(v12).values(document={**document, "manager_review_threshold_usd": "1.00"}))  # fmt: skip
    with pytest.raises(RuleIntegrityError, match="differs from its approved version"):
        await assess(populated, principal, draft.quote_id)


async def test_a_complete_version_cannot_lose_its_workflow_rule(populated, seed_plan):
    # K3 rule_07: a complete version keeps every provenance column NOT NULL by CHECK, even
    # under a privileged tamper with the guard triggers disabled, so ``provenance_missing``
    # for a missing workflow rule is reachable only on legacy (partial) versions.
    principal, draft = await drafted(populated, seed_plan)
    version = (QuoteVersion.quote_id == draft.quote_id, QuoteVersion.version == 1)
    async with corrupted(populated, "quote_versions"):
        savepoint = await populated.begin_nested()
        with pytest.raises(IntegrityError, match="ck_quote_versions_rule_07"):
            await populated.execute(update(QuoteVersion).where(*version).values(workflow_rule_version=None))  # fmt: skip
        await savepoint.rollback()
    populated.expunge_all()
    before = await state(populated, draft.quote_id)
    result = await assess(populated, principal, draft.quote_id)
    assert result.integrity_findings == () and await state(populated, draft.quote_id) == before


@pytest.mark.parametrize(
    "tamper",
    [{"status": "pending_approval"}, {"status": "rejected"}, {"status": "clarification_required"},
     {"status": "blocked"}, {"status": "approved"}],
)  # fmt: skip
async def test_states_outside_draft_are_not_assessable(populated, seed_plan, tamper):
    principal, draft = await drafted(populated, seed_plan)
    savepoint = await populated.begin_nested()
    try:
        async with corrupted(populated, "quotes"):  # no approved transition exists yet (K2)
            await populated.execute(update(Quote).where(Quote.quote_id == draft.quote_id).values(**tamper))  # fmt: skip
    except IntegrityError:  # 'approved' needs an approver (rule_04): the schema refuses it
        await savepoint.rollback()
        return
    await savepoint.commit()
    populated.expunge_all()
    await refused("not_assessable", assess(populated, principal, draft.quote_id))


async def test_forged_approval_and_released_quotations_are_not_assessable(populated, seed_plan):
    principal, draft = await drafted(populated, seed_plan)
    async with corrupted(populated, "quote_versions"):  # a forgery K2 refuses
        await populated.execute(update(QuoteVersion).where(QuoteVersion.quote_id == draft.quote_id).values(approval_valid=True))  # fmt: skip
    populated.expunge_all()
    await refused("not_assessable", assess(populated, principal, draft.quote_id))
    quote_id, owner = await release_one(populated)
    actor = (await drafters(populated))[0]
    if owner != customer_id(seed_plan):
        actor = (await drafters(populated))[1]
        await bound(populated, actor, owner)
    holder = Principal(owner, frozenset(), user_id=actor.user_id)
    current = await populated.scalar(select(Quote.current_version).where(Quote.quote_id == quote_id))  # fmt: skip
    await refused("not_assessable", assess(populated, holder, quote_id, current))
    assert await populated.scalar(select(Quote.status).where(Quote.quote_id == quote_id)) == "released"  # fmt: skip


# --- Authorization -------------------------------------------------------------------------------


async def test_every_role_is_checked_against_database_grants(populated, seed_plan):
    principal, draft = await drafted(populated, seed_plan)
    for user in await one_active_user_per_role(populated):
        if user.user_id != principal.user_id:  # the drafter is already bound
            await bound(populated, user, principal.customer_id)
        populated.expunge_all()
        # Permissions on the Principal are never trusted, even when they claim everything.
        claimed = Principal(principal.customer_id, frozenset({"view_assigned_rfqs", "approve_quote"}), user_id=user.user_id)  # fmt: skip
        if "view_assigned_rfqs" in await grants(populated, user.role):
            result = await assess(populated, claimed, draft.quote_id)
            assert result.approval_decision == "none" and result.assessed_version == 1
        else:  # administrator, auditor
            await refused("permission_denied", assess(populated, claimed, draft.quote_id))


async def test_identity_customer_and_assignment_isolation(populated, seed_plan):
    principal, draft = await drafted(populated, seed_plan)
    quote_id = draft.quote_id
    await refused("permission_denied", assess(populated, Principal(principal.customer_id, user_id="USER-NOPE"), quote_id))  # fmt: skip
    override = Principal(other_customer(seed_plan), frozenset(), user_id=principal.user_id)
    await refused("permission_denied", assess(populated, override, quote_id))  # not bound there
    other = (await drafters(populated))[1]
    await bound(populated, other, other_customer(seed_plan))
    intruder = Principal(other_customer(seed_plan), frozenset(), user_id=other.user_id)
    await refused("not_found", assess(populated, intruder, quote_id))  # absent or foreign: same
    await refused("not_found", assess(populated, principal, "QUOTE-NOPE"))
    unassigned = Principal(customer_id(seed_plan), frozenset(), user_id=principal.user_id, assignment_required=True)  # fmt: skip
    await refused("not_found", assess(populated, unassigned, quote_id))  # no rfq_assignments row
    await populated.execute(update(User).where(User.user_id == principal.user_id).values(active=False))  # fmt: skip
    populated.expunge_all()
    await refused("permission_denied", assess(populated, principal, quote_id))
    assert (await rows(populated, VERSIONS, quote_id))[0]["approval_valid"] is False
    assert len(await rows(populated, LINES, quote_id)) == 2
