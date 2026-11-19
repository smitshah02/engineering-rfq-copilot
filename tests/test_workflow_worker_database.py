"""Checkpoints A1 (concurrency, restricted grants) and A3 (worker) on COMMITTED test data.

Claims, leases and SKIP LOCKED need several real connections, so ``committed`` commits a
seeded world to the guarded ``rfq_copilot_test`` database (seed, approval-v1.2, rbac-policy
-v4, an operator, two customers, one service identity for customer A and B each, and the
queue activation) and truncates every application table in teardown. The development
database is never opened.

Restricted roles: no dedicated workflow role exists in the local cluster (creating one needs
a superuser), so ``worker_db`` applies exactly the reviewed ``a1_workflow_role.sql`` grant
block to the existing ``rfq_copilot_app_test`` role and connects with ``workflow=True``;
``runtime_db`` re-applies the K2 block for the API role. Both checks are live catalogue
checks (``require_workflow_runtime`` / ``require_restricted_runtime``).

Agents are offline: the specialist workflow fake intake model and fake MCP router, with the harness rule
fixtures served by ``pinned_rules`` (as every offline Supervisor test does). One test runs the
worker against the real in-memory MCP host and real services instead of the fake router.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, time
from pathlib import Path

import httpx2
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_auth import AUDIENCE as REST_AUDIENCE
from test_auth import trust as trust
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_intake import ev, model, reply
from test_intake_validation import COMPLETE, RULES, complete_args
from test_mcp_auth import BASE, MCP_AUDIENCE, settings
from test_mcp_host import host_settings, running
from test_pricing_agent import TRUSTED
from test_quotation_agent import APPROVAL_V12
from test_quote_drafts_concurrency import guarded
from test_runtime_role_database import grant_block
from test_specialized_agent_integration import Router
from test_supply_agent import MONDAY
from test_workflow_identity_database import service
from test_workflow_queue_database import customer

from rfq_copilot import dev_token, workflow_queue, workflow_worker
from rfq_copilot import intake_validation as a1v
from rfq_copilot import supervisor as sv
from rfq_copilot.api.auth import Principal
from rfq_copilot.api.mcp_integration import McpIntegration
from rfq_copilot.api.schemas.rfq import MAX_RAW_REQUEST_CHARS, RfqSubmissionRequest
from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database, PrivilegedRuntimeRole, runtime_role_violations
from rfq_copilot.intake import build_intake_graph
from rfq_copilot.integrity import audit_integrity
from rfq_copilot.mcp import host as h
from rfq_copilot.mcp.client import McpCallError, McpToolClient
from rfq_copilot.models import RFQ, AuditEvent, RolePermission, WorkflowRun
from rfq_copilot.rfq_submission import create_customer_rfq
from rfq_copilot.seeding.loader import execute_seed
from rfq_copilot.seeding.rules import register_rule_version
from rfq_copilot.workflow_worker import Claim, Dependencies, Worker, WorkerSettings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

ROOT = Path(__file__).resolve().parents[1]
WORKER_ARTIFACT = ROOT / "docs" / "database" / "a1_workflow_role.sql"
TEST_DATABASE = "rfq_copilot_test"
PART = {"part_id": "P-100", "part_type": "flat_panel", "material_id": "MAT-001", "finish_id": "powder_coat",
        "length_mm": "300", "width_mm": "200", "thickness_mm": "2", "tolerance_mm": "0.2", "quantity": 25}  # fmt: skip
FAST = dict(lease_seconds=3.0, heartbeat_seconds=0.5, poll_seconds=0.05, shutdown_grace_seconds=0.2)


def worker_block(role: str) -> list[str]:
    body = WORKER_ARTIFACT.read_text().split("-- BEGIN A1 WORKFLOW GRANTS\n")[1]
    body = body.split("-- END A1 WORKFLOW GRANTS")[0]
    return [s.strip().replace(':"worker"', role) for s in body.split(";") if s.strip()]


@pytest_asyncio.fixture(loop_scope="module")
async def committed(seed_database, seed_plan):
    """The committed world; every application table truncated again in teardown."""
    try:
        async with seed_database.session() as s, s.begin():
            await guarded(s)
            await execute_seed(s, seed_database.config, seed_plan, "apply", confirm_database=TEST_DATABASE)  # fmt: skip
            await register_rule_version(s, "approval-v1.2")
            user_a, a, _, operator = await customer(s, seed_plan, user_id="U-CUST-A")
            user_b, b, _, _ = await customer(s, seed_plan, user_id="U-CUST-B", nth=1)
            user_c, c, _, _ = await customer(s, seed_plan, user_id="U-CUST-C", nth=2)  # no service
            assert (
                await service(s, a, operator, user_id="U-SVC-A")
            ).result == "service_provisioned"
            assert (
                await service(s, b, operator, user_id="U-SVC-B")
            ).result == "service_provisioned"
            await workflow_queue.activate(s, await admin_id(s))
        yield {"a": (user_a, a), "b": (user_b, b), "c": (user_c, c), "operator": operator}
    finally:
        async with seed_database.session() as s, s.begin():
            await guarded(s)
            tables = (await s.scalars(text("SELECT quote_ident(schemaname)||'.'||quote_ident(tablename) FROM pg_tables WHERE schemaname IN ('business','orchestration')"))).all()  # fmt: skip
            if tables:
                await s.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))


async def admin_id(session):
    from rfq_copilot.models import User

    return await session.scalar(select(User.user_id).where(User.role == "administrator", User.active.is_(True)).order_by(User.user_id).limit(1))  # fmt: skip


async def regrant(seed_database, block) -> DatabaseConfig:
    config = DatabaseConfig.from_env("test", env_file=ROOT / ".env", runtime=True)
    quote = seed_database.engine.dialect.identifier_preparer.quote
    async with seed_database.session() as s, s.begin():
        await guarded(s)
        for statement in block(quote(config.url.username)):
            await s.execute(text(statement))
    return config


@pytest_asyncio.fixture(loop_scope="module")
async def worker_db(committed, seed_database):
    config = await regrant(seed_database, worker_block)
    async with Database(replace(config, runtime=False, workflow=True)) as database:
        yield database


@pytest_asyncio.fixture(loop_scope="module")
async def runtime_db(committed, seed_database):
    config = await regrant(seed_database, grant_block)
    async with Database(config) as database:
        yield database


@pytest.fixture(autouse=True)
def harness_rules(request, monkeypatch):
    """Offline agents: the harness rule fixtures the fake tools were built against, served by
    the worker's own ``pinned_rules`` call (after the real rows were checked to exist). The
    real-MCP test keeps the real rules end to end."""
    import rfq_copilot.mcp.runtime as runtime

    if "real_mcp_host" in request.node.name:
        return
    real = runtime.pinned_rules
    fixtures = {"mfg-v1.0": RULES, "price-v1.0": TRUSTED, "approval-v1.2": APPROVAL_V12}

    async def pinned(session, version):
        await real(session, version)  # still fails closed when a rule is missing or altered
        return fixtures[version]

    monkeypatch.setattr(runtime, "pinned_rules", pinned)
    original = sv._manufacturing_trusted
    monkeypatch.setattr(sv, "_manufacturing_trusted", lambda r: r is RULES or original(r))


async def submit(seed_database, committed, who="a", part=None, raw=COMPLETE, key=None) -> str:
    """One committed customer RFQ through the real service (enqueue on), as the owner role."""
    user, customer_id = committed[who]
    request = RfqSubmissionRequest.model_validate({"parts": [part or PART], "raw_request": raw})
    async with seed_database.session() as s, s.begin():
        receipt, replayed = await create_customer_rfq(
            s, Principal(customer_id, frozenset(), user_id=user), request,
            key or f"k-{asyncio.get_running_loop().time()}", enqueue=True,
        )  # fmt: skip
    assert not replayed
    return receipt.rfq_id


async def run_of(seed_database, rfq_id) -> WorkflowRun:
    async with seed_database.session() as s:
        run = await s.scalar(select(WorkflowRun).where(WorkflowRun.rfq_id == rfq_id))
        s.expunge(run)  # keep the loaded values past the closing rollback
        return run


async def trail(seed_database, rfq_id) -> list[dict]:
    async with seed_database.session() as s:
        events = (await s.scalars(select(AuditEvent).where(AuditEvent.operation == "workflow.attempt", AuditEvent.rfq_id == rfq_id))).all()  # fmt: skip
        return sorted((e.event_data | {"actor": e.actor_id, "outcome": e.outcome} for e in events), key=lambda d: (d["claim_generation"], d["status"]))  # fmt: skip


def intake(args=None):
    """The specialist workflow fake intake model, with enough identical replies for retries and batches."""
    return build_intake_graph(model(*[reply(args or complete_args())] * 20))


def deps(args=None, router=None, **kw) -> Dependencies:
    """Offline dependencies on the harness clock (its fake supply data starts on MONDAY)."""
    return Dependencies(intake_graph=intake(args), executor=lambda principal: router or Router(), now=lambda: datetime.combine(MONDAY, time(), UTC), **kw)  # fmt: skip


def settings_(**over) -> WorkerSettings:
    return WorkerSettings(**{**FAST, **over})


async def drain(database, d=None, **over) -> list[int]:
    worker = Worker(database, settings_(**over), d or deps())
    counts = []
    while (n := await worker.run_once()) and len(counts) < 20:
        counts.append(n)
    return counts


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def settled_between_tests(committed, seed_database):
    """Each test leaves no runnable run behind (expired leases are expired, then drained)."""
    yield
    async with seed_database.session() as s, s.begin():
        await s.execute(update(WorkflowRun).where(WorkflowRun.status == "running").values(lease_expires_at=func.now() - text("interval '1 hour'")))  # fmt: skip
    config = await regrant(seed_database, worker_block)
    async with Database(replace(config, runtime=False, workflow=True)) as database:
        await drain(database)


# --- restricted roles (A1) ----------------------------------------------------------------------


async def test_worker_role_holds_exactly_the_reviewed_queue_privileges(worker_db, seed_database):
    async with worker_db.session() as s:
        await guarded(s)
        assert await runtime_role_violations(s, workflow=True) == ()
        assert "update_privileges" in await runtime_role_violations(s)  # not the API role
        for statement in ("UPDATE orchestration.workflow_runs SET rfq_id = rfq_id", "UPDATE business.quotes SET status = status", "INSERT INTO business.rfqs (rfq_id) VALUES ('x')"):  # fmt: skip
            with pytest.raises(DBAPIError, match="permission denied"):
                async with s.begin_nested():
                    await s.execute(text(statement))


async def test_api_role_may_enqueue_but_never_claim_or_settle(runtime_db, committed, seed_database):
    user, customer_id = committed["a"]
    request = RfqSubmissionRequest.model_validate({"parts": [PART], "raw_request": COMPLETE})
    async with runtime_db.session() as s, s.begin():
        receipt, _ = await create_customer_rfq(s, Principal(customer_id, frozenset(), user_id=user), request, "k-runtime", enqueue=True)  # fmt: skip
    run = await run_of(seed_database, receipt.rfq_id)
    assert run.status == "queued"
    async with runtime_db.session() as s:
        assert "update_privileges" in await runtime_role_violations(s, workflow=True)
        await s.rollback()
        with pytest.raises(DBAPIError, match="permission denied"):
            async with s.begin():
                await s.execute(update(WorkflowRun).values(status="running"))
    config = replace(runtime_db.config, runtime=False, workflow=True)
    async with Database(config) as wrong:  # the API grants are not the worker's
        with pytest.raises(PrivilegedRuntimeRole, match="restricted workflow role"):
            async with wrong.session():
                pass


async def test_concurrent_submissions_enqueue_exactly_once_per_rfq(runtime_db, committed, seed_database):  # fmt: skip
    user, customer_id = committed["a"]
    principal = Principal(customer_id, frozenset(), user_id=user)
    request = RfqSubmissionRequest.model_validate({"parts": [PART], "raw_request": COMPLETE})

    async def attempt(key):
        async with runtime_db.session() as s, s.begin():
            return await create_customer_rfq(s, principal, request, key, enqueue=True)

    same = await asyncio.gather(*(attempt("k-race") for _ in range(4)))
    distinct = await asyncio.gather(*(attempt(f"k-race-{i}") for i in range(3)))
    assert len({r.rfq_id for r, _ in same}) == 1 and sum(not replayed for _, replayed in same) == 1
    rfq_ids = {r.rfq_id for r, _ in same} | {r.rfq_id for r, _ in distinct}
    async with seed_database.session() as s:
        per_rfq = (await s.execute(select(WorkflowRun.rfq_id, func.count()).where(WorkflowRun.rfq_id.in_(rfq_ids)).group_by(WorkflowRun.rfq_id))).all()  # fmt: skip
        enqueued = await s.scalar(select(func.count()).where(AuditEvent.operation == "workflow.enqueue", AuditEvent.rfq_id.in_(rfq_ids)))  # fmt: skip
    assert len(per_rfq) == 4 and {n for _, n in per_rfq} == {1} and enqueued == 4


# --- the worker (A3) -----------------------------------------------------------------------------


async def test_worker_runs_the_supervisor_and_records_ready_for_draft(worker_db, committed, seed_database, monkeypatch):  # fmt: skip
    calls = []
    real = sv.run_supervisor_workflow

    async def spy(context):
        calls.append((context.principal.user_id, context.principal.customer_id, context.rfq_id))
        return await real(context)

    monkeypatch.setattr(sv, "run_supervisor_workflow", spy)
    rfq_id = await submit(seed_database, committed)
    router = Router()
    drained = await drain(worker_db, deps(router=router))
    run = await run_of(seed_database, rfq_id)
    assert drained == [1], (
        run.status,
        run.last_error,
        router.calls,
        await trail(seed_database, rfq_id),
    )
    assert (run.status, run.disposition, run.last_error, run.attempt_count, run.claim_generation) == ("completed", "ready_for_draft", None, 1, 1)  # fmt: skip
    assert (run.lease_owner, run.lease_expires_at) == (None, None) and len(run.external_workflow_id) == 32  # fmt: skip
    assert calls == [
        ("U-SVC-A", committed["a"][1], rfq_id)
    ]  # the service identity, never the customer
    assert {"check_manufacturing_feasibility", "evaluate_quotation"} <= set(
        router.calls
    )  # A2..A5 ran
    (event,) = await trail(seed_database, rfq_id)
    assert event == {"workflow_run_id": str(run.workflow_run_id), "attempt": 1, "claim_generation": 1, "status": "completed", "disposition": "ready_for_draft", "error": None, "actor": "U-SVC-A", "outcome": "success"}  # fmt: skip
    async with seed_database.session() as s:  # the RFQ is untouched and no quote was created
        rfq = (await s.execute(select(RFQ.status, RFQ.row_version).where(RFQ.rfq_id == rfq_id))).one()  # fmt: skip
        quotes = await s.scalar(text("SELECT count(*) FROM business.quotes WHERE rfq_id = :r"), {"r": rfq_id})  # fmt: skip
    assert (*rfq, quotes) == ("draft", 1, 0)


BUSINESS = [
    # (name, stored part change, raw text, intake args, router, status, disposition)
    (
        "clarification",
        {},
        COMPLETE,
        complete_args(material=None, material_grade=None),
        None,
        "waiting_human",
        "clarification_required",
    ),  # fmt: skip
    (
        "review",
        {},
        COMPLETE + " Also cast the panels.",
        None,
        None,
        "waiting_human",
        "review_required",
    ),  # fmt: skip
    (
        "not_quotable",
        {"thickness_mm": "5"},
        COMPLETE.replace("2 mm thick", "5 mm thick"),
        complete_args(thickness=ev("5 mm", "5 mm thick")),
        None,
        "blocked",
        "not_quotable",
    ),  # fmt: skip
]


@pytest.mark.parametrize("name,change,raw,args,router,status,disposition", BUSINESS, ids=[b[0] for b in BUSINESS])  # fmt: skip
async def test_business_outcomes_are_final_and_never_retried(worker_db, committed, seed_database, name, change, raw, args, router, status, disposition):  # fmt: skip
    rfq_id = await submit(seed_database, committed, part={**PART, **change}, raw=raw)
    assert await drain(worker_db, deps(args, router)) == [1]  # one claim, then nothing to retry
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.attempt_count, run.last_error) == (status, disposition, 1, None)  # fmt: skip


async def test_worker_persists_supervisor_clarification_codes(worker_db, committed, seed_database, seed_plan, monkeypatch):  # fmt: skip
    """Real offline Supervisor -> worker classification/settlement -> stored audit -> integrity."""
    results = []
    original = sv.run_supervisor_workflow

    async def capture(context):
        result = await original(context)
        results.append(result)
        return result

    monkeypatch.setattr(sv, "run_supervisor_workflow", capture)
    rfq_id = await submit(seed_database, committed)
    assert await drain(worker_db, deps(complete_args(material=None, material_grade=None))) == [1]
    (result,) = results
    assert result.status == "executed" and result.disposition.category == "clarification_required"
    codes = result.disposition.clarification_codes
    assert codes and codes == result.route.clarifications == result.outcomes[-1].clarifications
    (event,) = await trail(seed_database, rfq_id)
    assert event[workflow_queue.CLARIFICATION_KEY] == list(codes)
    assert (event["status"], event["disposition"], event["actor"], event["outcome"]) == (
        "waiting_human",
        "clarification_required",
        "U-SVC-A",
        "success",
    )
    run = await run_of(seed_database, rfq_id)
    assert run.attempt_count == 1 and run.last_error is None and run.quote_id is None
    async with seed_database.session() as s:
        assert await s.scalar(text("SELECT count(*) FROM business.quotes WHERE rfq_id = :r"), {"r": rfq_id}) == 0  # fmt: skip
        report = await audit_integrity(s, seed_database.config, seed_plan, runtime=True)
    assert report["status"] == "PASS"


def hallucinating(monkeypatch, **change):
    """A1's validated output, altered after A1 verified it: the extraction now contradicts the
    persisted RFQ (a hallucinated value or part) while every Supervisor provenance check still
    holds. A1's own text verification cannot catch this; the worker's SourceGate must."""
    real = a1v.validate_intake

    async def altered(*args, **kwargs):
        result = await real(*args, **kwargs)
        parts = tuple(p.model_copy(update={"specification": p.specification.model_copy(update=change)}) if p.specification else p for p in result.parts)  # fmt: skip
        return result.model_copy(update={"parts": parts})

    monkeypatch.setattr(a1v, "validate_intake", altered)


@pytest.mark.parametrize("change", [{"quantity": 30}, {"material_id": "MAT-002"}, {"part_id": "P-999"}], ids=["quantity", "material", "invented_part"])  # fmt: skip
async def test_extraction_contradicting_the_stored_rfq_fails_closed(worker_db, committed, seed_database, monkeypatch, change):  # fmt: skip
    hallucinating(monkeypatch, **change)
    rfq_id = await submit(seed_database, committed)
    router = Router()
    assert await drain(worker_db, deps(router=router)) == [1]  # never retried
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.last_error, run.attempt_count) == ("blocked", "source_mismatch", "source_mismatch", 1)  # fmt: skip
    assert "check_manufacturing_feasibility" not in router.calls  # refused before any tool ran


async def test_stored_values_conflicting_with_the_notes_ask_for_clarification(worker_db, committed, seed_database):  # fmt: skip
    rfq_id = await submit(seed_database, committed, part={**PART, "material_id": "MAT-002"})
    router = Router()
    await drain(worker_db, deps(router=router))
    run = await run_of(seed_database, rfq_id)
    assert run.status == "waiting_human" and run.disposition in ("clarification_required", "review_required")  # fmt: skip
    assert "check_manufacturing_feasibility" not in router.calls  # nothing contradictory is priced


class Failing(Router):
    """A technical tool fault after A1/A2 (a catalog fault is an intake review, by design)."""

    def __init__(self, code="unavailable", tool="check_inventory", **kw):
        super().__init__(**kw)
        self.code, self.tool = code, tool

    async def __call__(self, tool, request):
        if tool == self.tool:
            self.calls.append(tool)
            raise McpCallError(self.code)
        return await super().__call__(tool, request)


async def test_technical_failures_retry_up_to_the_attempt_limit(worker_db, committed, seed_database):  # fmt: skip
    rfq_id = await submit(seed_database, committed)
    assert await drain(worker_db, deps(router=Failing())) == [1, 1, 1]
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.attempt_count, run.claim_generation) == ("failed", "technical_failure", 3, 3)  # fmt: skip
    assert run.last_error == "unavailable"
    statuses = [(e["claim_generation"], e["status"], e["outcome"]) for e in await trail(seed_database, rfq_id)]  # fmt: skip
    assert statuses == [(1, "queued", "error"), (2, "queued", "error"), (3, "failed", "error")]


@pytest.mark.parametrize("outage", ["model", "mcp"])
async def test_continuous_worker_recovers_from_temporary_dependencies(
    worker_db, committed, seed_database, outage, caplog
):
    from test_llm import FakeToolModel

    calls = []

    class FlakyModel(FakeToolModel):
        async def _agenerate(self, *args, **kwargs):
            calls.append("model")
            if len(calls) == 1:
                raise OSError("secret provider failure")
            return await super()._agenerate(*args, **kwargs)

    class FlakyRouter(Router):
        async def __call__(self, tool, request):
            if tool == "check_inventory":
                calls.append("mcp")
                if len(calls) == 1:
                    raise McpCallError("unavailable")
            return await super().__call__(tool, request)

    if outage == "mcp":
        dependencies = deps(router=FlakyRouter())
    else:
        graph = build_intake_graph(FlakyModel(messages=iter([reply(complete_args())] * 3)))
        dependencies = replace(deps(), intake_graph=graph)
    rfq_id = await submit(seed_database, committed)
    worker = Worker(worker_db, settings_(), dependencies)
    task = asyncio.create_task(worker.serve())
    try:
        async with asyncio.timeout(20):
            while (await run_of(seed_database, rfq_id)).status != "completed":
                if task.done():
                    await task
                await asyncio.sleep(0.05)
    finally:
        worker.stopping.set()
        await asyncio.wait_for(task, 10)
    run = await run_of(seed_database, rfq_id)
    assert (run.disposition, run.attempt_count, run.last_error) == ("ready_for_draft", 2, None)
    events = await trail(seed_database, rfq_id)
    expected = "model_error" if outage == "model" else "unavailable"
    assert [(e["status"], e["error"]) for e in events] == [
        ("queued", expected),
        ("completed", None),
    ]
    assert "secret provider failure" not in caplog.text


async def test_permission_failures_are_configuration_errors_not_retries(worker_db, committed, seed_database):  # fmt: skip
    rfq_id = await submit(seed_database, committed)
    await drain(worker_db, deps(router=Failing("forbidden")))
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.attempt_count) == ("blocked", "configuration_error", 1)


async def test_customer_without_a_service_identity_is_never_run_as_anyone_else(worker_db, committed, seed_database):  # fmt: skip
    rfq_id = await submit(seed_database, committed, who="c")
    router = Router()
    await drain(worker_db, deps(router=router))
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.last_error) == ("blocked", "configuration_error", "service_identity_unavailable")  # fmt: skip
    assert router.calls == [] and (await trail(seed_database, rfq_id))[0]["actor"] is None


class Slow(Router):
    def __init__(self, seconds, **kw):
        super().__init__(**kw)
        self.seconds = seconds

    async def __call__(self, tool, request):
        await asyncio.sleep(self.seconds)
        return await super().__call__(tool, request)


async def test_two_workers_never_claim_the_same_run(worker_db, committed, seed_database):
    rfq_ids = [await submit(seed_database, committed) for _ in range(3)]
    one = Worker(worker_db, settings_(worker_id="w-one"), deps(router=Slow(0.05)))
    two = Worker(worker_db, settings_(worker_id="w-two", concurrency=2), deps(router=Slow(0.05)))
    claimed = await asyncio.gather(one.run_once(), two.run_once())
    assert sum(claimed) == 3
    runs = [await run_of(seed_database, r) for r in rfq_ids]
    assert {r.status for r in runs} == {"completed"} and {r.claim_generation for r in runs} == {1}
    actors = {e["actor"] for r in rfq_ids for e in await trail(seed_database, r)}
    assert actors == {"U-SVC-A"}


async def test_two_continuous_workers_discover_new_submissions_once(
    worker_db, committed, seed_database
):
    workers = [
        Worker(worker_db, settings_(worker_id=f"continuous-{i}"), deps(router=Slow(0.05)))
        for i in range(2)
    ]
    tasks = [asyncio.create_task(worker.serve()) for worker in workers]
    try:
        await asyncio.sleep(0.1)  # both consumers start on an empty queue
        rfq_ids = [await submit(seed_database, committed) for _ in range(3)]
        async with asyncio.timeout(20):
            while True:
                runs = [await run_of(seed_database, rfq_id) for rfq_id in rfq_ids]
                if all(run.disposition == "ready_for_draft" for run in runs):
                    break
                for task in tasks:
                    if task.done():
                        await task
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.15)  # several more polls: finished evaluations stay terminal
    finally:
        for worker in workers:
            worker.stopping.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
    runs = [await run_of(seed_database, rfq_id) for rfq_id in rfq_ids]
    assert {(run.attempt_count, run.claim_generation) for run in runs} == {(1, 1)}
    trails = [await trail(seed_database, rfq_id) for rfq_id in rfq_ids]
    assert all(len(events) == 1 for events in trails)


async def test_expired_lease_is_reclaimed_and_the_stale_worker_is_fenced(worker_db, committed, seed_database):  # fmt: skip
    rfq_id = await submit(seed_database, committed)
    crashed = settings_(worker_id="w-crashed", lease_seconds=1.2, heartbeat_seconds=0.3)
    first = await workflow_worker.claim(worker_db, crashed)
    assert first.rfq_id == rfq_id and (first.claim_generation, first.attempt_count) == (1, 1)
    assert await workflow_worker.claim(worker_db, settings_(worker_id="w-other")) is None  # leased
    await asyncio.sleep(1.4)  # the crashed worker never heartbeats
    second = await workflow_worker.claim(worker_db, settings_(worker_id="w-other"))
    assert (second.workflow_run_id, second.claim_generation, second.attempt_count) == (first.workflow_run_id, 2, 2)  # fmt: skip
    # The old owner wakes up: its heartbeat and its settlement are both refused.
    assert not await workflow_worker.heartbeat(worker_db, crashed, first)
    assert not await workflow_worker.settle(worker_db, crashed, first, None, "completed", "ready_for_draft")  # fmt: skip
    status = await workflow_worker.process(
        worker_db, settings_(worker_id="w-other"), deps(), second
    )
    run = await run_of(seed_database, rfq_id)
    assert status == "completed" and (run.status, run.claim_generation, run.attempt_count) == ("completed", 2, 2)  # fmt: skip
    assert [e["claim_generation"] for e in await trail(seed_database, rfq_id)] == [2]


async def test_heartbeat_keeps_a_long_run_leased(worker_db, committed, seed_database):
    rfq_id = await submit(seed_database, committed)
    settings = settings_(worker_id="w-long", lease_seconds=0.8, heartbeat_seconds=0.2)
    claimed = await workflow_worker.claim(worker_db, settings)
    task = asyncio.create_task(workflow_worker.process(worker_db, settings, deps(router=Slow(0.25)), claimed))  # fmt: skip
    for _ in range(4):  # well past the original lease: still nobody else may claim it
        await asyncio.sleep(0.4)
        if task.done():
            break
        assert await workflow_worker.claim(worker_db, settings_(worker_id="w-thief")) is None
    assert await task == "completed"
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.claim_generation) == ("completed", 1)


async def test_exhausted_expired_lease_fails_without_another_attempt(worker_db, committed, seed_database):  # fmt: skip
    rfq_id = await submit(seed_database, committed)
    async with seed_database.session() as s, s.begin():  # the third attempt crashed long ago
        await s.execute(update(WorkflowRun).where(WorkflowRun.rfq_id == rfq_id).values(status="running", attempt_count=3, claim_generation=3, lease_owner="w-dead", lease_expires_at=func.now() - text("interval '1 minute'")))  # fmt: skip
    assert await workflow_worker.claim(worker_db, settings_()) is None
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.last_error, run.attempt_count) == ("failed", "technical_failure", "lease_expired", 3)  # fmt: skip


async def test_graceful_shutdown_releases_without_spending_the_attempt_and_restart_finishes(worker_db, committed, seed_database):  # fmt: skip
    rfq_id = await submit(seed_database, committed)
    worker = Worker(worker_db, settings_(worker_id="w-stop"), deps(router=Slow(5)))
    serving = asyncio.create_task(worker.serve())
    for _ in range(100):
        await asyncio.sleep(0.05)
        if (await run_of(seed_database, rfq_id)).status == "running":
            break
    worker.stopping.set()
    await asyncio.wait_for(serving, 10)
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.attempt_count, run.claim_generation, run.last_error) == ("queued", 0, 1, "worker_shutdown")  # fmt: skip
    restarted = Worker(worker_db, settings_(worker_id="w-restart"), deps())
    assert await restarted.run_once() == 1
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.attempt_count, run.claim_generation) == ("completed", 1, 2)


async def test_supervisor_deadline_is_a_technical_retry(worker_db, committed, seed_database):
    rfq_id = await submit(seed_database, committed)
    worker = Worker(worker_db, settings_(run_timeout_seconds=0.3), deps(router=Slow(2)))
    assert await worker.run_once() == 1
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.attempt_count, run.disposition) == ("queued", 1, None)
    assert workflow_worker._CODE.match(run.last_error)


async def test_worker_through_the_real_mcp_host_and_services(worker_db, committed, seed_database, trust, tmp_path):  # fmt: skip
    """No fake router: the service identity's minted credential, the gateway host's verifier and
    per-tool v2 permission checks, and the real read-only services on the committed data."""
    private, public = dev_token.keygen(tmp_path / "keys")
    auth = settings(trust, jwt_algorithm="EdDSA", jwt_public_key_file=public)
    async with Database(seed_database.config, read_only=True) as readonly:
        gateway = h.create_mcp_host(host_settings(rate_limit_per_minute=10_000), auth, readonly)
        client = McpToolClient(BASE, transport=httpx2.ASGITransport(app=gateway))
        provider = dev_token.DevMcpCredentialProvider(auth, private, environment="development")
        integration = McpIntegration(client, provider, MCP_AUDIENCE, REST_AUDIENCE)
        real = Dependencies(intake_graph=intake(), executor=lambda p: workflow_worker.mcp_executor(integration, p))  # fmt: skip
        async with running(gateway):
            allowed = await submit(seed_database, committed)
            await drain(worker_db, real)
            async with seed_database.session() as s, s.begin():  # take one tool permission away
                await s.execute(RolePermission.__table__.delete().where(RolePermission.role == "rfq_workflow_agent", RolePermission.permission == "view_catalog"))  # fmt: skip
            try:
                denied = await submit(seed_database, committed)
                await drain(worker_db, real)
            finally:
                async with seed_database.session() as s, s.begin():
                    await s.execute(RolePermission.__table__.insert().values(role="rfq_workflow_agent", permission="view_catalog", allowed=True))  # fmt: skip
    ok = await run_of(seed_database, allowed)
    assert ok.status in ("completed", "waiting_human", "blocked") and ok.disposition not in ("technical_failure", "configuration_error", "source_mismatch"), (ok.status, ok.disposition, ok.last_error)  # fmt: skip
    refused = await run_of(seed_database, denied)
    assert (refused.status, refused.disposition, refused.last_error) == ("blocked", "configuration_error", "service_permissions_invalid")  # fmt: skip


async def test_runtime_integrity_holds_after_every_worker_scenario(worker_db, committed, seed_database, seed_plan):  # fmt: skip
    """Every settlement path in one committed world, then the opt-in runtime audit."""
    ready = await submit(seed_database, committed)
    clarify = await submit(seed_database, committed)
    mismatch = await submit(
        seed_database, committed, part={**PART, "material_id": "MAT-002"}
    )  # A1 asks
    other = await submit(seed_database, committed, who="b")
    unserved = await submit(seed_database, committed, who="c")
    await drain(worker_db, deps())  # ready, mismatch, B, C (clarify is processed below)
    crashed = settings_(worker_id="w-crashed", lease_seconds=1.0, heartbeat_seconds=0.3)
    assert await workflow_worker.claim(worker_db, crashed) is None  # nothing left
    exhausted = await submit(seed_database, committed)
    await drain(worker_db, deps(router=Failing()))
    released = await submit(seed_database, committed)
    first = await workflow_worker.claim(worker_db, crashed)
    assert await workflow_worker.release(worker_db, crashed, first)
    await drain(worker_db, deps(complete_args(material=None, material_grade=None)))
    async with seed_database.session() as s:
        report = await audit_integrity(s, seed_database.config, seed_plan, runtime=True)
        states = dict((await s.execute(select(WorkflowRun.rfq_id, WorkflowRun.status))).all())
    records = report["runtime_records"]
    assert report["status"] == "PASS" and records["services"] == 2
    assert records["workflow_runs"] == records["rfqs"] == 7  # all submitted after activation
    assert [states[r] for r in (ready, mismatch, other, unserved, exhausted)] == ["completed", "waiting_human", "completed", "blocked", "failed"]  # fmt: skip
    assert states[clarify] == "completed" and states[released] == "waiting_human"


# --- D1: the Supervisor input limit (20,000 characters) ------------------------------------------


async def notes_for_total(seed_database, committed, worker_db, total: int) -> str:
    """Customer notes (COMPLETE plus spaces) that make the worker's source text exactly
    ``total`` characters: the rendered-line prefix is measured on a probe RFQ through the
    worker's own ``prepare``, never re-implemented here."""
    probe = await submit(seed_database, committed, raw="N")
    run = await run_of(seed_database, probe)
    prepared = await workflow_worker.prepare(worker_db, Claim(run.workflow_run_id, probe, run.customer_id, 0, 0))  # fmt: skip
    prefix = len(prepared.text) - 1  # everything before the one-character notes
    notes = COMPLETE + " " * (total - prefix - len(COMPLETE))
    assert len(notes) <= MAX_RAW_REQUEST_CHARS  # a valid submission on its own
    return notes


async def test_source_at_the_limit_is_evaluated(worker_db, committed, seed_database):
    notes = await notes_for_total(seed_database, committed, worker_db, MAX_RAW_REQUEST_CHARS)
    rfq_id = await submit(seed_database, committed, raw=notes)
    run = await run_of(seed_database, rfq_id)
    prepared = await workflow_worker.prepare(worker_db, Claim(run.workflow_run_id, rfq_id, run.customer_id, 0, 0))  # fmt: skip
    assert len(prepared.text) == MAX_RAW_REQUEST_CHARS and prepared.text.endswith(notes)  # whole
    await drain(worker_db, deps())
    run = await run_of(seed_database, rfq_id)
    assert run.last_error != "source_too_long" and run.attempt_count >= 1
    assert run.disposition not in ("configuration_error", "technical_failure", "source_mismatch")


@pytest.mark.parametrize("over", [1, 100])
async def test_source_over_the_limit_is_final_not_evaluated_and_never_truncated(worker_db, committed, seed_database, over):  # fmt: skip
    notes = await notes_for_total(seed_database, committed, worker_db, MAX_RAW_REQUEST_CHARS + over)
    rfq_id = await submit(seed_database, committed, raw=notes)
    await drain(worker_db, deps())  # the probe RFQ is drained too
    assert await drain(worker_db, deps()) == []  # final: nothing left to retry
    run = await run_of(seed_database, rfq_id)
    assert (run.status, run.disposition, run.last_error, run.attempt_count) == ("blocked", "not_evaluated", "source_too_long", 1)  # fmt: skip
    # actor None: refused inside prepare, before any service identity, model or tool ran,
    # so nothing ever saw a truncated request.
    (event,) = await trail(seed_database, rfq_id)
    assert (event["status"], event["disposition"], event["error"], event["actor"]) == ("blocked", "not_evaluated", "source_too_long", None)  # fmt: skip
    async with seed_database.session() as s:  # the original RFQ is preserved, complete
        stored = await s.scalar(select(RFQ.raw_request).where(RFQ.rfq_id == rfq_id))
    assert stored == notes


async def test_over_limit_rfqs_keep_runtime_integrity(worker_db, committed, seed_database, seed_plan):  # fmt: skip
    notes = await notes_for_total(seed_database, committed, worker_db, MAX_RAW_REQUEST_CHARS + 1)
    await submit(seed_database, committed, raw=notes)
    await drain(worker_db, deps())
    async with seed_database.session() as s:
        report = await audit_integrity(s, seed_database.config, seed_plan, runtime=True)
    assert report["status"] == "PASS" and report["runtime_records"]["workflow_runs"] == 2
