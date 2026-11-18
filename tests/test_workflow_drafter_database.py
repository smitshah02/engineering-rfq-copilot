"""Checkpoint A4: the quotation drafter on COMMITTED test data (guarded ``rfq_copilot_test``).

The world is ``test_workflow_worker_database.committed`` (seed, approval-v1.2, rbac-policy-v4,
an operator, customers A/B/C, service identities for A and B, the queue activation) plus the
``workflow-v1.2`` rule new drafts record. RFQs are stored through the real
``create_customer_rfq`` with RFQ-0041's two development lines (draftable by the deterministic
evaluation); a run reaches ``completed`` / ``ready_for_draft`` through the real A3 claim and
fenced settlement (``workflow_worker.claim`` / ``settle``), only the Supervisor itself is
skipped: A4 never trusts that state anyway, it recomputes.

Roles: the reviewed grant blocks of ``a1_workflow_role.sql`` and ``a4_drafter_role.sql`` are
applied in turn to the existing ``rfq_copilot_app_test`` role (no dedicated roles exist in
the local cluster), and every session passes the live ``require_*_runtime`` check. The
evaluation horizon is pinned to the dataset's first capacity week so the tests do not depend
on today's date; every other input is the production path. No MCP, model or network.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_quote_drafts_concurrency import guarded
from test_runtime_role_database import grant_block
from test_supply_agent import MONDAY
from test_workflow_worker_database import PART, regrant, run_of, worker_block
from test_workflow_worker_database import committed as committed

from rfq_copilot import identity_admin, quote_drafts, workflow_drafter, workflow_worker
from rfq_copilot import quote_evidence as k3
from rfq_copilot import workflow_queue as wq
from rfq_copilot.api.auth import Principal
from rfq_copilot.api.schemas.rfq import RfqSubmissionRequest
from rfq_copilot.database import (
    Database,
    DatabaseError,
    PrivilegedRuntimeRole,
    runtime_role_violations,
)
from rfq_copilot.integrity import IntegrityVerificationError, audit_integrity
from rfq_copilot.models import (
    AuditEvent,
    IdempotencyRecord,
    PricingCalculation,
    Quote,
    QuoteLineItem,
    QuoteVersion,
    RolePermission,
    WorkflowRun,
)
from rfq_copilot.repositories import get_released_quotation
from rfq_copilot.rfq_submission import create_customer_rfq
from rfq_copilot.seeding.rules import register_rule_version
from rfq_copilot.services.evaluation import evaluate_stored_rfq
from rfq_copilot.workflow_drafter import Drafter, DrafterSettings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

ROOT = Path(__file__).resolve().parents[1]
DRAFTER_ARTIFACT = ROOT / "docs" / "database" / "a4_drafter_role.sql"
GROUND_TRUTH = ROOT / "data/rfq_synthetic_data/evaluation/ground_truth/development/RFQ-0041.json"
PARTS = json.loads(GROUND_TRUTH.read_text())["expected_extraction"]  # read only
FAST = dict(lease_seconds=30.0, poll_seconds=0.05, shutdown_grace_seconds=0.2)


def drafter_block(role: str) -> list[str]:
    body = DRAFTER_ARTIFACT.read_text().split("-- BEGIN A4 DRAFTER GRANTS\n")[1]
    body = body.split("-- END A4 DRAFTER GRANTS")[0]
    return [s.strip().replace(':"drafter"', role) for s in body.split(";") if s.strip()]


@asynccontextmanager
async def as_role(seed_database, block, **flag):
    """The stand-in role with exactly ``block``'s grants, checked live as ``flag``'s role."""
    config = await regrant(seed_database, block)
    async with Database(replace(config, runtime=False, **flag)) as database:
        yield database


@pytest_asyncio.fixture(loop_scope="module")
async def world(committed, seed_database):
    async with seed_database.session() as s, s.begin():
        await guarded(s)
        await register_rule_version(s, "workflow-v1.1")
        await register_rule_version(s, "workflow-v1.2")
    return committed


@pytest.fixture(autouse=True)
def dataset_horizon(monkeypatch):
    """create_quote_draft dates the evaluation with k3.server_horizon(now) (54C); pin it to the
    dataset's first capacity Monday so the outcome never depends on the calendar."""
    monkeypatch.setattr(k3, "server_horizon", lambda now: MONDAY)


def settings_(**over) -> DrafterSettings:
    return DrafterSettings(**{"worker_id": "d-1", **FAST, **over})


async def submit(seed_database, world, who="a", parts=PARTS, key=None) -> str:
    user, customer_id = world[who]
    request = RfqSubmissionRequest.model_validate({"parts": list(parts), "raw_request": "See lines."})  # fmt: skip
    async with seed_database.session() as s, s.begin():
        receipt, _ = await create_customer_rfq(
            s, Principal(customer_id, frozenset(), user_id=user), request,
            key or f"k-{asyncio.get_running_loop().time()}", enqueue=True,
        )  # fmt: skip
    return receipt.rfq_id


async def ready(seed_database, world, who="a", parts=PARTS, actor=None) -> str:
    """A run made ready by the real A3 claim and fenced settlement (Supervisor skipped)."""
    rfq_id = await submit(seed_database, world, who, parts)
    async with as_role(seed_database, worker_block, workflow=True) as worker:
        a3 = workflow_worker.WorkerSettings(worker_id="w-a3")
        claimed = await workflow_worker.claim(worker, a3)
        assert claimed.rfq_id == rfq_id
        service = actor or {"a": "U-SVC-A", "b": "U-SVC-B"}.get(who)
        assert await workflow_worker.settle(worker, a3, claimed, service, "completed", "ready_for_draft")  # fmt: skip
    return rfq_id


@asynccontextmanager
async def drafter_db(seed_database):
    async with as_role(seed_database, drafter_block, drafter=True) as database:
        yield database


async def drain(database, settings=None) -> list[int]:
    drafter = Drafter(database, settings or settings_())
    counts = []
    while (n := await drafter.run_once()) and len(counts) < 10:
        counts.append(n)
    return counts


async def rows(seed_database, model, *where):
    async with seed_database.session() as s:
        statement = select(func.count()).select_from(model)
        return await s.scalar(statement.where(*where) if where else statement)


async def draft_trail(seed_database, rfq_id) -> list[dict]:
    async with seed_database.session() as s:
        events = (await s.scalars(select(AuditEvent).where(AuditEvent.operation == wq.DRAFT_OPERATION, AuditEvent.rfq_id == rfq_id))).all()  # fmt: skip
        return sorted((e.event_data | {"actor": e.actor_id, "outcome": e.outcome} for e in events), key=lambda d: d["claim_generation"])  # fmt: skip


async def runtime_audit(seed_database, seed_plan):
    async with seed_database.session() as s:
        return await audit_integrity(s, seed_database.config, seed_plan, runtime=True)


# --- the drafter role (A4.2) ---------------------------------------------------------------------


async def test_drafter_role_holds_exactly_the_reviewed_privileges(world, seed_database):
    async with drafter_db(seed_database) as database:
        async with database.session() as s:
            await guarded(s)
            assert await runtime_role_violations(s, drafter=True) == ()
            assert "insert_privileges" in await runtime_role_violations(s, workflow=True)
            assert "update_privileges" in await runtime_role_violations(s)  # not the API role
            await s.rollback()
            for statement in (
                "UPDATE business.quotes SET status = status",  # no revision/submit/decision
                "UPDATE business.quote_versions SET is_current = is_current",
                "UPDATE business.rfqs SET status = status",
                "UPDATE business.rfq_line_items SET quantity = quantity",
                "UPDATE business.user_customer_bindings SET revoked_at = revoked_at",
                "UPDATE orchestration.workflow_runs SET rfq_id = rfq_id",
                "UPDATE orchestration.workflow_runs SET attempt_count = attempt_count",
                "INSERT INTO business.approval_history DEFAULT VALUES",
                "INSERT INTO business.approval_requests DEFAULT VALUES",
                "INSERT INTO business.users DEFAULT VALUES",
                "INSERT INTO business.rfqs DEFAULT VALUES",
                "INSERT INTO orchestration.workflow_runs DEFAULT VALUES",
                "DELETE FROM business.quotes",
            ):
                with pytest.raises(DBAPIError, match="permission denied"):
                    async with s.begin_nested():
                        await s.execute(text(statement))
                await s.rollback()


async def test_drafter_refuses_every_other_credential(world, seed_database):
    for block in (worker_block, grant_block):  # the A1 worker's and the API role's grants
        config = await regrant(seed_database, block)
        async with Database(replace(config, runtime=False, drafter=True)) as wrong:
            with pytest.raises(PrivilegedRuntimeRole, match="restricted drafter role"):
                async with wrong.session():
                    pass
    async with Database(replace(seed_database.config, drafter=True)) as owner:
        with pytest.raises(PrivilegedRuntimeRole, match="restricted drafter role"):
            async with owner.session():
                pass


# --- the happy path, provenance, visibility (A4.3) ------------------------------------------------


async def test_ready_run_becomes_exactly_one_linked_internal_draft(world, seed_database):
    rfq_id = await ready(seed_database, world)
    async with drafter_db(seed_database) as database:
        assert await drain(database) == [1]
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.last_error, run.lease_owner) == ("completed", "draft_created", None, None)  # fmt: skip
    assert (run.attempt_count, run.draft_attempt_count, run.claim_generation) == (1, 1, 2)
    async with seed_database.session() as s:
        quote = await s.scalar(select(Quote).where(Quote.quote_id == run.quote_id))
        version = await s.scalar(select(QuoteVersion).where(QuoteVersion.quote_id == run.quote_id))
        receipt = await s.scalar(select(IdempotencyRecord).where(IdempotencyRecord.operation == "create_quote_draft"))  # fmt: skip
        created = await s.scalar(select(AuditEvent).where(AuditEvent.operation == "quote_draft.create"))  # fmt: skip
        snapshot = (
            (quote.rfq_id, quote.status, quote.created_by, quote.current_version, quote.released_at),
            (version.snapshot_completeness, version.approval_valid, version.workflow_rule_version, version.created_by),
            (receipt.idempotency_key, receipt.actor_id, receipt.correlation_id),
            (created.actor_id, created.quote_id, created.correlation_id),
        )  # fmt: skip
    assert snapshot == (
        (rfq_id, "draft", "U-SVC-A", 1, None),
        ("complete", False, quote_drafts.WORKFLOW_RULE_VERSION, "U-SVC-A"),
        (f"wf-{run.workflow_run_id}", "U-SVC-A", rfq_id),
        ("U-SVC-A", run.quote_id, rfq_id),
    )
    (event,) = await draft_trail(seed_database, rfq_id)
    assert event == {"workflow_run_id": str(run.workflow_run_id), "draft_attempt": 1, "claim_generation": 2,
                     "status": "completed", "disposition": "draft_created", "error": None,
                     "quote_id": run.quote_id, "actor": "U-SVC-A", "outcome": "success"}  # fmt: skip
    assert "total" not in json.dumps(event) and "cost" not in json.dumps(event)  # codes only


async def test_the_draft_is_the_deterministic_recomputation(world, seed_database):
    rfq_id = await ready(seed_database, world)
    async with drafter_db(seed_database) as database:
        await drain(database)
    run = await run_of(seed_database, rfq_id)
    async with seed_database.session() as s:
        version = await s.scalar(select(QuoteVersion).where(QuoteVersion.quote_id == run.quote_id))
        fresh = await evaluate_stored_rfq(
            s, rfq_id, customer_id=run.customer_id, **await quote_drafts.pinned_evaluation_rules(s),
            horizon_start=MONDAY, requested_delivery_date=None, contradictions=(),
            inventory_snapshot=None, capacity_snapshot=None,
        )  # fmt: skip
        total = await s.scalar(select(func.sum(QuoteLineItem.line_total_usd)).where(QuoteLineItem.quote_id == run.quote_id))  # fmt: skip
        record = await s.scalar(select(PricingCalculation).where(PricingCalculation.calculation_record_id == version.calculation_record_id))  # fmt: skip
        assert version.evaluation_fingerprint == fresh.evaluation_fingerprint
        assert (
            version.total_usd == total == record.quote_total_usd == fresh.quotation.quote_total_usd
        )


async def test_customer_cannot_see_the_internal_draft(world, seed_database):
    rfq_id = await ready(seed_database, world)
    async with drafter_db(seed_database) as database:
        await drain(database)
    run = await run_of(seed_database, rfq_id)
    async with seed_database.session() as s:  # the only customer quote path: human release
        assert await get_released_quotation(s, run.quote_id, customer_id=run.customer_id) is None


# --- refusals and configuration (A4.4) ------------------------------------------------------------


async def test_a_ready_run_that_is_not_draftable_is_refused_without_a_quote(world, seed_database):
    rfq_id = await ready(seed_database, world, parts=[{**PART, "thickness_mm": "5"}])  # A3 forged
    async with drafter_db(seed_database) as database:
        assert await drain(database) == [1]  # final: never retried
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.quote_id, run.draft_attempt_count) == ("blocked", "draft_refused", None, 1)  # fmt: skip
    assert run.last_error in workflow_drafter.REFUSED
    assert await rows(seed_database, Quote, Quote.rfq_id == rfq_id) == 0


async def test_a_quote_under_another_key_is_never_linked(world, seed_database):
    rfq_id = await ready(seed_database, world)
    _, customer_id = world["a"]
    async with seed_database.session() as s, s.begin():  # another path drafted it first
        principal = await workflow_worker.service_principal(s, customer_id, workflow_drafter.DRAFT_PERMISSIONS)  # fmt: skip
        other = await quote_drafts.create_quote_draft(s, principal, rfq_id, idempotency_key="manual-1", correlation_id=rfq_id)  # fmt: skip
    async with drafter_db(seed_database) as database:
        await drain(database)
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.last_error, run.quote_id) == ("blocked", "draft_refused", "quote_exists", None)  # fmt: skip
    assert await rows(seed_database, Quote, Quote.rfq_id == rfq_id) == 1 and other.quote_id


@pytest.mark.parametrize("change", ["revoked_binding", "no_draft_grant", "no_service"])
async def test_identity_binding_and_permission_faults_are_configuration_errors(world, seed_database, change):  # fmt: skip
    who = "c" if change == "no_service" else "a"
    rfq_id = await ready(seed_database, world, who=who, actor="U-SVC-A" if who == "a" else None)
    _, customer_id = world[who]
    async with seed_database.session() as s, s.begin():
        if change == "revoked_binding":
            await identity_admin.revoke_binding(s, operator=world["operator"], user="U-SVC-A", customer=customer_id, reason="security", execute=True)  # fmt: skip
        elif change == "no_draft_grant":
            await s.execute(delete(RolePermission).where(RolePermission.role == "rfq_workflow_agent", RolePermission.permission == "create_quote_draft"))  # fmt: skip
    try:
        async with drafter_db(seed_database) as database:
            await drain(database)
    finally:
        if change == "no_draft_grant":
            async with seed_database.session() as s, s.begin():
                await s.execute(insert(RolePermission).values(role="rfq_workflow_agent", permission="create_quote_draft", allowed=True))  # fmt: skip
    run = await run_of(seed_database, rfq_id)
    expected = {"revoked_binding": "service_identity_unavailable", "no_draft_grant": "service_permissions_invalid", "no_service": "service_identity_unavailable"}  # fmt: skip
    assert (run.status, run.disposition, run.last_error, run.quote_id) == ("blocked", "configuration_error", expected[change], None)  # fmt: skip
    assert await rows(seed_database, Quote, Quote.rfq_id == rfq_id) == 0


async def test_unregistered_workflow_rule_fails_closed(committed, seed_database):
    rfq_id = await ready(seed_database, committed)  # workflow-v1.2 deliberately not registered
    async with drafter_db(seed_database) as database:
        await drain(database)
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.last_error) == ("blocked", "configuration_error", "rules_untrusted")  # fmt: skip
    assert await rows(seed_database, Quote, Quote.rfq_id == rfq_id) == 0


# --- concurrency, fencing, crash and retry (A4.3/A4.4) --------------------------------------------


async def test_two_drafters_never_draft_the_same_run(world, seed_database):
    rfq_ids = [await ready(seed_database, world) for _ in range(2)]
    async with drafter_db(seed_database) as database:
        one, two = Drafter(database, settings_(worker_id="d-one")), Drafter(database, settings_(worker_id="d-two", concurrency=2))  # fmt: skip
        claimed = await asyncio.gather(one.run_once(), two.run_once())
    assert sum(claimed) == 2
    for rfq_id in rfq_ids:
        run = await run_of(seed_database, rfq_id)
        assert (run.status, run.disposition, run.claim_generation, run.draft_attempt_count) == ("completed", "draft_created", 2, 1)  # fmt: skip
        assert await rows(seed_database, Quote, Quote.rfq_id == rfq_id) == 1


async def test_the_draft_transaction_row_lock_excludes_every_reclaim(world, seed_database, monkeypatch):  # fmt: skip
    rfq_id = await ready(seed_database, world)
    inside, release_draft = asyncio.Event(), asyncio.Event()
    real = workflow_drafter.create_quote_draft

    async def held(*args, **kwargs):
        inside.set()
        await release_draft.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(workflow_drafter, "create_quote_draft", held)
    async with drafter_db(seed_database) as database:
        first = settings_(worker_id="d-holder", lease_seconds=0.5)
        claimed = await workflow_drafter.claim(database, first)
        task = asyncio.create_task(workflow_drafter.process(database, first, claimed))
        await inside.wait()
        await asyncio.sleep(0.7)  # the lease is past, but the run is row-locked
        assert await workflow_drafter.claim(database, settings_(worker_id="d-thief")) is None
        release_draft.set()
        assert await task == "completed"
    assert (await run_of(seed_database, rfq_id)).claim_generation == 2


async def test_expired_unreclaimed_lease_is_renewed_but_a_reclaimed_one_is_fenced(world, seed_database):  # fmt: skip
    renewed_id, fenced_id = await ready(seed_database, world), await ready(seed_database, world)
    async with drafter_db(seed_database) as database:
        slow = settings_(worker_id="d-slow", lease_seconds=0.5)
        first = await workflow_drafter.claim(database, slow)  # the older run
        await asyncio.sleep(0.7)
        assert await workflow_drafter.process(database, slow, first) == "completed"  # renewed
        stale = await workflow_drafter.claim(database, slow)
        await asyncio.sleep(0.7)
        fresh = settings_(worker_id="d-fresh")
        newer = await workflow_drafter.claim(database, fresh)
        assert (newer.workflow_run_id, newer.claim_generation, newer.draft_attempt_count) == (stale.workflow_run_id, stale.claim_generation + 1, 2)  # fmt: skip
        assert await workflow_drafter.draft(database, slow, stale, []) == "stale"  # writes nothing
        assert not await workflow_drafter.settle(database, slow, stale, None, "blocked", "draft_refused", "not_draftable")  # fmt: skip
        assert await workflow_drafter.process(database, fresh, newer) == "completed"
    assert (await run_of(seed_database, renewed_id)).disposition == "draft_created"
    run = await run_of(seed_database, fenced_id)
    assert (run.disposition, run.claim_generation, run.draft_attempt_count) == ("draft_created", 3, 2)  # fmt: skip
    assert [e["claim_generation"] for e in await draft_trail(seed_database, fenced_id)] == [3]


async def test_a_crash_before_commit_leaves_nothing_and_is_recovered(world, seed_database, monkeypatch):  # fmt: skip
    rfq_id = await ready(seed_database, world)
    real = workflow_drafter.create_quote_draft

    async def crash(*args, **kwargs):
        await real(*args, **kwargs)  # the draft rows are flushed ...
        raise RuntimeError("process died")  # ... and never committed

    monkeypatch.setattr(workflow_drafter, "create_quote_draft", crash)
    async with drafter_db(seed_database) as database:
        short = settings_(worker_id="d-crash", lease_seconds=0.5)
        claimed = await workflow_drafter.claim(database, short)
        with pytest.raises(RuntimeError):
            await workflow_drafter.process(database, short, claimed)
        assert await rows(seed_database, Quote, Quote.rfq_id == rfq_id) == 0
        assert await rows(seed_database, IdempotencyRecord, IdempotencyRecord.operation == "create_quote_draft") == 0  # fmt: skip
        assert (await run_of(seed_database, rfq_id)).status == "drafting"  # leased, not lost
        monkeypatch.setattr(workflow_drafter, "create_quote_draft", real)
        await asyncio.sleep(0.7)
        assert await drain(database) == [1]  # reclaimed after the lease
    run = await run_of(seed_database, rfq_id)
    assert (run.disposition, run.draft_attempt_count, run.claim_generation) == ("draft_created", 2, 3)  # fmt: skip
    assert await rows(seed_database, Quote, Quote.rfq_id == rfq_id) == 1


async def test_transient_faults_retry_up_to_the_limit_then_fail(world, seed_database, monkeypatch):
    rfq_id = await ready(seed_database, world)

    async def unavailable(*args, **kwargs):
        raise DatabaseError("Database operation failed; check server availability and access.")

    monkeypatch.setattr(workflow_drafter, "create_quote_draft", unavailable)
    async with drafter_db(seed_database) as database:
        assert await drain(database) == [1, 1, 1]
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.last_error, run.draft_attempt_count, run.quote_id) == ("failed", "technical_failure", "database_unavailable", 3, None)  # fmt: skip
    trail = [(e["status"], e["disposition"], e["outcome"]) for e in await draft_trail(seed_database, rfq_id)]  # fmt: skip
    assert trail == [("completed", "ready_for_draft", "error")] * 2 + [("failed", "technical_failure", "error")]  # fmt: skip


async def test_a_transient_fault_then_success_drafts_once(world, seed_database, monkeypatch):
    rfq_id = await ready(seed_database, world)
    real, calls = workflow_drafter.create_quote_draft, []

    async def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise DatabaseError("Database operation failed; check server availability and access.")
        return await real(*args, **kwargs)

    monkeypatch.setattr(workflow_drafter, "create_quote_draft", flaky)
    async with drafter_db(seed_database) as database:
        assert await drain(database) == [1, 1]
    run = await run_of(seed_database, rfq_id)
    assert (run.disposition, run.draft_attempt_count, run.last_error) == ("draft_created", 2, None)
    assert await rows(seed_database, Quote, Quote.rfq_id == rfq_id) == 1


async def test_replaying_the_run_key_returns_the_same_draft(world, seed_database):
    rfq_id = await ready(seed_database, world)
    async with drafter_db(seed_database) as database:
        await drain(database)
        run = await run_of(seed_database, rfq_id)
        async with database.session() as s, s.begin():  # the drafter role itself, same key
            principal = await workflow_worker.service_principal(s, run.customer_id, workflow_drafter.DRAFT_PERMISSIONS)  # fmt: skip
            again = await quote_drafts.create_quote_draft(s, principal, rfq_id, idempotency_key=wq.draft_key(run.workflow_run_id), correlation_id=rfq_id)  # fmt: skip
    assert again.replayed and again.quote_id == run.quote_id
    assert await rows(seed_database, Quote, Quote.rfq_id == rfq_id) == 1


async def test_graceful_shutdown_hands_the_run_back_without_spending_the_attempt(world, seed_database, monkeypatch):  # fmt: skip
    rfq_id = await ready(seed_database, world)
    real = workflow_drafter.create_quote_draft

    async def slow(*args, **kwargs):
        await asyncio.sleep(10)
        return await real(*args, **kwargs)

    monkeypatch.setattr(workflow_drafter, "create_quote_draft", slow)
    async with drafter_db(seed_database) as database:
        drafter = Drafter(database, settings_(worker_id="d-stop"))
        serving = asyncio.create_task(drafter.serve())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if (await run_of(seed_database, rfq_id)).status == "drafting":
                break
        drafter.stopping.set()
        await asyncio.wait_for(serving, 10)
        run = await run_of(seed_database, rfq_id)
        assert (run.status, run.disposition, run.draft_attempt_count, run.last_error, run.quote_id) == ("completed", "ready_for_draft", 0, "drafter_shutdown", None)  # fmt: skip
        monkeypatch.setattr(workflow_drafter, "create_quote_draft", real)
        assert await drain(database, settings_(worker_id="d-restart")) == [1]
    run = await run_of(seed_database, rfq_id)
    assert (run.disposition, run.draft_attempt_count, run.claim_generation) == ("draft_created", 1, 3)  # fmt: skip


async def test_exhausted_expired_drafting_lease_fails(world, seed_database):
    rfq_id = await ready(seed_database, world)
    async with seed_database.session() as s, s.begin():  # the third drafting attempt died
        await s.execute(update(WorkflowRun).where(WorkflowRun.rfq_id == rfq_id).values(status="drafting", disposition=None, draft_attempt_count=3, claim_generation=4, lease_owner="d-dead", lease_expires_at=func.now() - text("interval '1 minute'")))  # fmt: skip
    async with drafter_db(seed_database) as database:
        assert await workflow_drafter.claim(database, settings_()) is None
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.last_error, run.lease_owner) == ("failed", "technical_failure", "draft_lease_expired", None)  # fmt: skip


async def test_database_refuses_a_quote_link_on_any_other_state(world, seed_database):
    rfq_id = await ready(seed_database, world)
    async with drafter_db(seed_database) as database:
        await drain(database)
    run = await run_of(seed_database, rfq_id)
    async with seed_database.session() as s:  # caught inside: Database.session would wrap it
        for change in ({"status": "blocked", "disposition": "draft_refused"}, {"disposition": "ready_for_draft"}, {"quote_id": None}):  # fmt: skip
            with pytest.raises(DBAPIError, match="ck_workflow_runs_rule_(09|10)"):
                async with s.begin_nested():
                    await s.execute(update(WorkflowRun).where(WorkflowRun.rfq_id == rfq_id).values(**change))  # fmt: skip
        await s.rollback()
    assert (await run_of(seed_database, rfq_id)).quote_id == run.quote_id


# --- runtime integrity (A4.5) ---------------------------------------------------------------------


async def test_runtime_integrity_explains_legitimate_drafts_and_refusals(world, seed_database, seed_plan):  # fmt: skip
    drafted = await ready(seed_database, world)
    refused = await ready(seed_database, world, parts=[{**PART, "thickness_mm": "5"}])
    other = await ready(seed_database, world, who="b")
    async with drafter_db(seed_database) as database:
        await drain(database)
    report = await runtime_audit(seed_database, seed_plan)
    assert report["status"] == "PASS" and report["runtime_records"]["quotes"] == 2
    states = {r: (await run_of(seed_database, r)).disposition for r in (drafted, refused, other)}
    assert states == {drafted: "draft_created", refused: "draft_refused", other: "draft_created"}


@pytest.mark.parametrize("forgery", ["unlinked_quote", "skipped_supervisor", "forged_draft_event"])
async def test_runtime_integrity_detects_forged_drafts(world, seed_database, seed_plan, forgery):
    rfq_id = await ready(seed_database, world)
    if forgery == "unlinked_quote":  # a second, correctly computed draft no run created
        lone = await submit(seed_database, world)
        async with seed_database.session() as s, s.begin():
            principal = await workflow_worker.service_principal(s, world["a"][1], workflow_drafter.DRAFT_PERMISSIONS)  # fmt: skip
            await quote_drafts.create_quote_draft(s, principal, lone, idempotency_key="manual-2", correlation_id=lone)  # fmt: skip
        rule = "runtime quote without exactly its workflow run"
    elif forgery == "skipped_supervisor":  # ready_for_draft written without any A3 attempt
        rfq_id = await submit(seed_database, world)
        async with seed_database.session() as s, s.begin():
            await s.execute(update(WorkflowRun).where(WorkflowRun.rfq_id == rfq_id).values(status="completed", disposition="ready_for_draft", attempt_count=1, claim_generation=1))  # fmt: skip
        rule = "drafting without a ready Supervisor outcome"
    else:  # a blocked run claiming, through an extra event, to have drafted someone's quote
        refused = await ready(seed_database, world, parts=[{**PART, "thickness_mm": "5"}])
        rule = "draft event is inconsistent"
    async with drafter_db(seed_database) as database:
        await drain(database)
    if forgery == "forged_draft_event":
        run = await run_of(seed_database, refused)
        drafted = await run_of(seed_database, rfq_id)
        async with seed_database.session() as s, s.begin():
            s.add(AuditEvent(actor_id=None, customer_id=run.customer_id, rfq_id=refused, operation=wq.DRAFT_OPERATION, outcome="success", correlation_id=refused,
                             event_data={"workflow_run_id": str(run.workflow_run_id), "draft_attempt": 1, "claim_generation": run.claim_generation,
                                         "status": "completed", "disposition": "draft_created", "error": None, "quote_id": drafted.quote_id}))  # fmt: skip
    with pytest.raises(IntegrityVerificationError) as caught:
        await runtime_audit(seed_database, seed_plan)
    assert rule in str(caught.value)
