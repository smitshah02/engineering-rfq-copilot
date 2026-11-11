"""Day 18 protected reads against the guarded, seeded, rolled-back test database.

Requests go through the real application, dependencies, repositories and serializers.
Identity is injected only through dependency overrides. Seeded data lives in one
uncommitted transaction, so the data tests override the session dependency to use that
session; the lifecycle tests use a real ``Database`` and the real session dependency.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import func, insert, select, text, update
from test_database_queries import customer_id, other_customer, statements
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session

from rfq_copilot.api.app import create_app
from rfq_copilot.api.auth import VIEW_ASSIGNED_RFQS, VIEW_CUSTOMER, Principal, get_principal
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.db import get_read_session
from rfq_copilot.models import RFQ, Material, Quote
from rfq_copilot.repositories import find_materials
from rfq_copilot.seeding.mapping import TABLES

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

# Day 21: an injected principal is permission-checked like a real one; these reads need
# exactly these grants (business.role_permissions names).
READS = frozenset({VIEW_CUSTOMER, VIEW_ASSIGNED_RFQS})


DEV = ApiSettings(environment="development")


def app_for(principal_id, session=None, database=None):
    app = create_app(DEV, database=database)
    app.dependency_overrides[get_principal] = lambda: Principal(principal_id, READS)
    if session is not None:

        async def shared():
            yield session

        app.dependency_overrides[get_read_session] = shared
    return app


def client(app):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test")


async def get(app, path, **params):
    async with client(app) as http:
        return await http.get(path, params=params)


async def rfqs_of(session, customer):
    rows = await session.execute(
        select(RFQ.rfq_id, RFQ.submitted_at).where(RFQ.customer_id == customer)
    )
    return [r for r, _ in sorted(rows.all(), key=lambda row: (-row[1].timestamp(), row[0]))]


async def test_profile_is_the_verified_customers_own(populated, seed_plan):
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    row = next(c for c in seed_plan.rows["customers"] if c["customer_id"] == mine)
    response = await get(app_for(mine, populated), "/api/v1/customers/me")
    assert response.status_code == 200
    body = response.json()
    assert (body["customer_id"], body["company_name"]) == (mine, row["company_name"])
    assert theirs not in response.text
    contacts = [c for c in seed_plan.rows["contacts"] if c["customer_id"] == mine]
    assert len(body["contacts"]) == len(contacts)
    unknown = await get(app_for("CUST-NOT-REAL", populated), "/api/v1/customers/me")
    assert unknown.status_code == 404 and unknown.json() == {"detail": "Not found."}


async def test_rfq_list_is_scoped_ordered_and_paginated(populated, seed_plan):
    mine = customer_id(seed_plan)
    expected = await rfqs_of(populated, mine)
    assert len(expected) >= 2
    app = app_for(mine, populated)
    full = (await get(app, "/api/v1/rfqs")).json()
    assert [r["rfq_id"] for r in full["items"]] == expected and full["has_more"] is False
    paged = []
    for offset in range(len(expected)):
        page = (await get(app, "/api/v1/rfqs", limit=1, offset=offset)).json()
        paged += [r["rfq_id"] for r in page["items"]]
        assert page["has_more"] is (offset < len(expected) - 1)
    assert paged == expected
    beyond = (await get(app, "/api/v1/rfqs", offset=len(expected))).json()
    assert beyond["items"] == [] and beyond["has_more"] is False
    empty = (await get(app_for("CUST-NOT-REAL", populated), "/api/v1/rfqs")).json()
    assert empty == {"items": [], "limit": 20, "offset": 0, "has_more": False}


async def test_rfq_detail_hides_other_customers_rfqs(populated, seed_plan):
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    own_id = (await rfqs_of(populated, mine))[0]
    their_id = (await rfqs_of(populated, theirs))[0]
    own = await get(app_for(mine, populated), f"/api/v1/rfqs/{own_id}")
    assert own.status_code == 200
    assert set(own.json()) == {"rfq_id", "status", "submitted_at", "lines"}
    assert own.json()["lines"] == []  # historical RFQs have no runtime lines
    forged = await get(app_for(mine, populated), f"/api/v1/rfqs/{their_id}")
    absent = await get(app_for(mine, populated), "/api/v1/rfqs/RFQ-DOES-NOT-EXIST")
    assert forged.status_code == absent.status_code == 404
    assert forged.json() == absent.json() == {"detail": "Not found."}
    owner = await get(app_for(theirs, populated), f"/api/v1/rfqs/{their_id}")
    assert owner.status_code == 200


async def test_runtime_rfq_lines_are_returned_with_exact_decimals(populated, seed_plan):
    mine = customer_id(seed_plan)
    savepoint = await populated.begin_nested()
    try:
        await populated.execute(
            insert(TABLES["rfqs"]).values(
                rfq_id="TEST-API-RUNTIME", customer_id=mine, source_type="mcp_shell"
            )
        )
        await populated.execute(
            insert(TABLES["rfq_line_items"]).values(
                rfq_line_id=uuid4(),
                rfq_id="TEST-API-RUNTIME",
                line_number=1,
                part_id="TEST-API-PART",
                thickness_mm="2.000",
                quantity=7,
            )
        )
        populated.expunge_all()
        response = await get(app_for(mine, populated), "/api/v1/rfqs/TEST-API-RUNTIME")
        assert response.status_code == 200
        (line,) = response.json()["lines"]
        assert line["line_number"] == 1
        assert line["part"]["part_id"] == "TEST-API-PART"
        assert line["part"]["thickness_mm"] == "2.000" and line["part"]["quantity"] == 7
        other = await get(
            app_for(other_customer(seed_plan), populated), "/api/v1/rfqs/TEST-API-RUNTIME"
        )
        assert other.status_code == 404
    finally:
        await savepoint.rollback()


async def test_materials_match_the_catalog_repository(populated, seed_plan):
    app = app_for(customer_id(seed_plan), populated)
    total = await populated.scalar(select(func.count()).select_from(Material))
    everything = (await get(app, "/api/v1/materials", limit=100)).json()
    ids = [m["material_id"] for m in everything["items"]]
    assert len(ids) == total and ids == sorted(ids) and everything["has_more"] is False
    category = everything["items"][0]["category"]
    filtered = (await get(app, "/api/v1/materials", category=category, limit=100)).json()
    assert [m["material_id"] for m in filtered["items"]] == list(
        await find_materials(populated, category=category)
    )
    none = (await get(app, "/api/v1/materials", category="NO-SUCH-CATEGORY")).json()
    assert none["items"] == []


async def approved_quote_of(session, customer):
    return await session.scalar(
        select(Quote.quote_id)
        .join(Quote.rfq)
        .where(RFQ.customer_id == customer, Quote.status == "approved")
        .order_by(Quote.quote_id)
    )


async def test_only_released_quotes_are_visible_and_only_to_their_customer(populated):
    owner = await populated.scalar(
        select(RFQ.customer_id).join(RFQ.quote).where(Quote.status == "approved").limit(1)
    )
    quote_id = await approved_quote_of(populated, owner)
    stranger = await populated.scalar(
        select(RFQ.customer_id).where(RFQ.customer_id != owner).limit(1)
    )
    # Approved is not released: still internal.
    assert (await get(app_for(owner, populated), f"/api/v1/quotes/{quote_id}")).status_code == 404
    savepoint = await populated.begin_nested()
    try:
        approver = await populated.scalar(
            select(Quote.approver_id).where(Quote.quote_id == quote_id)
        )
        await populated.execute(
            update(Quote)
            .where(Quote.quote_id == quote_id)
            .values(status="released", released_by=approver, released_at=datetime.now(UTC))
        )
        populated.expunge_all()
        response = await get(app_for(owner, populated), f"/api/v1/quotes/{quote_id}")
        assert response.status_code == 200
        body = response.json()
        stored = await populated.scalar(select(Quote).where(Quote.quote_id == quote_id))
        assert (body["status"], body["version"]) == ("released", stored.approved_version)
        assert isinstance(body["total_usd"], str) and body["lines"]
        assert all(isinstance(line["line_total_usd"], str) for line in body["lines"])
        forged = await get(app_for(stranger, populated), f"/api/v1/quotes/{quote_id}")
        assert forged.status_code == 404 and forged.json() == {"detail": "Not found."}
    finally:
        await savepoint.rollback()


@pytest.mark.parametrize(
    ("path", "selects"),
    [
        ("/api/v1/customers/me", 2),
        ("/api/v1/rfqs", 1),
        ("/api/v1/rfqs/{rfq}", 2),
        ("/api/v1/materials", 2),
        ("/api/v1/quotes/RFQ-NOT-A-QUOTE", 1),  # TD-010: keyed lookup, was 3
    ],
)
async def test_reads_are_bounded_selects_only(populated, seed_database, seed_plan, path, selects):
    mine = customer_id(seed_plan)
    path = path.format(rfq=(await rfqs_of(populated, mine))[0])
    populated.expunge_all()
    with statements(seed_database) as captured:
        response = await get(app_for(mine, populated), path)
    assert response.status_code in (200, 404)
    assert all(s.lstrip().upper().startswith("SELECT") for s in captured)
    assert len(captured) == selects
    assert not populated.new and not populated.dirty and not populated.deleted


# --- Real session lifecycle (no session override) ----------------------------------------


async def test_real_sessions_are_per_request_and_returned_to_the_pool(
    seed_database, seed_plan, monkeypatch
):
    # The seeded rows are invisible outside the fixture's transaction, so this database
    # looks empty here; requests run the real dependency chain and Database.session().
    sessions = []
    real_session = seed_database.session

    @asynccontextmanager
    async def recording():
        async with real_session() as live:
            sessions.append(live)
            yield live

    monkeypatch.setattr(seed_database, "session", recording)
    app = app_for(customer_id(seed_plan), database=seed_database)
    baseline = seed_database.engine.pool.checkedout()  # the fixture's own lease
    async with client(app) as http:
        profiles = await asyncio.gather(*(http.get("/api/v1/customers/me") for _ in range(5)))
        lists = await asyncio.gather(*(http.get("/api/v1/rfqs") for _ in range(4)))
    assert [r.status_code for r in profiles] == [404] * 5
    assert all(r.status_code == 200 and r.json()["items"] == [] for r in lists)
    assert len({id(s) for s in sessions}) == 9  # one session per request, never shared
    assert seed_database.engine.pool.checkedout() == baseline  # every connection returned
    assert seed_database._disposed is False  # injected: owned by the caller


async def test_lazily_created_database_is_read_only(seed_database, monkeypatch):
    url = seed_database.config.url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)  # never printed
    app = app_for("CUST-ANY")
    assert (await get(app, "/api/v1/customers/me")).status_code == 404
    database = app.state.database
    try:
        assert app.state.owns_database is True
        async with database.session() as live:
            assert (await live.scalar(text("SHOW default_transaction_read_only"))) == "on"
    finally:
        await database.dispose()
