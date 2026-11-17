"""Day 51 quote-draft persistence against the guarded, seeded PostgreSQL test database.

Every test runs in the seeded fixture transaction, which is rolled back: the service only
flushes inside its own SAVEPOINT, and nothing is committed. The seed loader leaves that
transaction in ``SET CONSTRAINTS ALL IMMEDIATE``, so every test here also proves the draft
insert order works when the deferrable quote -> version pointer is immediate (the Day 51
review defect). The stale-check tests let a winning request run, in the same transaction,
between the loser's checks and its inserts; real cross-transaction races are in
``test_quote_drafts_concurrency.py``. The development database is never touched.
"""

from datetime import UTC, date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_database_queries import customer_id, other_customer
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day10_capacity import MONDAY
from test_day14_quotation import RFQ_0041
from test_day21_rbac_database import bound, grants, one_active_user_per_role

import rfq_copilot.quote_drafts as quote_drafts
from rfq_copilot.api.auth import Principal
from rfq_copilot.entities import Part
from rfq_copilot.mcp.runtime import planning_rules
from rfq_copilot.models import (
    RFQ,
    AuditEvent,
    IdempotencyRecord,
    PricingCalculation,
    Quote,
    QuoteLineItem,
    QuoteVersion,
    User,
)
from rfq_copilot.quote_drafts import DraftRefused, create_quote_draft
from rfq_copilot.repositories import get_released_quotation, get_rule_version
from rfq_copilot.seeding.mapping import TABLES
from rfq_copilot.seeding.rules import register_rule_version
from rfq_copilot.services.evaluation import evaluate_stored_rfq

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

RFQ_ID = "TEST-DAY51-RFQ"
EXPECTED = RFQ_0041["expected_pricing"]


async def runtime_rfq(session, rfq_id, customer):
    """RFQ-0041's two lines stored as runtime lines 10 and 20 (as Day 14's helper)."""
    await session.execute(
        insert(TABLES["rfqs"]).values(rfq_id=rfq_id, customer_id=customer, source_type="mcp_shell")
    )
    columns = [c.name for c in TABLES["rfq_line_items"].columns]
    for number, source in zip((10, 20), RFQ_0041["expected_extraction"], strict=True):
        row = {k: v for k, v in vars(Part.from_mapping(source)).items() if k in columns}
        await session.execute(
            insert(TABLES["rfq_line_items"]).values(
                rfq_line_id=uuid4(), rfq_id=rfq_id, line_number=number, **row
            )
        )


async def drafters(session):
    """Active users (one per role) whose role the database grants create_quote_draft."""
    users = [
        u
        for u in await one_active_user_per_role(session)
        if "create_quote_draft" in await grants(session, u.role)
    ]
    assert len(users) >= 2  # sales_representative and estimator (dataset grants)
    return users


async def setup(session, plan):
    """A drafter bound to the RFQ's customer, a runtime RFQ, approval-v1.2, workflow-v1.1
    and workflow-v1.2 registered (in this rolled-back fixture transaction only)."""
    await register_rule_version(session, "approval-v1.2")
    await register_rule_version(session, "workflow-v1.1")
    await register_rule_version(session, "workflow-v1.2")
    user = (await drafters(session))[0]
    mine = customer_id(plan)
    await bound(session, user, mine)
    await runtime_rfq(session, RFQ_ID, mine)
    # Permissions on the Principal are deliberately empty: the service re-reads grants.
    return Principal(mine, frozenset(), user_id=user.user_id)


def args(**changes):
    return {"idempotency_key": "draft-1", "correlation_id": "corr-1", "horizon_start": MONDAY,
            **changes}  # fmt: skip


async def count(session, model, *where):
    statement = select(func.count()).select_from(model)
    return await session.scalar(statement.where(*where) if where else statement)


async def nothing_written(session, rfq_id=RFQ_ID):
    assert await count(session, Quote, Quote.rfq_id == rfq_id) == 0
    assert await count(session, QuoteVersion, QuoteVersion.quote_id.like("QUOTE-%")) == 0
    assert await count(session, QuoteLineItem, QuoteLineItem.quote_id.like("QUOTE-%")) == 0
    assert await count(session, AuditEvent, AuditEvent.rfq_id == rfq_id) == 0
    assert await count(session, IdempotencyRecord) == 0
    assert await count(session, PricingCalculation, PricingCalculation.rfq_id == rfq_id) == 0


async def refused(reason, coroutine):
    with pytest.raises(DraftRefused) as caught:
        await coroutine
    assert caught.value.reason == reason


# --- Persistence correctness ----------------------------------------------------------------


async def test_draft_is_persisted_non_binding_and_bound_to_the_trusted_evaluation(
    populated, seed_plan, monkeypatch
):
    principal = await setup(populated, seed_plan)

    async def no_commit(self):
        raise AssertionError("the service must never commit")

    monkeypatch.setattr(AsyncSession, "commit", no_commit)
    draft = await create_quote_draft(populated, principal, RFQ_ID, **args())
    monkeypatch.undo()

    # Same deterministic result as an independent evaluation under the same pins.
    approval = await get_rule_version(populated, "approval", "approval-v1.2")
    independent = await evaluate_stored_rfq(
        populated, RFQ_ID, customer_id=principal.customer_id, **await planning_rules(populated),
        approval_rules=approval, horizon_start=MONDAY,
    )  # fmt: skip
    assert draft.evaluation_fingerprint == independent.evaluation_fingerprint
    assert (draft.calculation_id, str(draft.total_usd)) == (
        EXPECTED["calculation_id"],
        EXPECTED["quote_total_usd"],
    )
    assert (draft.status, draft.version, draft.replayed) == ("draft", 1, False)
    assert (draft.human_approval_required, draft.approved, draft.released) == (True, False, False)
    assert draft.rule_versions == ("mfg-v1.0", "price-v1.0", "approval-v1.2", "workflow-v1.2")

    quote = await populated.scalar(select(Quote).where(Quote.rfq_id == RFQ_ID))
    assert (quote.quote_id, quote.status, quote.current_version) == (draft.quote_id, "draft", 1)
    assert quote.created_by == principal.user_id
    assert (quote.submitted_by, quote.approved_version, quote.approver_id) == (None, None, None)
    assert (quote.approval_timestamp, quote.released_by, quote.released_at) == (None, None, None)
    rfq = await populated.scalar(select(RFQ).where(RFQ.rfq_id == RFQ_ID))
    assert rfq.customer_id == principal.customer_id

    version = await populated.scalar(select(QuoteVersion).where(QuoteVersion.quote_id == quote.quote_id))  # fmt: skip
    assert (version.version, version.is_current, version.approval_valid) == (1, True, False)
    assert (version.snapshot_completeness, version.currency) == ("complete", "USD")
    assert version.total_usd == Decimal(EXPECTED["quote_total_usd"])
    assert (version.pricing_rule_version, version.manufacturing_rule_version) == ("price-v1.0", "mfg-v1.0")  # fmt: skip
    assert (version.approval_rule_version, version.workflow_rule_version) == ("approval-v1.2", "workflow-v1.2")  # fmt: skip
    assert set(version.source_snapshot_versions) == {"inventory", "capacity", "dataset_version"}
    # K3: the persisted calculation record and the version's own evidence and validity.
    now = await populated.scalar(text("SELECT transaction_timestamp()"))
    assert version.created_at == quote.created_at == now  # database time, never the app clock
    assert version.calculation_id == EXPECTED["calculation_id"]  # unchanged identifier
    assert version.expiry_date == draft.expiry_date == now.astimezone(UTC).date() + timedelta(days=29)  # fmt: skip
    assert (version.rfq_id, version.horizon_start, version.requested_delivery_date) == (RFQ_ID, MONDAY, None)  # fmt: skip
    assert version.evaluation_fingerprint == draft.evaluation_fingerprint
    record = await populated.scalar(select(PricingCalculation).where(PricingCalculation.calculation_record_id == version.calculation_record_id))  # fmt: skip
    assert (record.rfq_id, record.calculation_id, record.quote_total_usd) == (RFQ_ID, version.calculation_id, version.total_usd)  # fmt: skip
    assert record.requirement_sha256 == version.rfq_source_sha256 and record.created_at == now
    # The current-version pointer resolves, and every deferred check is already satisfied.
    assert (quote.quote_id, quote.current_version) == (version.quote_id, version.version)
    await populated.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    lines = (
        await populated.scalars(
            select(QuoteLineItem)
            .where(QuoteLineItem.quote_id == quote.quote_id)
            .order_by(QuoteLineItem.line_number)
        )
    ).all()
    assert [line.line_number for line in lines] == [10, 20]
    assert sum(line.line_total_usd for line in lines) == version.total_usd
    assert all(line.cost_breakdown_json["pricing_rule_version"] == "price-v1.0" for line in lines)
    assert [line.part_json["part_id"] for line in lines] == [
        p["part_id"] for p in RFQ_0041["expected_extraction"]
    ]

    audit = await populated.scalar(select(AuditEvent).where(AuditEvent.quote_id == quote.quote_id))
    assert (audit.operation, audit.outcome, audit.actor_id) == ("quote_draft.create", "success", principal.user_id)  # fmt: skip
    assert (audit.customer_id, audit.rfq_id, audit.quote_version) == (principal.customer_id, RFQ_ID, 1)  # fmt: skip
    assert audit.event_data["horizon_start"] == MONDAY.isoformat()
    assert (audit.event_data["requested_delivery_date"], audit.event_data["contradiction_count"]) == (None, 0)  # fmt: skip
    assert audit.event_data["assignment_enforced"] is False
    receipt = await populated.scalar(select(IdempotencyRecord))
    assert (receipt.operation, receipt.idempotency_key, receipt.actor_id) == ("create_quote_draft", "draft-1", principal.user_id)  # fmt: skip
    assert receipt.result_json == draft.to_json()


async def test_internal_costs_stay_internal(populated, seed_plan):
    principal = await setup(populated, seed_plan)
    draft = await create_quote_draft(populated, principal, RFQ_ID, **args())
    public = [
        draft.to_json(),
        (await populated.scalar(select(IdempotencyRecord.result_json))),
        (await populated.scalar(select(AuditEvent.event_data).where(AuditEvent.rfq_id == RFQ_ID))),
    ]
    for payload in public:
        text = repr(payload)
        assert "cost" not in text and "margin" not in text and "labor" not in text
    # A draft is not released: the customer-facing read still finds nothing.
    assert (
        await get_released_quotation(populated, draft.quote_id, customer_id=principal.customer_id)
        is None
    )


# --- Validation and security ----------------------------------------------------------------


async def test_missing_and_cross_customer_rfqs_are_identical_and_write_nothing(
    populated, seed_plan
):
    principal = await setup(populated, seed_plan)
    await refused("not_found", create_quote_draft(populated, principal, "TEST-NO-SUCH-RFQ", **args()))  # fmt: skip
    other = (await drafters(populated))[1]
    theirs = other_customer(seed_plan)
    await bound(populated, other, theirs)
    intruder = Principal(theirs, frozenset({"create_quote_draft"}), user_id=other.user_id)
    await refused("not_found", create_quote_draft(populated, intruder, RFQ_ID, **args()))
    await nothing_written(populated)


async def test_a_caller_supplied_customer_cannot_override_the_binding(populated, seed_plan):
    principal = await setup(populated, seed_plan)
    forged = Principal(other_customer(seed_plan), frozenset({"create_quote_draft"}), user_id=principal.user_id)  # fmt: skip
    await refused("permission_denied", create_quote_draft(populated, forged, RFQ_ID, **args()))
    await nothing_written(populated)


async def test_unauthorized_roles_and_inactive_users_are_refused(populated, seed_plan):
    principal = await setup(populated, seed_plan)
    for user in await one_active_user_per_role(populated):
        if "create_quote_draft" in await grants(populated, user.role):
            continue
        await bound(populated, user, principal.customer_id)  # bound, but the role lacks it
        # A forged permission set on the Principal grants nothing.
        forged = Principal(principal.customer_id, frozenset({"create_quote_draft", "approve_quote"}), user_id=user.user_id)  # fmt: skip
        await refused("permission_denied", create_quote_draft(populated, forged, RFQ_ID, **args()))
    await refused("permission_denied", create_quote_draft(
        populated, Principal(principal.customer_id, user_id="USER-NOPE"), RFQ_ID, **args()))  # fmt: skip
    await populated.execute(update(User).where(User.user_id == principal.user_id).values(active=False))  # fmt: skip
    await refused("permission_denied", create_quote_draft(populated, principal, RFQ_ID, **args()))
    await nothing_written(populated)


async def test_a_reviewed_fingerprint_that_no_longer_matches_is_refused(populated, seed_plan):
    principal = await setup(populated, seed_plan)
    stale = args(expected_evaluation_fingerprint="0" * 64)
    await refused("evaluation_changed", create_quote_draft(populated, principal, RFQ_ID, **stale))
    await nothing_written(populated)


async def test_undraftable_and_already_quoted_rfqs_are_refused(populated, seed_plan):
    principal = await setup(populated, seed_plan)
    contradictory = args(contradictions=("drawing and BOM disagree on thickness",))
    await refused("not_draftable", create_quote_draft(populated, principal, RFQ_ID, **contradictory))  # fmt: skip
    await nothing_written(populated)
    # A historical RFQ of this customer already has its quote.
    quoted = await populated.scalar(
        select(Quote.rfq_id).join(RFQ, RFQ.rfq_id == Quote.rfq_id)
        .where(RFQ.customer_id == principal.customer_id).limit(1)
    )  # fmt: skip
    if quoted is not None:
        await refused("quote_exists", create_quote_draft(populated, principal, quoted, **args()))


async def test_no_autonomous_approval_or_release_is_possible(populated, seed_plan):
    principal = await setup(populated, seed_plan)
    draft = await create_quote_draft(populated, principal, RFQ_ID, **args())
    quote = await populated.scalar(select(Quote).where(Quote.quote_id == draft.quote_id))
    # The database itself refuses a release or approval without a human approver.
    savepoint = await populated.begin_nested()
    with pytest.raises(IntegrityError):
        await populated.execute(
            update(Quote).where(Quote.quote_id == quote.quote_id).values(status="released")
        )
    await savepoint.rollback()


# --- Transactions ---------------------------------------------------------------------------


async def test_a_failure_inside_the_write_leaves_no_partial_draft(
    populated, seed_plan, monkeypatch
):
    principal = await setup(populated, seed_plan)
    real_add = quote_drafts.add_receipt

    def bad_receipt(session, **kwargs):  # the last row fails its CHECK at the final flush
        return real_add(session, **{**kwargs, "request_sha256": "not-a-sha"})

    monkeypatch.setattr(quote_drafts, "add_receipt", bad_receipt)
    with pytest.raises(IntegrityError):  # an unexplained integrity failure is never masked
        await create_quote_draft(populated, principal, RFQ_ID, **args())
    monkeypatch.undo()
    await nothing_written(populated)
    # The caller's transaction is still usable, and a retry succeeds.
    draft = await create_quote_draft(populated, principal, RFQ_ID, **args())
    assert await count(populated, Quote, Quote.quote_id == draft.quote_id) == 1


async def test_a_dangling_reference_is_refused_by_the_database(populated, seed_plan, monkeypatch):
    principal = await setup(populated, seed_plan)
    real_add = quote_drafts.add_receipt

    def orphan(session, **kwargs):  # an actor that does not exist: FK violation
        return real_add(session, **{**kwargs, "actor_id": "USER-NOPE"})

    monkeypatch.setattr(quote_drafts, "add_receipt", orphan)
    with pytest.raises(IntegrityError):
        await create_quote_draft(populated, principal, RFQ_ID, **args())
    monkeypatch.undo()
    await nothing_written(populated)


async def test_unexpected_database_errors_propagate_and_roll_back(
    populated, seed_plan, monkeypatch
):
    principal = await setup(populated, seed_plan)
    real_add = quote_drafts.add_receipt

    def broken(session, **kwargs):
        real_add(session, **kwargs)
        raise ConnectionResetError("database went away")

    monkeypatch.setattr(quote_drafts, "add_receipt", broken)
    with pytest.raises(ConnectionResetError):
        await create_quote_draft(populated, principal, RFQ_ID, **args())
    monkeypatch.undo()
    await nothing_written(populated)


# --- Idempotency and concurrency ------------------------------------------------------------


async def test_a_retry_with_the_same_key_replays_without_a_duplicate(populated, seed_plan):
    principal = await setup(populated, seed_plan)
    first = await create_quote_draft(populated, principal, RFQ_ID, **args())
    again = await create_quote_draft(populated, principal, RFQ_ID, **args())
    assert again.replayed and again == type(first)(**{**vars(first), "replayed": True})
    assert await count(populated, Quote, Quote.rfq_id == RFQ_ID) == 1
    assert await count(populated, IdempotencyRecord) == 1
    assert await count(populated, AuditEvent, AuditEvent.rfq_id == RFQ_ID) == 1


async def test_a_reused_key_for_another_request_conflicts(populated, seed_plan):
    principal = await setup(populated, seed_plan)
    await create_quote_draft(populated, principal, RFQ_ID, **args())
    changed = args(requested_delivery_date=date(2027, 6, 1))
    await refused("idempotency_key_conflict", create_quote_draft(populated, principal, RFQ_ID, **changed))  # fmt: skip
    other_key = args(idempotency_key="draft-2")
    await refused("quote_exists", create_quote_draft(populated, principal, RFQ_ID, **other_key))
    assert await count(populated, Quote, Quote.rfq_id == RFQ_ID) == 1
    # The same key for another of this customer's RFQs is a different request.
    await runtime_rfq(populated, "TEST-DAY51-RFQ-2", principal.customer_id)
    await refused("idempotency_key_conflict", create_quote_draft(populated, principal, "TEST-DAY51-RFQ-2", **args()))  # fmt: skip
    assert await count(populated, IdempotencyRecord) == 1


def winner_runs_first(monkeypatch, populated, principal, **winner_args):
    """The winner completes between the loser's checks and the loser's inserts."""
    real = quote_drafts.evaluate_stored_rfq

    async def interleaved(*a, **kw):
        monkeypatch.setattr(quote_drafts, "evaluate_stored_rfq", real)
        await create_quote_draft(populated, principal, RFQ_ID, **winner_args)
        return await real(*a, **kw)

    monkeypatch.setattr(quote_drafts, "evaluate_stored_rfq", interleaved)


async def test_stale_check_identical_request_replays_the_winner(populated, seed_plan, monkeypatch):
    principal = await setup(populated, seed_plan)
    winner_runs_first(monkeypatch, populated, principal, **args())
    loser = await create_quote_draft(populated, principal, RFQ_ID, **args())
    assert loser.replayed  # met the unique constraint, then replayed the winner's receipt
    assert await count(populated, Quote, Quote.rfq_id == RFQ_ID) == 1
    assert await count(populated, IdempotencyRecord) == 1
    winner = await populated.scalar(select(Quote.quote_id).where(Quote.rfq_id == RFQ_ID))
    assert loser.quote_id == winner


async def test_stale_check_different_key_meets_the_unique_rfq(populated, seed_plan, monkeypatch):
    principal = await setup(populated, seed_plan)
    winner_runs_first(monkeypatch, populated, principal, **args(idempotency_key="winner"))
    await refused("quote_exists", create_quote_draft(populated, principal, RFQ_ID, **args()))
    assert await count(populated, Quote, Quote.rfq_id == RFQ_ID) == 1
    assert await count(populated, IdempotencyRecord) == 1  # only the winner's receipt


async def test_keys_are_isolated_per_tenant(populated, seed_plan):
    principal = await setup(populated, seed_plan)
    other = (await drafters(populated))[1]
    theirs = other_customer(seed_plan)
    await bound(populated, other, theirs)
    await runtime_rfq(populated, "TEST-DAY51-RFQ-B", theirs)
    tenant_b = Principal(theirs, frozenset(), user_id=other.user_id)
    one = await create_quote_draft(populated, principal, RFQ_ID, **args())
    two = await create_quote_draft(populated, tenant_b, "TEST-DAY51-RFQ-B", **args())
    assert not two.replayed and one.quote_id != two.quote_id
    assert (one.customer_id, two.customer_id) == (principal.customer_id, theirs)
    assert await count(populated, IdempotencyRecord) == 2
