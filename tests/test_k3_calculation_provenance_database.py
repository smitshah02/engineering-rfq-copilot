"""K3 (integrity) calculation records, version evidence and validity on the guarded test DB.

Fixture-transaction tests (``populated``) run as the owner fixture role and are rolled
back; K3 triggers bind the owner too (only the partial-version timestamp seal exempts it,
for seeding history). States K2/K3 refuse to create are staged with
``history_corruption.corrupted``. The committed race uses the draft persistence ``world`` and its
TRUNCATE teardown. Runtime-role limits are in ``test_runtime_role_database.py``. The
development database is never opened, and workflow-v1.2 is registered only inside these
rolled-back or truncated test transactions.
"""

from datetime import timedelta

import pytest
from history_corruption import ALL_GUARD_TRIGGERS, GUARDS, corrupted, k2_refusal, trigger_states
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from test_database_queries import other_customer
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_quote_drafts_concurrency import hold_after_checks, race
from test_quote_drafts_concurrency import world as world
from test_quote_drafts_database import (
    RFQ_ID,
    args,
    count,
    drafters,
    nothing_written,
    refused,
    runtime_rfq,
    setup,
)
from test_quote_versions_database import DUE, LATER, LINES, VERSIONS, rargs, rows, state
from test_rbac_database import bound

import rfq_copilot.quote_drafts as quote_drafts
from rfq_copilot import quote_evidence
from rfq_copilot.api.auth import Principal
from rfq_copilot.models import AuditEvent, BusinessRuleVersion, PricingCalculation, QuoteVersion
from rfq_copilot.quote_approval_policy import assess_quote_approval
from rfq_copilot.quote_drafts import create_quote_draft
from rfq_copilot.quote_versions import revise_quote

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

EVIDENCE, LINES_DONE, ITEMS_DONE = (
    "k3_quote_versions_evidence",
    "k3_quote_version_lines_complete",
    "k3_quote_line_items_complete",
)
DEFER = f"SET CONSTRAINTS {quote_evidence.LINE_CONSTRAINTS} DEFERRED"
CHECK_NOW = f"SET CONSTRAINTS {quote_evidence.LINE_CONSTRAINTS} IMMEDIATE"
RECORDS = PricingCalculation.__table__


async def records(session, rfq_id=None):
    statement = select(RECORDS)
    if rfq_id is not None:
        statement = statement.where(RECORDS.c.rfq_id == rfq_id)
    return [dict(r) for r in (await session.execute(statement.order_by(RECORDS.c.rfq_id))).mappings()]  # fmt: skip


async def drafted(session, plan, workflow=None, monkeypatch=None):
    if workflow is not None:
        monkeypatch.setattr(quote_drafts, "WORKFLOW_RULE_VERSION", workflow)
    principal = await setup(session, plan)
    draft = await create_quote_draft(session, principal, RFQ_ID, **args())
    if workflow is not None:
        monkeypatch.undo()
    return principal, draft


async def refused_by(session, trigger, statement, *params):
    savepoint = await session.begin_nested()
    with pytest.raises(IntegrityError) as caught:
        await session.execute(statement, *params)
    await savepoint.rollback()
    assert k2_refusal(caught.value) == trigger


def copy_version(v, number=9, **changes):
    return insert(VERSIONS).values({**v, "quote_version_id": f"{v['quote_id']}-V{number}", "version": number, "is_current": False, **changes})  # fmt: skip


def copy_line(line, number=9, **changes):
    return insert(LINES).values({**line, "quote_line_id": f"{line['quote_line_id']}-K3-{number}", "quote_version": number, **changes})  # fmt: skip


# --- Identity: one calculation_id, separate records per RFQ and customer -----------------------


async def test_two_customers_with_identical_parts_get_separate_records(populated, seed_plan):
    principal, mine = await drafted(populated, seed_plan)
    other = (await drafters(populated))[1]
    theirs = other_customer(seed_plan)
    await bound(populated, other, theirs)
    await runtime_rfq(populated, "TEST-K3-RFQ-B", theirs)  # the same two parts
    tenant_b = Principal(theirs, frozenset(), user_id=other.user_id)
    second = await create_quote_draft(populated, tenant_b, "TEST-K3-RFQ-B", **args())
    a, b = await records(populated, RFQ_ID), await records(populated, "TEST-K3-RFQ-B")
    assert len(a) == len(b) == 1
    assert a[0]["calculation_id"] == b[0]["calculation_id"] == mine.calculation_id == second.calculation_id  # fmt: skip
    assert a[0]["content_sha256"] == b[0]["content_sha256"]  # same reusable pricing content
    assert a[0]["calculation_record_id"] != b[0]["calculation_record_id"]  # separate identity
    assert a[0]["requirement_sha256"] != b[0]["requirement_sha256"]  # RFQ and customer bound
    for draft, record in ((mine, a[0]), (second, b[0])):
        (version,) = await rows(populated, VERSIONS, draft.quote_id)
        assert (version["rfq_id"], version["calculation_record_id"]) == (draft.rfq_id, record["calculation_record_id"])  # fmt: skip


async def test_two_rfqs_of_one_customer_get_separate_records(populated, seed_plan):
    principal, first = await drafted(populated, seed_plan)
    await runtime_rfq(populated, "TEST-K3-RFQ-2", principal.customer_id)
    second = await create_quote_draft(populated, principal, "TEST-K3-RFQ-2", **args(idempotency_key="draft-2"))  # fmt: skip
    a, b = await records(populated, RFQ_ID), await records(populated, "TEST-K3-RFQ-2")
    assert a[0]["calculation_id"] == b[0]["calculation_id"] == first.calculation_id == second.calculation_id  # fmt: skip
    assert a[0]["calculation_record_id"] != b[0]["calculation_record_id"]


async def test_revisions_reuse_the_record_and_each_version_has_its_own_window(populated, seed_plan):
    principal, draft = await drafted(populated, seed_plan)
    await revise_quote(populated, principal, draft.quote_id, **rargs())
    await revise_quote(populated, principal, draft.quote_id, **rargs(expected_version=2, idempotency_key="rev-2", requested_delivery_date=LATER))  # fmt: skip
    versions = await rows(populated, VERSIONS, draft.quote_id)
    assert len({v["calculation_record_id"] for v in versions}) == 1 and len(await records(populated, RFQ_ID)) == 1  # fmt: skip
    for v in versions:  # each window from the version's own creation time (database clock)
        assert v["snapshot_completeness"] == "complete"
        assert v["expiry_date"] == quote_evidence.expiry_date(v["created_at"], 30)
    assert [v["requested_delivery_date"] for v in versions] == [None, DUE, LATER]
    assert len({v["evaluation_fingerprint"] for v in versions}) == 3


async def test_a_version_cannot_reference_another_rfqs_record(populated, seed_plan):
    principal, mine = await drafted(populated, seed_plan)
    await runtime_rfq(populated, "TEST-K3-RFQ-2", principal.customer_id)
    await create_quote_draft(populated, principal, "TEST-K3-RFQ-2", **args(idempotency_key="draft-2"))  # fmt: skip
    foreign = (await records(populated, "TEST-K3-RFQ-2"))[0]["calculation_record_id"]
    savepoint = await populated.begin_nested()
    with pytest.raises(IntegrityError) as caught:  # the FK, even with every trigger disabled
        async with corrupted(populated, "quote_versions"):
            await populated.execute(update(QuoteVersion).where(QuoteVersion.quote_id == mine.quote_id).values(calculation_record_id=foreign))  # fmt: skip
    await savepoint.rollback()
    assert caught.value.orig.sqlstate == "23503"
    assert "fk_quote_versions_calculation_record_rfq" in str(caught.value.orig)


# --- The evidence trigger ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [{"created_at_shift": timedelta(seconds=1)}, {"expiry_shift": timedelta(days=1)},
     {"expiry_shift": timedelta(days=-1)}, {"workflow_rule_version": "workflow-v1.1"},
     {"rfq_source_sha256": "0" * 64}, {"total_usd": None}, {"calculation_id": "CALC-OTHER"},
     {"expiry_date": None}],
)  # fmt: skip
async def test_inconsistent_complete_versions_are_refused(populated, seed_plan, changes):
    _, draft = await drafted(populated, seed_plan)
    (v,) = await rows(populated, VERSIONS, draft.quote_id)
    values = {k: val for k, val in changes.items() if not k.endswith("_shift")}
    if "created_at_shift" in changes:
        values["created_at"] = v["created_at"] + changes["created_at_shift"]
    if "expiry_shift" in changes:
        values["expiry_date"] = v["expiry_date"] + changes["expiry_shift"]
    if "total_usd" in values:
        values["total_usd"] = v["total_usd"] + 1
    savepoint = await populated.begin_nested()
    with pytest.raises(IntegrityError) as caught:
        await populated.execute(copy_version(v, **values))
    await savepoint.rollback()
    # The trigger, or (no expiry) rule_07, which runs after BEFORE triggers allowed it.
    assert k2_refusal(caught.value) == EVIDENCE or caught.value.orig.sqlstate == "23514"


async def test_owner_imports_of_history_keep_their_time_but_never_claim_evidence(populated, seed_plan):  # fmt: skip
    _, draft = await drafted(populated, seed_plan)
    (v,) = await rows(populated, VERSIONS, draft.quote_id)
    partial = {**dict.fromkeys(quote_evidence.EVIDENCE_COLUMNS), "snapshot_completeness": "priced_partial", "created_at": v["created_at"] - timedelta(days=400)}  # fmt: skip
    savepoint = await populated.begin_nested()
    await populated.execute(copy_version(v, **partial))  # seeding-style: owner, historical time
    await savepoint.rollback()
    savepoint = await populated.begin_nested()
    with pytest.raises(IntegrityError):  # rule_09: a partial version carries no K3 evidence
        await populated.execute(copy_version(v, **{**partial, "evaluation_fingerprint": "0" * 64, "created_at": v["created_at"]}))  # fmt: skip
    await savepoint.rollback()
    record = (await records(populated, RFQ_ID))[0]
    forged = {**record, "calculation_record_id": "CALCREC-FORGED", "content_sha256": "1" * 64, "created_at": record["created_at"] - timedelta(days=1)}  # fmt: skip
    seal = "k3_pricing_calculations_seal"  # binds every role, the owner included
    await refused_by(populated, seal, insert(RECORDS).values(forged))


# --- Deferred line completeness --------------------------------------------------------------


async def complete_copy(session, quote_id, lines):
    """Version 9 as a complete copy of version 1, with these lines; checked at CHECK_NOW."""
    (v,) = await rows(session, VERSIONS, quote_id, 1)
    await session.execute(text(DEFER))
    await session.execute(copy_version(v))
    for line in lines:
        await session.execute(copy_line(line))
    await session.execute(text(CHECK_NOW))


async def test_an_exact_copy_passes_and_lines_cannot_be_missing_extra_or_tampered(populated, seed_plan):  # fmt: skip
    _, draft = await drafted(populated, seed_plan)
    lines = await rows(populated, LINES, draft.quote_id, 1)
    assert len(lines) == 2
    savepoint = await populated.begin_nested()
    await complete_copy(populated, draft.quote_id, lines)  # positive control
    with pytest.raises(IntegrityError) as caught:  # a later extra line, same transaction
        nested = await populated.begin_nested()
        await populated.execute(copy_line(lines[0], line_number=99, quote_line_id=lines[0]["quote_line_id"] + "-X"))  # fmt: skip
    await nested.rollback()
    assert k2_refusal(caught.value) == ITEMS_DONE
    await savepoint.rollback()
    variants = [
        [lines[0]],  # missing
        [lines[0], {**lines[1], "line_total_usd": lines[1]["line_total_usd"] + 1}],
        [lines[0], {**lines[1], "quantity": lines[1]["quantity"] + 1}],
        [lines[0], {**lines[1], "part_json": {**lines[1]["part_json"], "hole_count": 99}}],
        [lines[0], {**lines[1], "part_id": "OTHER"}],
    ]
    for variant in variants:
        savepoint = await populated.begin_nested()
        with pytest.raises(IntegrityError) as caught:
            await complete_copy(populated, draft.quote_id, variant)
        await savepoint.rollback()
        assert k2_refusal(caught.value) in (LINES_DONE, ITEMS_DONE)
        await populated.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    savepoint = await populated.begin_nested()
    with pytest.raises(IntegrityError) as caught:  # no lines at all, checked immediately
        (v,) = await rows(populated, VERSIONS, draft.quote_id, 1)
        await populated.execute(copy_version(v))
    await savepoint.rollback()
    assert k2_refusal(caught.value) == LINES_DONE


async def test_a_failed_line_check_inside_the_service_leaves_no_partial_evidence(populated, seed_plan, monkeypatch):  # fmt: skip
    principal = await setup(populated, seed_plan)
    real = quote_drafts.version_rows

    def short(*a, **kw):  # one line goes missing
        version, lines = real(*a, **kw)
        return version, lines[:-1]

    monkeypatch.setattr(quote_drafts, "version_rows", short)
    with pytest.raises(IntegrityError) as caught:  # raised inside the savepoint, not at COMMIT
        await create_quote_draft(populated, principal, RFQ_ID, **args())
    monkeypatch.undo()
    assert k2_refusal(caught.value) in (LINES_DONE, ITEMS_DONE)
    await nothing_written(populated)  # no quote, version, line, record, audit or receipt
    retry = await create_quote_draft(populated, principal, RFQ_ID, **args())
    assert not retry.replayed and len(await records(populated, RFQ_ID)) == 1


# --- Stored records, assessment and expiry ---------------------------------------------------


async def test_a_tampered_record_is_never_reused_and_is_reported(populated, seed_plan):
    principal, draft = await drafted(populated, seed_plan)
    record = (await records(populated, RFQ_ID))[0]
    forged_lines = [{**record["lines"][0], "line_total_usd": "0.01"}, *record["lines"][1:]]
    async with corrupted(populated, "pricing_calculations"):
        await populated.execute(update(PricingCalculation).where(PricingCalculation.rfq_id == RFQ_ID).values(lines=forged_lines))  # fmt: skip
    populated.expunge_all()
    result = await assess_quote_approval(populated, principal, draft.quote_id, expected_version=1)
    assert result.integrity_findings == ("CALCULATION_RECORD_MISMATCH",)
    assert result.classification == "integrity_mismatch"
    before = await state(populated, draft.quote_id)
    await refused("calculation_record_mismatch", revise_quote(populated, principal, draft.quote_id, **rargs()))  # fmt: skip
    assert await state(populated, draft.quote_id) == before


@pytest.mark.parametrize(
    "column,value,findings",
    [("calculation_id", "CALC-FORGED", ("CALCULATION_CHANGED", "EVIDENCE_MISMATCH")),
     ("rules_sha256", "0" * 64, ("EVIDENCE_MISMATCH",)),
     ("rfq_source_sha256", "0" * 64, ("EVIDENCE_MISMATCH",)),
     ("expiry_date", "plus_one_day", ("VALIDITY_MISMATCH",))],
)  # fmt: skip
async def test_tampered_version_evidence_is_an_integrity_mismatch(populated, seed_plan, column, value, findings):  # fmt: skip
    principal, draft = await drafted(populated, seed_plan)
    where = (QuoteVersion.quote_id == draft.quote_id, QuoteVersion.version == 1)
    if value == "plus_one_day":
        value = QuoteVersion.expiry_date + 1
    async with corrupted(populated, "quote_versions"):
        await populated.execute(update(QuoteVersion).where(*where).values({column: value}))
    populated.expunge_all()
    result = await assess_quote_approval(populated, principal, draft.quote_id, expected_version=1)
    assert result.integrity_findings == findings
    assert (result.classification, result.human_review) == ("integrity_mismatch", "not_eligible")
    assert result.ready_for_binding_approval is False


async def test_a_sound_complete_version_keeps_only_the_authentication_blocker(populated, seed_plan, monkeypatch):  # fmt: skip
    principal, draft = await drafted(populated, seed_plan)
    result = await assess_quote_approval(populated, principal, draft.quote_id, expected_version=1)
    assert result.integrity_findings == ()
    assert result.binding_approval_blockers == ("EVALUATION_CONTEXT_NOT_AUTHENTICATED",)
    assert (result.human_review, result.ready_for_binding_approval) == ("eligible", False)
    expires = quote_evidence.expires_at(draft.expiry_date)
    for now, expired in ((expires - timedelta(microseconds=1), False), (expires, True)):

        async def clock(session, now=now):
            return now

        monkeypatch.setattr(quote_evidence, "database_now", clock)
        result = await assess_quote_approval(populated, principal, draft.quote_id, expected_version=1)  # fmt: skip
        assert ("QUOTE_EXPIRED" in result.binding_approval_blockers) is expired
        assert result.human_review == ("not_eligible" if expired else "eligible")
        monkeypatch.undo()


async def test_records_hold_internal_costs_and_nothing_customer_facing_does(populated, seed_plan):
    principal, draft = await drafted(populated, seed_plan)
    assert "cost_usd_per_kg" in repr((await records(populated, RFQ_ID))[0]["lines"])  # internal
    receipt = await populated.scalar(text("SELECT result_json FROM business.idempotency_records LIMIT 1"))  # fmt: skip
    audit = await populated.scalar(select(AuditEvent.event_data).where(AuditEvent.quote_id == draft.quote_id))  # fmt: skip
    result = await assess_quote_approval(populated, principal, draft.quote_id, expected_version=1)
    for payload in (draft.to_json(), receipt, audit, result):
        text_ = repr(payload)
        for secret in ("cost", "density", "margin", "labor", "CALCREC-", "content_sha256"):
            assert secret not in text_


# --- Explicit one-way adoption -------------------------------------------------------------------


async def test_explicit_adoption_creates_a_complete_version_and_preserves_history(populated, seed_plan, monkeypatch):  # fmt: skip
    principal, legacy = await drafted(populated, seed_plan, "workflow-v1.1", monkeypatch)
    before = await state(populated, legacy.quote_id)
    assert before["versions"][0]["snapshot_completeness"] == "priced_partial"
    same = rargs(requested_delivery_date=None, idempotency_key="adopt-1")  # unchanged evaluation
    await refused("unchanged", revise_quote(populated, principal, legacy.quote_id, **same))
    adopted = await revise_quote(populated, principal, legacy.quote_id, **same, adopt_workflow_rule="workflow-v1.2")  # fmt: skip
    assert adopted.rule_versions[-1] == "workflow-v1.2" and adopted.expiry_date is not None
    assert adopted.evaluation_fingerprint == legacy.evaluation_fingerprint
    v1, v2 = await rows(populated, VERSIONS, legacy.quote_id)
    assert {**v1, "is_current": True} == before["versions"][0]  # history untouched
    assert (v2["workflow_rule_version"], v2["snapshot_completeness"]) == ("workflow-v1.2", "complete")  # fmt: skip
    event = await populated.scalar(select(AuditEvent.event_data).where(AuditEvent.quote_id == legacy.quote_id, AuditEvent.operation == "quote_version.adopt_workflow"))  # fmt: skip
    assert (event["adopted_from"], event["adopted_to"], event["previous_version"]) == ("workflow-v1.1", "workflow-v1.2", 1)  # fmt: skip
    again = await revise_quote(populated, principal, legacy.quote_id, **same, adopt_workflow_rule="workflow-v1.2")  # fmt: skip
    assert again.replayed and again.version == 2
    result = await assess_quote_approval(populated, principal, legacy.quote_id, expected_version=2)
    assert result.binding_approval_blockers == ("EVALUATION_CONTEXT_NOT_AUTHENTICATED",)


@pytest.mark.parametrize(
    "workflow,target,reason",
    [("workflow-v1.0", "workflow-v1.2", "revision_not_allowed"),  # never from v1.0
     ("workflow-v1.1", "workflow-v1.1", "revision_not_allowed"),  # only to the server pin
     ("workflow-v1.1", "workflow-v9.9", "revision_not_allowed"),
     ("workflow-v1.2", "workflow-v1.2", "revision_not_allowed"),  # already adopted
     ("workflow-v1.1", 12, "invalid_request")],
)  # fmt: skip
async def test_adoption_is_one_way_and_explicit(populated, seed_plan, monkeypatch, workflow, target, reason):  # fmt: skip
    principal, draft = await drafted(populated, seed_plan, workflow, monkeypatch)
    before = await state(populated, draft.quote_id)
    await refused(reason, revise_quote(populated, principal, draft.quote_id, **rargs(), adopt_workflow_rule=target))  # fmt: skip
    assert await state(populated, draft.quote_id) == before


async def test_adoption_needs_creation_provenance_and_its_own_key(populated, seed_plan, monkeypatch):  # fmt: skip
    principal, legacy = await drafted(populated, seed_plan, "workflow-v1.1", monkeypatch)
    await revise_quote(populated, principal, legacy.quote_id, **rargs(idempotency_key="k"))
    adopt = rargs(expected_version=2, idempotency_key="k")  # same key, another request
    await refused("idempotency_key_conflict", revise_quote(populated, principal, legacy.quote_id, **adopt, adopt_workflow_rule="workflow-v1.2"))  # fmt: skip
    async with corrupted(populated, "audit_events"):  # a quotation without its creation event
        await populated.execute(delete(AuditEvent).where(AuditEvent.quote_id == legacy.quote_id, AuditEvent.quote_version == 2))  # fmt: skip
    await refused("provenance_missing", revise_quote(populated, principal, legacy.quote_id, **rargs(expected_version=2, idempotency_key="k2"), adopt_workflow_rule="workflow-v1.2"))  # fmt: skip


# --- Rule immutability and test-only trigger restoration -------------------------------------------


async def test_trusted_rule_versions_are_append_only(populated, seed_plan):
    await setup(populated, seed_plan)  # registers by INSERT, which stays allowed
    v12 = BusinessRuleVersion.rule_version == "workflow-v1.2"
    trigger = "k3_business_rule_versions_immutable"
    await refused_by(populated, trigger, update(BusinessRuleVersion).where(v12).values(source_path="x"))  # fmt: skip
    await refused_by(populated, trigger, delete(BusinessRuleVersion).where(v12))
    assert await count(populated, BusinessRuleVersion, v12) == 1


async def test_corruption_scopes_restore_every_trigger_of_a_table(populated):
    always = await trigger_states(populated)
    assert set(always) == ALL_GUARD_TRIGGERS and set(always.values()) == {"A"}
    # K4 replaced K2's quote_versions guard.
    assert set(GUARDS["quote_versions"]) == {
        "k5_quote_versions_guard",
        EVIDENCE,
        LINES_DONE,
    }
    async with corrupted(populated, "quote_versions"):
        inside = await trigger_states(populated)
        assert {t for t, s in inside.items() if s == "D"} == set(GUARDS["quote_versions"])
    assert await trigger_states(populated) == always


# --- Committed race --------------------------------------------------------------------------------


async def test_racing_drafts_leave_one_quote_and_one_calculation_record(world, seed_database, monkeypatch):  # fmt: skip
    hold_after_checks(monkeypatch)
    results = await race(seed_database, world, args(idempotency_key="k3-a"), args(idempotency_key="k3-b"))  # fmt: skip
    assert sorted(type(r).__name__ for r in results) == ["QuoteDraft", "str"]
    async with seed_database.session() as s:
        assert await s.scalar(select(func.count()).select_from(RECORDS).where(RECORDS.c.rfq_id == RFQ_ID)) == 1  # fmt: skip
        complete = select(func.count()).select_from(VERSIONS).where(VERSIONS.c.snapshot_completeness == "complete")  # fmt: skip
        assert await s.scalar(complete) == 1
