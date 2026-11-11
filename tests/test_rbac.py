"""Day 21 role-based authorization: public tests (no PostgreSQL, no dataset).

Real ES256-signed JWTs from test_auth's generated trust; the user, binding and grant
lookups are the in-memory fake from test_auth. The PostgreSQL version, with grants read
from the seeded business.role_permissions, is test_day21_rbac_database.py.
"""

import asyncio

import httpx2
import pytest
from test_auth import PROTECTED, SUBJECT, USERS, FakeDatabase, api, bearer, call
from test_auth import identity as identity
from test_auth import trust as trust

import rfq_copilot.repositories as repositories
from rfq_copilot.api.app import create_app
from rfq_copilot.api.auth import (
    CREATE_RFQ,
    VIEW_ASSIGNED_RFQS,
    VIEW_CUSTOMER,
    Principal,
    require_permission,
)

# The documented permission matrix (docs/development/day_21_rbac_authorization.md).
MATRIX = {
    ("GET", "/api/v1/customers/me"): VIEW_CUSTOMER,
    ("GET", "/api/v1/rfqs"): VIEW_ASSIGNED_RFQS,
    ("GET", "/api/v1/rfqs/{rfq_id}"): VIEW_ASSIGNED_RFQS,
    ("GET", "/api/v1/materials"): VIEW_ASSIGNED_RFQS,
    ("GET", "/api/v1/quotes/{quote_id}"): VIEW_ASSIGNED_RFQS,
    ("GET", "/api/v1/rfqs/{rfq_id}/attachments"): VIEW_ASSIGNED_RFQS,
    ("POST", "/api/v1/rfqs/{rfq_id}/attachments"): CREATE_RFQ,
}
PUBLIC = {("GET", "/health/live"), ("GET", "/api/v1/system/info")}  # stay public
# The other permission records of business.permissions (dataset roles_permissions.csv).
UNRELATED = (
    "create_quote_draft",
    "edit_quote_draft",
    "request_approval",
    "engineering_review",
    "approve_quote",
    "reject_quote",
    "release_quote",
    "view_audit",
    "manage_catalog",
)
APPROVAL = {"request_approval", "approve_quote", "reject_quote", "release_quote"}


def concrete(path_template):
    return path_template.replace("{rfq_id}", "RFQ-1").replace("{quote_id}", "Q-1")


def granted(identity, *permissions, user="U-1", customer="CUST-A"):
    identity.bindings[user] = customer
    identity.grants[user] = frozenset(permissions)


def test_every_api_operation_is_in_the_documented_matrix(trust):
    # Enumerated from the public OpenAPI schema, so a new route without a matrix entry fails.
    schema = create_app(trust.settings()).openapi()
    operations = {(m.upper(), path) for path, item in schema["paths"].items() for m in item}
    assert operations - PUBLIC == set(MATRIX)
    assert {(m, concrete(p)) for m, p in MATRIX} == set(PROTECTED)
    assert not set(MATRIX.values()) & APPROVAL  # no route executes or implies approval


@pytest.mark.parametrize(("method", "template"), list(MATRIX))
def test_each_route_requires_its_permission_and_nothing_else_opens_it(
    trust, identity, tmp_path, method, template
):
    required = MATRIX[(method, template)]
    everything_else = {*MATRIX.values(), *UNRELATED} - {required}
    granted(identity, *everything_else)
    app = api(trust, attachment_dir=tmp_path / "store")
    response = call(app, method, concrete(template), headers=bearer(trust.mint()))
    assert response.status_code == 403
    granted(identity, required)
    response = call(app, method, concrete(template), headers=bearer(trust.mint()))
    assert response.status_code != 403  # past authorization (the fake session then fails)


def test_bound_user_with_no_grants_is_forbidden_before_any_route_work(trust, identity):
    granted(identity)  # bound, but the role grants nothing
    database = FakeDatabase()
    app = api(trust, database)
    for method, path in PROTECTED:
        response = call(app, method, path, headers=bearer(trust.mint()))
        assert response.status_code == 403, path
        assert response.json() == {"detail": "Forbidden."}
    # Only get_principal's own session per request; no route session was opened.
    assert database.opened == database.closed == len(PROTECTED)


@pytest.mark.parametrize("permission", UNRELATED)
def test_unrelated_permissions_grant_no_route(trust, identity, permission):
    granted(identity, permission)
    app = api(trust)
    for method, path in PROTECTED:
        assert call(app, method, path, headers=bearer(trust.mint())).status_code == 403, path


@pytest.mark.parametrize("permission", [VIEW_CUSTOMER, VIEW_ASSIGNED_RFQS, CREATE_RFQ])
def test_each_permission_opens_exactly_its_routes(trust, identity, permission, tmp_path):
    granted(identity, permission)
    app = api(trust, attachment_dir=tmp_path / "store")
    for (method, template), required in MATRIX.items():
        response = call(app, method, concrete(template), headers=bearer(trust.mint()))
        # Past authorization the fake session fails (500); what matters is 403 or not.
        assert (response.status_code == 403) == (required != permission), template


def test_forged_role_and_permission_claims_are_ignored(trust, identity):
    granted(identity)
    token = trust.mint(
        role="sales_manager",
        roles=["administrator"],
        permissions=[VIEW_CUSTOMER, VIEW_ASSIGNED_RFQS, CREATE_RFQ, "approve_quote"],
        scope="view_customer view_assigned_rfqs create_rfq",
        scp=["create_rfq"],
        groups=["admins"],
    )
    app = api(trust)
    for method, path in PROTECTED:
        assert call(app, method, path, headers=bearer(token)).status_code == 403, path


def test_spoofed_headers_cookies_and_query_grant_nothing(trust, identity):
    granted(identity)
    app = api(trust)
    spoof = {
        "X-Role": "administrator",
        "X-Permissions": "view_customer,view_assigned_rfqs,create_rfq",
        "X-User-Role": "sales_manager",
        "X-Forwarded-User": "U-ADMIN",
    }
    from fastapi.testclient import TestClient

    with TestClient(app, cookies={"role": "administrator", "permissions": "create_rfq"}) as client:
        for method, path in PROTECTED:
            response = client.request(
                method,
                path,
                headers={**bearer(trust.mint()), **spoof},
                params={"role": "administrator", "permission": VIEW_CUSTOMER},
            )
            assert response.status_code == 403, path


def test_grant_changes_apply_to_the_next_request(trust, identity):
    app = api(trust)
    auth = bearer(trust.mint())
    granted(identity, VIEW_ASSIGNED_RFQS)
    assert call(app, "GET", "/api/v1/customers/me", headers=auth).status_code == 403
    identity.grants["U-1"] = frozenset({VIEW_CUSTOMER})
    assert call(app, "GET", "/api/v1/customers/me", headers=auth).status_code != 403
    identity.grants["U-1"] = frozenset()
    assert call(app, "GET", "/api/v1/customers/me", headers=auth).status_code == 403


def test_upload_without_create_rfq_reads_and_writes_nothing(trust, identity, tmp_path):
    granted(identity, VIEW_CUSTOMER, VIEW_ASSIGNED_RFQS)  # may read, may not upload
    app = api(trust, attachment_dir=tmp_path / "store")
    response = call(
        app, "POST", "/api/v1/rfqs/RFQ-1/attachments", headers=bearer(trust.mint()),
        files={"file": ("a.pdf", b"%PDF-1.4 not parsed", "application/pdf")},
    )  # fmt: skip
    assert response.status_code == 403 and response.json() == {"detail": "Forbidden."}
    assert not (tmp_path / "store").exists()  # storage never resolved
    assert app.state.write_database is None  # no write database created, no row


def test_unbound_user_with_grants_is_still_forbidden(trust, identity):
    identity.grants["U-1"] = frozenset(MATRIX.values())  # grants without a binding
    app = api(trust)
    for method, path in PROTECTED:
        assert call(app, method, path, headers=bearer(trust.mint())).status_code == 403


def test_unknown_user_with_grants_is_unauthenticated(trust, identity):
    identity.grants["U-NOBODY"] = frozenset(MATRIX.values())
    response = call(api(trust), "GET", "/api/v1/customers/me", headers=bearer(trust.mint(sub="auth|x")))  # fmt: skip
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_concurrent_users_get_their_own_permissions(trust, identity, monkeypatch):
    USERS_EXTRA = {"auth|s2": type(USERS[SUBJECT])(user_id="U-2", active=True)}
    original_find = repositories.find_active_user
    original_grants = repositories.granted_permissions

    async def slow_find(session, subject):
        await asyncio.sleep(0.01)
        return USERS_EXTRA.get(subject) or await original_find(session, subject)

    async def slow_grants(session, user):
        await asyncio.sleep(0.01)
        return await original_grants(session, user)

    monkeypatch.setattr(repositories, "find_active_user", slow_find)
    monkeypatch.setattr(repositories, "granted_permissions", slow_grants)
    granted(identity, VIEW_CUSTOMER, user="U-1", customer="CUST-1")
    granted(identity, user="U-2", customer="CUST-2")
    app = api(trust)
    seen = []

    from fastapi import Depends

    @app.get("/probe")
    async def probe(principal: Principal = Depends(require_permission(VIEW_CUSTOMER))):
        seen.append(principal.customer_id)
        return {"customer": principal.customer_id}

    tokens = [trust.mint(), trust.mint(sub="auth|s2")] * 10
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://t") as http:
        responses = await asyncio.gather(*(http.get("/probe", headers=bearer(t)) for t in tokens))
    assert [r.status_code for r in responses] == [200, 403] * 10
    assert seen == ["CUST-1"] * 10


def test_principal_permissions_are_validated_and_default_to_none():
    assert Principal("CUST-A").permissions == frozenset()
    for bad in (["view_customer"], {"view_customer"}, frozenset({1}), "view_customer"):
        with pytest.raises(ValueError):
            Principal("CUST-A", bad)
    with pytest.raises(AttributeError):
        Principal("CUST-A", frozenset({VIEW_CUSTOMER})).permissions = frozenset()


def test_require_permission_checks_names_not_roles():
    dependency = require_permission(VIEW_CUSTOMER)
    assert dependency.required_permission == VIEW_CUSTOMER
    holder = Principal("CUST-A", frozenset({VIEW_CUSTOMER}))
    assert asyncio.run(dependency(holder)) is holder
    for principal in (
        Principal("CUST-A"),
        Principal("CUST-A", frozenset({"sales_manager", "administrator", "approve_quote"})),
        Principal("CUST-A", frozenset({"VIEW_CUSTOMER", "view_customer "})),  # near misses
    ):
        with pytest.raises(Exception) as caught:
            asyncio.run(dependency(principal))
        assert caught.value.status_code == 403
