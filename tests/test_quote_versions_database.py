"""Day 52 quotation versioning against the guarded, seeded PostgreSQL test database.

Every test runs in the seeded fixture transaction, which is rolled back; the service only
flushes inside its SAVEPOINT. Version 1 comes from the Day 51 draft service. A revision
needs a changed evaluation context, here the customer's requested delivery date (RFQ-0041's
labelled date keeps it draftable). Real cross-transaction races are in
``test_quote_versions_concurrency.py``. The development database is never touched. States
the application cannot reach yet, and history K2 refuses to change, are staged with
``history_corruption.corrupted`` (owner, guarded test database, rolled back).
"""

from datetime import date

import pytest
from history_corruption import corrupted
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from test_database_queries import customer_id, other_customer
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day10_capacity import MONDAY
from test_day21_rbac_database import bound, grants, one_active_user_per_role, release_one
from test_quote_drafts_database import EXPECTED, RFQ_ID, args, count, drafters, refused, setup

import rfq_copilot.quote_drafts as quote_drafts
import rfq_copilot.quote_versions as quote_versions
from rfq_copilot.api.auth import Principal
from rfq_copilot.integrity import audit_integrity
from rfq_copilot.mcp.runtime import planning_rules
from rfq_copilot.models import (
    ApprovalRequest,
    AuditEvent,
    BusinessRuleVersion,
    IdempotencyRecord,
    Quote,
    QuoteLineItem,
    QuoteVersion,
    User,
)
from rfq_copilot.quote_drafts import create_quote_draft
from rfq_copilot.quote_versions import QuoteRevision, revise_quote
from rfq_copilot.repositories import get_rule_version
from rfq_copilot.repositories.queries import RuleIntegrityError
from rfq_copilot.seeding.loader import execute_seed
from rfq_copilot.seeding.rules import register_rule_version
from rfq_copilot.services.evaluation import evaluate_stored_rfq

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

DUE = date(2027, 1, 21)  # RFQ-0041's labelled delivery date
LATER = date(2027, 6, 1)
VERSIONS, LINES = QuoteVersion.__table__, QuoteLineItem.__table__


def rargs(**changes):
    return {"expected_version": 1, "idempotency_key": "rev-1", "correlation_id": "corr-rev-1",
            "horizon_start": MONDAY, "requested_delivery_date": DUE, **changes}  # fmt: skip


async def drafted(session, plan):
    principal = await setup(session, plan)
    draft = await create_quote_draft(session, principal, RFQ_ID, **args())
    return principal, draft.quote_id


async def rows(session, table, quote_id, version=None):
    """Fresh column values (Core, never the identity map), in a stable order."""
    statement = select(table).where(table.c.quote_id == quote_id)
    if version is not None:
        column = table.c.version if table is VERSIONS else table.c.quote_version
        statement = statement.where(column == version)
    order = [table.c.version] if table is VERSIONS else [table.c.quote_version, table.c.line_number]
    return [dict(r) for r in (await session.execute(statement.order_by(*order))).mappings()]


async def quote_row(session, quote_id):
    return dict((await session.execute(select(Quote.__table__).where(Quote.quote_id == quote_id))).mappings().one())  # fmt: skip


async def unchanged_since(session, quote_id, before):
    """Nothing of a refused or failed revision is visible."""
    assert await quote_row(session, quote_id) == before["quote"]
    assert await rows(session, VERSIONS, quote_id) == before["versions"]
    assert await rows(session, LINES, quote_id) == before["lines"]
    assert await count(session, IdempotencyRecord, IdempotencyRecord.operation == "edit_quote_draft") == 0  # fmt: skip
    assert await count(session, AuditEvent, AuditEvent.operation == "quote_version.create") == 0


async def state(session, quote_id):
    audits = select(AuditEvent.__table__).where(AuditEvent.quote_id == quote_id).order_by(AuditEvent.quote_version)  # fmt: skip
    return {
        "quote": await quote_row(session, quote_id),
        "versions": await rows(session, VERSIONS, quote_id),
        "lines": await rows(session, LINES, quote_id),
        "audits": [dict(a) for a in (await session.execute(audits)).mappings()],
    }


# --- Version creation and immutability -------------------------------------------------------


async def test_revision_creates_version_2_and_preserves_version_1(populated, seed_plan):
    principal, quote_id = await drafted(populated, seed_plan)
    before = await state(populated, quote_id)
    v1_audit = (await populated.execute(select(AuditEvent.__table__).where(AuditEvent.quote_id == quote_id))).mappings().all()  # fmt: skip

    revised = await revise_quote(populated, principal, quote_id, **rargs())

    approval = await get_rule_version(populated, "approval", "approval-v1.2")
    independent = await evaluate_stored_rfq(
        populated, RFQ_ID, customer_id=principal.customer_id, **await planning_rules(populated),
        approval_rules=approval, horizon_start=MONDAY, requested_delivery_date=DUE,
    )  # fmt: skip
    assert isinstance(revised, QuoteRevision) and not revised.replayed
    assert (revised.quote_id, revised.version, revised.previous_version) == (quote_id, 2, 1)
    assert revised.status == "revised_draft"
    assert (revised.human_approval_required, revised.approved, revised.released) == (True, False, False)  # fmt: skip
    assert revised.evaluation_fingerprint == independent.evaluation_fingerprint
    assert (revised.calculation_id, str(revised.total_usd)) == (EXPECTED["calculation_id"], EXPECTED["quote_total_usd"])  # fmt: skip

    quote = await quote_row(populated, quote_id)
    assert (quote["current_version"], quote["status"], quote["row_version"]) == (2, "revised_draft", 2)  # fmt: skip
    assert (quote["approved_version"], quote["approver_id"], quote["released_at"]) == (None, None, None)  # fmt: skip
    assert await count(populated, Quote, Quote.rfq_id == RFQ_ID) == 1  # same parent quotation

    v1, v2 = await rows(populated, VERSIONS, quote_id)
    assert {**v1, "is_current": True} == before["versions"][0]  # only the current flag moved
    assert v1["is_current"] is False and v2["is_current"] is True
    assert (v2["version"], v2["quote_version_id"], v2["created_by"]) == (2, f"{quote_id}-V2", principal.user_id)  # fmt: skip
    assert (v2["approval_valid"], v2["snapshot_completeness"]) == (False, "complete")
    assert v2["created_at"] >= v1["created_at"]
    # K3: same pricing content, so the RFQ's calculation record is reused; the version's own
    # evidence and validity window come from its own creation (same transaction here).
    assert v2["calculation_record_id"] == v1["calculation_record_id"] is not None
    assert v2["expiry_date"] == revised.expiry_date
    assert (v2["requested_delivery_date"], v1["requested_delivery_date"]) == (DUE, None)
    assert v2["evaluation_fingerprint"] == revised.evaluation_fingerprint != v1["evaluation_fingerprint"]  # fmt: skip
    rules = ("manufacturing_rule_version", "pricing_rule_version", "approval_rule_version", "workflow_rule_version")  # fmt: skip
    assert [v2[r] for r in rules] == [v1[r] for r in rules]

    lines = await rows(populated, LINES, quote_id)
    assert [line for line in lines if line["quote_version"] == 1] == before["lines"]  # untouched
    v2_lines = [line for line in lines if line["quote_version"] == 2]
    assert [line["line_number"] for line in v2_lines] == [10, 20]
    assert sum(line["line_total_usd"] for line in v2_lines) == v2["total_usd"]
    assert all(line["quote_line_id"].startswith(f"{quote_id}-V2-") for line in v2_lines)

    audits = (await populated.execute(select(AuditEvent.__table__).where(AuditEvent.quote_id == quote_id).order_by(AuditEvent.quote_version))).mappings().all()  # fmt: skip
    assert [dict(a) for a in audits[:1]] == [dict(a) for a in v1_audit]  # v1's audit intact
    revision_audit = audits[1]
    assert (revision_audit["operation"], revision_audit["quote_version"], revision_audit["actor_id"]) == ("quote_version.create", 2, principal.user_id)  # fmt: skip
    data = revision_audit["event_data"]
    assert (data["previous_version"], data["evaluation_fingerprint"]) == (1, revised.evaluation_fingerprint)  # fmt: skip
    assert data["requested_delivery_date"] == DUE.isoformat()
    receipt = await populated.scalar(select(IdempotencyRecord).where(IdempotencyRecord.operation == "edit_quote_draft"))  # fmt: skip
    assert receipt.result_json == revised.to_json()
    await populated.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))  # every pointer resolves


@pytest.mark.parametrize("workflow", ["workflow-v1.1", "workflow-v1.2"])
async def test_repeated_revisions_v1_to_v4_preserve_history(populated, seed_plan, monkeypatch, workflow):  # fmt: skip
    monkeypatch.setattr(quote_drafts, "WORKFLOW_RULE_VERSION", workflow)  # v1.1: legacy partial
    principal, quote_id = await drafted(populated, seed_plan)
    monkeypatch.undo()
    history = [await state(populated, quote_id)]
    # v4 returns to v1's context: "unchanged" compares with the current version only.
    for number, requested in ((2, DUE), (3, LATER), (4, None)):
        revised = await revise_quote(populated, principal, quote_id, **rargs(
            expected_version=number - 1, idempotency_key=f"rev-{number}",
            correlation_id=f"corr-rev-{number}", requested_delivery_date=requested))  # fmt: skip
        assert (revised.version, revised.previous_version, revised.status) == (number, number - 1, "revised_draft")  # fmt: skip
        assert revised.rule_versions == ("mfg-v1.0", "price-v1.0", "approval-v1.2", workflow)  # fmt: skip
        assert (revised.human_approval_required, revised.approved, revised.released) == (True, False, False)  # fmt: skip
        before, after = history[-1], await state(populated, quote_id)
        history.append(after)
        quote = after["quote"]
        assert (quote["current_version"], quote["status"], quote["row_version"]) == (number, "revised_draft", number)  # fmt: skip
        assert (quote["approved_version"], quote["approver_id"], quote["released_by"], quote["released_at"]) == (None, None, None, None)  # fmt: skip
        # Every earlier version, its lines and its audit row are exactly as they were; the
        # previous version's current flag is the only change.
        assert after["versions"][:-1] == [{**v, "is_current": False} for v in before["versions"]]
        assert [line for line in after["lines"] if line["quote_version"] < number] == before["lines"]  # fmt: skip
        assert after["audits"][:-1] == before["audits"]
        new = after["versions"][-1]
        new_lines = [line for line in after["lines"] if line["quote_version"] == number]
        assert sum(line["line_total_usd"] for line in new_lines) == new["total_usd"]

    versions, audits = history[-1]["versions"], history[-1]["audits"]
    assert [(v["version"], v["is_current"], v["approval_valid"]) for v in versions] == [
        (1, False, False), (2, False, False), (3, False, False), (4, True, False)]  # fmt: skip
    assert [v["workflow_rule_version"] for v in versions] == [workflow] * 4
    assert [a["event_data"]["rule_versions"][-1] for a in audits] == [workflow] * 4
    complete = "complete" if workflow == "workflow-v1.2" else "priced_partial"
    assert [v["snapshot_completeness"] for v in versions] == [complete] * 4  # never upgraded
    assert [(a["quote_version"], a["operation"]) for a in audits] == [
        (1, "quote_draft.create"), (2, "quote_version.create"), (3, "quote_version.create"),
        (4, "quote_version.create")]  # fmt: skip
    assert [a["event_data"].get("previous_version") for a in audits] == [None, 1, 2, 3]
    prints = [a["event_data"]["evaluation_fingerprint"] for a in audits]
    assert all(a != b for a, b in zip(prints, prints[1:], strict=False)) and prints[3] == prints[0]
    assert await count(populated, IdempotencyRecord, IdempotencyRecord.operation == "edit_quote_draft") == 3  # fmt: skip
    assert await count(populated, ApprovalRequest, ApprovalRequest.quote_id == quote_id) == 0
    # K2 (and behind it the partial unique index) refuses: history is never current again.
    savepoint = await populated.begin_nested()
    with pytest.raises(IntegrityError):
        await populated.execute(update(QuoteVersion).where(QuoteVersion.quote_id == quote_id, QuoteVersion.version == 1).values(is_current=True))  # fmt: skip
    await savepoint.rollback()


async def test_internal_costs_stay_internal(populated, seed_plan):
    principal, quote_id = await drafted(populated, seed_plan)
    revised = await revise_quote(populated, principal, quote_id, **rargs())
    receipt = await populated.scalar(select(IdempotencyRecord.result_json).where(IdempotencyRecord.operation == "edit_quote_draft"))  # fmt: skip
    audit = await populated.scalar(select(AuditEvent.event_data).where(AuditEvent.operation == "quote_version.create"))  # fmt: skip
    for payload in (revised.to_json(), receipt, audit):
        text_ = repr(payload)
        assert "cost" not in text_ and "margin" not in text_ and "labor" not in text_


# --- Refusals ---------------------------------------------------------------------------------


async def test_an_unchanged_evaluation_is_not_a_revision(populated, seed_plan):
    principal, quote_id = await drafted(populated, seed_plan)
    before = await state(populated, quote_id)
    same = rargs(requested_delivery_date=None)  # exactly version 1's context
    await refused("unchanged", revise_quote(populated, principal, quote_id, **same))
    await unchanged_since(populated, quote_id, before)


async def test_stale_expected_versions_are_refused(populated, seed_plan):
    principal, quote_id = await drafted(populated, seed_plan)
    before = await state(populated, quote_id)
    await refused("stale_version", revise_quote(populated, principal, quote_id, **rargs(expected_version=2)))  # fmt: skip
    await unchanged_since(populated, quote_id, before)
    await revise_quote(populated, principal, quote_id, **rargs())
    late = rargs(idempotency_key="rev-other", requested_delivery_date=LATER)  # still expects 1
    await refused("stale_version", revise_quote(populated, principal, quote_id, **late))
    assert len(await rows(populated, VERSIONS, quote_id)) == 2


async def test_a_reviewed_fingerprint_that_no_longer_matches_is_refused(populated, seed_plan):
    principal, quote_id = await drafted(populated, seed_plan)
    before = await state(populated, quote_id)
    stale = rargs(expected_evaluation_fingerprint="0" * 64)
    await refused("evaluation_changed", revise_quote(populated, principal, quote_id, **stale))
    contradictory = rargs(contradictions=("drawing and BOM disagree on thickness",))
    await refused("not_draftable", revise_quote(populated, principal, quote_id, **contradictory))
    await unchanged_since(populated, quote_id, before)


async def test_identity_role_and_customer_checks(populated, seed_plan):
    principal, quote_id = await drafted(populated, seed_plan)
    before = await state(populated, quote_id)
    nobody = Principal(principal.customer_id, user_id="USER-NOPE")
    await refused("permission_denied", revise_quote(populated, nobody, quote_id, **rargs()))
    for user in await one_active_user_per_role(populated):
        if "edit_quote_draft" in await grants(populated, user.role):
            continue
        await bound(populated, user, principal.customer_id)
        forged = Principal(principal.customer_id, frozenset({"edit_quote_draft"}), user_id=user.user_id)  # fmt: skip
        await refused("permission_denied", revise_quote(populated, forged, quote_id, **rargs()))
    override = Principal(other_customer(seed_plan), frozenset(), user_id=principal.user_id)
    await refused("permission_denied", revise_quote(populated, override, quote_id, **rargs()))
    other = (await drafters(populated))[1]
    theirs = other_customer(seed_plan)
    await bound(populated, other, theirs)
    intruder = Principal(theirs, frozenset({"edit_quote_draft"}), user_id=other.user_id)
    await refused("not_found", revise_quote(populated, intruder, quote_id, **rargs()))
    await refused("not_found", revise_quote(populated, principal, "QUOTE-NOPE", **rargs()))
    await populated.execute(update(User).where(User.user_id == principal.user_id).values(active=False))  # fmt: skip
    await refused("permission_denied", revise_quote(populated, principal, quote_id, **rargs()))
    await unchanged_since(populated, quote_id, before)


@pytest.mark.parametrize(
    "tamper",
    [{"status": "pending_approval"}, {"status": "approved"}, {"status": "rejected"},
     {"status": "clarification_required"}],
)  # fmt: skip
async def test_states_outside_draft_are_never_revised(populated, seed_plan, tamper):
    principal, quote_id = await drafted(populated, seed_plan)
    savepoint = await populated.begin_nested()
    try:
        async with corrupted(populated, "quotes"):  # no approved transition exists yet (K2)
            await populated.execute(update(Quote).where(Quote.quote_id == quote_id).values(**tamper))  # fmt: skip
    except IntegrityError:  # 'approved' needs an approver (rule_04): the schema refuses it
        await savepoint.rollback()
        return
    await savepoint.commit()
    before = await state(populated, quote_id)
    # A staged 'rejected' status has no rejected request: not a genuine rejection, so the
    # service refuses it (K5 would too). The genuine path: test_day55_quote_lifecycle_database.
    await refused("revision_not_allowed", revise_quote(populated, principal, quote_id, **rargs()))
    await unchanged_since(populated, quote_id, before)


async def test_forged_approval_on_the_current_version_blocks_revision(populated, seed_plan):
    principal, quote_id = await drafted(populated, seed_plan)
    async with corrupted(populated, "quote_versions"):  # a forgery K2 refuses
        await populated.execute(update(QuoteVersion).where(QuoteVersion.quote_id == quote_id).values(approval_valid=True))  # fmt: skip
    before = await state(populated, quote_id)
    await refused("revision_not_allowed", revise_quote(populated, principal, quote_id, **rargs()))
    await unchanged_since(populated, quote_id, before)


async def test_a_released_quotation_is_never_revised(populated, seed_plan):
    await setup(populated, seed_plan)
    quote_id, owner = await release_one(populated)
    actor = next(u for u in await drafters(populated))
    if owner != customer_id(seed_plan):
        actor = (await drafters(populated))[1]
        await bound(populated, actor, owner)
    principal = Principal(owner, frozenset(), user_id=actor.user_id)
    current = (await quote_row(populated, quote_id))["current_version"]
    await refused("revision_not_allowed", revise_quote(populated, principal, quote_id, **rargs(expected_version=current)))  # fmt: skip
    assert (await quote_row(populated, quote_id))["status"] == "released"


async def test_changed_rules_and_missing_provenance_fail_closed(populated, seed_plan):
    principal, quote_id = await drafted(populated, seed_plan)
    async with corrupted(populated, "quote_versions"):
        await populated.execute(update(QuoteVersion).where(QuoteVersion.quote_id == quote_id).values(approval_rule_version="approval-v1.1"))  # fmt: skip
    await refused("rule_version_changed", revise_quote(populated, principal, quote_id, **rargs()))
    async with corrupted(populated, "quote_versions", "audit_events"):
        await populated.execute(update(QuoteVersion).where(QuoteVersion.quote_id == quote_id).values(approval_rule_version="approval-v1.2"))  # fmt: skip
        await populated.execute(delete(AuditEvent).where(AuditEvent.quote_id == quote_id))
    await refused("provenance_missing", revise_quote(populated, principal, quote_id, **rargs()))
    assert len(await rows(populated, VERSIONS, quote_id)) == 1


# --- Workflow rule provenance and compatibility ----------------------------------------------


async def test_a_workflow_v1_0_quotation_keeps_its_rule_and_is_revised_only_once(
    populated, seed_plan, monkeypatch
):
    monkeypatch.setattr(quote_drafts, "WORKFLOW_RULE_VERSION", "workflow-v1.0")  # pre-v1.1 draft
    principal, quote_id = await drafted(populated, seed_plan)
    monkeypatch.undo()
    # draft -> revised_draft is listed by workflow-v1.0 itself.
    revised = await revise_quote(populated, principal, quote_id, **rargs())
    assert revised.version == 2 and revised.rule_versions[-1] == "workflow-v1.0"
    v1, v2 = await rows(populated, VERSIONS, quote_id)
    assert (v1["workflow_rule_version"], v2["workflow_rule_version"]) == ("workflow-v1.0", "workflow-v1.0")  # fmt: skip
    # revised_draft -> revised_draft is not: refused, nothing written, nothing upgraded.
    before = await state(populated, quote_id)
    again = rargs(expected_version=2, idempotency_key="rev-2", requested_delivery_date=LATER)
    await refused("revision_not_allowed", revise_quote(populated, principal, quote_id, **again))
    assert await state(populated, quote_id) == before
    assert await count(populated, IdempotencyRecord, IdempotencyRecord.operation == "edit_quote_draft") == 1  # fmt: skip


async def test_missing_or_untrusted_workflow_rules_fail_closed(populated, seed_plan, monkeypatch):
    monkeypatch.setattr(quote_drafts, "WORKFLOW_RULE_VERSION", "workflow-v1.1")  # v1 records v1.1
    principal, quote_id = await drafted(populated, seed_plan)
    monkeypatch.undo()
    before = await state(populated, quote_id)
    this = QuoteVersion.quote_id == quote_id
    v10 = await populated.scalar(select(BusinessRuleVersion.document).where(BusinessRuleVersion.rule_version == "workflow-v1.0"))  # fmt: skip
    await populated.execute(insert(BusinessRuleVersion).values(
        rule_version="workflow-v9.9", rule_kind="workflow", sha256="e" * 64,
        document={**v10, "version": "workflow-v9.9"}, source_path="test://unregistered"))  # fmt: skip
    # A stored but unapproved workflow row, and another kind's approved rule.
    for recorded in ("workflow-v9.9", "approval-v1.2"):
        async with corrupted(populated, "quote_versions"):
            await populated.execute(update(QuoteVersion).where(this).values(workflow_rule_version=recorded))  # fmt: skip
        with pytest.raises(RuleIntegrityError, match="not an approved workflow rule"):
            await revise_quote(populated, principal, quote_id, **rargs())
    # No recorded rule at all.
    async with corrupted(populated, "quote_versions"):
        await populated.execute(update(QuoteVersion).where(this).values(workflow_rule_version=None))  # fmt: skip
    await refused("provenance_missing", revise_quote(populated, principal, quote_id, **rargs()))
    # The approved rule, with its stored content changed under the same sha256.
    async with corrupted(populated, "quote_versions"):
        await populated.execute(update(QuoteVersion).where(this).values(workflow_rule_version="workflow-v1.1"))  # fmt: skip
    v11 = BusinessRuleVersion.rule_version == "workflow-v1.1"
    document = await populated.scalar(select(BusinessRuleVersion.document).where(v11))
    widened = {**document["transitions"], "approved": ["revised_draft"], "released": ["revised_draft"]}  # fmt: skip
    async with corrupted(populated, "business_rule_versions"):  # K3 refuses this tamper
        await populated.execute(update(BusinessRuleVersion).where(v11).values(document={**document, "transitions": widened}))  # fmt: skip
    with pytest.raises(RuleIntegrityError, match="differs from its approved version"):
        await revise_quote(populated, principal, quote_id, **rargs())
    assert await rows(populated, VERSIONS, quote_id) == before["versions"]  # v1 recorded v1.1
    assert await rows(populated, LINES, quote_id) == before["lines"]
    assert await count(populated, IdempotencyRecord, IdempotencyRecord.operation == "edit_quote_draft") == 0  # fmt: skip
    assert await count(populated, AuditEvent, AuditEvent.operation == "quote_version.create") == 0


async def test_drafts_fail_closed_until_workflow_v1_2_is_registered(populated, seed_plan):
    principal = await setup(populated, seed_plan)
    # A database where v1.2 is absent (owner-staged: K3 refuses deleting a rule row).
    async with corrupted(populated, "business_rule_versions"):
        await populated.execute(delete(BusinessRuleVersion).where(BusinessRuleVersion.rule_version == "workflow-v1.2"))  # fmt: skip
    with pytest.raises(RuleIntegrityError, match="not seeded"):
        await create_quote_draft(populated, principal, RFQ_ID, **args())
    assert await count(populated, Quote, Quote.rfq_id == RFQ_ID) == 0


async def test_workflow_v1_1_and_v1_2_register_once_and_baseline_checks_accept_them(
    populated, seed_database, seed_plan
):
    assert await get_rule_version(populated, "workflow", "workflow-v1.1") is None
    assert await register_rule_version(populated, "workflow-v1.1") == "registered"
    assert await register_rule_version(populated, "workflow-v1.1") == "already present"
    rule = await get_rule_version(populated, "workflow", "workflow-v1.1")
    assert "revised_draft" in rule.document["transitions"]["revised_draft"]
    baseline = await get_rule_version(populated, "workflow", "workflow-v1.0")  # untouched
    assert "revised_draft" not in baseline.document["transitions"]["revised_draft"]
    assert await get_rule_version(populated, "workflow", "workflow-v1.2") is None
    assert await register_rule_version(populated, "workflow-v1.2") == "registered"
    assert await register_rule_version(populated, "workflow-v1.2") == "already present"
    v12 = await get_rule_version(populated, "workflow", "workflow-v1.2")
    assert v12.document["quote_validity"]["calendar_dates_inclusive"] == 30
    assert v12.document["transitions"] == rule.document["transitions"]  # validity only
    assert "quote_validity" not in rule.document  # v1.1 is never changed
    assert (await execute_seed(populated, seed_database.config, seed_plan, "verify"))["status"] == "PASS"  # fmt: skip
    assert (await audit_integrity(populated, seed_database.config, seed_plan))["status"] == "PASS"


# --- Idempotency ------------------------------------------------------------------------------


async def test_retry_replays_and_reused_keys_conflict(populated, seed_plan):
    principal, quote_id = await drafted(populated, seed_plan)
    first = await revise_quote(populated, principal, quote_id, **rargs())
    again = await revise_quote(populated, principal, quote_id, **rargs())
    assert again.replayed and again == QuoteRevision(**{**vars(first), "replayed": True})
    other = rargs(requested_delivery_date=LATER)
    await refused("idempotency_key_conflict", revise_quote(populated, principal, quote_id, **other))  # fmt: skip
    assert len(await rows(populated, VERSIONS, quote_id)) == 2
    assert await count(populated, IdempotencyRecord, IdempotencyRecord.operation == "edit_quote_draft") == 1  # fmt: skip
    assert await count(populated, AuditEvent, AuditEvent.operation == "quote_version.create") == 1
    # A Day 51 key never replays as a revision: operations are separate receipt scopes.
    assert await count(populated, IdempotencyRecord, IdempotencyRecord.operation == "create_quote_draft") == 1  # fmt: skip


async def test_stale_check_a_revision_that_won_meanwhile_is_not_overwritten(populated, seed_plan, monkeypatch):  # fmt: skip
    """Single-session stale check: another revision lands between this one's checks and
    its inserts; the version-number constraint refuses the duplicate."""
    principal, quote_id = await drafted(populated, seed_plan)
    real = quote_drafts.evaluate_stored_rfq

    async def winner_first(*a, **kw):
        monkeypatch.setattr(quote_drafts, "evaluate_stored_rfq", real)
        await revise_quote(populated, principal, quote_id, **rargs(idempotency_key="winner", requested_delivery_date=LATER))  # fmt: skip
        populated.expunge_all()  # another transaction would not share this identity map
        return await real(*a, **kw)

    monkeypatch.setattr(quote_drafts, "evaluate_stored_rfq", winner_first)
    await refused("stale_version", revise_quote(populated, principal, quote_id, **rargs()))
    versions = await rows(populated, VERSIONS, quote_id)
    assert [(v["version"], v["is_current"]) for v in versions] == [(1, False), (2, True)]
    assert await count(populated, IdempotencyRecord, IdempotencyRecord.operation == "edit_quote_draft") == 1  # fmt: skip


# --- Transactions -----------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["check", "foreign_key", "driver"])
async def test_a_failure_inside_the_write_leaves_no_partial_revision(populated, seed_plan, monkeypatch, failure):  # fmt: skip
    principal, quote_id = await drafted(populated, seed_plan)
    before = await state(populated, quote_id)
    real_add = quote_versions.add_receipt

    def broken(session, **kwargs):
        if failure == "check":
            return real_add(session, **{**kwargs, "request_sha256": "not-a-sha"})
        if failure == "foreign_key":
            return real_add(session, **{**kwargs, "actor_id": "USER-NOPE"})
        real_add(session, **kwargs)
        raise ConnectionResetError("database went away")

    monkeypatch.setattr(quote_versions, "add_receipt", broken)
    expected = ConnectionResetError if failure == "driver" else IntegrityError
    with pytest.raises(expected):  # never disguised as a replay or a stale version
        await revise_quote(populated, principal, quote_id, **rargs())
    monkeypatch.undo()
    await unchanged_since(populated, quote_id, before)
    retried = await revise_quote(populated, principal, quote_id, **rargs())  # still usable
    assert retried.version == 2 and not retried.replayed
