"""K2 (Day 54A) database-enforced history against the guarded PostgreSQL test database.

Connections here use the owner fixture role. Triggers bind it too, unless it disables them,
which only ``history_corruption`` does. Fixture-transaction tests share one transaction
with the seed and the drafts, so every version there was created by the current
transaction: the post-commit seal is proved on committed data (``world``) in separate
transactions and sessions, truncated again in teardown. Privileges of the restricted
runtime role are in ``test_runtime_role_database.py``. The development database is never
opened.
"""

from datetime import UTC, date, datetime

import pytest
from history_corruption import ALL_GUARD_TRIGGERS, corrupted, k2_refusal, trigger_states
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_quote_drafts_concurrency import guarded
from test_quote_drafts_concurrency import world as world
from test_quote_drafts_database import RFQ_ID, args
from test_quote_versions_database import LATER, LINES, VERSIONS, drafted, quote_row, rargs, rows

import rfq_copilot.quote_drafts as quote_drafts
from rfq_copilot.models import (
    RFQ,
    ApprovalHistory,
    AuditEvent,
    IdempotencyRecord,
    Quote,
    RFQLineItem,
)
from rfq_copilot.quote_drafts import create_quote_draft
from rfq_copilot.quote_versions import revise_quote
from rfq_copilot.schema_contract import CURRENT, contract_guards
from rfq_copilot.seeding import SeedError
from rfq_copilot.seeding.safety import verify_schema

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

VERSION_GUARD, LINE_GUARD, QUOTE_GUARD = (  # K4, then K5, replaced the K2 quote/version guards
    "k5_quote_versions_guard",
    "k2_quote_line_items_guard",
    "k5_quotes_guard",
)


async def refused_by(session, trigger, statement):
    """The statement fails with this K2 trigger's refusal; its savepoint leaves nothing."""
    savepoint = await session.begin_nested()
    with pytest.raises(IntegrityError) as caught:
        await session.execute(statement)
    await savepoint.rollback()
    assert k2_refusal(caught.value) == trigger


async def revised(session, plan):
    """Version 1 (history) and version 2 (current), both by this transaction."""
    principal, quote_id = await drafted(session, plan)
    await revise_quote(session, principal, quote_id, **rargs())
    return principal, quote_id


def version(quote_id, number):
    return update(VERSIONS).where(VERSIONS.c.quote_id == quote_id, VERSIONS.c.version == number)


def extra_line(line, suffix, number=99):
    return insert(LINES).values({**line, "quote_line_id": line["quote_line_id"] + suffix, "line_number": number})  # fmt: skip


async def current_xact(session):
    return await session.scalar(text("SELECT pg_current_xact_id()"))


def partial_drafts(monkeypatch):
    """K2's own line seal is proved on a workflow-v1.1 (priced_partial) version: K3's
    line-completeness triggers would refuse an extra line on a complete one first
    (test_k3_calculation_provenance_database covers that)."""
    monkeypatch.setattr(quote_drafts, "WORKFLOW_RULE_VERSION", "workflow-v1.1")


# --- Legitimate Day 51 / Day 52 writes ---------------------------------------------------------


async def test_drafts_and_revisions_pass_stamped_by_their_transaction(populated, seed_plan):
    principal, quote_id = await drafted(populated, seed_plan)
    await revise_quote(populated, principal, quote_id, **rargs())
    third = rargs(expected_version=2, idempotency_key="rev-2", requested_delivery_date=LATER)
    await revise_quote(populated, principal, quote_id, **third)
    versions = await rows(populated, VERSIONS, quote_id)
    assert [(v["version"], v["is_current"]) for v in versions] == [(1, False), (2, False), (3, True)]  # fmt: skip
    # The top-level transaction id, although each version was inserted inside a SAVEPOINT.
    assert {v["created_xact"] for v in versions} == {await current_xact(populated)}
    assert (await quote_row(populated, quote_id))["current_version"] == 3
    first = len(await rows(populated, LINES, quote_id, 1))
    assert first > 0 and len(await rows(populated, LINES, quote_id)) == 3 * first


async def test_superseding_the_current_version_is_its_only_permitted_change(populated, seed_plan):
    _, quote_id = await revised(populated, seed_plan)
    savepoint = await populated.begin_nested()
    assert (await populated.execute(version(quote_id, 2).values(is_current=False))).rowcount == 1
    await savepoint.rollback()
    for changes in ({"is_current": False, "total_usd": 0}, {"approval_valid": True}):
        await refused_by(populated, VERSION_GUARD, version(quote_id, 2).values(**changes))


# --- quote_versions ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [{"total_usd": VERSIONS.c.total_usd + 1}, {"currency": "EUR"},
     {"estimated_lead_business_days": 99}, {"approval_rule_version": "approval-v1.1"},
     {"workflow_rule_version": None}, {"source_snapshot_versions": {"dataset_version": "9.9.9"}},
     {"calculation_id": "CALC-FORGED"}, {"expiry_date": date(2027, 1, 1)},
     {"snapshot_completeness": "priced_partial"}, {"approval_valid": True}, {"is_current": True},
     {"created_by": "USER-001"}, {"created_xact": 1}, {"created_xact": None},
     # K3 evidence columns are history too (covered by K2's whole-row comparison).
     {"evaluation_fingerprint": "0" * 64}, {"rfq_source_sha256": "0" * 64},
     {"rules_sha256": "0" * 64}, {"calculation_record_id": None},
     {"horizon_start": date(2026, 1, 5)}, {"created_at": datetime(2026, 1, 5, tzinfo=UTC)}],
)  # fmt: skip
async def test_historical_version_fields_cannot_change(populated, seed_plan, changes):
    _, quote_id = await revised(populated, seed_plan)
    before = await rows(populated, VERSIONS, quote_id)
    await refused_by(populated, VERSION_GUARD, version(quote_id, 1).values(**changes))
    assert await rows(populated, VERSIONS, quote_id) == before


async def test_versions_cannot_be_deleted(populated, seed_plan):
    _, quote_id = await revised(populated, seed_plan)
    for number in (1, 2):
        await refused_by(populated, VERSION_GUARD, delete(VERSIONS).where(VERSIONS.c.quote_id == quote_id, VERSIONS.c.version == number))  # fmt: skip


async def test_a_supplied_creation_seal_is_replaced_by_the_database(populated, seed_plan):
    _, quote_id = await drafted(populated, seed_plan)
    v1 = (await rows(populated, VERSIONS, quote_id, 1))[0]
    # A partial copy (rule_09: no K3 evidence), so K3's line-completeness check is not involved.
    k3 = dict.fromkeys(("rfq_id", "calculation_record_id", "rfq_source_sha256", "evaluation_fingerprint", "rules_sha256", "horizon_start", "requested_delivery_date"))  # fmt: skip
    forged = {**v1, **k3, "snapshot_completeness": "priced_partial", "quote_version_id": f"{quote_id}-V9", "version": 9, "is_current": False, "created_xact": 1}  # fmt: skip
    savepoint = await populated.begin_nested()
    await populated.execute(insert(VERSIONS).values(**forged))
    stored = (await rows(populated, VERSIONS, quote_id, 9))[0]["created_xact"]
    assert stored == await current_xact(populated) != 1
    await savepoint.rollback()


# --- quote_line_items ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [{"line_total_usd": LINES.c.line_total_usd + 1}, {"quantity": 1}, {"part_json": {}},
     {"cost_breakdown_json": {}}, {"line_number": 77}, {"part_id": "OTHER"}],
)  # fmt: skip
async def test_line_items_cannot_change_or_disappear(populated, seed_plan, changes):
    _, quote_id = await revised(populated, seed_plan)
    before = await rows(populated, LINES, quote_id)
    for number in (1, 2):
        lines = (LINES.c.quote_id == quote_id, LINES.c.quote_version == number)
        await refused_by(populated, LINE_GUARD, update(LINES).where(*lines).values(**changes))
        await refused_by(populated, LINE_GUARD, delete(LINES).where(*lines))
    assert await rows(populated, LINES, quote_id) == before


async def test_a_legacy_version_without_a_seal_accepts_no_lines(populated, seed_plan, monkeypatch):  # fmt: skip
    partial_drafts(monkeypatch)
    _, quote_id = await drafted(populated, seed_plan)
    line = (await rows(populated, LINES, quote_id, 1))[0]
    # Positive control: the creating transaction may still add a line to its own version.
    savepoint = await populated.begin_nested()
    await populated.execute(extra_line(line, "-SAME"))
    await savepoint.rollback()
    # A version from before K2 has no creating transaction (NULL): it is sealed forever.
    async with corrupted(populated, "quote_versions"):
        await populated.execute(version(quote_id, 1).values(created_xact=None))
    await refused_by(populated, LINE_GUARD, extra_line(line, "-LEGACY"))


async def test_installing_k2_and_k3_certified_no_history(populated, seed_plan):
    await revised(populated, seed_plan)  # new K3 versions are complete; seeded history never
    seeded = VERSIONS.c.quote_id.not_like("QUOTE-%")
    complete = select(func.count()).select_from(VERSIONS).where(seeded, VERSIONS.c.snapshot_completeness == "complete")  # fmt: skip
    assert await populated.scalar(complete) == 0
    evidenced = select(func.count()).select_from(VERSIONS).where(seeded, VERSIONS.c.evaluation_fingerprint.is_not(None))  # fmt: skip
    assert await populated.scalar(evidenced) == 0


# --- Append-only tables ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table,model,where,changes",
    [("audit_events", AuditEvent, AuditEvent.quote_id.is_not(None), {"outcome": "error"}),
     ("idempotency_records", IdempotencyRecord, IdempotencyRecord.operation == "create_quote_draft", {"result_json": {}}),
     ("approval_history", ApprovalHistory, ApprovalHistory.approval_id.is_not(None), {"reason": "rewritten"}),
     ("rfqs", RFQ, RFQ.rfq_id == RFQ_ID, {"status": "archived"}),
     ("rfq_line_items", RFQLineItem, RFQLineItem.rfq_id == RFQ_ID, {"quantity": 1})],
)  # fmt: skip
async def test_append_only_rows_cannot_change_or_disappear(populated, seed_plan, table, model, where, changes):  # fmt: skip
    await drafted(populated, seed_plan)  # audit event, receipt, runtime RFQ; history is seeded
    trigger = f"k2_{table}_immutable"
    assert await populated.scalar(select(func.count()).select_from(model).where(where)) > 0
    await refused_by(populated, trigger, update(model).where(where).values(**changes))
    await refused_by(populated, trigger, delete(model).where(where))


# --- quotes ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [{"status": "pending_approval"}, {"status": "approved"}, {"status": "released"},
     {"status": "rejected"}, {"status": "expired"}, {"rejection_reason": "no"},
     {"submitted_by": "USER-001"}, {"rfq_id": "RFQ-0001"}, {"created_by": "USER-001"},
     {"row_version": 9}, {"current_version": 2},
     {"current_version": 2, "row_version": 2, "status": "draft"},
     {"current_version": 2, "row_version": 2, "status": "revised_draft", "rejection_reason": "x"},
     {"current_version": 0, "row_version": 2, "status": "revised_draft"}],
)  # fmt: skip
async def test_quotes_change_only_by_an_approved_transition(populated, seed_plan, changes):
    _, quote_id = await drafted(populated, seed_plan)
    before = await quote_row(populated, quote_id)
    statement = update(Quote).where(Quote.quote_id == quote_id).values(**changes)
    await refused_by(populated, QUOTE_GUARD, statement)
    await refused_by(populated, QUOTE_GUARD, delete(Quote).where(Quote.quote_id == quote_id))
    assert await quote_row(populated, quote_id) == before


async def test_seeded_approved_and_released_quotes_cannot_be_rewritten(populated):
    approved = await populated.scalar(select(Quote.quote_id).where(Quote.status == "approved").limit(1))  # fmt: skip
    for changes in ({"status": "released"}, {"approver_id": None}, {"status": "revised_draft"}):
        await refused_by(populated, QUOTE_GUARD, update(Quote).where(Quote.quote_id == approved).values(**changes))  # fmt: skip


# --- Atomicity -------------------------------------------------------------------------------------


async def test_a_refusal_rolls_back_the_whole_statement_and_its_savepoint(populated, seed_plan):
    _, quote_id = await revised(populated, seed_plan)
    before = await rows(populated, VERSIONS, quote_id)
    savepoint = await populated.begin_nested()
    populated.add(AuditEvent(operation="k2.probe", outcome="success", correlation_id="k2-probe"))
    await populated.flush()
    # One statement: clearing version 2 alone is allowed, version 1 (already false) is not.
    with pytest.raises(IntegrityError) as caught:
        await populated.execute(update(VERSIONS).where(VERSIONS.c.quote_id == quote_id).values(is_current=False))  # fmt: skip
    await savepoint.rollback()
    assert k2_refusal(caught.value) == VERSION_GUARD
    assert await rows(populated, VERSIONS, quote_id) == before  # version 2 is still current
    probe = select(func.count()).select_from(AuditEvent).where(AuditEvent.operation == "k2.probe")
    assert await populated.scalar(probe) == 0


async def test_staged_corruption_never_outlives_its_transaction(populated, seed_plan):
    _, quote_id = await drafted(populated, seed_plan)
    always = await trigger_states(populated)
    # The active contract's guards: 7 K2 + 5 K3 + 6 K5 (which replaced K4's 3) = 18.
    assert set(always) == ALL_GUARD_TRIGGERS and len(always) == len(contract_guards(CURRENT)[1]) == 18  # fmt: skip
    assert set(always.values()) == {"A"}
    savepoint = await populated.begin_nested()
    with pytest.raises(RuntimeError):
        async with corrupted(populated, "quote_versions", "audit_events"):
            disabled = await trigger_states(populated)
            assert disabled[VERSION_GUARD] == disabled["k2_audit_events_immutable"] == "D"
            raise RuntimeError("a failing test body: the helper never re-enables")
    await savepoint.rollback()
    assert await trigger_states(populated) == always
    await refused_by(populated, VERSION_GUARD, version(quote_id, 1).values(total_usd=0))


async def test_nested_corruption_scopes_restore_exactly_their_own_triggers(populated):
    always = await trigger_states(populated)
    async with corrupted(populated, "quote_versions"):
        async with corrupted(populated, "audit_events"):
            inner = await trigger_states(populated)
            assert inner[VERSION_GUARD] == inner["k2_audit_events_immutable"] == "D"
        outer = await trigger_states(populated)  # the outer scope still owns its trigger
        assert (outer[VERSION_GUARD], outer["k2_audit_events_immutable"]) == ("D", "A")
        with pytest.raises(AssertionError, match="already disabled"):  # before any DDL
            async with corrupted(populated, "quote_versions"):
                pass
    assert await trigger_states(populated) == always
    assert set(always.values()) == {"A"}


# --- Schema preflight allowlist --------------------------------------------------------------------


async def preflight(session, config):
    connection = await session.connection()
    return await connection.run_sync(verify_schema, config)


async def test_the_preflight_accepts_exactly_the_k2_and_k3_objects(populated, seed_database):
    report = await preflight(populated, seed_database.config)
    assert report["alembic_revision"] == CURRENT.revision == "20261118_01"


@pytest.mark.parametrize(
    "ddl",
    ["CREATE FUNCTION business.k2_extra() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END;$$",
     "CREATE TRIGGER k2_extra BEFORE UPDATE ON business.customers FOR EACH ROW EXECUTE FUNCTION business.k2_refuse_change()",
     "ALTER TABLE business.audit_events DISABLE TRIGGER k2_audit_events_immutable",
     "DROP TRIGGER k2_rfqs_immutable ON business.rfqs",
     "GRANT EXECUTE ON FUNCTION business.k2_refuse_change() TO PUBLIC",
     "ALTER FUNCTION business.k2_quotes_guard() SECURITY DEFINER",
     "ALTER FUNCTION business.k2_quotes_guard() RESET search_path",
     "CREATE OR REPLACE FUNCTION business.k2_refuse_change() RETURNS trigger LANGUAGE plpgsql "
     "SET search_path = pg_catalog, pg_temp AS $$BEGIN RETURN NULL; END;$$",
     "CREATE POLICY k2_extra ON business.quotes USING (true)"],
)  # fmt: skip
async def test_the_preflight_refuses_any_other_or_altered_object(populated, seed_database, ddl):
    savepoint = await populated.begin_nested()
    await populated.execute(text(ddl))
    with pytest.raises(SeedError):
        await preflight(populated, seed_database.config)
    await savepoint.rollback()


# --- Committed data, separate transactions and sessions ---------------------------------------------


async def test_lines_cannot_be_added_after_the_creating_transaction(world, seed_database):
    async with seed_database.session() as s, s.begin():
        await guarded(s)
        draft = await create_quote_draft(s, world, RFQ_ID, **args())
    async with seed_database.session() as s, s.begin():
        await revise_quote(s, world, draft.quote_id, **rargs())  # flips v1 from an older txn
    async with seed_database.session() as s:  # rolled back on exit
        await guarded(s)
        seeded = (await s.execute(select(LINES).limit(1))).mappings().one()
        for line in [dict(seeded), *(await rows(s, LINES, draft.quote_id))]:
            savepoint = await s.begin_nested()
            with pytest.raises(IntegrityError) as caught:
                await s.execute(extra_line(line, "-LATE"))
            await savepoint.rollback()
            assert k2_refusal(caught.value) == LINE_GUARD
        stamps = {v["created_xact"] for v in await rows(s, VERSIONS, draft.quote_id)}
        assert len(stamps) == 2 and None not in stamps  # one per creating transaction


async def test_an_open_transaction_cannot_lend_its_seal_to_another(world, seed_database, monkeypatch):  # fmt: skip
    partial_drafts(monkeypatch)
    async with seed_database.session() as mine:  # never committed
        draft = await create_quote_draft(mine, world, RFQ_ID, **args())
        line = (await rows(mine, LINES, draft.quote_id, 1))[0]
        async with seed_database.session() as other:
            await guarded(other)
            with pytest.raises(IntegrityError) as caught:
                await other.execute(extra_line(line, "-OTHER"))
            assert k2_refusal(caught.value) == LINE_GUARD
        await mine.execute(extra_line(line, "-MINE"))  # the creating transaction still may
    async with seed_database.session() as s:
        assert await s.scalar(select(func.count()).select_from(Quote).where(Quote.rfq_id == RFQ_ID)) == 0  # fmt: skip
