"""Staff discovery with real JWT/RBAC in guarded, rolled-back test fixtures only."""

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_auth import bearer
from test_auth import trust as trust
from test_auth_database import app_with, call
from test_auth_database import database as database
from test_database_queries import other_customer, statements
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_operator_assignment_admin_database import staff
from test_quote_drafts_database import args, runtime_rfq
from test_quote_versions_database import rargs
from test_rbac_database import bound
from test_staff_quotes_database import path
from test_staff_quotes_database import world as world

import rfq_copilot.repositories as repositories
from rfq_copilot.integrity import fingerprint
from rfq_copilot.models import Quote, QuoteVersion, RfqAssignment, RolePermission, User
from rfq_copilot.quote_drafts import create_quote_draft
from rfq_copilot.quote_versions import revise_quote

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

SUMMARY_FIELDS = {
    "quote_id",
    "rfq_id",
    "status",
    "current_version",
    "created_at",
    "expiry_date",
    "currency",
    "total_usd",
}


async def listing(world, **params):
    return await call(world.app, "GET", path(), headers=world.headers, params=params)


@pytest_asyncio.fixture(loop_scope="module")
async def three_quotes(populated, world):
    """Equal creation timestamps exercise the identifier tie breaker, with real versions."""
    ids = [world.draft.quote_id]
    for suffix in ("A", "B"):
        rfq_id = f"TEST-STAFF-LIST-{suffix}"
        await runtime_rfq(populated, rfq_id, world.principal.customer_id)
        draft = await create_quote_draft(
            populated,
            world.principal,
            rfq_id,
            **args(idempotency_key=f"staff-list-{suffix}", correlation_id=f"list-{suffix}"),
        )
        ids.append(draft.quote_id)
        populated.add(
            RfqAssignment(
                rfq_id=rfq_id,
                user_id=world.principal.user_id,
                assigned_by=world.principal.user_id,
            )
        )
    await populated.flush()
    timestamps = (await populated.scalars(
        select(Quote.created_at).where(Quote.quote_id.in_(ids))
    )).all()  # fmt: skip
    assert len(timestamps) == 3 and len(set(timestamps)) == 1
    return sorted(ids)


async def test_authorized_listing_is_a_safe_current_version_summary(populated, world):
    response = await listing(world)
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data) == {"items", "limit", "offset", "has_more"}
    assert (data["limit"], data["offset"], data["has_more"]) == (20, 0, False)
    assert len(data["items"]) == 1
    row = data["items"][0]
    stored = await populated.scalar(
        select(QuoteVersion).where(QuoteVersion.quote_id == world.draft.quote_id)
    )
    assert set(row) == SUMMARY_FIELDS
    assert (row["quote_id"], row["rfq_id"], row["status"], row["current_version"]) == (
        world.draft.quote_id,
        world.draft.rfq_id,
        "draft",
        1,
    )
    assert row["total_usd"] == str(stored.total_usd)
    assert row["currency"] == stored.currency
    assert row["expiry_date"] == stored.expiry_date.isoformat()
    for hidden in ("cost_breakdown", "part_json", "audit", "prompt", "reasoning", "calculation"):
        assert hidden not in response.text


async def test_pagination_orders_timestamp_ties_and_looks_ahead(world, three_quotes):
    seen = []
    for offset, expected in enumerate(three_quotes):
        response = await listing(world, limit=1, offset=offset)
        assert response.status_code == 200
        data = response.json()
        assert (data["limit"], data["offset"], data["has_more"]) == (1, offset, offset < 2)
        assert [row["quote_id"] for row in data["items"]] == [expected]
        seen.append(expected)
    assert len(set(seen)) == 3
    assert (await listing(world, limit=1)).json()["items"][0]["quote_id"] == three_quotes[0]
    assert (await listing(world, offset=3)).json()["items"] == []


async def test_status_filter_and_current_version_join(populated, world, three_quotes):
    await revise_quote(populated, world.principal, world.draft.quote_id, **rargs())
    response = await listing(world, status="revised_draft")
    assert response.status_code == 200
    rows = response.json()["items"]
    assert len(rows) == 1 and rows[0]["quote_id"] == world.draft.quote_id
    assert rows[0]["current_version"] == 2
    stored = await populated.scalar(
        select(QuoteVersion).where(
            QuoteVersion.quote_id == world.draft.quote_id, QuoteVersion.version == 2
        )
    )
    assert rows[0]["total_usd"] == str(stored.total_usd)
    assert rows[0]["expiry_date"] == stored.expiry_date.isoformat()
    draft_rows = (await listing(world, status="draft", limit=1)).json()
    assert len(draft_rows["items"]) == 1 and draft_rows["has_more"] is True
    assert draft_rows["items"][0]["quote_id"] in set(three_quotes) - {world.draft.quote_id}


@pytest.mark.parametrize("identifier", ["quote", "rfq"])
async def test_exact_identifier_search_and_combined_filter(world, identifier):
    value = world.draft.quote_id if identifier == "quote" else world.draft.rfq_id
    response = await listing(world, q=value, status="draft")
    assert response.status_code == 200
    assert [r["quote_id"] for r in response.json()["items"]] == [world.draft.quote_id]
    assert (await listing(world, q=value, status="released")).json()["items"] == []


@pytest.mark.parametrize("query", ["%", "QUOTE-*", "' OR 1=1 --", "QUOTE-UNKNOWN"])
async def test_search_is_literal_not_sql_or_a_wildcard(world, query):
    response = await listing(world, q=query)
    assert response.status_code == 200 and response.json()["items"] == []


async def test_customer_binding_and_rfq_ownership_isolate_list(populated, seed_plan, trust, world):
    user = await staff(populated, role="sales_manager")
    await bound(populated, user, other_customer(seed_plan))
    subject = "auth|foreign-staff-list"
    await populated.execute(
        update(User).where(User.user_id == user.user_id).values(authentication_subject=subject)
    )
    # Even an assignment to the foreign RFQ and a forged customer claim cannot widen scope.
    populated.add(RfqAssignment(
        rfq_id=world.draft.rfq_id, user_id=user.user_id, assigned_by=world.principal.user_id
    ))  # fmt: skip
    await populated.flush()
    headers = bearer(trust.mint(sub=subject, customer_id=world.principal.customer_id))
    for query in ({}, {"q": world.draft.quote_id}, {"q": world.draft.rfq_id}):
        response = await call(world.app, "GET", path(), headers=headers, params=query)
        assert response.status_code == 200
        assert response.json()["items"] == []


@pytest.mark.parametrize("assignment", ["missing", "revoked"])
async def test_assignment_scope_applies_before_pagination(
    populated, trust, database, world, assignment
):
    where = (
        RfqAssignment.rfq_id == world.draft.rfq_id,
        RfqAssignment.user_id == world.principal.user_id,
    )
    if assignment == "missing":
        await populated.execute(delete(RfqAssignment).where(*where))
    else:
        await populated.execute(update(RfqAssignment).where(*where).values(revoked_at=func.now()))
    response = await listing(world, limit=1, q=world.draft.quote_id)
    assert response.status_code == 200
    assert response.json()["items"] == [] and response.json()["has_more"] is False
    app = app_with(trust, database, permission_policy="v3", rfq_assignments="off")
    response = await call(
        app, "GET", path(), headers=world.headers, params={"q": world.draft.quote_id}
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


async def test_empty_results_have_no_phantom_next_page(world):
    response = await listing(world, status="released")
    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 20, "offset": 0, "has_more": False}


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": "many"},
        {"offset": -1},
        {"offset": 10001},
        {"offset": "1.5"},
        {"status": "unknown"},
        {"status": "Draft"},
        {"q": ""},
        {"q": " "},
        {"q": "x" * 65},
        {"q": "a\x00b"},
        {"customer_id": "CUST-OTHER"},
        {"assigned_user": "USER-OTHER"},
    ],
)
async def test_invalid_inputs_are_sanitized_422(world, params):
    response = await listing(world, **params)
    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_VALIDATION_ERROR"
    assert "CUST-OTHER" not in response.text and "USER-OTHER" not in response.text


async def test_operational_staff_role_without_database_read_grant_is_denied(populated, world):
    role = await populated.scalar(select(User.role).where(User.user_id == world.principal.user_id))
    await populated.execute(
        update(RolePermission)
        .where(RolePermission.role == role, RolePermission.permission == "view_assigned_rfqs")
        .values(allowed=False)
    )
    for endpoint in (path(), path(world.draft.quote_id)):
        response = await call(world.app, "GET", endpoint, headers=world.headers)
        assert response.status_code == 403


async def test_list_is_read_only_and_uses_one_bounded_projection_query(
    populated, seed_database, world, three_quotes, monkeypatch
):
    before = await fingerprint(populated)
    original, modes = repositories.list_staff_quotations, []

    async def checked(session, *args, **kwargs):
        modes.append(await session.scalar(text("SHOW transaction_read_only")))
        return await original(session, *args, **kwargs)

    async def no_commit(self):
        raise AssertionError("GET must not commit")

    monkeypatch.setattr(repositories, "list_staff_quotations", checked)
    monkeypatch.setattr(AsyncSession, "commit", no_commit)
    with statements(seed_database) as captured:
        response = await listing(world, limit=1)
    assert response.status_code == 200 and modes == ["on"]
    quote_reads = [
        sql
        for sql in captured
        if sql.lstrip().upper().startswith("SELECT") and "business.quotes" in sql
    ]
    assert len(quote_reads) == 1
    sql = quote_reads[0].upper()
    assert "LIMIT" in sql and "OFFSET" in sql and "ORDER BY" in sql
    assert "QUOTE_LINE_ITEMS" not in sql and "PART_JSON" not in sql
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in captured)
    assert not any("FOR SHARE" in s.upper() or "FOR UPDATE" in s.upper() for s in captured)
    assert await fingerprint(populated) == before
