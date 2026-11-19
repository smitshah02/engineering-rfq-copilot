"""Checkpoint A1: durable automatic RFQ queue against the guarded test database.

Runs in the rolled-back seeded fixture transaction; the runtime world is built only through
the audited paths (operator bootstrap, customer provisioning, binding, POST /api/v1/rfqs).
Concurrency and restricted-role grants need committed data: test_workflow_worker_database.
"""

import uuid

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from test_auth import bearer
from test_auth import trust as trust
from test_auth_database import call
from test_auth_database import database as database
from test_customer_access_database import v3
from test_customer_rfq_create_database import body
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_operator_assignment_admin_database import admin, first_operator, staff
from test_runtime_integrity_database import eligible, fails, runtime_audit

from rfq_copilot import identity_admin, workflow_queue
from rfq_copilot import operator_admin as operators
from rfq_copilot.models import RFQ, AuditEvent, IdempotencyRecord, RFQLineItem, User, WorkflowRun
from rfq_copilot.seeding.policy import register_policy

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


async def customer(populated, seed_plan, user_id="U-CUST-Q", nth=0, policy="rbac-policy-v4"):
    """(user, customer, subject, operator): provisioned and bound via the audited paths."""
    target = eligible(seed_plan, 3)[nth]
    await register_policy(populated, policy)
    operator = await populated.scalar(select(User.user_id).where(User.role == "identity_operator"))
    if operator is None:
        operator = await first_operator(populated)
    outcome = await operators.provision_customer(
        populated, new_user=user_id, display_name="Queue Buyer", email=f"{user_id}@customer.invalid",
        region="NA", subject=f"auth|{user_id}", verification_ref="SEC-401",
        approver=await admin(populated), requester=operator, execute=True,
    )  # fmt: skip
    assert outcome.result == "customer_provisioned"
    granted = await identity_admin.grant_binding(populated, operator=operator, user=user_id, customer=target, reason="onboarding", execute=True)  # fmt: skip
    assert granted.result == "granted"
    return user_id, target, f"auth|{user_id}", operator


async def submit(populated, trust, database, subject, key, queue="enabled"):
    app = v3(trust, database, workflow_queue=queue)
    headers = {**bearer(trust.mint(sub=subject)), "Idempotency-Key": key}
    return await call(app, "POST", "/api/v1/rfqs", json=await body(populated), headers=headers)


async def activate(populated):
    return await workflow_queue.activate(populated, await admin(populated))


async def runs(populated, rfq_id=None):
    query = select(WorkflowRun)
    if rfq_id is not None:
        query = query.where(WorkflowRun.rfq_id == rfq_id)
    return (await populated.scalars(query.execution_options(populate_existing=True))).all()


async def counts(populated):
    """(portal RFQs, lines of them, receipts, rfq.create events, runs, enqueue events)."""
    portal = select(RFQ.rfq_id).where(RFQ.source_type == "customer_portal")
    return tuple(
        [
            await populated.scalar(select(func.count()).select_from(portal.subquery())),
            await populated.scalar(
                select(func.count()).select_from(RFQLineItem).where(RFQLineItem.rfq_id.in_(portal))
            ),  # fmt: skip
            await populated.scalar(select(func.count()).select_from(IdempotencyRecord)),
            await populated.scalar(
                select(func.count()).where(AuditEvent.operation == "rfq.create")
            ),
            await populated.scalar(select(func.count()).select_from(WorkflowRun)),
            await populated.scalar(
                select(func.count()).where(AuditEvent.operation == "workflow.enqueue")
            ),  # fmt: skip
        ]
    )


# --- atomic enqueue, replay, ownership ------------------------------------------------------------


async def test_new_submission_atomically_stores_rfq_and_exactly_one_queued_run(populated, seed_plan, trust, database):  # fmt: skip
    user, mine, subject, _ = await customer(populated, seed_plan)
    await activate(populated)
    created = await submit(populated, trust, database, subject, "q-1")
    assert created.status_code == 201 and set(created.json()) == {"rfq_id", "status"}
    rfq_id = created.json()["rfq_id"]
    (run,) = await runs(populated, rfq_id)
    rfq = await populated.scalar(select(RFQ).where(RFQ.rfq_id == rfq_id))
    assert (run.status, run.customer_id, run.initiated_by, run.correlation_id) == ("queued", mine, user, rfq_id)  # fmt: skip
    assert (run.attempt_count, run.claim_generation, run.lease_owner, run.lease_expires_at) == (0, 0, None, None)  # fmt: skip
    assert (run.disposition, run.last_error, run.checkpoint_ref) == (None, None, None)
    assert run.rule_versions == workflow_queue.RULE_VERSIONS and run.graph_version == workflow_queue.GRAPH_VERSION  # fmt: skip
    assert run.created_at == rfq.submitted_at  # one transaction: no gap between RFQ and request
    assert rfq.status == "draft" and rfq.row_version == 1  # the immutable RFQ is untouched
    event = await populated.scalar(select(AuditEvent).where(AuditEvent.operation == "workflow.enqueue"))  # fmt: skip
    assert (event.actor_id, event.customer_id, event.rfq_id, event.correlation_id, event.outcome) == (user, mine, rfq_id, rfq_id, "success")  # fmt: skip
    assert event.event_data == {"workflow_run_id": str(run.workflow_run_id)}  # provenance only
    assert event.occurred_at == rfq.submitted_at


async def test_replay_returns_the_receipt_and_never_enqueues_again(populated, seed_plan, trust, database):  # fmt: skip
    _, _, subject, _ = await customer(populated, seed_plan)
    await activate(populated)
    first = await submit(populated, trust, database, subject, "q-replay")
    before = await counts(populated)
    again = await submit(populated, trust, database, subject, "q-replay")
    assert again.status_code == 201 and again.headers.get("Idempotent-Replay") == "true"
    assert again.json() == first.json() and await counts(populated) == before
    assert before[4:] == (1, 1)


async def test_run_customer_comes_from_the_binding_never_the_body(populated, seed_plan, trust, database):  # fmt: skip
    _, mine, subject, _ = await customer(populated, seed_plan)
    await activate(populated)
    other = eligible(seed_plan)[1]
    app = v3(trust, database, workflow_queue="enabled")
    forged = {**await body(populated), "customer_id": other}
    headers = {**bearer(trust.mint(sub=subject)), "Idempotency-Key": "q-own"}
    refused = await call(app, "POST", "/api/v1/rfqs", json=forged, headers=headers)
    assert refused.status_code == 422 and await counts(populated) == (0, 0, 0, 0, 0, 0)
    created = await submit(populated, trust, database, subject, "q-own-2")
    (run,) = await runs(populated, created.json()["rfq_id"])
    assert run.customer_id == mine != other


async def test_missing_permission_writes_nothing(populated, seed_plan, trust, database):
    await customer(populated, seed_plan)
    await activate(populated)
    staff_user = await staff(populated)
    subject = staff_user.authentication_subject
    refused = await submit(populated, trust, database, subject, "q-staff")
    assert refused.status_code == 403 and await counts(populated) == (0, 0, 0, 0, 0, 0)


# --- activation controls -----------------------------------------------------------------------


async def test_queue_off_keeps_the_pre_a1_behaviour(populated, seed_plan, trust, database):
    _, _, subject, _ = await customer(populated, seed_plan)
    await activate(populated)
    created = await submit(populated, trust, database, subject, "q-off", queue="off")
    assert created.status_code == 201 and await runs(populated) == []


async def test_enabled_queue_without_activation_refuses_and_stores_nothing(populated, seed_plan, trust, database):  # fmt: skip
    _, _, subject, _ = await customer(populated, seed_plan)
    refused = await submit(populated, trust, database, subject, "q-inactive")
    assert refused.status_code == 503 and await counts(populated) == (0, 0, 0, 0, 0, 0)


async def test_activation_is_once_only_and_needs_an_active_administrator(populated, seed_plan, trust, database):  # fmt: skip
    _, _, subject, operator = await customer(populated, seed_plan)
    historical = (await submit(populated, trust, database, subject, "q-hist", queue="off")).json()["rfq_id"]  # fmt: skip
    with pytest.raises(workflow_queue.ActivationRefused, match="approval_requires_active_administrator"):  # fmt: skip
        await workflow_queue.activate(populated, operator)
    assert await activate(populated) == [historical]
    with pytest.raises(workflow_queue.ActivationRefused, match="already_activated"):
        await activate(populated)


async def test_enqueue_failure_rolls_back_every_row_of_the_submission(populated, seed_plan, trust, database, monkeypatch):  # fmt: skip
    _, _, subject, _ = await customer(populated, seed_plan)
    await activate(populated)
    real = workflow_queue.enqueue

    def broken(session, **kwargs):  # the run violates ck_workflow_runs_rule_04 at flush
        run_id = real(session, **kwargs)
        next(o for o in session.new if isinstance(o, WorkflowRun)).attempt_count = 99
        return run_id

    monkeypatch.setattr(workflow_queue, "enqueue", broken)
    refused = await submit(populated, trust, database, subject, "q-broken")
    assert refused.status_code == 422  # CHECK (23514): refused, nothing written
    assert await counts(populated) == (0, 0, 0, 0, 0, 0)
    monkeypatch.setattr(workflow_queue, "enqueue", real)
    retried = await submit(populated, trust, database, subject, "q-broken")  # same key works
    assert retried.status_code == 201 and (await counts(populated))[4] == 1


# --- database-enforced queue uniqueness -----------------------------------------------------------


async def test_database_refuses_a_second_active_run_or_correlation_for_one_rfq(populated, seed_plan, trust, database):  # fmt: skip
    user, mine, subject, _ = await customer(populated, seed_plan)
    await activate(populated)
    rfq_id = (await submit(populated, trust, database, subject, "q-uniq")).json()["rfq_id"]
    duplicate = {"customer_id": mine, "rfq_id": rfq_id, "initiated_by": user, "status": "queued",
                 "graph_version": "g", "rule_versions": {}, "source_snapshot_versions": {}}  # fmt: skip
    for row, constraint in (
        ({**duplicate, "correlation_id": f"other-{rfq_id}"}, "uq_workflow_runs_active_rfq"),
        (
            {
                **duplicate,
                "status": "completed",
                "disposition": "ready_for_draft",
                "correlation_id": rfq_id,
            },
            "uq_workflow_runs_correlation_id",
        ),  # fmt: skip
    ):
        with pytest.raises(IntegrityError, match=constraint):
            async with populated.begin_nested():
                await populated.execute(insert(WorkflowRun).values(workflow_run_id=uuid.uuid4(), **row))  # fmt: skip
    # A settled run frees the slot (a later, separately correlated run is admissible).
    await populated.execute(update(WorkflowRun).values(status="blocked", disposition="not_evaluated"))  # fmt: skip
    async with populated.begin_nested():
        await populated.execute(insert(WorkflowRun).values(workflow_run_id=uuid.uuid4(), **{**duplicate, "correlation_id": f"other-{rfq_id}"}))  # fmt: skip


@pytest.mark.parametrize(
    "change",
    [
        {"lease_owner": "w-1"},  # a queued run holds no lease
        {"status": "running", "lease_owner": "w-1"},  # owner and expiry together
        {"attempt_count": 4, "claim_generation": 4},
        {"attempt_count": 2, "claim_generation": 1},  # the fence never trails the attempts
        {"disposition": "ready_for_draft"},  # outcomes only on settled runs
        {"status": "completed", "disposition": "approved"},
        {
            "status": "failed",
            "disposition": "technical_failure",
            "last_error": "Traceback: boom",
        },  # fmt: skip
    ],
)
async def test_database_refuses_inconsistent_queue_state(populated, seed_plan, trust, database, change):  # fmt: skip
    _, _, subject, _ = await customer(populated, seed_plan)
    await activate(populated)
    await submit(populated, trust, database, subject, "q-state")
    with pytest.raises(IntegrityError, match="ck_workflow_runs_"):
        async with populated.begin_nested():
            await populated.execute(update(WorkflowRun).values(**change))


# --- runtime integrity and historical compatibility -----------------------------------------------


async def test_runtime_integrity_accepts_historical_and_queued_rfqs(populated, seed_database, seed_plan, trust, database):  # fmt: skip
    _, _, subject, _ = await customer(populated, seed_plan)
    await submit(populated, trust, database, subject, "q-before", queue="off")  # pre-activation
    await activate(populated)
    await submit(populated, trust, database, subject, "q-after")
    report = await runtime_audit(populated, seed_database, seed_plan)
    assert report["status"] == "PASS" and report["policy_overlay"] == "applied:rbac-policy-v4"
    records = report["runtime_records"]
    assert (records["rfqs"], records["workflow_runs"], records["customers"]) == (2, 1, 1)


async def test_customers_keep_working_under_v3_without_any_queue(populated, seed_database, seed_plan, trust, database):  # fmt: skip
    _, _, subject, _ = await customer(populated, seed_plan, policy="rbac-policy-v3")
    assert (await submit(populated, trust, database, subject, "q-v3", queue="off")).status_code == 201  # fmt: skip
    report = await runtime_audit(populated, seed_database, seed_plan)
    assert report["policy_overlay"] == "applied:rbac-policy-v3" and report["runtime_records"]["workflow_runs"] == 0  # fmt: skip


async def test_post_activation_rfq_without_its_run_fails_integrity(populated, seed_database, seed_plan, trust, database):  # fmt: skip
    _, _, subject, _ = await customer(populated, seed_plan)
    await activate(populated)
    await submit(populated, trust, database, subject, "q-gap", queue="off")  # the gap A1 forbids
    await fails(populated, seed_database, seed_plan, "RFQ without exactly its processing request")


async def run_row(populated):
    return (await runs(populated))[0]


TAMPERING = [
    # (description, change applied to the stored world, the rule that must fire)
    ("forged second run", "duplicate", "RFQ without exactly its processing request"),
    ("run for a historical RFQ", "historical", "RFQ without exactly its processing request"),
    ("orphan enqueue event", "orphan_event", "RFQ without exactly its processing request"),
    ("wrong initiator", "initiator", "workflow run differs from its RFQ"),
    ("altered rule pins", "rules", "workflow run differs from its RFQ"),
    ("settled without attempt trail", "settled", "settled run without its attempt record"),
    ("forged attempt actor", "attempt_actor", "attempt event is inconsistent"),
    ("second activation", "activation", "more than one queue activation"),
]


@pytest.mark.parametrize("name,change,rule", TAMPERING, ids=[t[0] for t in TAMPERING])
async def test_runtime_integrity_detects_forged_duplicate_or_orphaned_queue_records(populated, seed_database, seed_plan, trust, database, name, change, rule):  # fmt: skip
    user, mine, subject, operator = await customer(populated, seed_plan)
    historical = (await submit(populated, trust, database, subject, "q-t0", queue="off")).json()["rfq_id"]  # fmt: skip
    await activate(populated)
    rfq_id = (await submit(populated, trust, database, subject, "q-t1")).json()["rfq_id"]
    run = await run_row(populated)
    base = {"customer_id": mine, "initiated_by": user, "status": "blocked", "disposition": "not_evaluated",
            "graph_version": workflow_queue.GRAPH_VERSION, "rule_versions": workflow_queue.RULE_VERSIONS,
            "source_snapshot_versions": {}}  # fmt: skip
    if change == "duplicate":
        await populated.execute(insert(WorkflowRun).values(workflow_run_id=uuid.uuid4(), rfq_id=rfq_id, correlation_id=f"x-{rfq_id}", **base))  # fmt: skip
    elif change == "historical":
        await populated.execute(insert(WorkflowRun).values(workflow_run_id=uuid.uuid4(), rfq_id=historical, correlation_id=historical, **base))  # fmt: skip
    elif change == "orphan_event":
        populated.add(AuditEvent(actor_id=user, customer_id=mine, rfq_id=historical, operation="workflow.enqueue", outcome="success", correlation_id=historical, event_data={"workflow_run_id": str(uuid.uuid4())}))  # fmt: skip
    elif change == "initiator":
        await populated.execute(update(WorkflowRun).values(initiated_by=operator))
    elif change == "rules":
        await populated.execute(update(WorkflowRun).values(rule_versions={**workflow_queue.RULE_VERSIONS, "pricing": "price-v0"}))  # fmt: skip
    elif change == "settled":
        await populated.execute(update(WorkflowRun).values(status="completed", disposition="ready_for_draft", attempt_count=1, claim_generation=1))  # fmt: skip
    elif change == "attempt_actor":
        await populated.execute(update(WorkflowRun).values(status="completed", disposition="ready_for_draft", attempt_count=1, claim_generation=1))  # fmt: skip
        data = {"workflow_run_id": str(run.workflow_run_id), "attempt": 1, "claim_generation": 1, "status": "completed", "disposition": "ready_for_draft", "error": None}  # fmt: skip
        populated.add(AuditEvent(actor_id=user, customer_id=mine, rfq_id=rfq_id, operation="workflow.attempt", outcome="success", correlation_id=rfq_id, event_data=data))  # fmt: skip
    elif change == "activation":
        populated.add(AuditEvent(actor_id=await admin(populated), operation="workflow.queue_activate", outcome="success", correlation_id="workflow-queue-activation", event_data={"historical_rfq_ids": [], "graph_version": workflow_queue.GRAPH_VERSION}))  # fmt: skip
    await populated.flush()
    await fails(populated, seed_database, seed_plan, rule)
