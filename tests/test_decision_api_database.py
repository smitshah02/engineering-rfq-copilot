"""Real signed JWT -> ASGI HTTP -> decision service -> K5 PostgreSQL transactions.

No identity/session/service overrides on normal paths. The reused fixtures commit synthetic
state, reapply existing test-role grant artifacts, and clean up ONLY rfq_copilot_test.
Failure-injection cases still execute real services/SQL and verify rollback/guard enforcement.
No service process, production signing material, LLM or MCP gateway is needed.
"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
import pytest_asyncio
from decision_races import hold_receipt_misses
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from test_auth import bearer
from test_auth import trust as trust
from test_database_queries import other_customer
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_quote_approval_database import attestation, decide, drafted, state, submitted
from test_quote_approval_database import decision as decision
from test_quote_approval_database import manager as manager
from test_quote_approval_database import releaser as releaser
from test_quote_drafts_concurrency import RACE_TIMEOUT, guarded
from test_quote_drafts_concurrency import world as world
from test_rbac_database import bound
from test_runtime_role_database import runtime as runtime

from rfq_copilot import quote_approval, quote_drafts
from rfq_copilot.api.app import create_app
from rfq_copilot.api.decision_app import create_decision_app
from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database, PrivilegedRuntimeRole
from rfq_copilot.models import User, UserCustomerBinding

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]
DECISION_FIELDS = {"quote_id", "version", "action", "status", "replayed"}


async def request(app, method, path, token=None, **kwargs):
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(
            method, path, headers=bearer(token) if token is not None else {}, **kwargs
        )


async def post(http, quote_id, action, *, actor="manager", body=None):
    return await request(
        http.app,
        "POST",
        f"/api/v1/quote-decisions/{quote_id}/{action}",
        http.tokens[actor],
        json=body,
    )


async def approval_body(seed_database, quote_id, key="http-approval"):
    return {
        "expected_version": 1,
        "idempotency_key": key,
        **(await attestation(seed_database, quote_id)).to_json(),
    }


def serving_config(database, *, decision=False):
    """After administrative fixture setup, give serving only its own role and metadata."""
    url = database.config.url
    selected = "TEST_DECISION_DATABASE_URL" if decision else "TEST_APP_DATABASE_URL"
    return DatabaseConfig.from_serving_env(
        "test",
        decision=decision,
        environ={
            selected: url.render_as_string(hide_password=False),
            "TEST_DATABASE_NAME": database.config.expected_test_database,
            "DEVELOPMENT_DATABASE_NAME": database.config.development_database,
            "DATABASE_HOST": url.host,
            "DATABASE_PORT": str(url.port or 5432),
        },
    )


@pytest_asyncio.fixture(loop_scope="module")
async def http(runtime, decision, world, manager, releaser, seed_database, trust):
    principals = {"writer": world, "manager": manager, "releaser": releaser}
    subjects = {name: f"auth|decision-http-{name}" for name in principals}
    async with seed_database.session() as session, session.begin():
        await guarded(session)
        for name, principal in principals.items():
            await session.execute(
                update(User)
                .where(User.user_id == principal.user_id)
                .values(authentication_subject=subjects[name])
            )
    settings = trust.settings(token_revocation="enforced")
    async with (
        Database(serving_config(runtime), read_only=True) as reader,
        Database(serving_config(decision, decision=True)) as writer,
    ):
        app = create_decision_app(settings, database=writer)
        main = create_app(settings, database=reader)
        assert app.dependency_overrides == {} and main.dependency_overrides == {}
        yield SimpleNamespace(
            app=app,
            main=main,
            principals=principals,
            subjects=subjects,
            tokens={name: trust.mint(sub=subject) for name, subject in subjects.items()},
        )


async def test_authenticated_approval_release_and_customer_projection(
    http, runtime, world, manager, seed_database
):
    draft = await submitted(runtime, world)
    quote_id = draft.quote_id
    path = f"/api/v1/quotes/{quote_id}"
    assert (await request(http.main, "GET", path, http.tokens["writer"])).status_code == 404
    approved = await post(
        http, quote_id, "approval", body=await approval_body(seed_database, quote_id)
    )
    assert approved.status_code == 200, approved.text
    assert set(approved.json()) == DECISION_FIELDS
    assert approved.json()["action"] == "approved" and not approved.json()["replayed"]
    assert (await request(http.main, "GET", path, http.tokens["writer"])).status_code == 404

    body = {"expected_version": 1, "idempotency_key": "http-release"}
    before = await state(seed_database, quote_id)
    self_release = await post(http, quote_id, "release", body=body)
    assert self_release.status_code == 409
    assert self_release.json() == {"detail": "separation_of_duties"}
    assert await state(seed_database, quote_id) == before
    released = await post(http, quote_id, "release", actor="releaser", body=body)
    assert released.status_code == 200, released.text
    assert set(released.json()) == DECISION_FIELDS and released.json()["action"] == "released"
    after = await state(seed_database, quote_id)
    replay = await post(http, quote_id, "release", actor="releaser", body=body)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert await state(seed_database, quote_id) == after
    visible = await request(http.main, "GET", path, http.tokens["writer"])
    assert visible.status_code == 200
    assert set(visible.json()) == {
        "quote_id",
        "rfq_id",
        "status",
        "released_at",
        "version",
        "currency",
        "total_usd",
        "expiry_date",
        "estimated_lead_business_days",
        "lines",
    }
    assert isinstance(visible.json()["total_usd"], str)
    for line in visible.json()["lines"]:
        assert set(line) == {"line_number", "part_id", "quantity", "line_total_usd"}
    assert [row[0] for row in after["history"]] == ["submitted", "approved", "released"]
    assert after["approver_id"] == manager.user_id and after["released"] is not None


async def test_authenticated_rejection_is_persisted_and_idempotent(
    http, runtime, world, seed_database
):
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    body = {"expected_version": 1, "idempotency_key": "http-reject", "reason": "Revise the terms."}
    response = await post(http, draft.quote_id, "rejection", body=body)
    assert response.status_code == 200, response.text
    assert set(response.json()) == DECISION_FIELDS and response.json()["status"] == "rejected"
    after = await state(seed_database, draft.quote_id)
    assert after["rejection_reason"] == body["reason"]
    assert after["row_version"] == before["row_version"] + 1
    assert len(after["history"]) == len(before["history"]) + 1
    assert after["receipts"] == before["receipts"] + 1
    again = await post(http, draft.quote_id, "rejection", body=body)
    assert again.status_code == 200 and again.json()["replayed"] is True
    conflict = await post(
        http, draft.quote_id, "rejection", body={**body, "reason": "Other terms."}
    )
    assert conflict.status_code == 409 and conflict.json()["detail"] == "idempotency_key_conflict"
    assert await state(seed_database, draft.quote_id) == after


async def test_an_existing_receipt_does_not_authorize_an_inactive_actor(
    http, runtime, world, manager, seed_database
):
    draft = await submitted(runtime, world)
    body = {"expected_version": 1, "idempotency_key": "revoked-retry", "reason": "Revise."}
    first = await post(http, draft.quote_id, "rejection", body=body)
    assert first.status_code == 200 and first.json()["replayed"] is False
    before = await state(seed_database, draft.quote_id)
    async with seed_database.session() as session, session.begin():
        await guarded(session)
        await session.execute(
            update(User).where(User.user_id == manager.user_id).values(active=False)
        )
    retry = await post(http, draft.quote_id, "rejection", body=body)
    assert retry.status_code == 401
    assert await state(seed_database, draft.quote_id) == before


async def test_missing_invalid_expired_and_wrong_audience_tokens_never_decide(
    http, trust, runtime, world, seed_database
):
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    valid = http.tokens["manager"]
    prefix, signature = valid.rsplit(".", 1)
    bad_signature = prefix + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    tokens = [
        None,
        "not-a-token",
        bad_signature,
        trust.mint(sub=http.subjects["manager"], aud="wrong-audience"),
        trust.mint(sub=http.subjects["manager"], iat=1, exp=2),
        trust.mint(sub="auth|unknown-decision-user"),
    ]
    body = await approval_body(seed_database, draft.quote_id)
    for token in tokens:
        response = await request(
            http.app, "POST", f"/api/v1/quote-decisions/{draft.quote_id}/approval", token, json=body
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "Authentication required."}
    assert await state(seed_database, draft.quote_id) == before


async def test_database_permissions_override_forged_jwt_claims(
    http, trust, runtime, world, seed_database
):
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    token = trust.mint(
        sub=http.subjects["writer"],
        role="sales_manager",
        permissions=["approve_quote"],
        customer_id=world.customer_id,
    )
    response = await request(
        http.app,
        "POST",
        f"/api/v1/quote-decisions/{draft.quote_id}/approval",
        token,
        json=await approval_body(seed_database, draft.quote_id),
    )
    assert response.status_code == 403
    assert await state(seed_database, draft.quote_id) == before


async def test_cross_customer_decision_is_not_found(
    http, runtime, world, manager, seed_database, seed_plan
):
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    async with seed_database.session() as session, session.begin():
        await guarded(session)
        await session.execute(
            update(UserCustomerBinding)
            .where(UserCustomerBinding.user_id == manager.user_id)
            .values(revoked_at=func.now())
        )
        user = await session.scalar(select(User).where(User.user_id == manager.user_id))
        await bound(session, user, other_customer(seed_plan))
    response = await post(
        http, draft.quote_id, "approval", body=await approval_body(seed_database, draft.quote_id)
    )
    assert response.status_code == 404 and response.json() == {"detail": "not_found"}
    assert await state(seed_database, draft.quote_id) == before


async def test_requester_cannot_approve_or_reject_their_own_submission(
    http, runtime, world, manager, seed_database
):
    draft = await drafted(runtime, world)
    submitted_by_manager = await decide(
        runtime, manager, quote_approval.submit_for_review, draft.quote_id, "manager-submits"
    )
    assert submitted_by_manager.action == "submitted"
    before = await state(seed_database, draft.quote_id)
    bodies = {
        "approval": await approval_body(seed_database, draft.quote_id),
        "rejection": {"expected_version": 1, "idempotency_key": "self-reject", "reason": "No."},
    }
    for action, body in bodies.items():
        response = await post(http, draft.quote_id, action, body=body)
        assert response.status_code == 409 and response.json()["detail"] == "separation_of_duties"
    assert await state(seed_database, draft.quote_id) == before


async def test_stale_versions_invalid_attestation_and_forged_bodies_change_nothing(
    http, runtime, world, seed_database
):
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    good = await approval_body(seed_database, draft.quote_id)
    cases = [
        ({**good, "expected_version": 2}, 409, "stale_version"),
        ({**good, "rfq_source_sha256": "0" * 64}, 409, "attestation_mismatch"),
        ({**good, "no_contradictions": False}, 422, None),
    ]
    cases.extend(
        ({k: v for k, v in good.items() if k != missing}, 422, None)
        for missing in (
            "rfq_source_sha256",
            "no_contradictions",
            "expected_version",
            "idempotency_key",
        )
    )
    cases.extend(
        ({**good, field: "forged"}, 422, None)
        for field in ("actor_id", "customer_id", "role", "approval_valid", "history")
    )
    for body, status, detail in cases:
        response = await post(http, draft.quote_id, "approval", body=body)
        assert response.status_code == status
        if detail:
            assert response.json()["detail"] == detail
    assert await state(seed_database, draft.quote_id) == before


async def test_missing_persisted_evidence_is_not_binding_approval(
    http, runtime, world, seed_database, monkeypatch
):
    # Legitimate legacy partial version: no trigger disabling, forged calculation or schema edit.
    monkeypatch.setattr(quote_drafts, "WORKFLOW_RULE_VERSION", "workflow-v1.1")
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    response = await post(
        http,
        draft.quote_id,
        "approval",
        body={
            "expected_version": 1,
            "idempotency_key": "legacy-approval",
            "rfq_source_sha256": "a" * 64,
            "no_contradictions": True,
        },
    )
    assert response.status_code == 409 and response.json()["detail"] == "not_approvable"
    assert await state(seed_database, draft.quote_id) == before


@pytest.mark.parametrize("action", ["approval", "rejection"])
async def test_concurrent_identical_http_decisions_return_one_result(
    http, runtime, world, seed_database, monkeypatch, action
):
    draft = await submitted(runtime, world)
    body = (
        await approval_body(seed_database, draft.quote_id)
        if action == "approval"
        else {"expected_version": 1, "idempotency_key": "http-race", "reason": "Revise."}
    )
    operation = quote_approval.APPROVE if action == "approval" else quote_approval.REJECT
    before = await state(seed_database, draft.quote_id)
    arrivals = hold_receipt_misses(monkeypatch, operation)
    responses = await asyncio.wait_for(
        asyncio.gather(
            post(http, draft.quote_id, action, body=body),
            post(http, draft.quote_id, action, body=body),
        ),
        RACE_TIMEOUT,
    )
    assert arrivals == [operation, operation]
    assert all(response.status_code == 200 for response in responses)
    bodies = [response.json() for response in responses]
    assert sorted(b["replayed"] for b in bodies) == [False, True]
    assert {k: v for k, v in bodies[0].items() if k != "replayed"} == {
        k: v for k, v in bodies[1].items() if k != "replayed"
    }
    assert all(set(b) == DECISION_FIELDS for b in bodies)
    after = await state(seed_database, draft.quote_id)
    assert after["row_version"] == before["row_version"] + 1
    assert len(after["history"]) == len(before["history"]) + 1
    assert len(after["audits"]) == len(before["audits"]) + 1
    assert after["receipts"] == before["receipts"] + 1


async def test_http_commit_failure_rolls_back_and_the_key_remains_retryable(
    http, runtime, world, seed_database, monkeypatch
):
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    body = await approval_body(seed_database, draft.quote_id)

    async def refuse_commit(session):
        raise SQLAlchemyError("synthetic-private-commit-diagnostic")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", refuse_commit)
        failed = await post(http, draft.quote_id, "approval", body=body)
    assert failed.status_code == 503 and failed.json() == {"detail": "Service unavailable."}
    assert await state(seed_database, draft.quote_id) == before
    retry = await post(http, draft.quote_id, "approval", body=body)
    assert retry.status_code == 200 and retry.json()["replayed"] is False


async def test_k5_refuses_forged_history_during_http_decision_and_rolls_back(
    http, runtime, world, seed_database, monkeypatch
):
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    body = await approval_body(seed_database, draft.quote_id)
    original = quote_approval._history

    def forged_history(*args, **kwargs):
        row = original(*args, **kwargs)
        if row.action == "approved":
            row.actor_id = world.user_id
        return row

    with monkeypatch.context() as patch:
        patch.setattr(quote_approval, "_history", forged_history)
        refused = await post(http, draft.quote_id, "approval", body=body)
    assert refused.status_code == 503 and refused.json() == {"detail": "Service unavailable."}
    assert await state(seed_database, draft.quote_id) == before
    retry = await post(http, draft.quote_id, "approval", body=body)
    assert retry.status_code == 200 and retry.json()["replayed"] is False


@pytest.mark.parametrize("credential", ["owner", "application"])
async def test_decision_http_refuses_nondecision_database_credentials(
    http, trust, runtime, decision, world, seed_database, credential
):
    draft = await submitted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    source = seed_database if credential == "owner" else runtime
    config = replace(source.config, runtime=False, decision=True)
    async with Database(config) as unsafe:
        app = create_decision_app(trust.settings(), database=unsafe)
        response = await request(
            app,
            "POST",
            f"/api/v1/quote-decisions/{draft.quote_id}/approval",
            http.tokens["manager"],
            json=await approval_body(seed_database, draft.quote_id),
        )
    assert response.status_code == 503 and response.json() == {"detail": "Service unavailable."}
    assert await state(seed_database, draft.quote_id) == before


@pytest.mark.parametrize("credential", ["owner", "decision"])
async def test_serving_sessions_check_authority_and_allow_explicit_transactions(
    runtime, decision, seed_database, credential
):
    async with runtime.session() as session:
        assert not session.in_transaction()
        async with session.begin():
            await guarded(session)
    async with decision.session() as session:
        assert not session.in_transaction()
        async with session.begin():
            await guarded(session)
    source = seed_database if credential == "owner" else decision
    config = replace(source.config, runtime=True, decision=False)
    async with Database(config, read_only=True) as unsafe:
        with pytest.raises(PrivilegedRuntimeRole):
            async with unsafe.session():
                pytest.fail("Privileged authority escaped into an application session")
