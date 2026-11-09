"""Business evaluation against the guarded, seeded, rolled-back test database.

Parity source: the business labels of every dev/validation RFQ (outcome, escalation
reason, draft gate, manager review, human-release flags). Contradiction findings are
Intake's job, so the labelled findings are supplied as input; recovery RFQs label
workflow faults (orchestration), so only their business gates are compared.
"""

import hashlib
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from test_database_queries import other_customer, statements
from test_database_queries import populated as populated
from test_database_seeding import SOURCE
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day09_inventory import PANEL, pinned, truth_files
from test_day10_capacity import MONDAY
from test_day11_lead_time import snapshots
from test_day14_quotation import RFQ_0041, insert_valid_runtime_rfq

from rfq_copilot.entities import Part
from rfq_copilot.models import Customer
from rfq_copilot.repositories import get_rule_version
from rfq_copilot.services.evaluation import evaluate_rfq, evaluate_stored_rfq

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

APPROVAL_SHA = hashlib.sha256((SOURCE / "config/approval_policies.json").read_bytes()).hexdigest()
GATES = (
    "draft_creation_allowed",
    "manager_review_required",
    "human_release_approval_required",
    "approval_must_be_human",
    "quote_release_authorized_for_agent",
)


async def rules(session):
    approval = await get_rule_version(
        session, "approval", "approval-v1.1", expected_sha256=APPROVAL_SHA
    )
    return {**await pinned(session), "approval_rules": approval}


async def test_business_labels_for_every_dev_and_validation_rfq(populated):
    rule_set = await rules(populated)
    pins = await snapshots(populated)
    outcomes, recovery = {}, 0
    for rfq_id, truth in truth_files():
        requested = truth["expected_delivery_date"]
        e = await evaluate_rfq(
            populated,
            [Part.from_mapping(p) for p in truth["expected_extraction"]],
            customer_id=truth["expected_customer_id"],
            **rule_set,
            **pins,
            horizon_start=MONDAY,
            requested_delivery_date=date.fromisoformat(requested) if requested else None,
            contradictions=truth["contradiction_findings"],
        )
        assert {g: getattr(e, g) for g in GATES} == {g: truth[g] for g in GATES}, rfq_id
        if truth["primary_category"] == "recovery":
            # The business outcome before the injected workflow fault.
            assert (e.outcome, e.escalation_reason) == ("quote_draft_ready_for_approval", None)
            recovery += 1
        else:
            assert (e.outcome, e.escalation_reason) == (
                truth["expected_outcome"],
                truth["escalation_reason"],
            ), rfq_id
        outcomes[e.outcome] = outcomes.get(e.outcome, 0) + 1
    assert sum(outcomes.values()) == 160 and recovery == 10
    assert outcomes == {
        "quote_draft_ready_for_approval": 83,  # 73 labelled + 10 recovery before their fault
        "clarification_required": 40,  # 20 incomplete + 20 contradictory
        "unsupported_declined": 20,
        "customer_hold_blocked_release": 10,
        "engineering_review_required": 4,
        "commercial_review_required": 3,
    }


async def test_live_customer_status_and_credit_drive_the_gates(populated):
    rule_set = await rules(populated)
    parts = [Part.from_mapping(PANEL)]
    customer = "CUST-001"

    async def evaluate():
        populated.expunge_all()
        return await evaluate_rfq(
            populated, parts, customer_id=customer, **rule_set, horizon_start=MONDAY
        )

    clean = await evaluate()
    assert (clean.outcome, clean.customer.account_status) == (
        "quote_draft_ready_for_approval",
        "active",
    )
    total = clean.quotation.quote_total_usd
    limit = clean.customer.credit_limit_usd
    await populated.execute(
        update(Customer)
        .where(Customer.customer_id == customer)
        .values(credit_exposure_usd=limit - total + Decimal("0.01"))
    )
    breach = await evaluate()
    assert (breach.outcome, breach.escalation_reason, breach.manager_review_reasons) == (
        "commercial_review_required",
        "CREDIT_LIMIT_EXCEEDED",
        ("CREDIT_LIMIT_BREACH",),
    )
    await populated.execute(
        update(Customer).where(Customer.customer_id == customer).values(account_status="on_hold")
    )
    held = await evaluate()
    assert (held.outcome, held.escalation_reason) == (
        "customer_hold_blocked_release",
        "CUSTOMER_ACCOUNT_RESTRICTION",
    )


async def test_stored_rfq_and_customer_scope(populated, seed_plan):
    rule_set = await rules(populated)
    customer = seed_plan.rows["rfqs"][0]["customer_id"]
    await insert_valid_runtime_rfq(populated, customer)
    e = await evaluate_stored_rfq(
        populated, "TEST-DAY14-RFQ", customer_id=customer, **rule_set, horizon_start=MONDAY
    )
    assert e.quotation.calculation_id == RFQ_0041["expected_pricing"]["calculation_id"]
    assert [o.line_number for o in e.quotation.lines] == [10, 20]
    assert e.customer.customer_id == customer
    other = other_customer(seed_plan)
    assert (
        await evaluate_stored_rfq(
            populated, "TEST-DAY14-RFQ", customer_id=other, **rule_set, horizon_start=MONDAY
        )
        is None
    )
    assert (
        await evaluate_rfq(
            populated,
            [Part.from_mapping(PANEL)],
            customer_id="CUST-NOPE",
            **rule_set,
            horizon_start=MONDAY,
        )
        is None
    )


async def test_wrong_rule_kind_is_rejected(populated):
    rule_set = await rules(populated)
    with pytest.raises(ValueError, match="approval rule"):
        await evaluate_rfq(
            populated,
            [Part.from_mapping(PANEL)],
            customer_id="CUST-001",
            **{**rule_set, "approval_rules": rule_set["pricing_rules"]},
            horizon_start=MONDAY,
        )


async def test_evaluation_reads_only_bounded_and_reproducible(populated, seed_database):
    rule_set = await rules(populated)
    table = Customer.__table__
    before = (await populated.execute(select(table).order_by(table.c.customer_id))).all()
    parts = [Part.from_mapping(s) for s in RFQ_0041["expected_extraction"]]
    populated.expunge_all()
    with statements(seed_database) as captured:
        first = await evaluate_rfq(
            populated, parts, customer_id="CUST-008", **rule_set, horizon_start=MONDAY
        )
    # Customer + contacts (2), then the quotation's 32.
    # 34 before hardening: +2 snapshot selections, -2 routing reads shared with capacity.
    assert len(captured) == 34
    assert all(s.lstrip().upper().startswith("SELECT") for s in captured)
    again = await evaluate_rfq(
        populated, parts, customer_id="CUST-008", **rule_set, horizon_start=MONDAY
    )
    assert again == first
    after = (await populated.execute(select(table).order_by(table.c.customer_id))).all()
    assert after == before
