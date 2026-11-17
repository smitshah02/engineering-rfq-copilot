"""Day 51 quote drafts under real concurrency: independent sessions, connections, transactions.

Data: other connections cannot see a rolled-back fixture transaction, so ``world`` COMMITS
the seeded dataset, ``approval-v1.2``, ``workflow-v1.1``, ``workflow-v1.2``, one drafter binding and one runtime RFQ to the
guarded ``rfq_copilot_test`` database, and truncates every application table in teardown
(the Day 30 ``test_mcp_live_e2e`` pattern). ``seed_database`` verifies the database identity
first and, at module end, asserts the tables empty, downgrades and checks nothing is left.
The development database is never opened.

Each request runs in its own ``Database.session()`` and transaction. A barrier holds both
requests after their pre-checks (authorization, ownership, receipt, existing quote) and
before evaluation, so neither can see the other's rows: both reach the INSERTs, and
PostgreSQL's unique constraints decide the race (the loser waits on the index entry until
the winner commits, then fails). Fresh transactions also leave the quote -> version pointer
in its declared DEFERRED mode, the production default.
"""

import asyncio
from datetime import date

import pytest
import pytest_asyncio
from history_corruption import k2_refusal
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from test_database_queries import customer_id
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_day21_rbac_database import bound
from test_quote_drafts_database import RFQ_ID, args, drafters, runtime_rfq

import rfq_copilot.quote_drafts as quote_drafts
from rfq_copilot.api.auth import Principal
from rfq_copilot.models import AuditEvent, IdempotencyRecord, Quote, QuoteVersion
from rfq_copilot.quote_drafts import DraftRefused, QuoteDraft, create_quote_draft
from rfq_copilot.seeding.loader import execute_seed
from rfq_copilot.seeding.rules import register_rule_version

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

RACE_TIMEOUT = 60  # seconds; a deadlock or a lost wake-up fails instead of hanging
TEST_DATABASE = "rfq_copilot_test"


async def guarded(session):
    """Identity of the very connection about to commit or truncate (defence in depth)."""
    assert await session.scalar(text("SELECT current_database()")) == TEST_DATABASE


@pytest_asyncio.fixture(loop_scope="module")
async def world(seed_database, seed_plan):
    """Committed synthetic world on the guarded test DB; truncated again in teardown."""
    try:
        async with seed_database.session() as s, s.begin():
            await guarded(s)
            await execute_seed(s, seed_database.config, seed_plan, "apply", confirm_database=TEST_DATABASE)  # fmt: skip
            await register_rule_version(s, "approval-v1.2")
            await register_rule_version(s, "workflow-v1.1")
            await register_rule_version(s, "workflow-v1.2")  # K3: new drafts' rule (test DB)
            user = (await drafters(s))[0]
            mine = customer_id(seed_plan)
            await bound(s, user, mine)
            await runtime_rfq(s, RFQ_ID, mine)
            principal = Principal(mine, frozenset(), user_id=user.user_id)
        yield principal
    finally:
        async with seed_database.session() as s, s.begin():
            await guarded(s)
            tables = (await s.scalars(text("SELECT quote_ident(schemaname)||'.'||quote_ident(tablename) FROM pg_tables WHERE schemaname IN ('business','orchestration')"))).all()  # fmt: skip
            if tables:
                await s.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))


async def attempt(database, principal, **kwargs):
    """One request in its own session and transaction; commits whatever it returns."""
    async with database.session() as s, s.begin():
        try:
            return await create_quote_draft(s, principal, RFQ_ID, **kwargs)
        except DraftRefused as refused:
            # The loser recovered inside its savepoint: its transaction is still usable.
            assert await s.scalar(text("SELECT 1")) == 1
            return refused.reason


def hold_after_checks(monkeypatch):
    """Both requests wait here, after every pre-check and before evaluation."""
    barrier = asyncio.Barrier(2)
    real = quote_drafts.evaluate_stored_rfq

    async def held(*a, **kw):
        await barrier.wait()
        return await real(*a, **kw)

    monkeypatch.setattr(quote_drafts, "evaluate_stored_rfq", held)


async def race(database, principal, first, second):
    requests = (attempt(database, principal, **first), attempt(database, principal, **second))
    return await asyncio.wait_for(asyncio.gather(*requests), RACE_TIMEOUT)


async def committed(database):
    """Rows visible to a new transaction: (quotes, versions, receipts, audit events)."""
    async with database.session() as s:
        quote = select(func.count()).select_from(Quote).where(Quote.rfq_id == RFQ_ID)
        quote_ids = select(Quote.quote_id).where(Quote.rfq_id == RFQ_ID)
        version = select(func.count()).select_from(QuoteVersion).where(QuoteVersion.quote_id.in_(quote_ids))  # fmt: skip
        receipt = select(func.count()).select_from(IdempotencyRecord)
        audit = select(func.count()).select_from(AuditEvent).where(AuditEvent.rfq_id == RFQ_ID)
        return tuple([await s.scalar(q) for q in (quote, version, receipt, audit)])


async def test_identical_requests_in_independent_transactions_create_one_draft(
    world, seed_database, monkeypatch
):
    hold_after_checks(monkeypatch)
    results = await race(seed_database, world, args(), args())
    assert all(isinstance(r, QuoteDraft) for r in results)
    assert sorted(r.replayed for r in results) == [False, True]  # the loser replayed
    assert results[0].quote_id == results[1].quote_id
    assert await committed(seed_database) == (1, 1, 1, 1)


async def test_different_keys_in_independent_transactions_cannot_duplicate_the_quote(
    world, seed_database, monkeypatch
):
    hold_after_checks(monkeypatch)
    results = await race(seed_database, world, args(), args(idempotency_key="draft-2"))
    drafts = [r for r in results if isinstance(r, QuoteDraft)]
    assert len(drafts) == 1 and not drafts[0].replayed
    assert [r for r in results if not isinstance(r, QuoteDraft)] == ["quote_exists"]
    assert await committed(seed_database) == (1, 1, 1, 1)  # only the winner's receipt


async def test_same_key_with_another_payload_in_independent_transactions_conflicts(
    world, seed_database, monkeypatch
):
    hold_after_checks(monkeypatch)
    later = args(requested_delivery_date=date(2027, 6, 1))
    results = await race(seed_database, world, args(), later)
    drafts = [r for r in results if isinstance(r, QuoteDraft)]
    assert len(drafts) == 1 and not drafts[0].replayed
    assert [r for r in results if not isinstance(r, QuoteDraft)] == ["idempotency_key_conflict"]
    assert await committed(seed_database) == (1, 1, 1, 1)


async def test_a_committed_draft_is_replayed_by_a_new_transaction(world, seed_database):
    first = await attempt(seed_database, world, **args())
    assert isinstance(first, QuoteDraft) and not first.replayed  # committed in DEFERRED mode
    again = await attempt(seed_database, world, **args())
    assert again == QuoteDraft(**{**vars(first), "replayed": True})
    assert await attempt(seed_database, world, **args(idempotency_key="draft-2")) == "quote_exists"
    assert await committed(seed_database) == (1, 1, 1, 1)


async def test_the_current_version_pointer_stays_immediate_after_a_draft(world, seed_database):
    """Documented side effect: after a successful draft the caller's transaction checks the
    quote -> version pointer per statement (stricter than the declared DEFERRED), and a
    failed draft's savepoint rollback restores the caller's previous mode."""
    async with seed_database.session() as s:  # autobegin; session() rolls back on exit
        draft = await create_quote_draft(s, world, RFQ_ID, **args())
        savepoint = await s.begin_nested()
        # Inside the K2 envelope (a revision-shaped change), so the pointer FK is what fails.
        revision = {"current_version": 2, "status": "revised_draft", "row_version": 2}
        with pytest.raises(IntegrityError) as refused:  # now, not at COMMIT: no version 2
            await s.execute(
                update(Quote).where(Quote.quote_id == draft.quote_id).values(**revision)
            )
        assert k2_refusal(refused.value) is None
        await savepoint.rollback()
        await s.rollback()  # nothing from this test is committed
    assert await committed(seed_database) == (0, 0, 0, 0)
