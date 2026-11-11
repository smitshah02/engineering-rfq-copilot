"""Phase 3.5 hardening against the guarded PostgreSQL test database.

Real ES256 tokens and seeded grants (Day 20/21 helpers). Every change happens inside the
seeded fixture transaction and is rolled back. The injected ``JoinedDatabase`` is
deliberately writable: it stands in for an accidentally writable injected Database.
"""

from typing import Annotated, Any

import pytest
from fastapi import Depends
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_attachments import pdf
from test_auth import bearer
from test_auth import trust as trust
from test_database_queries import customer_id, statements
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day20_auth_database import app_with, call
from test_day20_auth_database import database as database
from test_day21_rbac_database import bound, own_rfq, release_one, user_holding

from rfq_copilot.api.auth import CREATE_RFQ, VIEW_ASSIGNED_RFQS, VIEW_CUSTOMER
from rfq_copilot.api.db import get_read_session
from rfq_copilot.models import RFQ, Customer, Material, Quote, RFQAttachment
from rfq_copilot.reconcile_attachments import reconcile
from rfq_copilot.repositories import find_materials

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


async def reader(populated, seed_plan, customer=None):
    user = await user_holding(populated, {VIEW_CUSTOMER, VIEW_ASSIGNED_RFQS})
    await bound(populated, user, customer or customer_id(seed_plan))
    return user


# --- TD-009: read sessions are read-only even on a writable injected Database ---------------


async def test_read_session_refuses_writes_on_a_writable_injected_database(trust, database, populated, seed_plan, seed_database):  # fmt: skip
    user = await reader(populated, seed_plan)
    app = app_with(trust, database)
    mine = customer_id(seed_plan)
    outcome = {}

    @app.get("/probe-write")
    async def probe(session: Annotated[Any, Depends(get_read_session)]):
        outcome["mode"] = await session.scalar(text("SHOW transaction_read_only"))
        await session.execute(update(Customer).where(Customer.customer_id == mine).values(account_status="on_hold"))  # fmt: skip
        return {}

    before = await populated.scalar(select(Customer.account_status).where(Customer.customer_id == mine))  # fmt: skip
    with statements(seed_database) as captured:
        response = await call(app, "GET", "/probe-write", headers=bearer(trust.mint(sub=user.authentication_subject)))  # fmt: skip
    # Refused by PostgreSQL. (This test wrapper re-raises the driver error, hence 500; the
    # real Database maps it to 503, see the next test.)
    assert response.status_code == 500 and "on_hold" not in response.text
    assert outcome["mode"] == "on"
    assert any(s.strip().upper() == "SET TRANSACTION READ ONLY" for s in captured)
    populated.expunge_all()
    assert await populated.scalar(select(Customer.account_status).where(Customer.customer_id == mine)) == before  # fmt: skip
    # The fixture transaction itself is still writable afterwards (savepoint released).
    await populated.execute(update(Customer).where(Customer.customer_id == mine).values(account_status=before))  # fmt: skip
    ok = await call(app, "GET", "/api/v1/customers/me", headers=bearer(trust.mint(sub=user.authentication_subject)))  # fmt: skip
    assert ok.status_code == 200


# --- TD-010: keyed, customer-scoped released-quote lookup ---------------------------------


async def test_released_quote_lookup_is_keyed_and_bounded(trust, database, populated, seed_plan, seed_database):  # fmt: skip
    quote_id, owner = await release_one(populated)
    user = await reader(populated, seed_plan, owner)
    app, auth = app_with(trust, database), bearer(trust.mint(sub=user.authentication_subject))
    history = await populated.scalar(select(func.count()).select_from(Quote).join(RFQ).where(RFQ.customer_id == owner))  # fmt: skip
    with statements(seed_database) as captured:
        response = await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=auth)
    assert response.status_code == 200 and response.json()["status"] == "released"
    route = [s for s in captured if "business.quotes" in s or "business.quote_" in s]
    assert len(route) == 3, history  # quote (+rfq), versions, lines; not the whole history
    assert "business.quotes.quote_id = " in route[0] and "business.quotes.status = " in route[0]
    assert "business.rfqs.customer_id = " in route[0]


async def test_missing_unreleased_and_foreign_quotes_are_one_query_404s(trust, database, populated, seed_plan, seed_database):  # fmt: skip
    quote_id, owner = await release_one(populated)
    unreleased = await populated.scalar(select(Quote.quote_id).join(RFQ).where(RFQ.customer_id == owner, Quote.status != "released").limit(1))  # fmt: skip
    stranger = await populated.scalar(select(Customer.customer_id).where(Customer.customer_id != owner).order_by(Customer.customer_id).limit(1))  # fmt: skip
    owner_user = await reader(populated, seed_plan, owner)
    app = app_with(trust, database)
    owner_auth = bearer(trust.mint(sub=owner_user.authentication_subject))
    for target in [q for q in ("Q-DOES-NOT-EXIST", unreleased) if q]:
        with statements(seed_database) as captured:
            response = await call(app, "GET", f"/api/v1/quotes/{target}", headers=owner_auth)
        assert response.status_code == 404 and response.json() == {"detail": "Not found."}
        assert len([s for s in captured if "business.quote" in s]) == 1
    # Another customer's user, fully permitted, cannot see the released quote.
    from rfq_copilot.models import User

    other = await populated.scalar(
        select(User).where(User.role == owner_user.role, User.user_id != owner_user.user_id, User.active.is_(True)).limit(1)
    )  # fmt: skip
    assert other is not None  # same role and grants, different customer
    await bound(populated, other, stranger)
    foreign = await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=bearer(trust.mint(sub=other.authentication_subject)))  # fmt: skip
    assert foreign.status_code == 404


# --- TD-011: material paging in SQL -----------------------------------------------------


async def test_material_pages_match_the_full_ordered_catalog(trust, database, populated, seed_plan, seed_database):  # fmt: skip
    user = await reader(populated, seed_plan)
    app, auth = app_with(trust, database), bearer(trust.mint(sub=user.authentication_subject))
    everything = list(await find_materials(populated))
    assert len(everything) > 7
    pages, offset = [], 0
    while True:
        with statements(seed_database) as captured:
            body = (await call(app, "GET", "/api/v1/materials", headers=auth, params={"limit": 7, "offset": offset})).json()  # fmt: skip
        identifiers = [s for s in captured if "FROM business.materials" in s and " LIMIT " in s]
        assert len(identifiers) == 1 and " OFFSET " in identifiers[0]  # paged by the database
        pages += [item["material_id"] for item in body["items"]]
        if not body["has_more"]:
            break
        offset += 7
    assert pages == everything
    exact = await call(app, "GET", "/api/v1/materials", headers=auth, params={"limit": len(everything)})  # fmt: skip
    assert exact.json()["has_more"] is False and len(exact.json()["items"]) == len(everything)
    beyond = await call(app, "GET", "/api/v1/materials", headers=auth, params={"offset": len(everything)})  # fmt: skip
    assert beyond.json()["items"] == [] and beyond.json()["has_more"] is False


async def test_filtered_paging_and_empty_results(populated):
    category = await populated.scalar(select(Material.category).order_by(Material.material_id).limit(1))  # fmt: skip
    matching = await find_materials(populated, category=category)
    paged = await find_materials(populated, category=category, limit=1, offset=1)
    assert paged == matching[1:2]
    assert await find_materials(populated, category="no-such-category", limit=5) == ()
    assert await find_materials(populated, limit=3, offset=10_000) == ()


# --- Ambiguous COMMIT: success with a lost acknowledgement ------------------------------


async def test_commit_that_succeeds_but_reports_failure_keeps_file_and_row(trust, database, populated, seed_plan, tmp_path, monkeypatch):  # fmt: skip
    user = await user_holding(populated, {CREATE_RFQ})
    mine = customer_id(seed_plan)
    await bound(populated, user, mine)
    rfq = await own_rfq(populated, mine)
    store = tmp_path / "store"
    app = app_with(trust, database, attachment_dir=store)
    real_commit = AsyncSession.commit

    async def committed_then_lost(self):
        await real_commit(self)
        raise ConnectionResetError("acknowledgement lost")  # an OSError after success

    before = await populated.scalar(select(func.count()).select_from(RFQAttachment))
    monkeypatch.setattr(AsyncSession, "commit", committed_then_lost)
    response = await call(
        app, "POST", f"/api/v1/rfqs/{rfq}/attachments",
        headers=bearer(trust.mint(sub=user.authentication_subject)),
        files={"file": ("spec.pdf", pdf("Spec"), "application/pdf")},
    )  # fmt: skip
    monkeypatch.undo()
    assert response.status_code == 503 and "acknowledgement" not in response.text
    refs = (await populated.scalars(select(RFQAttachment.storage_ref))).all()
    assert len(refs) == before + 1  # the row committed
    files = sorted(p.name for p in store.iterdir())
    assert len(files) == 1 and files[0] in refs  # and its file was not deleted blindly

    async def unreferenced(name):
        return name not in refs

    report = await reconcile(store, refs, still_unreferenced=unreferenced, grace_seconds=3600, delete=True)  # fmt: skip
    assert (report.referenced, report.deleted, report.rows_missing_file) == (1, 0, len(refs) - 1)
    assert sorted(p.name for p in store.iterdir()) == files


async def test_real_writable_database_injected_into_reads_answers_503(test_config, trust):
    from rfq_copilot.api.app import create_app
    from rfq_copilot.api.auth import Principal, get_principal
    from rfq_copilot.database import Database

    async with Database(test_config) as writable:  # read_only=False: the TD-009 mistake
        app = create_app(trust.settings(), database=writable)
        app.dependency_overrides[get_principal] = lambda: Principal("CUST-A")
        seen = {}

        @app.get("/probe-write")
        async def probe(session: Annotated[Any, Depends(get_read_session)]):
            seen["mode"] = await session.scalar(text("SHOW transaction_read_only"))
            await session.execute(text("CREATE TEMPORARY TABLE td009_probe (x int)"))
            return {}

        response = await call(app, "GET", "/probe-write")
        assert response.status_code == 503 and response.json() == {"detail": "Service unavailable."}
        assert seen["mode"] == "on"
        async with writable.session() as direct:  # the Database itself stays writable
            assert await direct.scalar(text("SHOW transaction_read_only")) == "off"


async def test_storage_fingerprints_distinguish_test_and_development_databases():
    from conftest import PROJECT_ROOT

    from rfq_copilot.reconcile_attachments import _Catalog

    env_file = PROJECT_ROOT / ".env"
    async with _Catalog("test", env_file) as test_catalog:
        test_print = await test_catalog.fingerprint()
        assert test_print == await test_catalog.fingerprint()  # stable
    async with _Catalog("development", env_file) as development:  # read-only, catalog query
        assert await development.fingerprint() != test_print
    assert len(test_print) == 64 and all(c in "0123456789abcdef" for c in test_print)
