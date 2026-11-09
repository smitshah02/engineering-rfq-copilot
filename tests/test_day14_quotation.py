"""Multi-part quotations against the guarded, seeded, rolled-back test database.

Parity source: dev/validation ground truth, end to end (inventory, schedule, delivery
findings, pricing and calculation_id) for every RFQ, including the 20 multi-part RFQs.
"""

import json
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import insert
from test_database_queries import other_customer, statements
from test_database_queries import populated as populated
from test_database_seeding import SOURCE
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day06_repositories import insert_runtime_rfq
from test_day09_inventory import pinned, truth_files
from test_day10_capacity import MONDAY
from test_day11_lead_time import snapshots

from rfq_copilot.entities import Part
from rfq_copilot.seeding.mapping import TABLES
from rfq_copilot.services.quotation import quote_parts, quote_rfq

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

D = Decimal
RFQ_0041 = json.loads((SOURCE / "evaluation/ground_truth/development/RFQ-0041.json").read_text())


async def test_ground_truth_quotations_end_to_end(populated):
    rule_set = await pinned(populated)
    pins = await snapshots(populated)
    quoted, multipart, blocked = 0, 0, 0
    for rfq_id, truth in truth_files():
        sources = truth.get("expected_extraction") or []
        if not sources:
            continue
        requested = truth["expected_delivery_date"]
        q = await quote_parts(
            populated,
            [Part.from_mapping(s) for s in sources],
            **rule_set,
            **pins,
            horizon_start=MONDAY,
            requested_delivery_date=date.fromisoformat(requested) if requested else None,
        )
        pricing = truth.get("expected_pricing")
        if pricing is None:
            if truth["primary_category"] in ("incomplete", "unsupported"):
                expected = {
                    "incomplete": "needs_clarification",
                    "unsupported": "not_manufacturable",
                }
                assert (q.result, q.blocking_lines) == (expected[truth["primary_category"]], (1,))
                assert q.quote_total_usd is None and q.calculation_id is None
                blocked += 1
            continue
        schedule = truth["expected_schedule"]
        ready = schedule["estimated_ready_date"]
        assert (q.result, q.calculation_id, str(q.quote_total_usd)) == (
            "quoted",
            pricing["calculation_id"],
            pricing["quote_total_usd"],
        ), rfq_id
        assert [str(o.priced.line_total_usd) for o in q.lines] == [
            line["line_total_usd"] for line in pricing["lines"]
        ]
        assert (q.estimated_ready_date, q.lead_business_days) == (
            date.fromisoformat(ready) if ready else None,
            schedule["lead_business_days"],
        )
        assert (q.findings == ()) is truth["draft_creation_allowed"], rfq_id
        statuses = {
            (r["material_id"], D(r["thickness_mm"])): r["sufficient"]
            for r in truth["expected_inventory"]
        }
        assert all((o.stock_status == "sufficient") is statuses[o.material_key] for o in q.lines)
        quoted += 1
        multipart += len(q.lines) > 1
    assert (quoted, multipart, blocked) == (100, 20, 40)


async def test_mixed_validity_multi_part_rfq_names_the_blocking_line(populated):
    rule_set = await pinned(populated)
    first, second = RFQ_0041["expected_extraction"]
    incomplete = {k: v for k, v in second.items() if k != "tolerance_mm"}
    q = await quote_parts(
        populated,
        [Part.from_mapping(first), Part.from_mapping(incomplete)],
        **rule_set,
        horizon_start=MONDAY,
    )
    assert (q.result, q.blocking_lines) == ("needs_clarification", (2,))
    assert q.lines[0].manufacturing.result == "valid"
    assert q.lines[1].manufacturing.findings == ("MISSING_TOLERANCE_MM",)
    assert (q.costs, q.pricing, q.lines[0].operations) == (None, None, ())


async def insert_valid_runtime_rfq(session, customer):
    """RFQ-0041's two lines stored as runtime lines numbered 10 and 20."""
    await session.execute(
        insert(TABLES["rfqs"]).values(
            rfq_id="TEST-DAY14-RFQ", customer_id=customer, source_type="mcp_shell"
        )
    )
    columns = [c.name for c in TABLES["rfq_line_items"].columns]
    for number, source in zip((10, 20), RFQ_0041["expected_extraction"], strict=True):
        part = Part.from_mapping(source)
        row = {k: v for k, v in vars(part).items() if k in columns}
        await session.execute(
            insert(TABLES["rfq_line_items"]).values(
                rfq_line_id=uuid4(), rfq_id="TEST-DAY14-RFQ", line_number=number, **row
            )
        )


async def test_quote_stored_rfq_lines_with_customer_scope(populated, seed_plan):
    rule_set = await pinned(populated)
    customer = seed_plan.rows["rfqs"][0]["customer_id"]
    await insert_valid_runtime_rfq(populated, customer)
    q = await quote_rfq(
        populated, "TEST-DAY14-RFQ", customer_id=customer, **rule_set, horizon_start=MONDAY
    )
    # Stored Numeric(12,3) values; same quotation and ID as the labelled RFQ-0041.
    assert [o.line_number for o in q.lines] == [10, 20]
    assert (q.result, q.calculation_id, str(q.quote_total_usd)) == (
        "quoted",
        RFQ_0041["expected_pricing"]["calculation_id"],
        RFQ_0041["expected_pricing"]["quote_total_usd"],
    )
    other = other_customer(seed_plan)
    assert (
        await quote_rfq(
            populated, "TEST-DAY14-RFQ", customer_id=other, **rule_set, horizon_start=MONDAY
        )
        is None
    )
    assert (
        await quote_rfq(
            populated, "MISSING", customer_id=customer, **rule_set, horizon_start=MONDAY
        )
        is None
    )


async def test_stored_rfq_with_an_incomplete_line_and_historical_rfq(populated, seed_plan):
    rule_set = await pinned(populated)
    customer = seed_plan.rows["rfqs"][0]["customer_id"]
    await insert_runtime_rfq(populated, customer)  # PART-A valid, PART-B quantity only
    q = await quote_rfq(
        populated, "TEST-DAY6-RFQ", customer_id=customer, **rule_set, horizon_start=MONDAY
    )
    assert (q.result, q.blocking_lines) == ("needs_clarification", (2,))
    assert q.lines[1].manufacturing.findings[:2] == ("MISSING_FINISH_ID", "MISSING_LENGTH_MM")
    historical = seed_plan.rows["rfqs"][0]["rfq_id"]
    with pytest.raises(ValueError, match="no runtime lines"):
        await quote_rfq(
            populated, historical, customer_id=customer, **rule_set, horizon_start=MONDAY
        )


async def test_each_service_runs_once_and_only_reads(populated, seed_database):
    rule_set = await pinned(populated)
    parts = [Part.from_mapping(s) for s in RFQ_0041["expected_extraction"]]
    populated.expunge_all()
    with statements(seed_database) as captured:
        first = await quote_parts(populated, parts, **rule_set, horizon_start=MONDAY)
    # Manufacturing 7 x 2 + stock 2 x 2 + routing 2 + workcentres 5 x 2 + materials 1 + operations 1.
    assert len(captured) == 32
    assert all(s.lstrip().upper().startswith("SELECT") for s in captured)
    assert await quote_parts(populated, parts, **rule_set, horizon_start=MONDAY) == first
