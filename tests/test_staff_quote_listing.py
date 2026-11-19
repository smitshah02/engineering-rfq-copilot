"""Offline validation/projection checks; real authorization is covered by database tests."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_api_reads import DEV, Tripwire

import rfq_copilot.repositories as repositories
from rfq_copilot.api.app import create_app
from rfq_copilot.api.auth import Principal, get_principal
from rfq_copilot.api.db import get_database, get_read_session
from rfq_copilot.api.routers.staff_quotes import require_staff_reader
from rfq_copilot.repositories.results import StaffQuotationSummary


def test_listing_authenticates_before_database_access():
    app = create_app(DEV)
    tripwire = Tripwire()
    app.dependency_overrides[get_database] = tripwire
    with TestClient(app) as client:
        response = client.get("/api/v1/staff/quotes")
    assert response.status_code == 401 and tripwire.calls == 0


def test_listing_bounds_lookahead_and_preserves_nullable_commercial_total(monkeypatch):
    calls = []
    principal = Principal("CUST-A", user_id="USER-A", assignment_required=True)
    rows = tuple(
        StaffQuotationSummary(
            f"QUOTE-{i}",
            f"RFQ-{i}",
            "draft",
            2,
            datetime(2026, 11, 18, tzinfo=UTC),
            None,
            "USD",
            None if i == 0 else Decimal("123.40"),
        )
        for i in range(3)
    )

    async def read(session, customer_id, **kwargs):
        calls.append((customer_id, kwargs))
        return rows

    async def fake_session():
        yield object()

    app = create_app(DEV)
    app.dependency_overrides[get_principal] = lambda: principal
    app.dependency_overrides[require_staff_reader] = lambda: principal
    app.dependency_overrides[get_read_session] = fake_session
    monkeypatch.setattr(repositories, "list_staff_quotations", read)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/staff/quotes", params={"limit": 2, "offset": 4, "status": "draft", "q": "Q"}
        )
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 2 and data["has_more"] is True
    assert data["items"][0]["total_usd"] is None
    assert data["items"][1]["total_usd"] == "123.40"
    assert calls == [
        (
            "CUST-A",
            {"limit": 3, "offset": 4, "status": "draft", "q": "Q", "assigned_user": "USER-A"},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"limit": 0},
        {"limit": 102},
        {"limit": True},
        {"offset": -1},
        {"offset": 10001},
        {"offset": True},
        {"q": ""},
        {"q": "x" * 65},
    ],
)
async def test_repository_rejects_unbounded_arguments_before_sql(changes):
    arguments = {"limit": 20, **changes}
    with pytest.raises(ValueError):
        await repositories.list_staff_quotations(object(), "CUST-A", **arguments)
