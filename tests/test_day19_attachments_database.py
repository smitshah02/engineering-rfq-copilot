"""Day 19 attachments against the guarded PostgreSQL test database and dataset parity.

The upload route commits. Each test joins the seeded fixture transaction through a
SAVEPOINT session, so a route commit only releases the savepoint and the fixture still
rolls everything back. The development database is never touched.
"""

import json
from pathlib import Path

import httpx2
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_attachments import BOM_COLUMNS, ROW, bom, pdf
from test_database_queries import customer_id, other_customer
from test_database_queries import populated as populated
from test_database_seeding_integration import seed_bundle as seed_bundle
from test_database_seeding_integration import seed_database as seed_database
from test_database_seeding_integration import seed_plan as seed_plan
from test_database_seeding_integration import session as session

import rfq_copilot.repositories as repositories
from rfq_copilot.api.app import create_app
from rfq_copilot.api.auth import Principal, get_principal
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.db import get_read_session, get_write_session
from rfq_copilot.attachments import extract_bom, extract_pdf
from rfq_copilot.models import RFQ, RFQAttachment

pytestmark = pytest.mark.integration
async_test = pytest.mark.asyncio(loop_scope="module")

DATASET = Path(__file__).resolve().parents[1] / "data" / "rfq_synthetic_data"


@pytest_asyncio.fixture(loop_scope="module")
async def joined(populated):
    connection = await populated.connection()
    async with AsyncSession(bind=connection, join_transaction_mode="create_savepoint") as joined:
        yield joined


def app_for(principal_id, joined, storage):
    storage = storage if storage.name == "store" else storage / "store"
    app = create_app(ApiSettings(environment="development", attachment_dir=storage))
    if principal_id is not None:
        app.dependency_overrides[get_principal] = lambda: Principal(principal_id)

    async def shared():
        yield joined

    app.dependency_overrides[get_write_session] = shared
    app.dependency_overrides[get_read_session] = shared
    return app


async def call(app, method, path, **kwargs):
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.request(method, path, **kwargs)


async def rfq_of(session, customer):
    rows = await session.scalars(
        select(RFQ.rfq_id).where(RFQ.customer_id == customer).order_by(RFQ.rfq_id)
    )
    return rows.first()


async def attachment_count(session):
    return await session.scalar(select(func.count()).select_from(RFQAttachment))


def files_in(storage):
    storage = storage if storage.name == "store" else storage / "store"
    return sorted(p.name for p in storage.iterdir()) if storage.exists() else []


@async_test
async def test_upload_persists_linked_metadata_and_file(joined, seed_plan, tmp_path):
    mine = customer_id(seed_plan)
    rfq_id = await rfq_of(joined, mine)
    storage = tmp_path / "store"
    data = pdf(f"Specification for {rfq_id}")
    response = await call(
        app_for(mine, joined, storage),
        "POST",
        f"/api/v1/rfqs/{rfq_id}/attachments",
        files={"file": ("spec.pdf", data, "application/pdf")},
    )
    assert response.status_code == 201
    body = response.json()
    row = await joined.scalar(
        select(RFQAttachment).where(RFQAttachment.attachment_id == body["attachment_id"])
    )
    assert row.rfq_id == rfq_id and row.filename == "spec.pdf" and row.document_type == "pdf"
    assert row.sha256 == body["sha256"] and row.revision is None
    assert row.source_role == "customer_supplied" and row.is_trusted_instruction is False
    assert files_in(storage) == [row.storage_ref]
    assert (storage / row.storage_ref).read_bytes() == data
    assert rfq_id in body["extraction"]["pages"][0]["text"]

    listing = await call(
        app_for(mine, joined, storage), "GET", f"/api/v1/rfqs/{rfq_id}/attachments"
    )
    assert listing.status_code == 200
    assert listing.json() == {
        "items": [
            {
                k: body[k]
                for k in (
                    "attachment_id",
                    "rfq_id",
                    "filename",
                    "document_type",
                    "revision",
                    "sha256",
                )
            }
        ]  # fmt: skip
    }
    assert row.storage_ref not in listing.text


@async_test
async def test_bom_upload_round_trips_exact_values(joined, seed_plan, tmp_path):
    mine = customer_id(seed_plan)
    rfq_id = await rfq_of(joined, mine)
    response = await call(
        app_for(mine, joined, tmp_path),
        "POST",
        f"/api/v1/rfqs/{rfq_id}/attachments",
        files={"file": ("bom.csv", bom(ROW), "text/csv")},
    )
    assert response.status_code == 201
    assert response.json()["extraction"]["rows"][0]["width_mm"] == "80.50"
    assert await attachment_count(joined) == 1


@async_test
async def test_other_customers_rfq_is_never_written_or_listed(joined, seed_plan, tmp_path):
    mine, theirs = customer_id(seed_plan), other_customer(seed_plan)
    their_rfq = await rfq_of(joined, theirs)
    before = await attachment_count(joined)
    app = app_for(mine, joined, tmp_path)
    forged = await call(
        app, "POST", f"/api/v1/rfqs/{their_rfq}/attachments",
        files={"file": ("spec.pdf", pdf("x"), "application/pdf")},
    )  # fmt: skip
    absent = await call(
        app, "POST", "/api/v1/rfqs/RFQ-DOES-NOT-EXIST/attachments",
        files={"file": ("spec.pdf", pdf("x"), "application/pdf")},
    )  # fmt: skip
    assert forged.status_code == absent.status_code == 404
    assert forged.json() == absent.json() == {"detail": "Not found."}
    listed = await call(app, "GET", f"/api/v1/rfqs/{their_rfq}/attachments")
    assert listed.status_code == 404
    assert await attachment_count(joined) == before and files_in(tmp_path) == []
    # The owner's upload works; the other customer's list then still hides it.
    owner = app_for(theirs, joined, tmp_path)
    ok = await call(
        owner, "POST", f"/api/v1/rfqs/{their_rfq}/attachments",
        files={"file": ("spec.pdf", pdf("x"), "application/pdf")},
    )  # fmt: skip
    assert ok.status_code == 201
    assert (await call(app, "GET", f"/api/v1/rfqs/{their_rfq}/attachments")).status_code == 404
    assert (await repositories.list_attachments(joined, their_rfq, customer_id=mine)) == ()


@async_test
async def test_unauthenticated_and_rejected_uploads_write_nothing(joined, seed_plan, tmp_path):
    mine = customer_id(seed_plan)
    rfq_id = await rfq_of(joined, mine)
    path = f"/api/v1/rfqs/{rfq_id}/attachments"
    before = await attachment_count(joined)
    anonymous = await call(
        app_for(None, joined, tmp_path), "POST", path,
        files={"file": ("spec.pdf", pdf("x"), "application/pdf")},
    )  # fmt: skip
    assert anonymous.status_code == 401
    for files in (
        {"file": ("spec.pdf", b"%PDF-1.4 broken", "application/pdf")},
        {"file": ("bom.csv", bom(header=",".join(BOM_COLUMNS[:-1])), "text/csv")},
        {"file": ("../x.pdf", pdf("x"), "application/pdf")},
    ):
        rejected = await call(app_for(mine, joined, tmp_path), "POST", path, files=files)
        assert rejected.status_code in (415, 422)
    assert await attachment_count(joined) == before and files_in(tmp_path) == []


@async_test
async def test_database_failure_rolls_back_and_removes_the_file(
    joined, seed_plan, tmp_path, monkeypatch
):
    mine = customer_id(seed_plan)
    rfq_id = await rfq_of(joined, mine)
    original = repositories.add_attachment

    def conflicting(session, **row):
        # A foreign key the database refuses at commit, after the file was written.
        original(session, **{**row, "rfq_id": "RFQ-MISSING-FK"})

    monkeypatch.setattr(repositories, "add_attachment", conflicting)
    before = await attachment_count(joined)
    response = await call(
        app_for(mine, joined, tmp_path), "POST", f"/api/v1/rfqs/{rfq_id}/attachments",
        files={"file": ("spec.pdf", pdf("x"), "application/pdf")},
    )  # fmt: skip
    assert response.status_code == 500 and response.text == "Internal Server Error"
    await joined.rollback()
    assert await attachment_count(joined) == before
    assert files_in(tmp_path) == []


@async_test
async def test_database_check_constraints_back_the_metadata(joined, seed_plan):
    rfq_id = await rfq_of(joined, customer_id(seed_plan))
    base = {
        "attachment_id": "ATT-CHECK", "rfq_id": rfq_id, "filename": "x.pdf",
        "storage_ref": "ATT-CHECK.pdf", "document_type": "pdf", "source_role": "customer_supplied",
        "sha256": "0" * 64,
    }  # fmt: skip
    for bad in ({"is_trusted_instruction": True}, {"document_type": "exe"}, {"sha256": "XYZ"}):
        savepoint = await joined.begin_nested()
        joined.add(RFQAttachment(**{**base, **bad}))
        with pytest.raises(IntegrityError):
            await joined.flush()
        await savepoint.rollback()


@async_test
async def test_lazily_created_write_database_is_separate_and_writable(seed_database, monkeypatch):
    url = seed_database.config.url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)  # never printed
    app = create_app(ApiSettings(environment="development", attachment_dir=Path("/nonexistent")))
    app.dependency_overrides[get_principal] = lambda: Principal("CUST-ANY")
    response = await call(
        app, "POST", "/api/v1/rfqs/RFQ-NONE/attachments",
        files={"file": ("spec.pdf", pdf("x"), "application/pdf")},
    )  # fmt: skip
    assert response.status_code == 404  # seeded rows are invisible here; nothing written
    assert app.state.database is None  # the read-only database was not repurposed
    writer = app.state.write_database
    try:
        assert app.state.owns_write_database is True
        async with writer.session() as live:
            assert (await live.scalar(text("SHOW default_transaction_read_only"))) == "off"
        assert (await call(app, "GET", "/api/v1/customers/me")).status_code == 404
        async with app.state.database.session() as live:
            assert (await live.scalar(text("SHOW default_transaction_read_only"))) == "on"
    finally:
        await writer.dispose()
        await app.state.database.dispose()


# --- Dataset parity: development and validation attachments only ----------------------


def _manifest():
    entries = json.loads((DATASET / "attachments" / "attachment_manifest.json").read_text())
    return [e for e in entries if e["split"] in ("development", "validation")]


def test_dataset_boms_extract_exactly():
    import csv

    entries = [e for e in _manifest() if e["type"] == "csv"]
    assert len(entries) == 32
    for entry in entries:
        raw = (DATASET / entry["path"]).read_bytes()
        rows = extract_bom(raw).rows
        reference = list(csv.DictReader(raw.decode("utf-8").splitlines()))
        assert len(rows) == len(reference)
        for row, expected in zip(rows, reference, strict=True):
            for column in BOM_COLUMNS:
                value = getattr(row, column)
                assert ("" if value is None else str(value)) == expected[column]


def test_dataset_pdfs_extract_their_rfq_text():
    entries = [e for e in _manifest() if e["type"] == "pdf"]
    assert len(entries) == 50
    for entry in entries:
        result = extract_pdf((DATASET / entry["path"]).read_bytes())
        assert result.status == "text" and not result.truncated
        assert entry["rfq_id"] in "".join(page.text for page in result.pages)
