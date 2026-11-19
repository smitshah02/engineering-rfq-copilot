"""Staff quote GET through real JWT/RBAC and SQL, in guarded rolled-back test fixtures.

Drafting/revision are deterministic fixture setup only. No model, MCP or decision API.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_auth import bearer
from test_auth import trust as trust
from test_auth_database import app_with, call
from test_auth_database import database as database
from test_customer_access_database import RELEASED_FIELDS, customer_account, v3
from test_database_queries import other_customer, statements
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_operator_assignment_admin_database import first_operator, staff
from test_quote_approval_policy_database import drafted
from test_quote_versions_database import rargs
from test_rbac_database import bound, release_one
from test_workflow_identity_database import service

from rfq_copilot.api.routers import staff_quotes
from rfq_copilot.api.schemas.rfq import PartSpecification
from rfq_copilot.integrity import fingerprint
from rfq_copilot.models import (
    Quote,
    QuoteLineItem,
    QuoteVersion,
    RfqAssignment,
    User,
    UserCustomerBinding,
)
from rfq_copilot.quote_versions import revise_quote
from rfq_copilot.seeding.policy import register_policy

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


def path(quote_id=None):
    return "/api/v1/staff/quotes" + (f"/{quote_id}" if quote_id is not None else "")


@pytest_asyncio.fixture(loop_scope="module")
async def world(populated, seed_plan, trust, database):
    principal, draft = await drafted(populated, seed_plan)
    await register_policy(populated, "rbac-policy-v4")
    subject = "auth|staff-quote-reader"
    await populated.execute(
        update(User).where(User.user_id == principal.user_id).values(authentication_subject=subject)
    )
    populated.add(
        RfqAssignment(
            rfq_id=draft.rfq_id,
            user_id=principal.user_id,
            assigned_by=principal.user_id,
        )
    )
    await populated.flush()
    return SimpleNamespace(
        principal=principal, draft=draft, subject=subject,
        app=v3(trust, database), headers=bearer(trust.mint(sub=subject)),
    )  # fmt: skip


@pytest.mark.parametrize("version", [1, 2])
async def test_staff_quote_returns_current_stored_version(populated, world, version):
    if version == 2:
        await revise_quote(populated, world.principal, world.draft.quote_id, **rargs())
    response = await call(world.app, "GET", path(world.draft.quote_id), headers=world.headers)
    assert response.status_code == 200, response.text
    data = response.json()
    quote = await populated.scalar(
        select(Quote)
        .where(Quote.quote_id == world.draft.quote_id)
        .execution_options(populate_existing=True)
    )
    stored = await populated.scalar(
        select(QuoteVersion).where(
            QuoteVersion.quote_id == quote.quote_id,
            QuoteVersion.version == version,
        )
    )
    lines = (await populated.scalars(
        select(QuoteLineItem).where(
            QuoteLineItem.quote_id == quote.quote_id, QuoteLineItem.quote_version == version
        ).order_by(QuoteLineItem.line_number)
    )).all()  # fmt: skip
    assert (data["quote_id"], data["rfq_id"], data["current_version"]) == (
        quote.quote_id,
        quote.rfq_id,
        version,
    )
    assert data["status"] == quote.status
    assert data["quote_version_id"] == stored.quote_version_id
    assert data["created_at"] == quote.created_at.isoformat().replace("+00:00", "Z")
    assert data["version_created_at"] == stored.created_at.isoformat().replace("+00:00", "Z")
    assert data["expiry_date"] == stored.expiry_date.isoformat()
    assert data["total_usd"] == str(stored.total_usd) and data["currency"] == stored.currency
    assert data["estimated_lead_business_days"] == stored.estimated_lead_business_days
    assert data["rfq"]["rfq_id"] == quote.rfq_id
    assert [line["line_number"] for line in data["lines"]] == [line.line_number for line in lines]
    for actual, expected in zip(data["lines"], lines, strict=True):
        assert actual["part_id"] == expected.part_id
        assert actual["quantity"] == expected.quantity
        assert actual["line_total_usd"] == str(expected.line_total_usd)
        assert actual["part"] == PartSpecification(**expected.part_json).model_dump(mode="json")


async def test_staff_quote_requires_authentication(world):
    for endpoint in (path(), path(world.draft.quote_id)):
        response = await call(world.app, "GET", endpoint)
        assert response.status_code == 401


@pytest.mark.parametrize("change,status", [("inactive", 401), ("unbound", 403)])
async def test_staff_quote_requires_active_identity_and_binding(populated, world, change, status):
    if change == "inactive":
        await populated.execute(
            update(User).where(User.user_id == world.principal.user_id).values(active=False)
        )
    else:
        await populated.execute(
            update(UserCustomerBinding)
            .where(UserCustomerBinding.user_id == world.principal.user_id)
            .values(revoked_at=func.now())
        )
    for endpoint in (path(), path(world.draft.quote_id)):
        response = await call(world.app, "GET", endpoint, headers=world.headers)
        assert response.status_code == status


async def test_customer_cannot_read_staff_draft_or_gain_access_from_claims(populated, trust, world):
    _, subject, _ = await customer_account(populated, world.principal.customer_id)
    token = trust.mint(sub=subject, role="sales_manager", permissions=["view_assigned_rfqs"])
    headers = bearer(token)
    for quote_id in (None, world.draft.quote_id, "QUOTE-UNKNOWN"):
        response = await call(world.app, "GET", path(quote_id), headers=headers)
        assert response.status_code == 403
    customer_path = f"/api/v1/quotes/{world.draft.quote_id}"
    assert (await call(world.app, "GET", customer_path, headers=headers)).status_code == 404
    assert (await call(world.app, "GET", path(world.draft.quote_id), headers=world.headers)).status_code == 200  # fmt: skip
    assert (await call(world.app, "GET", customer_path, headers=headers)).status_code == 404


async def test_staff_quote_requires_the_existing_read_grant(populated, trust, world):
    user = await staff(populated, role="auditor")
    await bound(populated, user, world.principal.customer_id)
    await populated.execute(
        update(User)
        .where(User.user_id == user.user_id)
        .values(authentication_subject="auth|staff-quote-auditor")
    )
    headers = bearer(trust.mint(sub="auth|staff-quote-auditor"))
    for endpoint in (path(), path(world.draft.quote_id)):
        response = await call(world.app, "GET", endpoint, headers=headers)
        assert response.status_code == 403


async def test_workflow_service_read_grant_does_not_make_it_staff(populated, trust, world):
    operator = await first_operator(populated)
    provisioned = await service(populated, world.principal.customer_id, operator)
    assert provisioned.result == "service_provisioned"
    subject = await populated.scalar(
        select(User.authentication_subject).where(User.user_id == "U-SVC-1")
    )
    headers = bearer(trust.mint(sub=subject))
    for endpoint in (path(), path(world.draft.quote_id)):
        response = await call(world.app, "GET", endpoint, headers=headers)
        assert response.status_code == 403


async def test_staff_quote_hides_foreign_customer_and_unknown_ids(
    populated, seed_plan, trust, world
):
    user = await staff(populated, role="sales_manager")
    await bound(populated, user, other_customer(seed_plan))
    await populated.execute(
        update(User)
        .where(User.user_id == user.user_id)
        .values(authentication_subject="auth|foreign-staff-quote-reader")
    )
    headers = bearer(trust.mint(sub="auth|foreign-staff-quote-reader", customer_id=world.principal.customer_id))  # fmt: skip
    responses = [
        await call(
            world.app,
            "GET",
            path(quote_id),
            headers=headers,
            params={"customer_id": world.principal.customer_id},
        )
        for quote_id in (world.draft.quote_id, "QUOTE-UNKNOWN")
    ]
    assert all(r.status_code == 404 and r.json() == {"detail": "Not found."} for r in responses)


@pytest.mark.parametrize("assignment", ["missing", "revoked"])
async def test_staff_quote_enforces_optional_assignment_scope(populated, trust, database, world, assignment):  # fmt: skip
    where = (RfqAssignment.rfq_id == world.draft.rfq_id, RfqAssignment.user_id == world.principal.user_id)  # fmt: skip
    if assignment == "missing":
        await populated.execute(delete(RfqAssignment).where(*where))
    else:
        await populated.execute(update(RfqAssignment).where(*where).values(revoked_at=func.now()))
    response = await call(world.app, "GET", path(world.draft.quote_id), headers=world.headers)
    assert response.status_code == 404
    app = app_with(trust, database, permission_policy="v3", rfq_assignments="off")
    response = await call(app, "GET", path(world.draft.quote_id), headers=world.headers)
    assert response.status_code == 200


async def test_staff_quote_unknown_id_is_404(world):
    response = await call(world.app, "GET", path("QUOTE-UNKNOWN"), headers=world.headers)
    assert response.status_code == 404 and response.json() == {"detail": "Not found."}


async def test_staff_quote_response_has_only_explicit_safe_fields(world):
    response = await call(world.app, "GET", path(world.draft.quote_id), headers=world.headers)
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {
        "quote_id",
        "rfq_id",
        "status",
        "current_version",
        "quote_version_id",
        "created_at",
        "version_created_at",
        "expiry_date",
        "currency",
        "total_usd",
        "estimated_lead_business_days",
        "rfq",
        "lines",
    }
    assert set(data["rfq"]) == {"rfq_id", "status", "submitted_at"}
    for line in data["lines"]:
        assert set(line) == {"line_number", "part_id", "quantity", "line_total_usd", "part"}
        assert set(line["part"]) == set(PartSpecification.model_fields)
    for hidden in ("cost_breakdown", "calculation_id", "source_evidence", "password", "prompt", "reasoning", "actor_id", "assessment"):  # fmt: skip
        assert hidden not in response.text


async def test_staff_quote_get_is_sql_read_only_and_preserves_all_rows(populated, seed_database, world, monkeypatch):  # fmt: skip
    import rfq_copilot.repositories as repositories

    before = await fingerprint(populated)
    original, modes = repositories.get_staff_quotation, []

    async def checked(session, *args, **kwargs):
        modes.append(await session.scalar(text("SHOW transaction_read_only")))
        return await original(session, *args, **kwargs)

    async def no_commit(self):
        raise AssertionError("GET must not commit")

    monkeypatch.setattr(repositories, "get_staff_quotation", checked)
    monkeypatch.setattr(AsyncSession, "commit", no_commit)
    with statements(seed_database) as captured:
        response = await call(world.app, "GET", path(world.draft.quote_id), headers=world.headers)
    assert response.status_code == 200 and modes == ["on"]
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in captured)
    assert not any("FOR SHARE" in s.upper() or "FOR UPDATE" in s.upper() for s in captured)
    assert await fingerprint(populated) == before


async def test_staff_quote_never_returns_a_truncated_line_collection(world, monkeypatch):
    monkeypatch.setattr(staff_quotes, "MAX_PARTS", 1)  # this fixture has two priced lines
    response = await call(world.app, "GET", path(world.draft.quote_id), headers=world.headers)
    assert response.status_code == 404


async def test_customer_released_quote_projection_remains_unchanged(populated, trust, database):
    quote_id, owner = await release_one(populated)
    _, subject, _ = await customer_account(populated, owner)
    response = await call(
        v3(trust, database),
        "GET",
        f"/api/v1/quotes/{quote_id}",
        headers=bearer(trust.mint(sub=subject)),
    )
    assert response.status_code == 200 and set(response.json()) == RELEASED_FIELDS
