"""HTTP lifecycle integration on the existing isolated K2/K5 test-role fixtures.

Only test PostgreSQL is used when the user runs this module. No model or MCP calls.
"""

import asyncio

import pytest
from decision_races import hold_receipt_misses
from sqlalchemy import delete, select
from test_auth import trust as trust
from test_database_queries import other_customer
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_decision_api_database import approval_body, post, request, serving_config
from test_decision_api_database import http as http
from test_quote_approval_database import attestation, drafted, state
from test_quote_approval_database import decision as decision
from test_quote_approval_database import manager as manager
from test_quote_approval_database import releaser as releaser
from test_quote_drafts_concurrency import world as world
from test_rbac_database import bound
from test_runtime_role_database import runtime as runtime
from test_supply_agent import MONDAY

from rfq_copilot import quote_approval, quote_evidence
from rfq_copilot.database import Database
from rfq_copilot.models import (
    ApprovalHistory,
    RfqAssignment,
    RolePermission,
    User,
    UserCustomerBinding,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(autouse=True)
def dataset_horizon(monkeypatch):
    monkeypatch.setattr(quote_evidence, "server_horizon", lambda now: MONDAY)


async def submit(http, runtime, quote_id, *, actor="writer", **changes):
    body = {"expected_version": 1, "idempotency_key": "staff-submit", **changes}
    async with Database(serving_config(runtime)) as writer:
        http.main.state.write_database = writer
        return await request(
            http.main,
            "POST",
            f"/api/v1/staff/quotes/{quote_id}/submit-for-review",
            http.tokens[actor],
            json=body,
        )


async def actions(http, runtime, quote_id, actor="writer", version=1):
    async with Database(serving_config(runtime)) as writer:
        http.main.state.write_database = writer
        return await request(
            http.main,
            "GET",
            f"/api/v1/staff/quotes/{quote_id}/actions?expected_version={version}",
            http.tokens[actor],
        )


async def test_submit_http_receipt_history_retry_and_conflict(http, runtime, world, seed_database):
    draft = await drafted(runtime, world)
    response = await submit(http, runtime, draft.quote_id)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "quote_id": draft.quote_id,
        "version": 1,
        "action": "submitted",
        "status": "pending_approval",
        "replayed": False,
    }
    recorded = await state(seed_database, draft.quote_id)
    assert recorded["status"] == "pending_approval" and recorded["row_version"] == 2
    assert [h[0] for h in recorded["history"]] == ["submitted"]
    retry = await submit(http, runtime, draft.quote_id)
    assert retry.status_code == 200 and retry.json()["replayed"] is True
    assert await state(seed_database, draft.quote_id) == recorded
    conflict = await submit(http, runtime, draft.quote_id, expected_version=2)
    assert conflict.status_code == 409 and conflict.json()["detail"] == "idempotency_key_conflict"
    assert (await submit(http, runtime, draft.quote_id, idempotency_key="other")).status_code == 409
    assert await state(seed_database, draft.quote_id) == recorded


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"expected_version": 0, "idempotency_key": "s"},
        {"expected_version": True, "idempotency_key": "s"},
        {"expected_version": 1, "idempotency_key": " padded"},
        {"expected_version": 1, "idempotency_key": "s", "actor_id": "USER-004"},
    ],
)
async def test_submit_rejects_invalid_contract_without_mutation(
    http, runtime, world, seed_database, body
):
    draft = await drafted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    async with Database(serving_config(runtime)) as writer:
        http.main.state.write_database = writer
        response = await request(
            http.main,
            "POST",
            f"/api/v1/staff/quotes/{draft.quote_id}/submit-for-review",
            http.tokens["writer"],
            json=body,
        )
    assert response.status_code == 422
    assert await state(seed_database, draft.quote_id) == before


async def test_submit_auth_scope_assignment_and_stale_guards(http, runtime, world, seed_database):
    draft = await drafted(runtime, world)
    endpoint = f"/api/v1/staff/quotes/{draft.quote_id}/submit-for-review"
    before = await state(seed_database, draft.quote_id)
    for token in (None, "invalid"):
        assert (
            await request(
                http.main,
                "POST",
                endpoint,
                token,
                json={"expected_version": 1, "idempotency_key": "s"},
            )
        ).status_code == 401
    stale = await submit(http, runtime, draft.quote_id, expected_version=2)
    assert stale.status_code == 409 and stale.json()["detail"] == "stale_version"
    http.main.state.settings = http.main.state.settings.model_copy(
        update={"rfq_assignments": "required"}
    )
    assert (await submit(http, runtime, draft.quote_id)).status_code == 404
    async with seed_database.session() as session, session.begin():
        session.add(
            RfqAssignment(rfq_id=draft.rfq_id, user_id=world.user_id, assigned_by=world.user_id)
        )
    assert (await actions(http, runtime, draft.quote_id)).json()["actions"]["request_approval"][
        "allowed"
    ]
    # Removing the binding fails identity resolution before a mutation can start.
    async with seed_database.session() as session, session.begin():
        await session.execute(
            delete(UserCustomerBinding).where(UserCustomerBinding.user_id == world.user_id)
        )
    assert (await submit(http, runtime, draft.quote_id)).status_code == 403
    assert await state(seed_database, draft.quote_id) == before


async def test_concurrent_identical_http_submissions_persist_once(
    http, runtime, world, seed_database, monkeypatch
):
    draft = await drafted(runtime, world)
    hold_receipt_misses(monkeypatch, quote_approval.SUBMIT)
    async with Database(serving_config(runtime)) as writer:
        http.main.state.write_database = writer

        async def send():
            return await request(
                http.main,
                "POST",
                f"/api/v1/staff/quotes/{draft.quote_id}/submit-for-review",
                http.tokens["writer"],
                json={"expected_version": 1, "idempotency_key": "race"},
            )

        responses = await asyncio.wait_for(asyncio.gather(send(), send()), 60)
    assert [r.status_code for r in responses] == [200, 200]
    assert sorted(r.json()["replayed"] for r in responses) == [False, True]
    assert [h[0] for h in (await state(seed_database, draft.quote_id))["history"]] == ["submitted"]


async def test_submission_rolls_back_after_late_receipt_failure(
    http, runtime, world, seed_database, monkeypatch
):
    draft = await drafted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    original = quote_approval._finish

    async def broken(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("injected after all rows were flushed")

    monkeypatch.setattr(quote_approval, "_finish", broken)
    assert (await submit(http, runtime, draft.quote_id)).status_code == 500
    assert await state(seed_database, draft.quote_id) == before


async def test_actions_are_lock_capable_read_only_projection_and_decisions_stay_separate(
    http,
    runtime,
    world,
    seed_database,
):
    draft = await drafted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    projection = await actions(http, runtime, draft.quote_id)
    assert projection.status_code == 200, projection.text
    assert projection.json()["actions"]["request_approval"]["allowed"] is True
    assert projection.json()["actions"]["approve_quote"]["allowed"] is False
    assert projection.json()["attestation_context"] is None
    assert await state(seed_database, draft.quote_id) == before
    assert (await submit(http, runtime, draft.quote_id, actor="manager")).status_code == 200
    own = (await actions(http, runtime, draft.quote_id, "manager")).json()
    assert own["actions"]["approve_quote"] == {"allowed": False, "reason": "separation_of_duties"}
    independent = (await actions(http, runtime, draft.quote_id, "releaser")).json()
    assert independent["actions"]["approve_quote"]["allowed"] is True
    context = (await attestation(seed_database, draft.quote_id)).to_json()
    context.pop("no_contradictions")
    assert independent["attestation_context"] == context
    assert (
        await request(
            http.main,
            "POST",
            f"/api/v1/quote-decisions/{draft.quote_id}/approval",
            http.tokens["releaser"],
            json=await approval_body(seed_database, draft.quote_id),
        )
    ).status_code == 404
    assert (
        await post(
            http,
            draft.quote_id,
            "approval",
            actor="releaser",
            body=await approval_body(seed_database, draft.quote_id),
        )
    ).status_code == 200
    release = (await actions(http, runtime, draft.quote_id, "manager")).json()
    assert release["actions"]["release_quote"]["allowed"] is True
    own_release = (await actions(http, runtime, draft.quote_id, "releaser")).json()
    assert own_release["actions"]["release_quote"]["allowed"] is False
    async with seed_database.session() as session:
        assert (
            len(
                (
                    await session.scalars(
                        select(ApprovalHistory).where(ApprovalHistory.quote_id == draft.quote_id)
                    )
                ).all()
            )
            == 2
        )


async def test_submission_permission_is_database_derived(
    http, runtime, world, seed_database, trust
):
    draft = await drafted(runtime, world)
    async with seed_database.session() as session, session.begin():
        role = await session.scalar(select(User.role).where(User.user_id == world.user_id))
        await session.execute(
            delete(RolePermission).where(
                RolePermission.role == role, RolePermission.permission == "request_approval"
            )
        )
    http.tokens["writer"] = trust.mint(
        sub=http.subjects["writer"], role="sales_manager", permissions=["request_approval"]
    )
    before = await state(seed_database, draft.quote_id)
    assert (await submit(http, runtime, draft.quote_id)).status_code == 403
    assert await state(seed_database, draft.quote_id) == before


async def test_cross_customer_submission_is_concealed_as_missing(
    http, runtime, world, seed_database, seed_plan
):
    draft = await drafted(runtime, world)
    before = await state(seed_database, draft.quote_id)
    async with seed_database.session() as session, session.begin():
        user = await session.scalar(select(User).where(User.user_id == world.user_id))
        await session.execute(
            delete(UserCustomerBinding).where(UserCustomerBinding.user_id == world.user_id)
        )
        await bound(session, user, other_customer(seed_plan))
    foreign = await submit(http, runtime, draft.quote_id)
    missing = await submit(http, runtime, "QUOTE-MISSING")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert await state(seed_database, draft.quote_id) == before
