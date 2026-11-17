"""Day 54D on the guarded test database (K5 on K4 on K3 on K2).

Drafts and submissions run on the restricted runtime role (``TEST_APP_DATABASE_URL``; the
``runtime`` fixture applies the K2 marked grant block); approvals, rejections and releases
run on the K5 decision role (``TEST_DECISION_DATABASE_URL``; the ``decision`` fixture
applies the K5 marked grant block; the login must already be a member of
``rfq_quote_decision``). The owner credential only inspects state, provisions bindings and
is shown to be refused for decisions. The ``world`` fixture truncates everything written.
"""

import asyncio
from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_database_queries import customer_id
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_day21_rbac_database import bound, one_active_user_per_role
from test_quote_drafts_concurrency import RACE_TIMEOUT, guarded
from test_quote_drafts_concurrency import world as world
from test_quote_drafts_database import RFQ_ID, args, refused
from test_runtime_role_database import runtime as runtime

from rfq_copilot.api.auth import Principal
from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database, PrivilegedRuntimeRole
from rfq_copilot.models import (
    ApprovalHistory,
    ApprovalRequest,
    AuditEvent,
    IdempotencyRecord,
    Quote,
    QuoteVersion,
    User,
)
from rfq_copilot.quote_approval import (
    ContextAttestation,
    approve_quote,
    reject_quote,
    submit_for_review,
)
from rfq_copilot.quote_approval_policy import assess_quote_approval
from rfq_copilot.quote_drafts import DraftRefused, create_quote_draft
from rfq_copilot.repositories import eligible_customers

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

REFUSED, DENIED = "23000", "42501"  # K4/K5 guard refusal; insufficient_privilege
ROOT = Path(__file__).resolve().parents[1]
K5_ARTIFACT = ROOT / "docs" / "database" / "k5_decision_roles.sql"
DECISIONS = frozenset({"approve_quote", "reject_quote", "release_quote"})
_DECISION: dict[str, Database] = {}  # the decision Database of the running test


def decision_grant_block(login: str) -> list[str]:
    """The K5 artifact's marked grants, with ``:"decision"`` bound to the test login."""
    body = K5_ARTIFACT.read_text().split("-- BEGIN K5 DECISION GRANTS\n")[1]
    body = body.split("-- END K5 DECISION GRANTS")[0]
    return [s.strip().replace(':"decision"', login) for s in body.split(";") if s.strip()]


@pytest_asyncio.fixture(loop_scope="module")
async def decision(runtime, world, seed_database):
    """The K5 decision role on the guarded test database (re-granted after the upgrade)."""
    config = DatabaseConfig.from_env("test", env_file=ROOT / ".env", decision=True)
    quote = seed_database.engine.dialect.identifier_preparer.quote
    async with seed_database.session() as s, s.begin():
        await guarded(s)
        for statement in decision_grant_block(quote(config.url.username)):
            await s.execute(text(statement))
    async with Database(config) as database:
        _DECISION["db"] = database
        try:
            yield database
        finally:
            _DECISION.clear()


def database_for(runtime, action):
    """Decisions on the decision role, everything else (draft, revise, submit) on runtime."""
    return _DECISION["db"] if action.__name__ in DECISIONS else runtime


async def sales_manager(seed_database, seed_plan, *, other_than=None):
    """An active sales_manager bound to the RFQ's customer (committed; world truncates)."""
    mine = customer_id(seed_plan)
    async with seed_database.session() as s, s.begin():
        await guarded(s)
        if other_than is None:
            user = next(u for u in await one_active_user_per_role(s) if u.role == "sales_manager")  # fmt: skip
        else:
            user = await s.scalar(
                select(User)
                .where(User.role == "sales_manager", User.active.is_(True), User.user_id != other_than)
                .order_by(User.user_id)
                .limit(1)
            )  # fmt: skip
        if mine not in await eligible_customers(s, user):
            await bound(s, user, mine)
        user_id = user.user_id  # read inside the session: rollback on exit expires ORM state
    return Principal(mine, frozenset(), user_id=user_id)


@pytest_asyncio.fixture(loop_scope="module")
async def manager(runtime, decision, world, seed_database, seed_plan):
    """The approving sales_manager."""
    return await sales_manager(seed_database, seed_plan)


@pytest_asyncio.fixture(loop_scope="module")
async def releaser(manager, seed_database, seed_plan):
    """Another sales_manager: K5 (Day 55 policy) refuses a release by the approver."""
    return await sales_manager(seed_database, seed_plan, other_than=manager.user_id)


async def drafted(runtime, world, key="draft-1", **changes):
    """A server-dated (54C) draft unless ``changes`` names a caller horizon."""
    async with runtime.session() as s, s.begin():
        await guarded(s)
        return await create_quote_draft(
            s, world, RFQ_ID, idempotency_key=key, correlation_id="c", **changes
        )


async def decide(runtime, principal, action, quote_id, key, **kwargs):
    """One step in its own session (decision role for decisions); a refusal is its reason."""
    async with database_for(runtime, action).session() as s, s.begin():
        try:
            return await action(
                s, principal, quote_id, expected_version=1, idempotency_key=key,
                correlation_id="c", **kwargs,
            )  # fmt: skip
        except DraftRefused as caught:
            assert await s.scalar(text("SELECT 1")) == 1  # the transaction survives
            return caught.reason
        except DBAPIError as error:  # test-only: Database.session would sanitize it away
            driver = getattr(error.orig, "orig", None)
            pytest.fail(f"{getattr(error.orig, 'sqlstate', None)} {getattr(driver, 'constraint_name', None)}")  # fmt: skip


async def submitted(runtime, world, key="draft-1", **changes):
    draft = await drafted(runtime, world, key, **changes)
    decision = await decide(runtime, world, submit_for_review, draft.quote_id, "submit-1")
    assert decision.action == "submitted"
    return draft


async def attestation(seed_database, quote_id, version=1, **changes):
    async with seed_database.session() as s:
        row = (
            await s.execute(
                select(QuoteVersion.rfq_source_sha256, QuoteVersion.requested_delivery_date)
                .where(QuoteVersion.quote_id == quote_id, QuoteVersion.version == version)
            )
        ).one()  # fmt: skip
    facts = {"rfq_source_sha256": row[0], "requested_delivery_date": row[1], "no_contradictions": True}  # fmt: skip
    return ContextAttestation(**{**facts, **changes})


async def state(seed_database, quote_id):
    """Everything a decision may touch, read as the owner."""
    async with seed_database.session() as s:
        quote = (await s.execute(select(Quote).where(Quote.quote_id == quote_id))).scalar_one()
        valid = await s.scalar(
            select(QuoteVersion.approval_valid).where(
                QuoteVersion.quote_id == quote_id, QuoteVersion.version == 1
            )
        )
        requests = (
            await s.execute(
                select(ApprovalRequest.status, ApprovalRequest.resolved_by)
                .where(ApprovalRequest.quote_id == quote_id)
                .order_by(ApprovalRequest.requested_at)
            )
        ).all()
        history = (
            await s.execute(
                select(ApprovalHistory.action, ApprovalHistory.actor_id, ApprovalHistory.reason)
                .where(ApprovalHistory.quote_id == quote_id)
                .order_by(ApprovalHistory.action_at, ApprovalHistory.action)
            )
        ).all()
        audits = (
            await s.scalars(
                select(AuditEvent.operation)
                .where(AuditEvent.quote_id == quote_id)
                .order_by(AuditEvent.occurred_at, AuditEvent.operation)
            )
        ).all()
        receipts = await s.scalar(select(func.count()).select_from(IdempotencyRecord))
        # Plain values only, built while the session is open (rollback expires ORM state).
        return {
            "status": quote.status,
            "row_version": quote.row_version,
            "submitted_by": quote.submitted_by,
            "approved_version": quote.approved_version,
            "approver_id": quote.approver_id,
            "approved_at": quote.approval_timestamp is not None,
            "rejection_reason": quote.rejection_reason,
            "released": quote.released_at,
            "approval_valid": valid,
            "requests": [tuple(r) for r in requests],
            "history": [tuple(h) for h in history],
            "audits": audits,
            "receipts": receipts,
        }


# --- The approved path ------------------------------------------------------------------------


async def test_submit_then_approve_records_one_bound_decision(runtime, world, manager, seed_database):  # fmt: skip
    draft = await drafted(runtime, world)
    quote_id = draft.quote_id
    assert draft.rule_versions[-1] == "workflow-v1.2"  # complete, server-dated version

    decision = await decide(runtime, world, submit_for_review, quote_id, "submit-1")
    assert (decision.action, decision.status, decision.version) == ("submitted", "pending_approval", 1)  # fmt: skip
    pending = await state(seed_database, quote_id)
    assert (pending["status"], pending["row_version"], pending["submitted_by"]) == ("pending_approval", 2, world.user_id)  # fmt: skip
    assert pending["requests"] == [("pending", None)] and pending["approval_valid"] is False
    assert [h[:2] for h in pending["history"]] == [("submitted", world.user_id)]
    assert pending["audits"] == ["quote_draft.create", "quote_approval.submit"]
    # Day 53 never assesses a submitted version (a pending request exists).
    async with runtime.session() as s:
        await refused("not_assessable", assess_quote_approval(s, manager, quote_id, expected_version=1))  # fmt: skip

    attested = await attestation(seed_database, quote_id)
    approved = await decide(runtime, manager, approve_quote, quote_id, "approve-1", attestation=attested)  # fmt: skip
    assert (approved.action, approved.status, approved.actor_id) == ("approved", "approved", manager.user_id)  # fmt: skip
    assert approved.approval_request_id == decision.approval_request_id and not approved.replayed
    after = await state(seed_database, quote_id)
    assert (after["status"], after["row_version"], after["approved_version"]) == ("approved", 3, 1)  # fmt: skip
    assert (after["approver_id"], after["approved_at"], after["approval_valid"]) == (manager.user_id, True, True)  # fmt: skip
    assert after["requests"] == [("approved", manager.user_id)] and after["rejection_reason"] is None  # fmt: skip
    assert [h[:2] for h in after["history"]] == [("submitted", world.user_id), ("approved", manager.user_id)]  # fmt: skip
    assert after["history"][1][2].startswith("approved; attested ")
    assert after["audits"][-1] == "quote_approval.approve" and after["receipts"] == 3
    text_ = repr(approved)
    assert "cost" not in text_ and "margin" not in text_

    # Idempotent: the same key replays; a new key or another decision changes nothing.
    again = await decide(runtime, manager, approve_quote, quote_id, "approve-1", attestation=attested)  # fmt: skip
    assert again.replayed and again.to_json() == approved.to_json()
    assert await decide(runtime, manager, approve_quote, quote_id, "approve-2", attestation=attested) == "not_assessable"  # fmt: skip
    assert await decide(runtime, manager, reject_quote, quote_id, "reject-1", reason="late") == "not_assessable"  # fmt: skip
    other = await attestation(seed_database, quote_id, rfq_source_sha256="1" * 64)
    assert await decide(runtime, manager, approve_quote, quote_id, "approve-1", attestation=other) == "idempotency_key_conflict"  # fmt: skip
    assert await state(seed_database, quote_id) == after


async def test_reject_records_the_reason_and_closes_the_request(runtime, world, manager, seed_database):  # fmt: skip
    draft = await submitted(runtime, world)
    quote_id = draft.quote_id
    rejected = await decide(runtime, manager, reject_quote, quote_id, "reject-1", reason="Margin too thin for this customer.")  # fmt: skip
    assert (rejected.action, rejected.status) == ("rejected", "rejected")
    after = await state(seed_database, quote_id)
    assert (after["status"], after["row_version"], after["approved_version"]) == ("rejected", 3, None)  # fmt: skip
    assert after["rejection_reason"] == "Margin too thin for this customer."
    assert after["requests"] == [("rejected", manager.user_id)] and after["approval_valid"] is False  # fmt: skip
    assert after["history"][-1] == ("rejected", manager.user_id, "Margin too thin for this customer.")  # fmt: skip
    assert after["audits"][-1] == "quote_approval.reject"
    same = await decide(runtime, manager, reject_quote, quote_id, "reject-1", reason="Margin too thin for this customer.")  # fmt: skip
    assert same.replayed and same.to_json() == rejected.to_json()
    assert await decide(runtime, manager, reject_quote, quote_id, "reject-1", reason="other") == "idempotency_key_conflict"  # fmt: skip
    attested = await attestation(seed_database, quote_id)
    assert await decide(runtime, manager, approve_quote, quote_id, "approve-1", attestation=attested) == "not_assessable"  # fmt: skip
    assert await state(seed_database, quote_id) == after


# --- Refusals: identity, role, separation, context, attestation, runtime role -----------------


async def test_the_owner_credential_never_decides(runtime, world, manager, seed_database):
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    attested = await attestation(seed_database, draft.quote_id)
    async with seed_database.session() as s, s.begin():
        with pytest.raises(PrivilegedRuntimeRole):
            await approve_quote(s, manager, draft.quote_id, expected_version=1, idempotency_key="owner", correlation_id="c", attestation=attested)  # fmt: skip
        with pytest.raises(PrivilegedRuntimeRole):
            await reject_quote(s, manager, draft.quote_id, expected_version=1, idempotency_key="owner", correlation_id="c", reason="no")  # fmt: skip
    assert await state(seed_database, draft.quote_id) == before


async def test_only_a_separate_sales_manager_decides(runtime, world, manager, seed_database):
    draft = await drafted(runtime, world)
    quote_id = draft.quote_id
    attested = await attestation(seed_database, quote_id)
    # Nothing is pending yet: no decision, whoever asks.
    assert await decide(runtime, manager, approve_quote, quote_id, "a0", attestation=attested) == "not_assessable"  # fmt: skip
    # The manager may submit (dataset grant) but then never decides their own request.
    submitted_by_manager = await decide(runtime, manager, submit_for_review, quote_id, "s1")
    assert submitted_by_manager.action == "submitted"
    pending = await state(seed_database, quote_id)
    assert await decide(runtime, manager, approve_quote, quote_id, "a1", attestation=attested) == "separation_of_duties"  # fmt: skip
    assert await decide(runtime, manager, reject_quote, quote_id, "r1", reason="no") == "separation_of_duties"  # fmt: skip
    # The drafter holds no approve_quote/reject_quote grant; a stranger is not a user.
    assert await decide(runtime, world, approve_quote, quote_id, "a2", attestation=attested) == "permission_denied"  # fmt: skip
    assert await decide(runtime, world, reject_quote, quote_id, "r2", reason="no") == "permission_denied"  # fmt: skip
    stranger = Principal(manager.customer_id, frozenset({"approve_quote"}), user_id="USER-NOBODY")
    assert await decide(runtime, stranger, approve_quote, quote_id, "a3", attestation=attested) == "permission_denied"  # fmt: skip
    foreign = Principal("CUST-OTHER", frozenset(), user_id=manager.user_id)
    assert await decide(runtime, foreign, approve_quote, quote_id, "a4", attestation=attested) == "permission_denied"  # fmt: skip
    assert await state(seed_database, quote_id) == pending


async def test_the_attestation_must_match_the_version_evidence(runtime, world, manager, seed_database):  # fmt: skip
    draft = await submitted(runtime, world)
    quote_id = draft.quote_id
    before = await state(seed_database, quote_id)
    forged = await attestation(seed_database, quote_id, rfq_source_sha256="0" * 64)
    assert await decide(runtime, manager, approve_quote, quote_id, "a1", attestation=forged) == "attestation_mismatch"  # fmt: skip
    other_date = await attestation(seed_database, quote_id, requested_delivery_date=date(2027, 3, 1))  # fmt: skip
    assert await decide(runtime, manager, approve_quote, quote_id, "a2", attestation=other_date) == "attestation_mismatch"  # fmt: skip
    assert await state(seed_database, quote_id) == before


async def test_a_caller_dated_context_is_submittable_but_never_approved(runtime, world, manager, seed_database):  # fmt: skip
    # Caller-dated horizon: legacy context.
    draft = await submitted(runtime, world, horizon_start=args()["horizon_start"])
    before = await state(seed_database, draft.quote_id)
    attested = await attestation(seed_database, draft.quote_id)
    assert await decide(runtime, manager, approve_quote, draft.quote_id, "a1", attestation=attested) == "not_approvable"  # fmt: skip
    assert await state(seed_database, draft.quote_id) == before
    # Rejection of such a version is still a valid human decision.
    assert (await decide(runtime, manager, reject_quote, draft.quote_id, "r1", reason="Re-evaluate under a current horizon.")).action == "rejected"  # fmt: skip


# --- The K4 guards: no lifecycle write without its bound row ----------------------------------


@pytest.mark.parametrize(
    "statement,sqlstate",
    # K5: the app role holds no decision column (42501); what it holds, the guard refuses.
    [("UPDATE business.quotes SET status = 'approved', approved_version = 1, approver_id = '{manager}', approval_timestamp = now(), row_version = row_version + 1 WHERE quote_id = '{quote}'", DENIED),
     ("UPDATE business.quotes SET status = 'rejected', rejection_reason = 'x', row_version = row_version + 1 WHERE quote_id = '{quote}'", REFUSED),
     ("UPDATE business.quotes SET status = 'released', released_by = '{manager}', released_at = now(), row_version = row_version + 1 WHERE quote_id = '{quote}'", DENIED),
     ("UPDATE business.quotes SET submitted_by = '{manager}', row_version = row_version + 1 WHERE quote_id = '{quote}'", REFUSED),
     ("UPDATE business.quote_versions SET approval_valid = true WHERE quote_id = '{quote}'", DENIED),
     ("UPDATE business.approval_requests SET requested_by = '{manager}' WHERE quote_id = '{quote}'", DENIED),  # column never granted
     ("UPDATE business.approval_requests SET status = 'approved' WHERE quote_id = '{quote}'", DENIED),
     ("DELETE FROM business.approval_requests WHERE quote_id = '{quote}'", DENIED),
     ("DELETE FROM business.approval_history WHERE quote_id = '{quote}'", DENIED),
     ("UPDATE business.approval_history SET reason = 'x' WHERE quote_id = '{quote}'", DENIED),
     ("UPDATE business.quotes SET created_by = '{manager}' WHERE quote_id = '{quote}'", DENIED)],
)  # fmt: skip
async def test_forged_lifecycle_writes_are_refused(runtime, world, manager, seed_database, statement, sqlstate):  # fmt: skip
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    async with runtime.session() as s:  # rolled back on exit
        await guarded(s)
        with pytest.raises(DBAPIError) as caught:
            await s.execute(text(statement.format(quote=draft.quote_id, manager=manager.user_id)))
        assert caught.value.orig.sqlstate == sqlstate
    assert await state(seed_database, draft.quote_id) == before


async def test_concurrent_decisions_resolve_exactly_once(runtime, world, manager, seed_database):
    draft = await submitted(runtime, world)
    quote_id = draft.quote_id
    attested = await attestation(seed_database, quote_id)
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            decide(runtime, manager, approve_quote, quote_id, "race-a", attestation=attested),
            decide(runtime, manager, reject_quote, quote_id, "race-b", reason="no"),
        ),
        RACE_TIMEOUT,
    )
    decisions = [o for o in outcomes if not isinstance(o, str)]
    assert len(decisions) == 1 and {o for o in outcomes if isinstance(o, str)} <= {"not_assessable", "not_pending", "stale_version"}  # fmt: skip
    after = await state(seed_database, quote_id)
    assert after["status"] == decisions[0].status and len(after["requests"]) == 1
    assert [h[0] for h in after["history"]].count(decisions[0].action) == 1
    assert after["approval_valid"] is (decisions[0].action == "approved")
