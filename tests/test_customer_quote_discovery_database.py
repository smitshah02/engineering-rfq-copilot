"""Discovery via real JWT/RBAC and scoped SELECTs in guarded rolled-back test fixtures."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from history_corruption import corrupted
from sqlalchemy import func, select, update
from test_api_reads_database import app_for, get, rfqs_of
from test_auth import bearer
from test_auth import trust as trust
from test_auth_database import call
from test_auth_database import database as database
from test_customer_access_database import customer_account, v3
from test_customer_rfq_create_database import bound_customer, second_account
from test_database_queries import other_customer, statements
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_operator_assignment_admin_database import staff
from test_rbac_database import bound

from rfq_copilot import repositories
from rfq_copilot.models import RFQ, Quote, QuoteVersion, RfqAssignment, RolePermission

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(loop_scope="module")
async def released(populated):
    # Reuse seeded approved versions; stage a historical release only in the guarded
    # fixture transaction. No live release engine, model or external tool is invoked.
    quote_id, rfq_id, owner, approver, version, expiry = (await populated.execute(
        select(
            Quote.quote_id, Quote.rfq_id, RFQ.customer_id, Quote.approver_id,
            QuoteVersion.version, QuoteVersion.expiry_date,
        )
        .select_from(Quote)
        .join(RFQ, RFQ.rfq_id == Quote.rfq_id)
        .join(QuoteVersion, (QuoteVersion.quote_id == Quote.quote_id)
              & (QuoteVersion.version == Quote.approved_version))
        .where(Quote.status == "approved", QuoteVersion.total_usd.is_not(None))
        .order_by(Quote.quote_id).limit(1)
    )).one()  # fmt: skip
    async with corrupted(populated, "quotes"):
        await populated.execute(
            update(Quote)
            .where(Quote.quote_id == quote_id)
            .values(
                status="released",
                released_by=approver,
                released_at=func.now() - timedelta(days=1),
            )
        )
    populated.expunge_all()
    return SimpleNamespace(
        quote_id=quote_id,
        rfq_id=rfq_id,
        owner=owner,
        version=version,
        expiry=expiry,
    )


async def test_real_customer_discovers_only_its_approved_release(
    populated,
    seed_plan,
    released,
    trust,
    database,
):
    _, subject, operator = await customer_account(populated, released.owner)
    app = v3(trust, database)
    headers = bearer(trust.mint(sub=subject, role="administrator", customer_id="FORGED"))
    before = await populated.scalar(select(RFQ.status).where(RFQ.rfq_id == released.rfq_id))
    detail = await call(app, "GET", f"/api/v1/rfqs/{released.rfq_id}", headers=headers)
    assert detail.status_code == 200
    reference = detail.json()["released_quote"]
    assert reference["quote_id"] == released.quote_id and reference["version"] == released.version
    assert set(reference) == {"quote_id", "version", "released_at", "expiry_date"}
    assert detail.json()["status"] == before
    page = await call(app, "GET", "/api/v1/rfqs?limit=100", headers=headers)
    matching = next(r for r in page.json()["items"] if r["rfq_id"] == released.rfq_id)
    assert matching["released_quote"] == reference
    quote = await call(app, "GET", f"/api/v1/quotes/{released.quote_id}", headers=headers)
    assert quote.status_code == 200 and quote.json()["version"] == reference["version"]
    assert quote.json()["rfq_id"] == released.rfq_id
    # An independently bound customer, even with forged owner claims, cannot discover it.
    stranger = other_customer(seed_plan)
    if stranger == released.owner:
        stranger = next(
            c["customer_id"]
            for c in seed_plan.rows["customers"]
            if c["customer_id"] != released.owner
        )
    await bound_customer(populated, stranger)
    other_subject = await second_account(
        populated,
        stranger,
        operator,
        user_id="U-CUST-OTHER",
    )
    other_headers = bearer(trust.mint(sub=other_subject, customer_id=released.owner))
    other_page = await call(app, "GET", "/api/v1/rfqs?limit=100", headers=other_headers)
    assert other_page.status_code == 200
    assert released.quote_id not in other_page.text
    for path in (f"/rfqs/{released.rfq_id}", f"/quotes/{released.quote_id}"):
        assert (await call(app, "GET", f"/api/v1{path}", headers=other_headers)).status_code == 404
    for path in ("/staff/quotes", f"/staff/quotes/{released.quote_id}"):
        assert (await call(app, "GET", f"/api/v1{path}", headers=headers)).status_code == 403


async def test_revoked_quote_read_grant_removes_reference_without_hiding_rfq(
    populated,
    released,
    trust,
    database,
):
    _, subject, _ = await customer_account(populated, released.owner)
    app, headers = v3(trust, database), bearer(trust.mint(sub=subject))
    await populated.execute(
        update(RolePermission)
        .where(
            RolePermission.role == "customer",
            RolePermission.permission == "customer_view_quote",
        )
        .values(allowed=False)
    )
    detail = await call(app, "GET", f"/api/v1/rfqs/{released.rfq_id}", headers=headers)
    assert detail.status_code == 200 and detail.json()["released_quote"] is None
    assert released.quote_id not in detail.text
    assert (
        await call(app, "GET", f"/api/v1/quotes/{released.quote_id}", headers=headers)
    ).status_code == 403


@pytest.mark.parametrize(
    "status",
    [
        "draft",
        "revised_draft",
        "pending_approval",
        "approved",
        "rejected",
        "expired",
        "clarification_required",
        "blocked",
    ],
)
async def test_unreleased_statuses_are_filtered_in_sql(populated, released, status):
    async with corrupted(populated, "quotes"):
        await populated.execute(
            update(Quote)
            .where(Quote.quote_id == released.quote_id)
            .values(
                status=status,
                released_at=None,
                released_by=None,
            )
        )
    assert (
        await repositories.released_quotation_references(
            populated,
            [released.rfq_id],
            customer_id=released.owner,
        )
        == ()
    )


async def test_future_release_timestamp_is_not_discoverable(populated, released):
    async with corrupted(populated, "quotes"):
        await populated.execute(
            update(Quote)
            .where(Quote.quote_id == released.quote_id)
            .values(
                released_at=func.now() + timedelta(days=1),
            )
        )
    assert (
        await repositories.released_quotation_references(
            populated,
            [released.rfq_id],
            customer_id=released.owner,
        )
        == ()
    )


async def test_assignment_scope_is_enforced_for_discovery(populated, released):
    user = await staff(populated)
    await bound(populated, user, released.owner)
    kwargs = {"customer_id": released.owner, "assigned_user": user.user_id}
    assert (
        await repositories.released_quotation_references(populated, [released.rfq_id], **kwargs)
        == ()
    )
    populated.add(
        RfqAssignment(
            rfq_id=released.rfq_id,
            user_id=user.user_id,
            assigned_by=user.user_id,
        )
    )
    await populated.flush()
    assert (
        len(
            await repositories.released_quotation_references(populated, [released.rfq_id], **kwargs)
        )
        == 1
    )
    await populated.execute(
        update(RfqAssignment)
        .where(
            RfqAssignment.rfq_id == released.rfq_id,
            RfqAssignment.user_id == user.user_id,
        )
        .values(revoked_at=func.now())
    )
    assert (
        await repositories.released_quotation_references(populated, [released.rfq_id], **kwargs)
        == ()
    )


@pytest.mark.parametrize("expiry", [None, date(2020, 1, 1)])
async def test_missing_or_elapsed_expiry_remains_a_readable_release(populated, released, expiry):
    async with corrupted(populated, "quote_versions"):
        await populated.execute(
            update(QuoteVersion)
            .where(
                QuoteVersion.quote_id == released.quote_id,
                QuoteVersion.version == released.version,
            )
            .values(expiry_date=expiry)
        )
    (reference,) = await repositories.released_quotation_references(
        populated,
        [released.rfq_id],
        customer_id=released.owner,
    )
    assert reference.expiry_date == expiry
    quote = await get(app_for(released.owner, populated), f"/api/v1/quotes/{released.quote_id}")
    assert quote.status_code == 200
    assert quote.json()["expiry_date"] == (None if expiry is None else expiry.isoformat())


async def test_discovery_is_one_select_and_has_no_session_mutations(
    populated,
    released,
    seed_database,
):
    with statements(seed_database) as captured:
        rows = await repositories.released_quotation_references(
            populated,
            [released.rfq_id, "MISSING-RFQ"],
            customer_id=released.owner,
        )
    assert len(rows) == 1 and len(captured) == 1
    assert captured[0].lstrip().upper().startswith("SELECT")
    assert not populated.new and not populated.dirty and not populated.deleted


async def test_reference_follows_its_rfq_through_history_pagination(populated, released):
    ids = await rfqs_of(populated, released.owner)
    offset = ids.index(released.rfq_id)
    app = app_for(released.owner, populated)
    page = (await get(app, "/api/v1/rfqs", limit=1, offset=offset)).json()
    assert page["offset"] == offset and page["has_more"] == (offset < len(ids) - 1)
    (item,) = page["items"]
    assert item["rfq_id"] == released.rfq_id
    assert item["released_quote"]["quote_id"] == released.quote_id
    beyond = (await get(app, "/api/v1/rfqs", limit=1, offset=len(ids))).json()
    assert beyond["items"] == [] and beyond["has_more"] is False


async def test_missing_commercial_data_does_not_advertise_an_unrenderable_quote(
    populated, released
):
    async with corrupted(populated, "quote_versions"):
        await populated.execute(
            update(QuoteVersion)
            .where(
                QuoteVersion.quote_id == released.quote_id,
                QuoteVersion.version == released.version,
            )
            .values(total_usd=None, snapshot_completeness="metadata_only")
        )
    assert (
        await repositories.released_quotation_references(
            populated,
            [released.rfq_id],
            customer_id=released.owner,
        )
        == ()
    )
