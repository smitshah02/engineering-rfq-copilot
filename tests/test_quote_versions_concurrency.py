"""Day 52 revisions under real concurrency: independent sessions, connections, transactions.

Reuses the Day 51 committed ``world`` (guarded ``rfq_copilot_test``, ``current_database()``
asserted on the committing and truncating connections, TRUNCATE in teardown). Version 1 is
committed first. A barrier then holds both revision requests after authorization and before
the quote row lock, so both race for the lock: the winner commits, the loser wakes on the
committed row and must replay, conflict or be stale. The development database is never
opened.
"""

import asyncio

import pytest
from sqlalchemy import func, select, text
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_quote_drafts_concurrency import RACE_TIMEOUT
from test_quote_drafts_concurrency import attempt as draft_in
from test_quote_drafts_concurrency import world as world
from test_quote_drafts_database import args
from test_quote_versions_database import LATER, rargs

import rfq_copilot.quote_drafts as quote_drafts
from rfq_copilot.models import AuditEvent, IdempotencyRecord, Quote, QuoteVersion
from rfq_copilot.quote_drafts import DraftRefused
from rfq_copilot.quote_versions import QuoteRevision, revise_quote

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


async def revise_in(database, principal, quote_id, **kwargs):
    """One revision in its own session and transaction; commits whatever it returns."""
    async with database.session() as s, s.begin():
        try:
            return await revise_quote(s, principal, quote_id, **kwargs)
        except DraftRefused as refused:
            assert await s.scalar(text("SELECT 1")) == 1  # the loser's transaction survives
            return refused.reason


def hold_before_the_lock(monkeypatch):
    barrier = asyncio.Barrier(2)
    real = quote_drafts._authorize

    async def held(*a, **kw):
        await real(*a, **kw)
        await barrier.wait()

    monkeypatch.setattr(quote_drafts, "_authorize", held)


async def race(database, principal, quote_id, first, second):
    requests = (
        revise_in(database, principal, quote_id, **first),
        revise_in(database, principal, quote_id, **second),
    )
    return await asyncio.wait_for(asyncio.gather(*requests), RACE_TIMEOUT)


async def committed(database, quote_id):
    """(versions, current versions, quote pointer, edit receipts, revision audits)."""
    async with database.session() as s:
        versions = (await s.scalars(select(QuoteVersion.version).where(QuoteVersion.quote_id == quote_id).order_by(QuoteVersion.version))).all()  # fmt: skip
        current = (await s.scalars(select(QuoteVersion.version).where(QuoteVersion.quote_id == quote_id, QuoteVersion.is_current))).all()  # fmt: skip
        pointer = await s.scalar(select(Quote.current_version).where(Quote.quote_id == quote_id))
        receipts = await s.scalar(select(func.count()).select_from(IdempotencyRecord).where(IdempotencyRecord.operation == "edit_quote_draft"))  # fmt: skip
        audits = await s.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.operation == "quote_version.create"))  # fmt: skip
        return list(versions), list(current), pointer, receipts, audits


async def committed_draft(database, principal):
    draft = await draft_in(database, principal, **args())
    assert not isinstance(draft, str)
    return draft.quote_id


async def test_identical_revisions_create_one_version(world, seed_database, monkeypatch):
    quote_id = await committed_draft(seed_database, world)
    hold_before_the_lock(monkeypatch)
    results = await race(seed_database, world, quote_id, rargs(), rargs())
    assert all(isinstance(r, QuoteRevision) for r in results)
    assert sorted(r.replayed for r in results) == [False, True]
    assert {r.version for r in results} == {2}
    assert await committed(seed_database, quote_id) == ([1, 2], [2], 2, 1, 1)


async def test_different_revisions_of_one_version_cannot_both_succeed(
    world, seed_database, monkeypatch
):
    quote_id = await committed_draft(seed_database, world)
    hold_before_the_lock(monkeypatch)
    other = rargs(idempotency_key="rev-2", requested_delivery_date=LATER)
    results = await race(seed_database, world, quote_id, rargs(), other)
    revisions = [r for r in results if isinstance(r, QuoteRevision)]
    assert len(revisions) == 1 and revisions[0].version == 2 and not revisions[0].replayed
    assert [r for r in results if not isinstance(r, QuoteRevision)] == ["stale_version"]
    assert await committed(seed_database, quote_id) == ([1, 2], [2], 2, 1, 1)


async def test_same_key_with_another_revision_conflicts(world, seed_database, monkeypatch):
    quote_id = await committed_draft(seed_database, world)
    hold_before_the_lock(monkeypatch)
    results = await race(seed_database, world, quote_id, rargs(), rargs(requested_delivery_date=LATER))  # fmt: skip
    revisions = [r for r in results if isinstance(r, QuoteRevision)]
    assert len(revisions) == 1 and not revisions[0].replayed
    assert [r for r in results if not isinstance(r, QuoteRevision)] == ["idempotency_key_conflict"]
    assert await committed(seed_database, quote_id) == ([1, 2], [2], 2, 1, 1)


async def test_concurrent_revisions_of_a_revised_draft_create_one_version(
    world, seed_database, monkeypatch
):
    """workflow-v1.2 (revised_draft -> revised_draft, as v1.1): version 2 raced by two
    different requests; the loser leaves no calculation record or evidence behind."""
    quote_id = await committed_draft(seed_database, world)
    v2 = await revise_in(seed_database, world, quote_id, **rargs())
    assert isinstance(v2, QuoteRevision) and v2.status == "revised_draft"
    hold_before_the_lock(monkeypatch)
    first = rargs(expected_version=2, idempotency_key="rev-a", requested_delivery_date=LATER)
    second = rargs(expected_version=2, idempotency_key="rev-b", requested_delivery_date=None)
    results = await race(seed_database, world, quote_id, first, second)
    revisions = [r for r in results if isinstance(r, QuoteRevision)]
    assert len(revisions) == 1 and revisions[0].version == 3 and not revisions[0].replayed
    assert revisions[0].rule_versions[-1] == "workflow-v1.2"
    assert [r for r in results if not isinstance(r, QuoteRevision)] == ["stale_version"]
    assert await committed(seed_database, quote_id) == ([1, 2, 3], [3], 3, 2, 2)


async def test_committed_revision_replays_and_later_attempts_are_stale(world, seed_database):
    quote_id = await committed_draft(seed_database, world)
    first = await revise_in(seed_database, world, quote_id, **rargs())
    assert isinstance(first, QuoteRevision) and not first.replayed
    again = await revise_in(seed_database, world, quote_id, **rargs())
    assert again == QuoteRevision(**{**vars(first), "replayed": True})
    stale = rargs(idempotency_key="rev-late", requested_delivery_date=LATER)
    assert await revise_in(seed_database, world, quote_id, **stale) == "stale_version"
    third = await revise_in(seed_database, world, quote_id, **{**stale, "expected_version": 2})
    assert isinstance(third, QuoteRevision) and third.version == 3
    assert await committed(seed_database, quote_id) == ([1, 2, 3], [3], 3, 2, 2)
