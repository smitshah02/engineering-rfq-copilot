"""Day 18 protected read routes without PostgreSQL. Public smoke suite.

Covers the fail-closed identity seam, spoofing attempts, request validation, safe
database-failure behaviour, database ownership, and response field selection. Real
PostgreSQL behaviour is covered in tests/test_day18_api_database.py.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

import rfq_copilot.repositories as repositories
from rfq_copilot.api.app import create_app
from rfq_copilot.api.auth import Principal, get_principal
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.db import get_database, get_read_session
from rfq_copilot.api.errors import REDACTED
from rfq_copilot.config import DatabaseConfig
from rfq_copilot.database import Database

DEV = ApiSettings(environment="development")
PROD = ApiSettings(environment="production")
SECRET = "sk_live_9f8e7d6c5b4a"
PROTECTED = [
    "/api/v1/customers/me",
    "/api/v1/rfqs",
    "/api/v1/rfqs/RFQ-1",
    "/api/v1/materials",
    "/api/v1/quotes/Q-1",
]
WHEN = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)


class Tripwire:
    """Records any attempt to reach the database layer."""

    def __init__(self):
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        raise AssertionError("database dependency reached")


def authorized_app(customer_id="CUST-A", **kwargs):
    app = create_app(DEV, **kwargs)
    app.dependency_overrides[get_principal] = lambda: Principal(customer_id)
    return app


def fake_session(app):
    session = object()

    async def override():
        yield session

    app.dependency_overrides[get_read_session] = override
    return session


# --- Fail-closed identity ----------------------------------------------------------------


@pytest.mark.parametrize("settings", [DEV, PROD])
@pytest.mark.parametrize("path", PROTECTED)
def test_protected_routes_deny_without_verified_identity(settings, path):
    app = create_app(settings)
    tripwire = Tripwire()
    app.dependency_overrides[get_database] = tripwire
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}
    assert response.headers["www-authenticate"] == "Bearer"
    assert tripwire.calls == 0 and app.state.database is None


@pytest.mark.parametrize(
    "spoof",
    [
        {"headers": {"X-Customer-Id": "CUST-A"}},
        {"headers": {"X-User-Id": "U-1", "X-Forwarded-User": "CUST-A"}},
        {"headers": {"Authorization": "Bearer forged.jwt.token"}},
        {"headers": {"Authorization": "Basic Q1VTVC1BOnB3"}},
        {"cookies": {"session": "CUST-A", "customer_id": "CUST-A"}},
        {"params": {"customer_id": "CUST-A"}},
    ],
)
def test_client_supplied_identity_is_never_accepted(spoof):
    app = create_app(DEV)
    tripwire = Tripwire()
    app.dependency_overrides[get_database] = tripwire
    with TestClient(app, cookies=spoof.get("cookies")) as client:
        for path in PROTECTED:
            response = client.get(path, headers=spoof.get("headers"), params=spoof.get("params"))
            assert response.status_code == 401, path
    assert tripwire.calls == 0


def test_principal_requires_a_real_identifier():
    for bad in ("", "   ", "x" * 65, "a\x00b", None):
        with pytest.raises(ValueError):
            Principal(bad)


def test_unauthenticated_invalid_input_still_gets_401_not_validation_detail():
    with TestClient(create_app(DEV)) as client:
        assert client.get("/api/v1/rfqs", params={"limit": "0"}).status_code == 401
        assert client.get("/api/v1/rfqs/" + "x" * 65).status_code == 401


def test_database_free_endpoints_are_unchanged():
    with TestClient(create_app(PROD)) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        assert client.get("/api/v1/system/info").status_code == 200
    # Writes are not routed anywhere.
    with TestClient(authorized_app()) as client:
        for path in PROTECTED:
            assert client.post(path, json={}).status_code == 405


# --- Request validation (authorized) -----------------------------------------------------


@pytest.mark.parametrize(
    ("path", "params", "expected"),
    [
        ("/api/v1/rfqs", {"limit": "0"}, [("query.limit", "greater_than_equal")]),
        ("/api/v1/rfqs", {"limit": "101"}, [("query.limit", "less_than_equal")]),
        ("/api/v1/rfqs", {"limit": "ten"}, [("query.limit", "int_parsing")]),
        ("/api/v1/rfqs", {"offset": "-1"}, [("query.offset", "greater_than_equal")]),
        ("/api/v1/rfqs", {"offset": "10001"}, [("query.offset", "less_than_equal")]),
        ("/api/v1/rfqs", {"customer_id": "CUST-B"}, [(f"query.{REDACTED}", "extra_forbidden")]),
        ("/api/v1/rfqs", {SECRET: "1"}, [(f"query.{REDACTED}", "extra_forbidden")]),
        (
            "/api/v1/materials",
            {"thickness_mm": "1.5e1"},
            [("query.thickness_mm", "exact_decimal_type")],
        ),
        ("/api/v1/materials", {"thickness_mm": "0"}, [("query.thickness_mm", "greater_than")]),
        ("/api/v1/materials", {"grade": "   "}, [("query.grade", "blank_or_invalid_text")]),
        ("/api/v1/rfqs/" + "x" * 65, {}, [("path.rfq_id", "string_too_long")]),
        ("/api/v1/rfqs/%20%20", {}, [("path.rfq_id", "blank_or_invalid_text")]),
        ("/api/v1/quotes/a%00b", {}, [("path.quote_id", "blank_or_invalid_text")]),
    ],
)
def test_invalid_parameters_get_the_sanitized_422(path, params, expected):
    # FastAPI resolves the (lazy) session before reporting parameter errors, but no query
    # runs: against an unreachable database any I/O would turn this into a 503.
    app = authorized_app(database=unreachable_database())
    with TestClient(app) as client:
        response = client.get(path, params=params)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "REQUEST_VALIDATION_ERROR"
    assert [(d["field"], d["code"]) for d in body["details"]] == expected
    assert SECRET not in response.text and "CUST-B" not in response.text


# --- Database failures and ownership -----------------------------------------------------


def unreachable_database():
    # A real driver failure: nothing listens on port 1, so the connection is refused.
    url = make_url("postgresql+asyncpg://leak_role:leak_password@127.0.0.1:1/leak_db")
    return Database(DatabaseConfig(url=url), read_only=True)


def test_driver_failure_is_an_opaque_503():
    database = unreachable_database()
    with TestClient(authorized_app(database=database)) as client:
        for path in PROTECTED:
            response = client.get(path)
            assert response.status_code == 503, path
            assert response.json() == {"detail": "Service unavailable."}
            for leak in ("leak_", "127.0.0.1", "postgresql", "SELECT", "Traceback"):
                assert leak not in response.text
    # The application did not own the injected database, so it did not dispose it.
    assert database._disposed is False


def test_unexpected_error_is_an_opaque_500(monkeypatch):
    async def explode(session, customer_id):
        raise RuntimeError(f"SELECT credit_limit_usd FROM customers WHERE id = '{SECRET}'")

    monkeypatch.setattr(repositories, "get_customer", explode)
    app = authorized_app()
    fake_session(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/customers/me")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"


def test_missing_database_configuration_is_a_503(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    app = authorized_app()
    with TestClient(app) as client:
        response = client.get("/api/v1/customers/me")
    assert response.status_code == 503 and response.json() == {"detail": "Service unavailable."}
    assert app.state.database is None


def test_lazily_created_database_is_owned_reused_and_disposed(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://r:p@127.0.0.1:1/lazy_db")
    app = authorized_app()
    with TestClient(app) as client:
        assert client.get("/api/v1/customers/me").status_code == 503  # refused, as above
        database = app.state.database
        assert app.state.owns_database is True
        assert database.config.url.database == "lazy_db"
        assert client.get("/api/v1/customers/me").status_code == 503
        assert app.state.database is database  # created once, then reused
    assert database._disposed is True and app.state.database is None


def test_lazy_database_sets_read_only_transactions(monkeypatch):
    created = []
    real_init = Database.__init__

    def spy(self, config, *, read_only=False):
        created.append(read_only)
        real_init(self, config, read_only=read_only)

    monkeypatch.setattr(Database, "__init__", spy)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://r:p@127.0.0.1:1/lazy_db")
    with TestClient(authorized_app()) as client:
        client.get("/api/v1/rfqs")
    assert created == [True]


# --- Field selection and scoping (repositories stubbed) ----------------------------------


def test_customer_profile_exposes_only_the_contract_fields(monkeypatch):
    seen = []
    customer = SimpleNamespace(
        customer_id="CUST-A",
        company_name="Acme Test",
        industry="test",
        city="Springfield",
        state="ST",
        country="US",
        payment_terms="NET30",
        preferred_shipping="ground",
        region="internal-region",
        account_status="credit_restricted",
        credit_limit_usd=Decimal("50000.00"),
        credit_exposure_usd=Decimal("49999.99"),
        restrictions="internal note",
        approval_requirement="manager",
    )
    contact = SimpleNamespace(
        contact_id="CON-INTERNAL",
        contact_name="Pat",
        email="p@example.test",
        phone="1",
        is_primary=True,
    )

    async def stub(session, customer_id):
        seen.append(customer_id)
        return repositories.CustomerDetails(customer, (contact,))

    monkeypatch.setattr(repositories, "get_customer", stub)
    app = authorized_app("CUST-A")
    fake_session(app)
    with TestClient(app) as client:
        response = client.get("/api/v1/customers/me", headers={"X-Customer-Id": "CUST-B"})
    assert response.status_code == 200 and seen == ["CUST-A"]
    body = response.json()
    assert set(body) == {
        "customer_id",
        "company_name",
        "industry",
        "city",
        "state",
        "country",
        "payment_terms",
        "preferred_shipping",
        "contacts",
    }
    assert body["contacts"] == [
        {"contact_name": "Pat", "email": "p@example.test", "phone": "1", "is_primary": True}
    ]
    for hidden in ("credit_restricted", "50000", "49999", "internal", "CON-INTERNAL", "manager"):
        assert hidden not in response.text


def test_materials_expose_identity_only_and_page_deterministically(monkeypatch):
    ids = tuple(f"MAT-{i:03d}" for i in range(5))
    calls = []

    async def find(session, **filters):
        calls.append(filters)
        return ids

    async def rows(session, material_ids):
        return tuple(
            SimpleNamespace(
                material_id=m,
                category="steel",
                grade="G",
                cost_usd_per_kg=Decimal("9.99"),
                density_kg_m3=Decimal("7850"),
                availability_class="restricted",
                pricing_rule_version="price-internal",
            )
            for m in material_ids
        )

    monkeypatch.setattr(repositories, "find_materials", find)
    monkeypatch.setattr(repositories, "list_materials", rows)
    app = authorized_app()
    fake_session(app)
    with TestClient(app) as client:
        first = client.get("/api/v1/materials", params={"limit": 2, "thickness_mm": "2.0"}).json()
        last = client.get("/api/v1/materials", params={"limit": 2, "offset": 4}).json()
        beyond = client.get("/api/v1/materials", params={"offset": 9}).json()
    assert [m["material_id"] for m in first["items"]] == ["MAT-000", "MAT-001"]
    assert first["has_more"] is True and calls[0]["thickness_mm"] == Decimal("2.0")
    assert [m["material_id"] for m in last["items"]] == ["MAT-004"] and last["has_more"] is False
    assert beyond == {"items": [], "limit": 20, "offset": 9, "has_more": False}
    assert set(first["items"][0]) == {"material_id", "category", "grade"}


def quote_history(status, released_at=WHEN, approved_version=2):
    def version(number, total):
        line = SimpleNamespace(
            line_number=1,
            part_id="PART-T",
            quantity=3,
            line_total_usd=Decimal(total),
            cost_breakdown_json={"material_cost": "1.23"},
            part_json={"internal": True},
        )
        record = SimpleNamespace(
            version=number,
            currency="USD",
            total_usd=Decimal(total),
            expiry_date=None,
            estimated_lead_business_days=12,
            pricing_rule_version="price-internal",
            created_by="U-INTERNAL",
        )
        return repositories.QuoteVersionDetails(record, (line,))

    quote = SimpleNamespace(
        quote_id="Q-1",
        rfq_id="RFQ-1",
        status=status,
        released_at=released_at,
        approved_version=approved_version,
        approver_id="U-APPROVER",
        created_by="U-INTERNAL",
    )
    return (repositories.QuotationDetails(quote, (version(1, "10.00"), version(2, "123.40"))),)


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
def test_unreleased_quotes_are_never_returned(monkeypatch, status):
    async def history(session, customer_id):
        return quote_history(status, released_at=None)

    monkeypatch.setattr(repositories, "list_quotation_history", history)
    app = authorized_app()
    fake_session(app)
    with TestClient(app) as client:
        response = client.get("/api/v1/quotes/Q-1")
    assert response.status_code == 404 and response.json() == {"detail": "Not found."}


def test_released_quote_returns_the_approved_version_only(monkeypatch):
    seen = []

    async def history(session, customer_id):
        seen.append(customer_id)
        return quote_history("released")

    monkeypatch.setattr(repositories, "list_quotation_history", history)
    app = authorized_app("CUST-A")
    fake_session(app)
    with TestClient(app) as client:
        response = client.get("/api/v1/quotes/Q-1")
        missing = client.get("/api/v1/quotes/Q-2")
    assert seen == ["CUST-A", "CUST-A"] and missing.status_code == 404
    assert response.json() == {
        "quote_id": "Q-1",
        "rfq_id": "RFQ-1",
        "status": "released",
        "released_at": "2026-11-02T09:30:00Z",
        "version": 2,
        "currency": "USD",
        "total_usd": "123.40",
        "expiry_date": None,
        "estimated_lead_business_days": 12,
        "lines": [
            {"line_number": 1, "part_id": "PART-T", "quantity": 3, "line_total_usd": "123.40"}
        ],
    }
    for hidden in ("U-INTERNAL", "U-APPROVER", "price-internal", "material_cost", "10.00"):
        assert hidden not in response.text


def test_openapi_documents_protected_contracts():
    schema = create_app(DEV).openapi()
    rfq = schema["paths"]["/api/v1/rfqs"]["get"]
    assert {p["name"] for p in rfq["parameters"]} == {"limit", "offset"}
    assert rfq["responses"]["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ValidationErrorResponse"
    }
    components = schema["components"]["schemas"]
    for name in ("CustomerProfile", "RfqPage", "RfqDetail", "MaterialPage", "ReleasedQuote"):
        assert components[name]["additionalProperties"] is False
    assert "credit_limit_usd" not in str(components) and "cost_usd_per_kg" not in str(components)
    assert components["ReleasedQuote"]["properties"]["status"]["const"] == "released"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("customer", "limit", "offset"),
    [("", 1, 0), ("C", 0, 0), ("C", 1001, 0), ("C", True, 0), ("C", 1, -1), ("C", 1, "0")],
)
async def test_list_rfqs_rejects_bad_arguments_before_sql(customer, limit, offset):
    # object() is not a session: any SQL attempt would raise AttributeError instead.
    with pytest.raises(ValueError):
        await repositories.list_rfqs(object(), customer, limit=limit, offset=offset)
