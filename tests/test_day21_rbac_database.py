"""Day 21 authorization end to end against the guarded PostgreSQL test database.

Real ES256-signed JWTs; no dependency override touches identity or permissions.
get_principal reads the user, the binding and the role's grants from the seeded
business tables. Expectations are derived from business.role_permissions at test time,
never from role names. Every change (bindings, grants, roles) happens inside the seeded
fixture transaction and is rolled back; the dataset and the development database are
never touched.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from test_attachments import pdf
from test_auth import bearer
from test_auth import trust as trust
from test_database_queries import customer_id, other_customer, statements
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session
from test_day20_auth_database import app_with, bind, call
from test_day20_auth_database import database as database
from test_rbac import APPROVAL, MATRIX, concrete

from rfq_copilot.api.auth import CREATE_RFQ, VIEW_ASSIGNED_RFQS, VIEW_CUSTOMER
from rfq_copilot.models import (
    RFQ,
    Customer,
    Permission,
    Quote,
    RFQAttachment,
    RolePermission,
    User,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


async def grants(session, role):
    rows = await session.scalars(
        select(RolePermission.permission).where(
            RolePermission.role == role, RolePermission.allowed.is_(True)
        )
    )
    return frozenset(rows.all())


async def one_active_user_per_role(session):
    rows = (await session.scalars(select(User).where(User.active.is_(True)).order_by(User.user_id))).all()  # fmt: skip
    chosen = {}
    for user in rows:
        chosen.setdefault(user.role, user)
    return list(chosen.values())


async def user_holding(session, required, lacking=()):
    """An active user whose role (per the database) holds ``required`` and none of ``lacking``."""
    for user in await one_active_user_per_role(session):
        held = await grants(session, user.role)
        if set(required) <= held and not set(lacking) & held:
            return user
    pytest.fail("no seeded role has that grant combination")


async def bound(session, user, customer):
    await session.execute(update(Customer).where(Customer.customer_id == customer).values(account_status="active"))  # fmt: skip
    await bind(session, user.user_id, customer)


async def own_rfq(session, customer):
    return await session.scalar(select(RFQ.rfq_id).where(RFQ.customer_id == customer).order_by(RFQ.rfq_id).limit(1))  # fmt: skip


async def release_one(session):
    """Mark one approved quote released (fixture transaction only; the seed has none)."""
    quote_id, owner, approver = (
        await session.execute(
            select(Quote.quote_id, RFQ.customer_id, Quote.approver_id)
            .join(RFQ, RFQ.rfq_id == Quote.rfq_id)
            .where(Quote.status == "approved").order_by(Quote.quote_id).limit(1)
        )
    ).one()  # fmt: skip
    await session.execute(
        update(Quote).where(Quote.quote_id == quote_id)
        .values(status="released", released_by=approver, released_at=datetime.now(UTC))
    )  # fmt: skip
    session.expunge_all()
    return quote_id, owner


def upload(rfq):
    return {"files": {"file": ("spec.pdf", pdf("Spec"), "application/pdf")}}


async def test_route_permissions_are_real_grantable_permission_records(populated):
    records = set((await populated.scalars(select(Permission.permission))).all())
    granted_somewhere = set(
        (
            await populated.scalars(
                select(RolePermission.permission).where(RolePermission.allowed.is_(True))
            )
        ).all()  # fmt: skip
    )
    assert set(MATRIX.values()) <= records & granted_somewhere
    assert APPROVAL <= records  # exist in the vocabulary, required by no route


async def test_every_role_gets_exactly_the_routes_its_database_grants_allow(
    trust, database, populated, seed_plan, tmp_path
):
    released, mine = await release_one(populated)
    rfq = await own_rfq(populated, mine)
    app = app_with(trust, database, attachment_dir=tmp_path / "store")
    roles = await one_active_user_per_role(populated)
    assert len(roles) == 6  # every seeded role is exercised
    denied_somewhere = allowed_somewhere = 0
    for user in roles:
        await bound(populated, user, mine)
        held = await grants(populated, user.role)
        auth = bearer(trust.mint(sub=user.authentication_subject))
        for (method, template), required in MATRIX.items():
            path = template.replace("{rfq_id}", rfq).replace("{quote_id}", released)
            kwargs = upload(rfq) if method == "POST" else {}
            before = await populated.scalar(select(func.count()).select_from(RFQAttachment))
            response = await call(app, method, path, headers=auth, **kwargs)
            if required in held:
                allowed_somewhere += 1
                assert response.status_code in (200, 201), (user.role, template)
            else:
                denied_somewhere += 1
                assert response.status_code == 403, (user.role, template)
                assert response.json() == {"detail": "Forbidden."}
                after = await populated.scalar(select(func.count()).select_from(RFQAttachment))
                assert after == before
    assert denied_somewhere and allowed_somewhere
    stored = list((tmp_path / "store").iterdir()) if (tmp_path / "store").exists() else []
    uploaders = [u for u in roles if CREATE_RFQ in await grants(populated, u.role)]
    assert len(stored) == len(uploaders)  # only permitted uploads wrote files


async def test_denied_request_runs_only_identity_reads(trust, database, populated, seed_plan, seed_database):  # fmt: skip
    user = await user_holding(populated, {VIEW_CUSTOMER}, lacking={VIEW_ASSIGNED_RFQS})
    mine = customer_id(seed_plan)
    await bound(populated, user, mine)
    app = app_with(trust, database)
    rfq = await own_rfq(populated, mine)
    auth = bearer(trust.mint(sub=user.authentication_subject))
    with statements(seed_database) as captured:
        response = await call(app, "GET", f"/api/v1/rfqs/{rfq}", headers=auth)
    assert response.status_code == 403
    work = [s for s in captured if "SAVEPOINT" not in s.upper()]  # the fixture's savepoints
    selects = [s for s in work if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == len(work) == 3  # user, binding, grants; no RFQ query
    assert "business.role_permissions" in selects[2]
    assert not any("business.rfqs" in s for s in captured)


async def test_valid_role_but_wrong_customer_is_concealed_404(trust, database, populated, seed_plan):  # fmt: skip
    user = await user_holding(populated, {VIEW_ASSIGNED_RFQS, CREATE_RFQ})
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    await bound(populated, user, mine)
    their_rfq = await own_rfq(populated, theirs)
    app = app_with(trust, database)
    auth = bearer(trust.mint(sub=user.authentication_subject, customer_id=theirs))
    for method, template in [("GET", "/api/v1/rfqs/{rfq_id}"), ("GET", "/api/v1/rfqs/{rfq_id}/attachments")]:  # fmt: skip
        forged = await call(app, method, template.replace("{rfq_id}", their_rfq), headers=auth)
        missing = await call(app, method, template.replace("{rfq_id}", "RFQ-NOPE"), headers=auth)
        assert forged.status_code == missing.status_code == 404
        assert forged.json() == missing.json()


async def test_correct_customer_insufficient_permission_gets_403_not_data(trust, database, populated, seed_plan):  # fmt: skip
    user = await user_holding(populated, {VIEW_CUSTOMER}, lacking={VIEW_ASSIGNED_RFQS, CREATE_RFQ})
    mine = customer_id(seed_plan)
    await bound(populated, user, mine)
    rfq = await own_rfq(populated, mine)
    app = app_with(trust, database)
    auth = bearer(trust.mint(sub=user.authentication_subject))
    assert (await call(app, "GET", "/api/v1/customers/me", headers=auth)).status_code == 200
    for method, template in MATRIX:
        if MATRIX[(method, template)] == VIEW_CUSTOMER:
            continue
        response = await call(app, method, template.replace("{rfq_id}", rfq).replace("{quote_id}", "Q-1"), headers=auth, **(upload(rfq) if method == "POST" else {}))  # fmt: skip
        assert response.status_code == 403 and rfq not in response.text


async def test_revoking_and_restoring_a_grant_applies_to_the_next_request(trust, database, populated, seed_plan):  # fmt: skip
    user = await user_holding(populated, {VIEW_ASSIGNED_RFQS})
    await bound(populated, user, customer_id(seed_plan))
    app, auth = app_with(trust, database), bearer(trust.mint(sub=user.authentication_subject))
    grant = (RolePermission.role == user.role) & (RolePermission.permission == VIEW_ASSIGNED_RFQS)
    assert (await call(app, "GET", "/api/v1/rfqs", headers=auth)).status_code == 200
    await populated.execute(update(RolePermission).where(grant).values(allowed=False))
    assert (await call(app, "GET", "/api/v1/rfqs", headers=auth)).status_code == 403
    await populated.execute(update(RolePermission).where(grant).values(allowed=True))
    assert (await call(app, "GET", "/api/v1/rfqs", headers=auth)).status_code == 200
    await populated.execute(delete(RolePermission).where(grant))
    assert (await call(app, "GET", "/api/v1/rfqs", headers=auth)).status_code == 403


async def test_role_change_applies_to_the_next_request(trust, database, populated, seed_plan):
    user = await user_holding(populated, {VIEW_ASSIGNED_RFQS})
    target = await user_holding(populated, {VIEW_CUSTOMER}, lacking={VIEW_ASSIGNED_RFQS})
    await bound(populated, user, customer_id(seed_plan))
    app, auth = app_with(trust, database), bearer(trust.mint(sub=user.authentication_subject))
    assert (await call(app, "GET", "/api/v1/rfqs", headers=auth)).status_code == 200
    await populated.execute(update(User).where(User.user_id == user.user_id).values(role=target.role))  # fmt: skip
    assert (await call(app, "GET", "/api/v1/rfqs", headers=auth)).status_code == 403
    assert (await call(app, "GET", "/api/v1/customers/me", headers=auth)).status_code == 200


async def test_role_with_no_permissions_is_forbidden_everywhere(trust, database, populated, seed_plan):  # fmt: skip
    user = await user_holding(populated, {VIEW_CUSTOMER, VIEW_ASSIGNED_RFQS})
    await bound(populated, user, customer_id(seed_plan))
    await populated.execute(delete(RolePermission).where(RolePermission.role == user.role))
    app, auth = app_with(trust, database), bearer(trust.mint(sub=user.authentication_subject))
    for method, template in MATRIX:
        response = await call(app, method, concrete(template), headers=auth)
        assert response.status_code == 403, template


async def test_unknown_roles_cannot_exist(populated):
    user = await user_holding(populated, {VIEW_CUSTOMER})
    savepoint = await populated.begin_nested()
    with pytest.raises(IntegrityError):  # users.role references business.roles
        await populated.execute(update(User).where(User.user_id == user.user_id).values(role="superuser"))  # fmt: skip
    await savepoint.rollback()


async def test_forged_privilege_claims_with_a_real_token_change_nothing(trust, database, populated, seed_plan):  # fmt: skip
    user = await user_holding(populated, {VIEW_CUSTOMER}, lacking={VIEW_ASSIGNED_RFQS, CREATE_RFQ})
    await bound(populated, user, customer_id(seed_plan))
    token = trust.mint(
        sub=user.authentication_subject,
        role="sales_manager",
        permissions=sorted({*MATRIX.values(), *APPROVAL}),
        scope="view_assigned_rfqs create_rfq",
    )
    app = app_with(trust, database)
    for method, template in MATRIX:
        if MATRIX[(method, template)] != VIEW_CUSTOMER:
            response = await call(app, method, concrete(template), headers=bearer(token))
            assert response.status_code == 403, template


async def test_disabled_user_and_revoked_binding_lose_access_despite_grants(trust, database, populated, seed_plan):  # fmt: skip
    first = await user_holding(populated, {VIEW_CUSTOMER})
    mine = customer_id(seed_plan)
    await bound(populated, first, mine)
    app, auth = app_with(trust, database), bearer(trust.mint(sub=first.authentication_subject))
    assert (await call(app, "GET", "/api/v1/customers/me", headers=auth)).status_code == 200
    await populated.execute(update(User).where(User.user_id == first.user_id).values(active=False))
    assert (await call(app, "GET", "/api/v1/customers/me", headers=auth)).status_code == 401
    await populated.execute(update(User).where(User.user_id == first.user_id).values(active=True))
    from rfq_copilot.models import UserCustomerBinding

    await populated.execute(update(UserCustomerBinding).where(UserCustomerBinding.user_id == first.user_id).values(revoked_at=func.now()))  # fmt: skip
    assert (await call(app, "GET", "/api/v1/customers/me", headers=auth)).status_code == 403


async def test_approval_authority_neither_implies_viewing_nor_changes_quotes(trust, database, populated, seed_plan, seed_database):  # fmt: skip
    approver = await user_holding(populated, {"approve_quote", "release_quote"})
    _, mine = await release_one(populated)
    await bound(populated, approver, mine)
    app, auth = app_with(trust, database), bearer(trust.mint(sub=approver.authentication_subject))
    quotes = (
        await populated.execute(
            select(Quote.quote_id, Quote.status)
            .join(RFQ, RFQ.rfq_id == Quote.rfq_id)
            .where(RFQ.customer_id == mine)  # fmt: skip
        )
    ).all()
    before = dict(quotes)
    assert "released" in before.values()
    with statements(seed_database) as captured:
        for quote_id, status in quotes:
            response = await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=auth)
            assert response.status_code == (200 if status == "released" else 404), status
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in captured)
    after = dict((await populated.execute(select(Quote.quote_id, Quote.status).where(Quote.quote_id.in_(before)))).all())  # fmt: skip
    assert after == before  # viewing is not approving or releasing
    # Approval grants alone do not open the read routes.
    view = (RolePermission.role == approver.role) & (RolePermission.permission == VIEW_ASSIGNED_RFQS)  # fmt: skip
    await populated.execute(update(RolePermission).where(view).values(allowed=False))
    for quote_id, _ in quotes[:1]:
        assert (await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=auth)).status_code == 403  # fmt: skip


async def test_two_users_interleaved_keep_their_own_permissions(trust, database, populated, seed_plan):  # fmt: skip
    reader = await user_holding(populated, {VIEW_ASSIGNED_RFQS})
    outsider = await user_holding(populated, {VIEW_CUSTOMER}, lacking={VIEW_ASSIGNED_RFQS})
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    await bound(populated, reader, mine)
    await bound(populated, outsider, theirs)
    app = app_with(trust, database)
    their_rfq = await own_rfq(populated, theirs)
    for _ in range(3):
        a = await call(app, "GET", "/api/v1/rfqs", headers=bearer(trust.mint(sub=reader.authentication_subject)))  # fmt: skip
        b = await call(app, "GET", "/api/v1/rfqs", headers=bearer(trust.mint(sub=outsider.authentication_subject)))  # fmt: skip
        assert (a.status_code, b.status_code) == (200, 403)
        assert their_rfq not in {item["rfq_id"] for item in a.json()["items"]}
