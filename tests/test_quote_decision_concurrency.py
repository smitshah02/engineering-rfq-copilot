"""Independent PostgreSQL transactions; barriers force both initial receipt misses.

State-changing on the existing guarded *_test fixture only. No new roles/grants/migrations.
"""

import asyncio

import pytest
from decision_races import hold_receipt_misses
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_quote_approval_database import attestation, database_for, drafted, state, submitted
from test_quote_approval_database import decision as decision
from test_quote_approval_database import manager as manager
from test_quote_drafts_concurrency import RACE_TIMEOUT
from test_quote_drafts_concurrency import world as world
from test_runtime_role_database import runtime as runtime

from rfq_copilot import quote_approval as approval
from rfq_copilot.quote_drafts import DraftRefused

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


async def perform(runtime, principal, action, quote_id, *, version=1, **payload):
    async with database_for(runtime, action).session() as session, session.begin():
        try:
            return await action(
                session,
                principal,
                quote_id,
                expected_version=version,
                idempotency_key="identical-race",
                correlation_id="test-decision-race",
                **payload,
            )
        except DraftRefused as refused:
            return refused.reason


@pytest.mark.parametrize("operation", [approval.SUBMIT, approval.APPROVE, approval.REJECT])
async def test_identical_concurrent_decisions_replay_once(
    runtime, world, manager, seed_database, monkeypatch, operation
):
    action = {
        approval.SUBMIT: approval.submit_for_review,
        approval.APPROVE: approval.approve_quote,
        approval.REJECT: approval.reject_quote,
    }[operation]
    if operation == approval.SUBMIT:
        draft = await drafted(runtime, world)
        principal, payload = world, {}
    else:
        draft = await submitted(runtime, world)
        principal = manager
        payload = (
            {"attestation": await attestation(seed_database, draft.quote_id)}
            if operation == approval.APPROVE
            else {"reason": "Please revise the quotation."}
        )
    before = await state(seed_database, draft.quote_id)
    arrivals = hold_receipt_misses(monkeypatch, operation)
    results = await asyncio.wait_for(
        asyncio.gather(
            perform(runtime, principal, action, draft.quote_id, **payload),
            perform(runtime, principal, action, draft.quote_id, **payload),
        ),
        RACE_TIMEOUT,
    )
    assert arrivals == [operation, operation]
    assert all(isinstance(result, approval.QuoteDecision) for result in results)
    assert results[0].to_json() == results[1].to_json()
    assert sorted(result.replayed for result in results) == [False, True]
    after = await state(seed_database, draft.quote_id)
    assert after["row_version"] == before["row_version"] + 1
    assert len(after["history"]) == len(before["history"]) + 1
    assert len(after["audits"]) == len(before["audits"]) + 1
    assert after["receipts"] == before["receipts"] + 1
    # A changed payload is a conflict even after the winning transition changed state.
    conflict = await perform(runtime, principal, action, draft.quote_id, version=2, **payload)
    assert conflict == "idempotency_key_conflict"
    assert await state(seed_database, draft.quote_id) == after


async def test_concurrent_different_rejection_payloads_conflict(
    runtime, world, manager, seed_database, monkeypatch
):
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    arrivals = hold_receipt_misses(monkeypatch, approval.REJECT)
    results = await asyncio.wait_for(
        asyncio.gather(
            perform(runtime, manager, approval.reject_quote, draft.quote_id, reason="First reason"),
            perform(runtime, manager, approval.reject_quote, draft.quote_id, reason="Other reason"),
        ),
        RACE_TIMEOUT,
    )
    assert arrivals == [approval.REJECT, approval.REJECT]
    assert sum(isinstance(r, approval.QuoteDecision) for r in results) == 1
    assert [r for r in results if isinstance(r, str)] == ["idempotency_key_conflict"]
    after = await state(seed_database, draft.quote_id)
    assert after["status"] == "rejected" and after["row_version"] == before["row_version"] + 1
    assert len(after["history"]) == len(before["history"]) + 1
    assert after["receipts"] == before["receipts"] + 1
