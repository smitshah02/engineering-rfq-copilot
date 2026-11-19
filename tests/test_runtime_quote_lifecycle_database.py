"""Real service-created runtime drafts through human lifecycle and the full seed audit.

Reuses the guarded committed workflow world, audited binding CLI, K2 and K5 grant
artifacts. No seeded row is changed, no model/MCP runs, no development DB is opened.
"""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from test_auth import trust as trust
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_decision_api_database import request
from test_quote_approval_database import attestation, decision_grant_block
from test_quote_drafts_concurrency import guarded
from test_runtime_role_database import grant_block
from test_supply_agent import MONDAY
from test_workflow_drafter_database import drafter_db, drain, ready, runtime_audit
from test_workflow_drafter_database import world as world
from test_workflow_worker_database import committed as committed
from test_workflow_worker_database import regrant

from rfq_copilot import identity_admin, quote_approval, quote_evidence, quote_versions
from rfq_copilot.api.app import create_app
from rfq_copilot.api.auth import Principal
from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database
from rfq_copilot.integrity import IntegrityVerificationError, audit_integrity
from rfq_copilot.models import User, WorkflowRun
from rfq_copilot.repositories import get_released_quotation

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def dataset_horizon(monkeypatch):
    monkeypatch.setattr(quote_evidence, "server_horizon", lambda now: MONDAY)


@asynccontextmanager
async def ordinary(seed_database):
    config = await regrant(seed_database, grant_block)
    async with Database(config) as database:
        yield database


@asynccontextmanager
async def decision_database(seed_database):
    from sqlalchemy import text

    config = DatabaseConfig.from_env("test", env_file=ROOT / ".env", decision=True)
    login = seed_database.engine.dialect.identifier_preparer.quote(config.url.username)
    async with seed_database.session() as session, session.begin():
        await guarded(session)
        for statement in decision_grant_block(login):
            await session.execute(text(statement))
    async with Database(config) as database:
        yield database


@pytest_asyncio.fixture(loop_scope="module")
async def lifecycle(world, seed_database):
    rfq_id = await ready(seed_database, world)
    async with drafter_db(seed_database) as database:
        await drain(database)
    async with seed_database.session() as session, session.begin():
        quote_id = await session.scalar(
            select(WorkflowRun.quote_id).where(WorkflowRun.rfq_id == rfq_id)
        )
        assert quote_id is not None
        customer = world["a"][1]
        managers = (
            await session.scalars(
                select(User)
                .where(User.active.is_(True), User.role == "sales_manager")
                .order_by(User.user_id)
                .limit(2)
            )
        ).all()
        editor = await session.scalar(
            select(User)
            .where(User.active.is_(True), User.role == "estimator")
            .order_by(User.user_id)
            .limit(1)
        )
        assert len(managers) == 2 and editor is not None
        for user in [*managers, editor]:
            await identity_admin.grant_binding(
                session,
                operator=world["operator"],
                user=user.user_id,
                customer=customer,
                reason="onboarding",
                execute=True,
            )
        principals = [Principal(customer, user_id=user.user_id) for user in [*managers, editor]]
    return quote_id, principals


async def act(database, principal, action, quote_id, *, version=1, **extra):
    async with database.session() as session, session.begin():
        return await action(
            session,
            principal,
            quote_id,
            expected_version=version,
            idempotency_key=f"{action.__name__}-{version}",
            correlation_id="runtime-human-test",
            **extra,
        )


async def customer_quote(seed_database, trust, world, quote_id, who="a"):
    async with seed_database.session() as session:
        subject = await session.scalar(
            select(User.authentication_subject).where(User.user_id == world[who][0])
        )
    async with ordinary(seed_database) as database:
        app = create_app(trust.settings(permission_policy="v3"), database=database)
        return await request(app, "GET", f"/api/v1/quotes/{quote_id}", trust.mint(sub=subject))


async def test_full_runtime_submit_approve_release_and_customer_boundary(
    lifecycle, seed_database, seed_plan, trust, world
):
    quote_id, (requester, approver, _) = lifecycle
    assert (await runtime_audit(seed_database, seed_plan))["status"] == "PASS"
    assert (await customer_quote(seed_database, trust, world, quote_id)).status_code == 404
    async with ordinary(seed_database) as database:
        first = await act(database, requester, quote_approval.submit_for_review, quote_id)
        again = await act(database, requester, quote_approval.submit_for_review, quote_id)
        assert first.action == "submitted" and again.replayed
    assert (await runtime_audit(seed_database, seed_plan))["status"] == "PASS"
    assert (await customer_quote(seed_database, trust, world, quote_id)).status_code == 404
    async with seed_database.session() as session:
        assert (
            await get_released_quotation(session, quote_id, customer_id=requester.customer_id)
            is None
        )
    async with decision_database(seed_database) as database:
        context = await attestation(seed_database, quote_id)
        await act(database, approver, quote_approval.approve_quote, quote_id, attestation=context)
        assert (
            await act(
                database, approver, quote_approval.approve_quote, quote_id, attestation=context
            )
        ).replayed
        assert (await runtime_audit(seed_database, seed_plan))["status"] == "PASS"
        assert (await customer_quote(seed_database, trust, world, quote_id)).status_code == 404
        async with seed_database.session() as session:
            assert (
                await get_released_quotation(session, quote_id, customer_id=requester.customer_id)
                is None
            )
        await act(database, requester, quote_approval.release_quote, quote_id)
        assert (await act(database, requester, quote_approval.release_quote, quote_id)).replayed
    assert (await runtime_audit(seed_database, seed_plan))["status"] == "PASS"
    visible = await customer_quote(seed_database, trust, world, quote_id)
    assert visible.status_code == 200 and visible.json()["version"] == 1
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
    assert (await customer_quote(seed_database, trust, world, quote_id, "b")).status_code == 404
    async with seed_database.session() as session:
        released = await get_released_quotation(
            session, quote_id, customer_id=requester.customer_id
        )
        assert released is not None
        assert await get_released_quotation(session, quote_id, customer_id="CUST-NOT-MINE") is None


async def test_runtime_rejection_revision_resubmission_and_retry(
    lifecycle, seed_database, seed_plan, trust, world
):
    from datetime import timedelta

    quote_id, (requester, approver, editor) = lifecycle
    async with ordinary(seed_database) as database:
        await act(database, requester, quote_approval.submit_for_review, quote_id)
    async with decision_database(seed_database) as database:
        await act(
            database,
            approver,
            quote_approval.reject_quote,
            quote_id,
            reason="Review delivery context",
        )
        assert (
            await act(
                database,
                approver,
                quote_approval.reject_quote,
                quote_id,
                reason="Review delivery context",
            )
        ).replayed
    assert (await runtime_audit(seed_database, seed_plan))["status"] == "PASS"
    assert (await customer_quote(seed_database, trust, world, quote_id)).status_code == 404
    async with ordinary(seed_database) as database:
        revision = await act(
            database,
            editor,
            quote_versions.revise_quote,
            quote_id,
            requested_delivery_date=MONDAY + timedelta(days=120),
        )
        assert revision.version == 2
        assert (
            await act(
                database,
                editor,
                quote_versions.revise_quote,
                quote_id,
                requested_delivery_date=MONDAY + timedelta(days=120),
            )
        ).replayed
        await act(database, requester, quote_approval.submit_for_review, quote_id, version=2)
    assert (await runtime_audit(seed_database, seed_plan))["status"] == "PASS"
    visible = await customer_quote(seed_database, trust, world, quote_id)
    assert visible.status_code == 404  # revised version is pending, still internal
    async with decision_database(seed_database) as database:
        await act(
            database,
            approver,
            quote_approval.approve_quote,
            quote_id,
            version=2,
            attestation=await attestation(seed_database, quote_id, version=2),
        )
        await act(database, requester, quote_approval.release_quote, quote_id, version=2)
    assert (await runtime_audit(seed_database, seed_plan))["status"] == "PASS"
    visible = await customer_quote(seed_database, trust, world, quote_id)
    assert visible.status_code == 200 and visible.json()["version"] == 2


@pytest.mark.parametrize(
    "corruption", ["missing_audit", "assessment", "commercial_total", "source_digest", "extra_line"]
)
async def test_full_audit_detects_corruption_without_writing_database(
    lifecycle,
    seed_database,
    seed_plan,
    monkeypatch,
    corruption,
):
    from copy import deepcopy

    import rfq_copilot.integrity as integrity
    from rfq_copilot.runtime_integrity import verify_runtime

    quote_id, (requester, _, _) = lifecycle
    async with ordinary(seed_database) as database:
        await act(database, requester, quote_approval.submit_for_review, quote_id)

    # Corrupt only the auditor's extracted row copy. No triggers/constraints are disabled
    # and no protected row is edited to arrange a corruption case.
    def altered(extras, users, overlay, *, authority):
        extras_copy = deepcopy(extras)
        if corruption == "missing_audit":
            extras_copy["audit_events"] = [
                e
                for e in extras_copy["audit_events"]
                if e["operation"] != quote_approval.AUDIT[quote_approval.SUBMIT]
            ]
        elif corruption == "assessment":
            receipt = next(
                r
                for r in extras_copy["idempotency_records"]
                if r["operation"] == quote_approval.SUBMIT
            )
            receipt["result_json"]["assessment_fingerprint"] = "0" * 64
        elif corruption in {"commercial_total", "source_digest"}:
            version = next(v for v in extras_copy["quote_versions"] if v["quote_id"] == quote_id)
            version["total_usd" if corruption == "commercial_total" else "rfq_source_sha256"] = (
                version["total_usd"] + 1 if corruption == "commercial_total" else "0" * 64
            )
        else:
            line = deepcopy(extras_copy["quote_line_items"][0])
            line["quote_version"] = 99
            extras_copy["quote_line_items"].append(line)
        return verify_runtime(extras_copy, users, overlay, authority=authority)

    if corruption == "source_digest":
        import rfq_copilot.runtime_quote_lifecycle as module

        original = module.verify_evidence

        async def evidence_copy(session, extras):
            changed = deepcopy(extras)
            changed["quote_versions"][0]["rfq_source_sha256"] = "0" * 64
            await original(session, changed)

        monkeypatch.setattr(module, "verify_evidence", evidence_copy)
    else:
        # audit_integrity imports verify_runtime locally; replace the exported function.
        import rfq_copilot.runtime_integrity as module

        monkeypatch.setattr(module, "verify_runtime", altered)
    async with seed_database.session() as session:
        before = await integrity.fingerprint(session)
    with pytest.raises(IntegrityVerificationError):
        async with seed_database.session() as session:
            await audit_integrity(session, seed_database.config, seed_plan, runtime=True)
    async with seed_database.session() as session:
        assert await integrity.fingerprint(session) == before
