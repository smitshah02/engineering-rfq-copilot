"""K5 (Day 55) decision authority on the guarded test database.

Needs both provisioned test logins: ``TEST_APP_DATABASE_URL`` (K2 artifact) and
``TEST_DECISION_DATABASE_URL`` (K5 artifact; the login is a member of
``rfq_quote_decision``). Missing credentials fail, never skip. Every attack runs in a
session that is rolled back; the state is then re-read as the owner.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_quote_approval_database import attestation, decide, state, submitted
from test_quote_approval_database import decision as decision
from test_quote_approval_database import manager as manager
from test_quote_approval_database import releaser as releaser
from test_quote_drafts_concurrency import guarded
from test_quote_drafts_concurrency import world as world
from test_runtime_role_database import runtime as runtime

from rfq_copilot.database import (
    PrivilegedRuntimeRole,
    require_decision_runtime,
    require_restricted_runtime,
    runtime_role_violations,
)
from rfq_copilot.quote_approval import approve_quote, reject_quote, release_quote

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

DENIED, REFUSED = "42501", "23000"  # insufficient_privilege; K5 guard refusal

HISTORY = (
    "INSERT INTO business.approval_history (approval_id, quote_id, quote_version, action, "
    "actor_id, action_at, reason, approval_request_id) SELECT 'APH-K5-FORGED', quote_id, "
    "quote_version, '{action}', '{actor}', transaction_timestamp(), 'forged', "
    "approval_request_id FROM business.approval_requests WHERE quote_id = '{quote}' LIMIT 1"
)
RESOLVE = (
    "UPDATE business.approval_requests SET status = 'approved', resolved_by = '{actor}', "
    "resolved_at = {at} WHERE quote_id = '{quote}' AND status = 'pending'"
)
FORGED_REQUEST = (
    "INSERT INTO business.approval_requests (approval_request_id, quote_id, quote_version, "
    "requested_by, requested_at, required_role, status, approval_rule_version, resolved_by, "
    "resolved_at) SELECT 'APR-K5-FORGED', quote_id, quote_version, requested_by, "
    "transaction_timestamp(), required_role, 'approved', approval_rule_version, '{manager}', "
    "transaction_timestamp() FROM business.approval_requests WHERE quote_id = '{quote}' LIMIT 1"
)
APPROVE_QUOTE = (
    "UPDATE business.quotes SET status = 'approved', approved_version = 1, "
    "approver_id = '{manager}', approval_timestamp = transaction_timestamp(), "
    "row_version = row_version + 1 WHERE quote_id = '{quote}'"
)
RELEASE_QUOTE = (
    "UPDATE business.quotes SET status = 'released', released_by = '{actor}', "
    "released_at = transaction_timestamp(), row_version = row_version + 1 "
    "WHERE quote_id = '{quote}'"
)


async def attack(database, statement, sqlstate):
    async with database.session() as s:  # rolled back on exit
        await guarded(s)
        with pytest.raises(DBAPIError) as caught:
            await s.execute(text(statement))
        assert caught.value.orig.sqlstate == sqlstate, statement


async def test_only_the_decision_role_passes_the_decision_check(runtime, decision, seed_database):
    async with decision.session() as s:
        assert await runtime_role_violations(s, decision=True) == ()
        await require_decision_runtime(s)
        assert "decision_membership" in await runtime_role_violations(s)  # never "the app"
        with pytest.raises(PrivilegedRuntimeRole):
            await require_restricted_runtime(s)
    async with runtime.session() as s:
        assert await runtime_role_violations(s) == ()  # the app role is not a member
        assert "decision_membership" in await runtime_role_violations(s, decision=True)
        with pytest.raises(PrivilegedRuntimeRole):
            await require_decision_runtime(s)
    async with seed_database.session() as s:
        with pytest.raises(PrivilegedRuntimeRole):
            await require_decision_runtime(s)


@pytest.mark.parametrize(
    "template,sqlstate",
    [(RESOLVE.replace("{at}", "transaction_timestamp()"), DENIED),  # no resolution columns
     (APPROVE_QUOTE, DENIED),
     ("UPDATE business.quote_versions SET approval_valid = true WHERE quote_id = '{quote}'", DENIED),
     (HISTORY.replace("{action}", "approved"), REFUSED),  # INSERT granted (submit); K5 refuses
     (HISTORY.replace("{action}", "released"), REFUSED),
     (FORGED_REQUEST, REFUSED),  # a request born resolved
     ("SET ROLE rfq_quote_decision", DENIED)],
)  # fmt: skip
async def test_the_app_role_cannot_forge_a_decision_by_sql(runtime, world, manager, seed_database, template, sqlstate):  # fmt: skip
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    statement = template.format(quote=draft.quote_id, manager=manager.user_id, actor=manager.user_id)  # fmt: skip
    await attack(runtime, statement, sqlstate)
    assert await state(seed_database, draft.quote_id) == before


@pytest.mark.parametrize(
    "template,actor,sqlstate",
    [(RESOLVE.replace("{at}", "transaction_timestamp()"), "requester", REFUSED),  # requester, no grant
     (RESOLVE.replace("{at}", "transaction_timestamp() - interval '1 day'"), "manager", REFUSED),  # backdated
     (APPROVE_QUOTE, "manager", REFUSED),  # no resolved request, history or valid version
     ("UPDATE business.quote_versions SET approval_valid = true WHERE quote_id = '{quote}'", "manager", REFUSED),
     (HISTORY.replace("{action}", "approved"), "manager", REFUSED),  # request still pending
     (HISTORY.replace("{action}", "released"), "manager", REFUSED),  # nothing approved
     ("UPDATE business.quote_versions SET total_usd = 0 WHERE quote_id = '{quote}'", "manager", DENIED),
     ("INSERT INTO business.approval_requests (approval_request_id) VALUES ('APR-X')", "manager", DENIED),
     ("DELETE FROM business.approval_history WHERE quote_id = '{quote}'", "manager", DENIED)],
)  # fmt: skip
async def test_the_decision_role_cannot_bypass_the_rules_by_sql(runtime, decision, world, manager, seed_database, template, actor, sqlstate):  # fmt: skip
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    who = world.user_id if actor == "requester" else manager.user_id
    statement = template.format(quote=draft.quote_id, manager=manager.user_id, actor=who)
    await attack(decision, statement, sqlstate)
    assert await state(seed_database, draft.quote_id) == before


@pytest.mark.parametrize(
    "template,actor",
    [(HISTORY.replace("{action}", "released"), "approver"),  # the approver never releases
     (RELEASE_QUOTE, "approver"),
     (RELEASE_QUOTE, "releaser"),  # an entitled releaser, but no bound 'released' history row
     (HISTORY.replace("{action}", "released"), "requester")],  # no release_quote grant
)  # fmt: skip
async def test_a_release_by_sql_needs_a_separate_entitled_releaser_and_its_history(runtime, decision, world, manager, releaser, seed_database, template, actor):  # fmt: skip
    draft = await submitted(runtime, world)
    attested = await attestation(seed_database, draft.quote_id)
    approved = await decide(runtime, manager, approve_quote, draft.quote_id, "approve-1", attestation=attested)  # fmt: skip
    assert approved.status == "approved"
    before = await state(seed_database, draft.quote_id)
    who = {"approver": manager.user_id, "releaser": releaser.user_id, "requester": world.user_id}[actor]  # fmt: skip
    await attack(decision, template.format(quote=draft.quote_id, manager=manager.user_id, actor=who), REFUSED)  # fmt: skip
    assert await state(seed_database, draft.quote_id) == before


# --- Commit-time binding: a decision row never commits without its quotation transition ------


async def forged_commit(decision, seed_database, quote_id, statements):
    """Run each statement on a raw decision-role connection (driver errors unmasked): every
    statement must pass the BEFORE guards (the integrity boundary is reached), the commit
    must be refused, and another session never sees the uncommitted rows."""
    before = await state(seed_database, quote_id)
    async with decision.engine.connect() as connection:
        transaction = await connection.begin()
        for statement in statements:
            await connection.execute(text(statement))  # accepted row by row
        assert await state(seed_database, quote_id) == before  # isolation: nothing visible
        with pytest.raises(DBAPIError) as caught:
            await transaction.commit()
        assert caught.value.orig.sqlstate == REFUSED
    assert await state(seed_database, quote_id) == before  # rolled back: nothing persisted


async def test_a_released_history_row_without_the_release_never_commits(runtime, decision, world, manager, releaser, seed_database):  # fmt: skip
    draft = await submitted(runtime, world)
    attested = await attestation(seed_database, draft.quote_id)
    assert (await decide(runtime, manager, approve_quote, draft.quote_id, "approve-1", attestation=attested)).status == "approved"  # fmt: skip
    # Otherwise valid: decision role, approved quote and request, entitled releaser != approver.
    released_only = HISTORY.format(action="released", actor=releaser.user_id, quote=draft.quote_id)  # fmt: skip
    await forged_commit(decision, seed_database, draft.quote_id, [released_only])
    # The real release still works afterwards and records exactly one released row.
    released = await decide(runtime, releaser, release_quote, draft.quote_id, "release-1")
    assert released.status == "released"
    after = await state(seed_database, draft.quote_id)
    assert [h[0] for h in after["history"]].count("released") == 1


@pytest.mark.parametrize("action", ["approved", "rejected"])
async def test_a_resolved_request_and_history_row_without_the_decision_never_commit(runtime, decision, world, manager, seed_database, action):  # fmt: skip
    draft = await submitted(runtime, world)
    # Otherwise valid: entitled sales manager != requester/creator, resolved now, then the
    # matching history row; but no approval_valid flip and no quotation transition.
    resolve = RESOLVE.replace("'approved'", f"'{action}'").format(actor=manager.user_id, at="transaction_timestamp()", quote=draft.quote_id)  # fmt: skip
    history = HISTORY.format(action=action, actor=manager.user_id, quote=draft.quote_id)
    await forged_commit(decision, seed_database, draft.quote_id, [resolve, history])
    # The request is still pending, so the legitimate decision still commits.
    if action == "approved":
        attested = await attestation(seed_database, draft.quote_id)
        done = await decide(runtime, manager, approve_quote, draft.quote_id, "approve-1", attestation=attested)  # fmt: skip
    else:
        done = await decide(runtime, manager, reject_quote, draft.quote_id, "reject-1", reason="Re-quote.")  # fmt: skip
    assert done.status == action
    after = await state(seed_database, draft.quote_id)
    assert after["requests"] == [(action, manager.user_id)]
    assert [h[0] for h in after["history"]] == ["submitted", action]
