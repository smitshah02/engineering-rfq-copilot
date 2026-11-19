"""Customer RFQ enrichment contracts, bounded SQL and unchanged released-detail behavior."""

from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from test_api_reads import WHEN, authorized_app, fake_session, quote_history

from rfq_copilot import repositories
from rfq_copilot.api.app import create_app
from rfq_copilot.api.auth import (
    CUSTOMER_VIEW_QUOTE,
    CUSTOMER_VIEW_RFQ,
    Principal,
    get_principal,
)
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.repositories.results import ReleasedQuotationReference


def test_rfq_page_enrichment_is_bounded_scoped_and_has_no_internal_fields(monkeypatch):
    calls = []

    async def rfqs(session, customer_id, **kwargs):
        calls.append(("rfqs", customer_id, kwargs))
        return tuple(
            SimpleNamespace(rfq_id=f"RFQ-{i}", status="draft", submitted_at=WHEN) for i in range(3)
        )

    async def released(session, rfq_ids, **kwargs):
        calls.append(("quotes", rfq_ids, kwargs))
        return (ReleasedQuotationReference("RFQ-0", "QUOTE-SAFE", 2, WHEN, None),)

    monkeypatch.setattr(repositories, "list_rfqs", rfqs)
    monkeypatch.setattr(repositories, "released_quotation_references", released)
    app = authorized_app()
    app.dependency_overrides[get_principal] = lambda: Principal(
        "CUST-A",
        frozenset({CUSTOMER_VIEW_RFQ, CUSTOMER_VIEW_QUOTE}),
        user_id="USER-A",
        assignment_required=True,
    )
    app.state.settings = ApiSettings(environment="development", permission_policy="v3")
    fake_session(app)
    with TestClient(app) as client:
        response = client.get("/api/v1/rfqs?limit=2&offset=4")
    assert response.status_code == 200
    body = response.json()
    assert (body["limit"], body["offset"], body["has_more"]) == (2, 4, True)
    first, second = body["items"]
    assert first["status"] == "draft"  # quotation release never invents an RFQ transition
    assert first["released_quote"] == {
        "quote_id": "QUOTE-SAFE",
        "version": 2,
        "released_at": "2026-11-02T09:30:00Z",
        "expiry_date": None,
    }
    assert second["released_quote"] is None
    assert set(first) == {"rfq_id", "status", "submitted_at", "released_quote"}
    assert calls == [
        ("rfqs", "CUST-A", {"limit": 3, "offset": 4, "assigned_user": "USER-A"}),
        ("quotes", ["RFQ-0", "RFQ-1"], {"customer_id": "CUST-A", "assigned_user": "USER-A"}),
    ]
    assert "USER-A" not in response.text


@pytest.mark.parametrize("quote_permission", [True, False])
def test_rfq_detail_requires_quote_read_to_discover_a_release(monkeypatch, quote_permission):
    rfq = SimpleNamespace(rfq_id="RFQ-1", status="draft", submitted_at=WHEN)
    monkeypatch.setattr(
        repositories,
        "get_rfq",
        AsyncMock(
            return_value=SimpleNamespace(
                rfq=rfq,
                runtime_lines=(),
            )
        ),
    )
    read = AsyncMock(
        return_value=(ReleasedQuotationReference("RFQ-1", "QUOTE-SAFE", 2, WHEN, date(2020, 1, 1)),)
    )
    monkeypatch.setattr(repositories, "released_quotation_references", read)
    app = create_app(ApiSettings(environment="development", permission_policy="v3"))
    permissions = {CUSTOMER_VIEW_RFQ} | ({CUSTOMER_VIEW_QUOTE} if quote_permission else set())
    app.dependency_overrides[get_principal] = lambda: Principal("CUST-A", frozenset(permissions))
    fake_session(app)
    with TestClient(app) as client:
        response = client.get("/api/v1/rfqs/RFQ-1")
    assert response.status_code == 200 and response.json()["status"] == "draft"
    if quote_permission:
        assert response.json()["released_quote"]["expiry_date"] == "2020-01-01"
        read.assert_awaited_once()
    else:
        assert response.json()["released_quote"] is None
        assert "QUOTE-SAFE" not in response.text
        read.assert_not_awaited()


def test_empty_history_and_missing_rfq_do_not_query_quotations(monkeypatch):
    monkeypatch.setattr(repositories, "list_rfqs", AsyncMock(return_value=()))
    monkeypatch.setattr(repositories, "get_rfq", AsyncMock(return_value=None))
    read = AsyncMock(side_effect=AssertionError("Unnecessary quotation query"))
    monkeypatch.setattr(repositories, "released_quotation_references", read)
    app = authorized_app()
    fake_session(app)
    with TestClient(app) as client:
        assert client.get("/api/v1/rfqs").json()["items"] == []
        assert client.get("/api/v1/rfqs/MISSING").status_code == 404
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_reference_query_is_one_read_with_approved_version_and_sql_scope():
    # No SQL is executed: inspect the actual repository statement via a recording session.
    session = SimpleNamespace(
        no_autoflush=nullcontext(),
        execute=AsyncMock(
            return_value=SimpleNamespace(
                all=lambda: [
                    ("RFQ-1", "QUOTE-1", 2, WHEN, None),
                ]
            )
        ),
    )
    result = await repositories.released_quotation_references(
        session,
        ["RFQ-1"],
        customer_id="CUST-A",
        assigned_user="USER-A",
    )
    assert result == (ReleasedQuotationReference("RFQ-1", "QUOTE-1", 2, WHEN, None),)
    session.execute.assert_awaited_once()
    statement = session.execute.call_args.args[0]
    assert statement.is_select and statement._for_update_arg is None
    sql = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    for required in (
        "business.rfqs.customer_id = 'CUST-A'",
        "business.rfqs.rfq_id IN ('RFQ-1')",
        "business.quotes.status = 'released'",
        "business.quotes.released_at IS NOT NULL",
        "business.quotes.released_at <= now()",
        "business.quote_versions.version = business.quotes.approved_version",
        "business.quote_versions.total_usd IS NOT NULL",
        "business.rfq_assignments.user_id = 'USER-A'",
        "business.rfq_assignments.revoked_at IS NULL",
        "ORDER BY business.quotes.rfq_id",
    ):
        assert required in sql
    for hidden in ("cost_breakdown", "part_json", "approval_history", "current_version"):
        assert hidden not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("ids,customer", [(["RFQ-1"] * 101, "C"), ([""], "C"), (["R"], "")])
async def test_reference_query_rejects_invalid_or_unbounded_input(ids, customer):
    with pytest.raises(ValueError):
        await repositories.released_quotation_references(object(), ids, customer_id=customer)


@pytest.mark.asyncio
async def test_reference_query_skips_empty_input():
    assert await repositories.released_quotation_references(object(), [], customer_id="C") == ()


@pytest.mark.parametrize("approved_version", [None, 99])
def test_missing_approved_version_keeps_existing_detail_unavailable(monkeypatch, approved_version):
    monkeypatch.setattr(
        repositories,
        "get_released_quotation",
        AsyncMock(
            return_value=quote_history("released", approved_version=approved_version)[0],
        ),
    )
    app = authorized_app()
    fake_session(app)
    with TestClient(app) as client:
        assert client.get("/api/v1/quotes/Q-1").status_code == 404


def test_missing_release_timestamp_keeps_existing_detail_unavailable(monkeypatch):
    monkeypatch.setattr(
        repositories,
        "get_released_quotation",
        AsyncMock(
            return_value=quote_history("released", released_at=None)[0],
        ),
    )
    app = authorized_app()
    fake_session(app)
    with TestClient(app) as client:
        assert client.get("/api/v1/quotes/Q-1").status_code == 404
