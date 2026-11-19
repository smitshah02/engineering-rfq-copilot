"""approval policy assessments racing revisions: independent sessions, connections, transactions.

Reuses the draft persistence/52 committed ``world`` (guarded ``rfq_copilot_test``, TRUNCATE in
teardown). Isolation is the application's READ COMMITTED. The assessment locks the quote
row FOR SHARE; a revision needs FOR NO KEY UPDATE, which conflicts. So a revision waits
until an open assessment's transaction ends, and an assessment that waits behind a
revision wakes on the committed row and is ``stale_version``. Either way an assessment
describes exactly the version it names, and that version is history once revised. The
development database is never opened.
"""

import asyncio

import pytest
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_quote_drafts_concurrency import RACE_TIMEOUT
from test_quote_drafts_concurrency import world as world
from test_quote_versions_concurrency import (
    committed,
    committed_draft,
    hold_before_the_lock,
    revise_in,
)
from test_quote_versions_database import rargs

from rfq_copilot.quote_approval_policy import QuoteApprovalAssessment, assess_quote_approval
from rfq_copilot.quote_drafts import DraftRefused
from rfq_copilot.quote_versions import QuoteRevision

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


async def assess_in(database, principal, quote_id, version):
    """One assessment in its own session and transaction."""
    async with database.session() as s, s.begin():
        try:
            return await assess_quote_approval(s, principal, quote_id, expected_version=version)
        except DraftRefused as refused:
            return refused.reason


async def test_an_open_assessment_holds_off_a_revision(world, seed_database):
    quote_id = await committed_draft(seed_database, world)
    async with seed_database.session() as s, s.begin():
        first = await assess_quote_approval(s, world, quote_id, expected_version=1)
        revision = asyncio.create_task(revise_in(seed_database, world, quote_id, **rargs()))
        done, _ = await asyncio.wait({revision}, timeout=1)
        assert not done  # blocked on the FOR SHARE lock while the assessment is open
    revised = await asyncio.wait_for(revision, RACE_TIMEOUT)
    assert isinstance(revised, QuoteRevision) and revised.version == 2
    assert (first.assessed_version, first.quote_row_version) == (1, 1)
    # The finished assessment never stands for the new version.
    assert await assess_in(seed_database, world, quote_id, 1) == "stale_version"
    second = await assess_in(seed_database, world, quote_id, 2)
    assert (second.assessed_version, second.quote_row_version) == (2, 2)
    assert second.assessment_fingerprint != first.assessment_fingerprint
    assert await committed(seed_database, quote_id) == ([1, 2], [2], 2, 1, 1)


async def test_an_assessment_racing_a_revision_is_never_mixed(world, seed_database, monkeypatch):
    quote_id = await committed_draft(seed_database, world)
    hold_before_the_lock(monkeypatch)  # both requests released together after authorization
    requests = (
        assess_in(seed_database, world, quote_id, 1),
        revise_in(seed_database, world, quote_id, **rargs()),
    )
    assessment, revision = await asyncio.wait_for(asyncio.gather(*requests), RACE_TIMEOUT)
    monkeypatch.undo()
    assert isinstance(revision, QuoteRevision) and revision.version == 2  # never blocked for good
    if isinstance(assessment, QuoteApprovalAssessment):  # it locked first: wholly version 1
        assert (assessment.assessed_version, assessment.quote_row_version) == (1, 1)
        assert assessment.integrity_findings == ()
    else:  # the revision locked first: the assessment woke on version 2
        assert assessment == "stale_version"
    latest = await assess_in(seed_database, world, quote_id, 2)
    assert (latest.assessed_version, latest.quote_status) == (2, "revised_draft")
    assert await committed(seed_database, quote_id) == ([1, 2], [2], 2, 1, 1)  # nothing added
