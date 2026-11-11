"""Day 20 bearer authentication end to end against the guarded PostgreSQL test database.

No dependency override touches identity here: requests carry real ES256-signed JWTs
(test-generated key; no stored credentials), get_principal verifies them and looks the
subject up in the seeded business.users, then its customer in business.user_customer_bindings
(TD-012), through the application's own Database. Bindings are provisioned by each test
inside the seeded fixture transaction (rolled back afterwards); no existing user is bound
by default, the dataset is unchanged and the development database is never touched.
"""

from contextlib import asynccontextmanager

import httpx2
import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_attachments import pdf
from test_auth import PROTECTED, bearer
from test_auth import trust as trust
from test_database_queries import customer_id, other_customer, statements
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session

import rfq_copilot.repositories as repositories
from rfq_copilot.api.app import create_app
from rfq_copilot.models import RFQ, Customer, Quote, RFQAttachment, User, UserCustomerBinding

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


class JoinedDatabase:
    """The app's Database, bound to the fixture transaction (one savepoint per session)."""

    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def session(self):
        async with AsyncSession(
            bind=self.connection, join_transaction_mode="create_savepoint"
        ) as joined:
            yield joined


@pytest_asyncio.fixture(loop_scope="module")
async def database(populated):
    return JoinedDatabase(await populated.connection())


def app_with(trust, database, **settings):
    app = create_app(trust.settings(**settings), database=database)
    app.state.write_database = database  # uploads use the same fixture transaction
    return app


async def call(app, method, path, **kwargs):
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.request(method, path, **kwargs)


async def active_user(session):
    return await session.scalar(
        select(User).where(User.active.is_(True)).order_by(User.user_id).limit(1)
    )


async def counts(session):
    return (
        await session.scalar(select(func.count()).select_from(RFQAttachment)),
        await session.scalar(select(func.count()).select_from(User)),
    )


async def test_active_user_without_a_binding_is_forbidden(
    trust, database, populated, seed_database
):
    user = await active_user(populated)
    app = app_with(trust, database)
    before = await counts(populated)
    with statements(seed_database) as captured:
        for method, path in PROTECTED:
            response = await call(app, method, path, headers=bearer(trust.mint(sub=user.authentication_subject)))  # fmt: skip
            assert response.status_code == 403, path
            assert response.json() == {"detail": "Forbidden."}
    # Per request: the user lookup and the binding lookup, reads only; nothing else ran.
    selects = [s for s in captured if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 2 * len(PROTECTED)
    assert sum("business.users" in s for s in selects) == len(PROTECTED)
    assert sum("business.user_customer_bindings" in s for s in selects) == len(PROTECTED)
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in captured)
    assert await counts(populated) == before


async def test_forged_customer_claims_without_a_binding_grant_nothing(
    trust, database, populated, seed_plan
):
    user = await active_user(populated)
    token = trust.mint(
        sub=user.authentication_subject,
        customer_id=customer_id(seed_plan),
        role="administrator",
        scope="customers:read",
    )
    response = await call(app_with(trust, database), "GET", "/api/v1/customers/me", headers=bearer(token))  # fmt: skip
    assert response.status_code == 403
    assert customer_id(seed_plan) not in response.text


async def test_unknown_and_disabled_users_are_rejected_like_bad_tokens(trust, database, populated):
    app = app_with(trust, database)
    unknown = await call(app, "GET", "/api/v1/rfqs", headers=bearer(trust.mint(sub="auth|no-such-user")))  # fmt: skip
    assert unknown.status_code == 401 and unknown.headers["www-authenticate"] == "Bearer"
    user = await active_user(populated)
    await populated.execute(update(User).where(User.user_id == user.user_id).values(active=False))
    disabled = await call(app, "GET", "/api/v1/rfqs", headers=bearer(trust.mint(sub=user.authentication_subject)))  # fmt: skip
    assert disabled.status_code == 401
    assert disabled.json() == unknown.json() == {"detail": "Authentication required."}


async def test_subject_match_is_exact(trust, database, populated):
    user = await active_user(populated)
    app = app_with(trust, database)
    subject = user.authentication_subject
    for variant in (subject.upper() if subject != subject.upper() else subject.lower(), subject + "x", "%"):  # fmt: skip
        response = await call(app, "GET", "/api/v1/rfqs", headers=bearer(trust.mint(sub=variant)))
        assert response.status_code == 401


async def test_invalid_tokens_never_reach_the_database(trust, database, seed_database):
    from cryptography.hazmat.primitives.asymmetric import ec

    app = app_with(trust, database)
    attacker = ec.generate_private_key(ec.SECP256R1())
    with statements(seed_database) as captured:
        for token in (trust.mint(key=attacker), trust.mint(iss="https://evil.invalid/"), "x.y.z"):
            for method, path in PROTECTED:
                response = await call(app, method, path, headers=bearer(token))
                assert response.status_code == 401
    assert captured == []


# --- Real bindings (TD-012) ---------------------------------------------------------------


async def bind(session, user_id, customer, *, revoked=False):
    await session.execute(
        insert(UserCustomerBinding).values(
            user_id=user_id,
            customer_id=customer,
            revoked_at=func.now() if revoked else None,
        )
    )


async def users(session, count):
    rows = await session.scalars(
        select(User).where(User.active.is_(True)).order_by(User.user_id).limit(count)
    )
    return rows.all()


async def test_bound_user_reads_only_their_customers_data(
    trust, database, populated, seed_plan, tmp_path
):
    user = await active_user(populated)
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    await populated.execute(update(Customer).where(Customer.customer_id == mine).values(account_status="active"))  # fmt: skip
    await bind(populated, user.user_id, mine)
    app = app_with(trust, database, attachment_dir=tmp_path / "store")
    # The forged customer claim names the other customer; it is ignored.
    auth = bearer(trust.mint(sub=user.authentication_subject, customer_id=theirs))

    profile = await call(app, "GET", "/api/v1/customers/me", headers=auth)
    assert profile.status_code == 200 and profile.json()["customer_id"] == mine

    listing = await call(app, "GET", "/api/v1/rfqs", headers=auth, params={"limit": 100})
    own = {r for r in await populated.scalars(select(RFQ.rfq_id).where(RFQ.customer_id == mine))}
    assert listing.status_code == 200
    assert {item["rfq_id"] for item in listing.json()["items"]} <= own
    assert listing.json()["items"]

    own_rfq = sorted(own)[0]
    their_rfq = await populated.scalar(select(RFQ.rfq_id).where(RFQ.customer_id == theirs).limit(1))  # fmt: skip
    assert (await call(app, "GET", f"/api/v1/rfqs/{own_rfq}", headers=auth)).status_code == 200
    forged = await call(app, "GET", f"/api/v1/rfqs/{their_rfq}", headers=auth)
    absent = await call(app, "GET", "/api/v1/rfqs/RFQ-NOPE", headers=auth)
    assert forged.status_code == absent.status_code == 404
    assert forged.json() == absent.json() == {"detail": "Not found."}
    assert (await call(app, "GET", "/api/v1/materials", headers=auth)).status_code == 200

    quotes = (
        await populated.execute(
            select(Quote.quote_id, Quote.status, RFQ.customer_id).join(
                RFQ, RFQ.rfq_id == Quote.rfq_id
            )  # fmt: skip
        )
    ).all()
    for quote_id, status, owner in quotes:
        response = await call(app, "GET", f"/api/v1/quotes/{quote_id}", headers=auth)
        visible = owner == mine and status == "released"
        assert response.status_code == (200 if visible else 404), (status, owner == mine)

    upload = await call(
        app, "POST", f"/api/v1/rfqs/{own_rfq}/attachments", headers=auth,
        files={"file": ("spec.pdf", pdf("Spec"), "application/pdf")},
    )  # fmt: skip
    assert upload.status_code == 201
    blocked = await call(
        app, "POST", f"/api/v1/rfqs/{their_rfq}/attachments", headers=auth,
        files={"file": ("spec.pdf", pdf("Spec"), "application/pdf")},
    )  # fmt: skip
    assert blocked.status_code == 404
    assert len(list((tmp_path / "store").iterdir())) == 1
    listed = await call(app, "GET", f"/api/v1/rfqs/{own_rfq}/attachments", headers=auth)
    assert [i["attachment_id"] for i in listed.json()["items"]] == [upload.json()["attachment_id"]]


async def test_revoked_binding_is_forbidden(trust, database, populated, seed_plan):
    user = await active_user(populated)
    await bind(populated, user.user_id, customer_id(seed_plan), revoked=True)
    token = trust.mint(sub=user.authentication_subject)
    response = await call(app_with(trust, database), "GET", "/api/v1/customers/me", headers=bearer(token))  # fmt: skip
    assert response.status_code == 403 and response.json() == {"detail": "Forbidden."}


async def test_revocation_takes_effect_on_the_next_request(trust, database, populated, seed_plan):
    user = await active_user(populated)
    await bind(populated, user.user_id, customer_id(seed_plan))
    await populated.execute(update(Customer).where(Customer.customer_id == customer_id(seed_plan)).values(account_status="active"))  # fmt: skip
    app, auth = app_with(trust, database), bearer(trust.mint(sub=user.authentication_subject))
    assert (await call(app, "GET", "/api/v1/customers/me", headers=auth)).status_code == 200
    await populated.execute(
        update(UserCustomerBinding)
        .where(UserCustomerBinding.user_id == user.user_id)
        .values(revoked_at=func.now())
    )
    assert (await call(app, "GET", "/api/v1/customers/me", headers=auth)).status_code == 403


@pytest.mark.parametrize(
    "status, allowed",
    [
        ("active", True),
        ("pending_verification", True),
        ("on_hold", True),
        ("credit_restricted", True),
        ("inactive", False),
    ],
)
async def test_customer_account_status_gates_sign_in(
    trust, database, populated, seed_plan, status, allowed
):
    user = await active_user(populated)
    mine = customer_id(seed_plan)
    await bind(populated, user.user_id, mine)
    await populated.execute(update(Customer).where(Customer.customer_id == mine).values(account_status=status))  # fmt: skip
    response = await call(
        app_with(trust, database), "GET", "/api/v1/customers/me",
        headers=bearer(trust.mint(sub=user.authentication_subject)),
    )  # fmt: skip
    assert response.status_code == (200 if allowed else 403)


async def test_disabled_user_with_a_binding_is_rejected(trust, database, populated, seed_plan):
    user = await active_user(populated)
    await bind(populated, user.user_id, customer_id(seed_plan))
    await populated.execute(update(User).where(User.user_id == user.user_id).values(active=False))
    response = await call(
        app_with(trust, database), "GET", "/api/v1/customers/me",
        headers=bearer(trust.mint(sub=user.authentication_subject)),
    )  # fmt: skip
    assert response.status_code == 401


async def test_each_user_gets_only_their_own_binding(trust, database, populated, seed_plan):
    first, second = await users(populated, 2)
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    await populated.execute(update(Customer).where(Customer.customer_id.in_([mine, theirs])).values(account_status="active"))  # fmt: skip
    await bind(populated, first.user_id, mine)
    await bind(populated, second.user_id, theirs)
    app = app_with(trust, database)
    for user, expected in ((first, mine), (second, theirs)):
        response = await call(app, "GET", "/api/v1/customers/me", headers=bearer(trust.mint(sub=user.authentication_subject)))  # fmt: skip
        assert response.json()["customer_id"] == expected


async def test_schema_allows_at_most_one_active_binding_per_user(populated, seed_plan):
    user = await active_user(populated)
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    await bind(populated, user.user_id, mine)
    savepoint = await populated.begin_nested()
    with pytest.raises(IntegrityError):
        await bind(populated, user.user_id, theirs)  # a second active binding
    await savepoint.rollback()
    await bind(populated, user.user_id, theirs, revoked=True)  # history is fine
    for bad in (
        dict(user_id="U-NOPE", customer_id=mine),
        dict(user_id=user.user_id, customer_id="CUST-NOPE"),
    ):
        savepoint = await populated.begin_nested()
        with pytest.raises(IntegrityError):
            await populated.execute(insert(UserCustomerBinding).values(**bad))
        await savepoint.rollback()


async def test_ambiguous_bindings_fail_closed_even_without_the_index(populated, seed_plan):
    # Defence in depth: if the partial unique index were ever missing, two eligible active
    # rows must still not pick one silently.
    user = await active_user(populated)
    await populated.execute(text("DROP INDEX business.uq_user_customer_bindings_active_user"))
    await bind(populated, user.user_id, customer_id(seed_plan))
    await bind(populated, user.user_id, other_customer(seed_plan))
    await populated.execute(update(Customer).values(account_status="active"))
    assert await repositories.customer_binding(populated, user) is None


async def test_invalid_422_contract_is_unchanged_behind_real_auth(
    trust, database, populated, seed_plan
):
    user = await active_user(populated)
    await bind(populated, user.user_id, customer_id(seed_plan))
    await populated.execute(update(Customer).where(Customer.customer_id == customer_id(seed_plan)).values(account_status="active"))  # fmt: skip
    app = app_with(trust, database)
    response = await call(
        app, "GET", "/api/v1/rfqs", params={"limit": "x", "secret_field": "1"},
        headers=bearer(trust.mint(sub=user.authentication_subject)),
    )  # fmt: skip
    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_VALIDATION_ERROR"
    assert "secret_field" not in response.text
