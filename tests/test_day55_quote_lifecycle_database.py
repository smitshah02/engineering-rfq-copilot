"""Day 55 lifecycle on the guarded test database (K5).

Draft, revision and submission on the restricted runtime role; approval, rejection and
release on the K5 decision role (``test_quote_approval_database.database_for``). Release
needs a second sales manager (releaser != approver). The owner credential inspects state
and is shown to be refused. Reuses the Day 54D fixtures; ``world`` truncates everything.
"""

import asyncio
from datetime import date

import pytest
from sqlalchemy import select
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_quote_approval_database import attestation, database_for, decide, state, submitted
from test_quote_approval_database import decision as decision
from test_quote_approval_database import manager as manager
from test_quote_approval_database import releaser as releaser
from test_quote_drafts_concurrency import RACE_TIMEOUT
from test_quote_drafts_concurrency import world as world
from test_runtime_role_database import runtime as runtime

from rfq_copilot.api.auth import Principal
from rfq_copilot.database import PrivilegedRuntimeRole
from rfq_copilot.models import AuditEvent, IdempotencyRecord, Quote
from rfq_copilot.quote_approval import (
    approve_quote,
    assess_release,
    reject_quote,
    release_quote,
    submit_for_review,
)
from rfq_copilot.quote_drafts import DraftRefused
from rfq_copilot.quote_versions import revise_quote
from rfq_copilot.repositories import get_released_quotation

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

DUE = date(2027, 1, 21)  # RFQ-0041's labelled delivery date: a changed, draftable context
SECRET_WORDS = ("cost", "margin", "labor")


class CallerAborts(Exception):
    pass


async def act(runtime, principal, action, quote_id, key, version, **kwargs):
    """``decide`` for any expected version; a refusal is returned as its reason."""
    async with database_for(runtime, action).session() as s, s.begin():
        try:
            return await action(
                s, principal, quote_id, expected_version=version, idempotency_key=key,
                correlation_id="c", **kwargs,
            )  # fmt: skip
        except DraftRefused as caught:
            return caught.reason


async def approved(runtime, world, manager, seed_database):
    draft = await submitted(runtime, world)
    attested = await attestation(seed_database, draft.quote_id)
    decision = await decide(runtime, manager, approve_quote, draft.quote_id, "approve-1", attestation=attested)  # fmt: skip
    assert decision.action == "approved"
    return draft.quote_id


async def customer_view(seed_database, customer, quote_id):
    """What the customer-facing released-quote read returns (None: 404)."""
    async with seed_database.session() as s:
        match = await get_released_quotation(s, quote_id, customer_id=customer, assigned_user=None)
        return None if match is None else (match.quote.status, match.quote.approved_version)


async def released_by(seed_database, quote_id):
    async with seed_database.session() as s:
        return await s.scalar(select(Quote.released_by).where(Quote.quote_id == quote_id))


# --- The complete path ------------------------------------------------------------------------


async def test_draft_submit_approve_release_and_customer_visibility(runtime, world, manager, releaser, seed_database):  # fmt: skip
    quote_id = await approved(runtime, world, manager, seed_database)
    assert await customer_view(seed_database, world.customer_id, quote_id) is None  # approved only

    async with runtime.session() as s:
        eligibility = await assess_release(s, releaser, quote_id, expected_version=1)
    assert eligibility.eligible and eligibility.blockers == () and eligibility.version == 1
    assert not any(w in repr(eligibility) for w in (*SECRET_WORDS, "total"))
    before = await state(seed_database, quote_id)

    # Day 55 policy (service and k5_quotes_guard): the approver never releases.
    assert await decide(runtime, manager, release_quote, quote_id, "self") == "separation_of_duties"  # fmt: skip
    assert await state(seed_database, quote_id) == before

    released = await decide(runtime, releaser, release_quote, quote_id, "release-1")
    assert (released.action, released.status, released.version) == ("released", "released", 1)
    assert released.actor_id == releaser.user_id and not released.replayed
    assert released.approval_request_id == eligibility.approval_request_id
    after = await state(seed_database, quote_id)
    assert (after["status"], after["row_version"]) == ("released", before["row_version"] + 1)
    assert after["released"] is not None and await released_by(seed_database, quote_id) == releaser.user_id  # fmt: skip
    # The approval itself is untouched: same version, request, approver and validity.
    for f in ("approved_version", "approver_id", "approval_valid", "requests", "submitted_by"):
        assert after[f] == before[f], f
    assert after["approver_id"] == manager.user_id
    assert after["history"][:-1] == before["history"]
    assert after["history"][-1] == ("released", releaser.user_id, "released")
    assert after["audits"] == [*before["audits"], "quote_approval.release"]
    assert after["receipts"] == before["receipts"] + 1

    # Audit linkage: actor, customer, exact version, approval request, approver, rule.
    async with seed_database.session() as s:
        event = (
            await s.execute(
                select(AuditEvent.actor_id, AuditEvent.customer_id, AuditEvent.quote_version, AuditEvent.outcome, AuditEvent.event_data)
                .where(AuditEvent.quote_id == quote_id, AuditEvent.operation == "quote_approval.release")
            )
        ).one()  # fmt: skip
        receipt = await s.scalar(select(IdempotencyRecord.result_json).where(IdempotencyRecord.operation == "release_quote"))  # fmt: skip
    assert tuple(event[:4]) == (releaser.user_id, releaser.customer_id, 1, "success")
    data = event[4]
    assert (data["approval_request_id"], data["approver_id"]) == (released.approval_request_id, manager.user_id)  # fmt: skip
    assert data["approval_rule_version"] == "approval-v1.2"
    assert data["assessment_fingerprint"] == released.assessment_fingerprint
    assert receipt == released.to_json()
    for payload in (released.to_json(), data, receipt):
        assert not any(w in repr(payload) for w in SECRET_WORDS)

    # Customer-facing only now, and only for the owning customer.
    assert await customer_view(seed_database, world.customer_id, quote_id) == ("released", 1)
    assert await customer_view(seed_database, "CUST-OTHER", quote_id) is None

    # Retries: same key replays; another key, decision or a conflicting key changes nothing.
    again = await decide(runtime, releaser, release_quote, quote_id, "release-1")
    assert again.replayed and again.to_json() == released.to_json()
    assert await decide(runtime, releaser, release_quote, quote_id, "release-2") == "not_assessable"  # fmt: skip
    assert await act(runtime, releaser, release_quote, quote_id, "release-1", 2) == "idempotency_key_conflict"  # fmt: skip
    assert await decide(runtime, manager, reject_quote, quote_id, "reject-9", reason="late") == "not_assessable"  # fmt: skip
    assert await act(runtime, world, revise_quote, quote_id, "rev-9", 1, requested_delivery_date=DUE) == "revision_not_allowed"  # fmt: skip
    assert await state(seed_database, quote_id) == after


# --- Refusals ---------------------------------------------------------------------------------


async def test_release_refuses_unapproved_unauthorized_forged_and_agent_callers(runtime, world, manager, releaser, seed_database):  # fmt: skip
    draft = await submitted(runtime, world)
    quote_id = draft.quote_id
    # Pending, not approved: nothing to release or assess for release.
    assert await decide(runtime, releaser, release_quote, quote_id, "early") == "not_assessable"
    async with runtime.session() as s:
        with pytest.raises(DraftRefused, match="not_assessable"):
            await assess_release(s, releaser, quote_id, expected_version=1)
    attested = await attestation(seed_database, quote_id)
    assert (await decide(runtime, manager, approve_quote, quote_id, "approve-1", attestation=attested)).action == "approved"  # fmt: skip
    before = await state(seed_database, quote_id)

    # Grants are re-read: a claimed permission set grants nothing.
    forged = Principal(world.customer_id, frozenset({"release_quote", "approve_quote"}), user_id=world.user_id)  # fmt: skip
    assert await decide(runtime, forged, release_quote, quote_id, "r1") == "permission_denied"
    # An agent or Supervisor identity is not a business user and holds no grant.
    for agent_id in ("AGENT-QUOTATION", "SUPERVISOR"):
        agent = Principal(releaser.customer_id, frozenset({"release_quote"}), user_id=agent_id)
        assert await decide(runtime, agent, release_quote, quote_id, f"r-{agent_id}") == "permission_denied"  # fmt: skip
    # The releaser's own identity with another customer: no binding, no release.
    foreign = Principal("CUST-OTHER", frozenset(), user_id=releaser.user_id)
    assert await decide(runtime, foreign, release_quote, quote_id, "r2") == "permission_denied"
    # Stale expected version.
    assert await act(runtime, releaser, release_quote, quote_id, "r3", 2) == "stale_version"
    # Neither the owner nor the ordinary runtime role ever releases.
    for database in (seed_database, runtime):
        async with database.session() as s, s.begin():
            with pytest.raises(PrivilegedRuntimeRole):
                await release_quote(s, releaser, quote_id, expected_version=1, idempotency_key="other-role", correlation_id="c")  # fmt: skip
    assert await state(seed_database, quote_id) == before
    assert await customer_view(seed_database, world.customer_id, quote_id) is None


async def test_rejection_revision_resubmission_fresh_approval_and_release(runtime, world, manager, releaser, seed_database):  # fmt: skip
    draft = await submitted(runtime, world)
    quote_id = draft.quote_id
    reason = "Re-quote against the customer's requested delivery date."
    assert (await decide(runtime, manager, reject_quote, quote_id, "reject-1", reason=reason)).status == "rejected"  # fmt: skip
    # The rejected version never returns to review, approval or release.
    attested = await attestation(seed_database, quote_id)
    assert await decide(runtime, world, submit_for_review, quote_id, "submit-2") == "not_assessable"  # fmt: skip
    assert await decide(runtime, manager, approve_quote, quote_id, "approve-1", attestation=attested) == "not_assessable"  # fmt: skip
    assert await decide(runtime, releaser, release_quote, quote_id, "release-1") == "not_assessable"  # fmt: skip
    rejected = await state(seed_database, quote_id)

    # rejected -> revised_draft (K5): version 2; the rejection stays in history, the quote's
    # submission and rejection reason are cleared for a fresh review.
    revised = await act(runtime, world, revise_quote, quote_id, "rev-1", 1, requested_delivery_date=DUE)  # fmt: skip
    assert (revised.version, revised.previous_version, revised.status) == (2, 1, "revised_draft")
    after = await state(seed_database, quote_id)
    assert (after["status"], after["row_version"]) == ("revised_draft", rejected["row_version"] + 1)  # fmt: skip
    assert (after["submitted_by"], after["rejection_reason"]) == (None, None)
    assert rejected["rejection_reason"] == reason and after["history"] == rejected["history"]
    assert after["requests"] == rejected["requests"] == [("rejected", manager.user_id)]
    assert after["audits"] == [*rejected["audits"], "quote_version.create"]

    # The superseded version is stale for every binding step.
    assert await decide(runtime, releaser, release_quote, quote_id, "release-old") == "stale_version"  # fmt: skip
    assert await decide(runtime, world, submit_for_review, quote_id, "submit-old") == "stale_version"  # fmt: skip

    # Resubmission and a fresh human approval of version 2, then release by another manager.
    resubmitted = await act(runtime, world, submit_for_review, quote_id, "submit-3", 2)
    assert (resubmitted.action, resubmitted.version) == ("submitted", 2)
    stale = await attestation(seed_database, quote_id, 1)  # version 1's facts: no longer valid
    assert await act(runtime, manager, approve_quote, quote_id, "approve-old", 2, attestation=stale) == "attestation_mismatch"  # fmt: skip
    fresh = await attestation(seed_database, quote_id, 2)
    approval = await act(runtime, manager, approve_quote, quote_id, "approve-2", 2, attestation=fresh)  # fmt: skip
    assert (approval.action, approval.version) == ("approved", 2)
    assert approval.approval_request_id == resubmitted.approval_request_id  # the fresh request
    released = await act(runtime, releaser, release_quote, quote_id, "release-2", 2)
    assert (released.status, released.version) == ("released", 2)
    final = await state(seed_database, quote_id)
    assert final["requests"] == [("rejected", manager.user_id), ("approved", manager.user_id)]
    assert [h[0] for h in final["history"]] == ["submitted", "rejected", "submitted", "approved", "released"]  # fmt: skip
    assert await customer_view(seed_database, world.customer_id, quote_id) == ("released", 2)


# --- Races, retries and rollback --------------------------------------------------------------


async def test_concurrent_releases_with_different_keys_release_exactly_once(runtime, world, manager, releaser, seed_database):  # fmt: skip
    quote_id = await approved(runtime, world, manager, seed_database)
    before = await state(seed_database, quote_id)
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            decide(runtime, releaser, release_quote, quote_id, "race-a"),
            decide(runtime, releaser, release_quote, quote_id, "race-b"),
        ),
        RACE_TIMEOUT,
    )
    decisions = [o for o in outcomes if not isinstance(o, str)]
    assert len(decisions) == 1 and [o for o in outcomes if isinstance(o, str)] == ["not_assessable"]  # fmt: skip
    after = await state(seed_database, quote_id)
    assert [h[0] for h in after["history"]].count("released") == 1
    assert (after["row_version"], after["receipts"]) == (before["row_version"] + 1, before["receipts"] + 1)  # fmt: skip


async def test_concurrent_identical_retries_return_one_release(runtime, world, manager, releaser, seed_database):  # fmt: skip
    quote_id = await approved(runtime, world, manager, seed_database)
    before = await state(seed_database, quote_id)
    first, second = await asyncio.wait_for(
        asyncio.gather(
            decide(runtime, releaser, release_quote, quote_id, "same"),
            decide(runtime, releaser, release_quote, quote_id, "same"),
        ),
        RACE_TIMEOUT,
    )
    assert not isinstance(first, str) and not isinstance(second, str)
    assert first.to_json() == second.to_json() and [first.replayed, second.replayed].count(True) == 1  # fmt: skip
    after = await state(seed_database, quote_id)
    assert [h[0] for h in after["history"]].count("released") == 1
    assert after["receipts"] == before["receipts"] + 1


async def test_a_release_rolled_back_by_its_caller_leaves_nothing(runtime, decision, world, manager, releaser, seed_database):  # fmt: skip
    quote_id = await approved(runtime, world, manager, seed_database)
    before = await state(seed_database, quote_id)
    with pytest.raises(CallerAborts):
        async with decision.session() as s, s.begin():
            result = await release_quote(s, releaser, quote_id, expected_version=1, idempotency_key="rb", correlation_id="c")  # fmt: skip
            assert result.status == "released"
            raise CallerAborts
    assert await state(seed_database, quote_id) == before  # no history, audit, receipt or status
    # No orphaned receipt: the same key now performs the release for real.
    released = await decide(runtime, releaser, release_quote, quote_id, "rb")
    assert released.status == "released" and not released.replayed
