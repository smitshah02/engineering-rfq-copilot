"""Day 19 attachment intake: content checks, extraction, storage and the upload API.

Public smoke tests: no PostgreSQL, no private dataset, no credentials. Every document is
built here. Repository calls are replaced by in-memory fakes; the PostgreSQL behaviour is
covered by test_day19_attachments_database.py.
"""

import asyncio
import hashlib
import io
import os
import socket
import stat
import threading
import zlib
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx2
import pytest
from pypdf import PdfWriter
from sqlalchemy.exc import OperationalError

import rfq_copilot.repositories as repositories
from rfq_copilot import attachments
from rfq_copilot.api.app import create_app
from rfq_copilot.api.auth import Principal, get_principal
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.db import get_read_session, get_write_database
from rfq_copilot.api.routers import attachments as router_module
from rfq_copilot.attachments import (
    BOM_COLUMNS,
    AttachmentRejected,
    check_file,
    check_filename,
    extract_bom,
    extract_pdf,
    remove_file,
    store_file,
)
from rfq_copilot.database import DatabaseError

pytestmark = pytest.mark.asyncio

MINE, THEIRS = "CUST-A", "CUST-B"
OWNERS = {"RFQ-A1": MINE, "RFQ-B1": THEIRS}
URL = "/api/v1/rfqs/{}/attachments"
LIMIT = 64 * 1024


# --- Document builders -------------------------------------------------------------------


def pdf(*texts, catalog_extra=b"", raw_streams=None):
    """A minimal valid PDF; ``None`` makes a page with no text (like a scanned image)."""
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R " + catalog_extra + b">>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids, number = [], 4
    for index, text in enumerate(texts):
        page, content = number, number + 1
        number += 2
        kids.append(b"%d 0 R" % page)
        if raw_streams and index in raw_streams:
            stream, extra = raw_streams[index]
        else:
            stream = b"" if text is None else b"BT /F1 12 Tf 72 720 Td (%s) Tj ET" % text.encode()
            extra = b""
        objects[page] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content
        )
        objects[content] = (
            b"<< /Length %d %s>>\nstream\n" % (len(stream), extra) + stream + b"\nendstream"
        )
    objects[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), len(kids))
    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for key in sorted(objects):
        offsets[key] = len(out)
        out += b"%d 0 obj\n" % key + objects[key] + b"\nendobj\n"
    xref, size = len(out), max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for key in range(1, size):
        out += b"%010d 00000 n \n" % offsets[key]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


def encrypted_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("secret-password")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


HEADER = ",".join(BOM_COLUMNS)
ROW = "RFQ-A1,B,1,flat_panel,120,80.50,,MAT-T01,10,0.10,powder_coat"


def bom(*rows, header=HEADER, newline="\n"):
    return newline.join((header, *rows)).encode() + newline.encode()


def reason(callable_, *args):
    with pytest.raises(AttachmentRejected) as caught:
        callable_(*args)
    return caught.value


# --- Filenames ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd.pdf",
        "..\\..\\boot.csv",
        "dir/drawing.pdf",
        "C:\\temp\\bom.csv",
        ".hidden.pdf",
        "..pdf",
        " leading.pdf",
        "trailing.pdf ",
        "nul\x00.pdf",
        "line\nbreak.csv",
        "bidi\u202efdp.csv",  # right-to-left override hides the real extension
        "x" * 252 + ".pdf",
        "",
        None,
    ],
)
async def test_unsafe_filenames_are_rejected(name):
    assert reason(check_filename, name).reason == "invalid_filename"


@pytest.mark.parametrize("name", ["drawing.exe", "bom.xlsx", "spec.pdf.exe", "notes.md", "pdf"])
async def test_unsupported_extensions_are_rejected(name):
    assert reason(check_filename, name).reason == "unsupported_type"


async def test_supported_names_map_to_table_document_types():
    assert check_filename("Spec Rev B.PDF") == "pdf"
    assert check_filename("RFQ-0001_revB.csv") == "csv"


# --- Content checks ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, client_type, data, expected",
    [
        ("spec.pdf", "application/pdf", b"", "empty_file"),
        ("bom.csv", "text/csv", pdf("x"), "content_type_mismatch"),  # PDF named .csv
        ("spec.pdf", "application/pdf", bom(ROW), "content_type_mismatch"),  # CSV named .pdf
        ("spec.pdf", "image/png", pdf("x"), "content_type_mismatch"),
        ("bom.csv", "application/pdf", bom(ROW), "content_type_mismatch"),
        ("bom.csv", "text/csv", b"PK\x03\x04" + b"\x00" * 64, "unsupported_type"),  # xlsx/zip
        ("spec.pdf", "application/pdf", b"%PDF-" + b"x" * LIMIT, "file_too_large"),
    ],
)
async def test_content_must_match_name_declared_type_and_size(
    filename, client_type, data, expected
):
    assert reason(check_file, filename, client_type, data, LIMIT).reason == expected


async def test_checked_file_records_size_and_sha256():
    data = pdf("hello")
    checked = check_file("spec.pdf", "application/pdf; charset=binary", data, LIMIT)
    assert checked.document_type == "pdf" and checked.size_bytes == len(data)
    assert checked.sha256 == hashlib.sha256(data).hexdigest()
    assert check_file("spec.pdf", None, data, LIMIT).sha256 == checked.sha256


# --- PDF extraction ----------------------------------------------------------------------


async def test_pdf_text_is_extracted_per_page_and_deterministically():
    data = pdf("Bracket spec RFQ-A1", "Finish powder coat")
    first, second = extract_pdf(data), extract_pdf(data)
    assert first == second
    assert first.status == "text" and first.page_count == 2 and not first.truncated
    assert [p.page_number for p in first.pages] == [1, 2]
    assert "RFQ-A1" in first.pages[0].text and "powder" in first.pages[1].text


async def test_pdf_without_text_is_reported_not_invented():
    result = extract_pdf(pdf(None, None))
    assert result.status == "no_text" and result.page_count == 2
    assert all(page.text == "" for page in result.pages)


async def test_encrypted_pdf_is_rejected():
    assert reason(extract_pdf, encrypted_pdf()).reason == "pdf_encrypted"


@pytest.mark.parametrize(
    "data",
    [
        pdf("cut off")[:60],
        b"%PDF-1.4\n" + os.urandom(2048),
        b"%PDF-1.4\n1 0 obj\n" + b"[" * 50_000 + b"\nendobj\n%%EOF",
        b"%PDF-1.4\n%%EOF\n",
        pdf(),  # zero pages
    ],
)
async def test_corrupt_or_hostile_pdfs_are_rejected_safely(data):
    assert reason(extract_pdf, data).reason == "pdf_malformed"


async def test_pdf_page_count_is_bounded(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_PDF_PAGES", 2)
    assert reason(extract_pdf, pdf("a", "b", "c")).reason == "pdf_too_many_pages"


async def test_pdf_text_is_bounded_and_flagged(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_PDF_TEXT_CHARS", 10)
    result = extract_pdf(pdf("abcdefgh", "ijklmnop"))
    assert result.truncated and sum(len(p.text) for p in result.pages) == 10


async def test_compression_bomb_is_rejected_by_bounded_decoding():
    bomb = zlib.compress(b"0" * 80_000_000, 9)  # beyond pypdf's 75 MB decode limit
    data = pdf("x", raw_streams={0: (bomb, b"/Filter /FlateDecode ")})
    assert len(data) < 200_000
    assert reason(extract_pdf, data).reason == "pdf_malformed"


async def test_active_content_and_links_are_never_executed_or_fetched(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("extraction must not open sockets")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    hostile = (
        b"/OpenAction << /S /JavaScript /JS (app.launchURL\\('http://attacker.invalid'\\)) >> "
        b"/Names << /EmbeddedFiles << /Names [] >> >> "
        b"/URI << /Base (http://attacker.invalid/) >> "
    )
    result = extract_pdf(pdf("Visible text", catalog_extra=hostile))
    assert result.status == "text"
    assert all("attacker" not in page.text for page in result.pages)


# --- BOM extraction ----------------------------------------------------------------------


async def test_bom_rows_keep_exact_values_and_identifiers():
    result = extract_bom(bom(ROW, "RFQ-A1,B,2,l_bracket,0007.250,40,25.0,MAT-T02,3,0,none"))
    first, second = result.rows
    assert first.rfq_id == "RFQ-A1" and first.revision == "B" and first.line == 1
    assert first.width_mm == Decimal("80.50") and str(first.width_mm) == "80.50"
    assert first.height_mm is None  # blank for flat parts, not zero
    assert str(first.tolerance_mm) == "0.10" and first.quantity == 10
    # Same exact value and scale; Decimal does not keep leading zeros.
    assert second.length_mm == Decimal("7.250") and str(second.length_mm) == "7.250"
    assert str(second.height_mm) == "25.0"
    assert extract_bom(bom(ROW)) == extract_bom(bom(ROW, newline="\r\n"))


async def test_bom_with_utf8_signature_is_accepted():
    assert len(extract_bom(b"\xef\xbb\xbf" + bom(ROW)).rows) == 1


@pytest.mark.parametrize(
    "header",
    [
        HEADER.replace(",finish_id", ""),  # missing
        HEADER + ",unit_price",  # extra
        HEADER.replace("length_mm,width_mm", "width_mm,length_mm"),  # reordered
        HEADER.replace("quantity", "qty"),  # renamed
        HEADER.replace("quantity", "Quantity"),  # case is not folded
        HEADER.replace("rfq_id", "rfq_id,rfq_id", 1),  # duplicated
        "",
    ],
)
async def test_bom_columns_must_match_the_dataset_schema(header):
    rejected = reason(extract_bom, bom(ROW, header=header))
    assert rejected.reason == "bom_columns" and rejected.issues == ()


@pytest.mark.parametrize(
    "cell_index, value, code",
    [
        (4, "1e3", "invalid_number"),
        (4, "-5", "invalid_number"),
        (4, " 12", "invalid_number"),
        (4, "１２", "invalid_number"),  # full-width digits are not reinterpreted
        (4, "12,5", "invalid_number"),  # quoted: one cell, not a decimal comma
        (4, None, None),  # unquoted 12,5 shifts the columns: wrong field count
        (4, "", "missing_value"),
        (8, "2.5", "invalid_integer"),
        (8, "0", "invalid_integer"),
        (8, "99999999999", "invalid_integer"),
        (7, '=HYPERLINK("http://x.invalid","MAT")', "formula_like"),
        (7, "@SUM(A1:A9)", "formula_like"),
        (3, "+cmd", "formula_like"),
        (10, " powder_coat", "invalid_text"),
        (7, "M" * 65, "too_long"),
    ],
)
async def test_bom_cell_problems_are_reported_by_row_and_column(cell_index, value, code):
    cells = ROW.split(",")
    if value is None:
        cells[cell_index] = "12,5"
    elif "," in value or '"' in value:
        cells[cell_index] = '"' + value.replace('"', '""') + '"'
    else:
        cells[cell_index] = value
    rejected = reason(extract_bom, bom(",".join(cells)))
    assert rejected.reason == "bom_rows"
    if code is None:
        assert rejected.issues == (attachments.BomIssue(1, None, "wrong_field_count"),)
    else:
        assert rejected.issues == (attachments.BomIssue(1, BOM_COLUMNS[cell_index], code),)


@pytest.mark.parametrize(
    "data, expected",
    [
        ("rfq_id,café".encode("latin-1"), "bom_encoding"),
        (bom(ROW).replace(b"MAT", b"M\x00T"), "bom_encoding"),
        (bom('RFQ-A1,"B,1'), "bom_malformed"),  # unterminated quote
        (bom(), "bom_rows"),  # header only
    ],
)
async def test_malformed_boms_are_rejected(data, expected):
    assert reason(extract_bom, data).reason == expected


async def test_bom_row_count_and_issue_list_are_bounded(monkeypatch):
    bad = ROW.replace(",10,", ",x,")
    rejected = reason(extract_bom, bom(*[bad] * 30))
    assert len(rejected.issues) == attachments.MAX_ISSUES
    monkeypatch.setattr(attachments, "MAX_BOM_ROWS", 2)
    assert reason(extract_bom, bom(ROW, ROW, ROW)).reason == "bom_too_many_rows"


# --- Storage -----------------------------------------------------------------------------


async def test_storage_uses_server_names_and_private_permissions(tmp_path):
    directory = tmp_path / "store"
    attachment_id = attachments.new_attachment_id()
    ref = store_file(directory, attachment_id, "pdf", b"%PDF-data")
    assert ref == f"{attachment_id}.pdf" and len(attachment_id) <= 64
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / ref).stat().st_mode) == 0o600
    assert (directory / ref).read_bytes() == b"%PDF-data"
    remove_file(directory, ref)
    assert list(directory.iterdir()) == []


async def test_storage_never_overwrites_or_follows_symlinks(tmp_path):
    attachment_id = attachments.new_attachment_id()
    store_file(tmp_path, attachment_id, "csv", b"original")
    with pytest.raises(FileExistsError):
        store_file(tmp_path, attachment_id, "csv", b"replacement")
    assert (tmp_path / f"{attachment_id}.csv").read_bytes() == b"original"
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    planted = attachments.new_attachment_id()
    (tmp_path / f"{planted}.pdf").symlink_to(victim)
    with pytest.raises(FileExistsError):
        store_file(tmp_path, planted, "pdf", b"evil")
    assert victim.read_bytes() == b"keep"


@pytest.mark.parametrize("bad_id", ["../escape", "ATT-../../x", "client-name", "ATT-a/b"])
async def test_storage_refuses_ids_that_are_not_server_generated(tmp_path, bad_id):
    with pytest.raises(ValueError):
        store_file(tmp_path, bad_id, "pdf", b"x")
    assert list(tmp_path.iterdir()) == []


async def test_partial_write_is_removed(tmp_path, monkeypatch):
    def failing_fsync(descriptor):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError):
        store_file(tmp_path, attachments.new_attachment_id(), "pdf", b"%PDF-partial")
    assert list(tmp_path.iterdir()) == []


async def test_remove_file_only_touches_the_storage_directory(tmp_path):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"keep")
    inner = tmp_path / "store"
    inner.mkdir(mode=0o700)
    remove_file(inner, "../outside.pdf")
    assert outside.read_bytes() == b"keep"


async def test_existing_private_directory_is_used(tmp_path):
    directory = tmp_path / "existing"
    directory.mkdir(mode=0o700)
    ref = store_file(directory, attachments.new_attachment_id(), "pdf", b"%PDF-ok")
    assert (directory / ref).read_bytes() == b"%PDF-ok"
    assert stat.S_IMODE((directory / ref).stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o750, 0o701, 0o720])
async def test_existing_directory_open_to_others_is_rejected_unchanged(tmp_path, mode):
    directory = tmp_path / "shared"
    directory.mkdir()
    directory.chmod(mode)
    with pytest.raises(attachments.UnsafeStorageError):
        store_file(directory, attachments.new_attachment_id(), "pdf", b"%PDF-x")
    assert list(directory.iterdir()) == []
    assert stat.S_IMODE(directory.stat().st_mode) == mode  # never silently "fixed"
    with pytest.raises(attachments.UnsafeStorageError):
        remove_file(directory, "ATT-X.pdf")


async def test_symlinked_storage_directory_is_rejected(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    link = tmp_path / "store"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(attachments.UnsafeStorageError):
        store_file(link, attachments.new_attachment_id(), "pdf", b"%PDF-x")
    assert list(target.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["elsewhere", "store"]


async def test_storage_directory_owned_by_someone_else_is_rejected(tmp_path, monkeypatch):
    directory = tmp_path / "theirs"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(directory).st_uid + 1)
    with pytest.raises(attachments.UnsafeStorageError):
        store_file(directory, attachments.new_attachment_id(), "pdf", b"%PDF-x")
    assert list(directory.iterdir()) == []


async def test_directory_swapped_after_the_check_is_never_written(tmp_path, monkeypatch):
    # The file is created relative to the verified descriptor, not by path: replacing
    # the path with a symlink after the check cannot redirect the write.
    directory = tmp_path / "store"
    directory.mkdir(mode=0o700)
    target = tmp_path / "attacker"
    target.mkdir(mode=0o700)
    real_open = attachments._open_directory

    def open_then_swap(path, *, create):
        descriptor = real_open(path, create=create)
        path.rename(tmp_path / "moved")
        path.symlink_to(target, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(attachments, "_open_directory", open_then_swap)
    ref = store_file(directory, attachments.new_attachment_id(), "pdf", b"%PDF-x")
    assert list(target.iterdir()) == []
    assert (tmp_path / "moved" / ref).read_bytes() == b"%PDF-x"


# --- Settings ----------------------------------------------------------------------------


async def test_attachment_settings_are_validated():
    settings = ApiSettings.from_env(
        {"RFQ_API_ATTACHMENT_DIR": "/srv/rfq", "RFQ_API_MAX_UPLOAD_BYTES": "2048"}
    )
    assert str(settings.attachment_dir) == "/srv/rfq" and settings.max_upload_bytes == 2048
    assert ApiSettings().attachment_dir is None  # uploads are off until configured
    for bad in ({"attachment_dir": "relative/dir"}, {"max_upload_bytes": 0}):
        with pytest.raises(ValueError):
            ApiSettings(**bad)


# --- API ---------------------------------------------------------------------------------


class FakeSession:
    def __init__(self, fail_commit=False):
        self.added, self.commits, self.rollbacks = [], 0, 0
        self.fail_commit = fail_commit

    async def commit(self):
        if self.fail_commit:
            raise OperationalError("COMMIT", {}, Exception("server closed the connection"))
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakeDatabase:
    """Mirrors Database.session: driver errors become a URL-free DatabaseError."""

    def __init__(self, session):
        self.fake = session

    @asynccontextmanager
    async def session(self):
        try:
            yield self.fake
        except OperationalError:
            raise DatabaseError("Database operation failed.") from None


@pytest.fixture
def fakes(monkeypatch):
    async def belongs(session, rfq_id, *, customer_id):
        return OWNERS.get(rfq_id) == customer_id

    def add(session, **row):
        session.added.append(row)

    async def listing(session, rfq_id, *, customer_id):
        return ()

    monkeypatch.setattr(repositories, "rfq_belongs_to", belongs)
    monkeypatch.setattr(repositories, "add_attachment", add)
    monkeypatch.setattr(repositories, "list_attachments", listing)


def make_app(tmp_path, principal=MINE, session=None, max_bytes=LIMIT, storage=True):
    settings = ApiSettings(
        environment="development",
        attachment_dir=tmp_path / "store" if storage else None,
        max_upload_bytes=max_bytes,
    )
    app = create_app(settings)
    if principal is not None:
        app.dependency_overrides[get_principal] = lambda: Principal(principal)
    app.state.write_database = FakeDatabase(session or FakeSession())
    app.state.fake_session = app.state.write_database.fake

    async def read_session():
        yield app.state.fake_session

    app.dependency_overrides[get_read_session] = read_session
    return app


def stored(tmp_path):
    directory = tmp_path / "store"
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


async def upload(app, rfq_id="RFQ-A1", files=None, data=None, headers=None, content=None):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as http:
        if content is not None:
            return await http.post(URL.format(rfq_id), content=content, headers=headers)
        return await http.post(URL.format(rfq_id), files=files, data=data, headers=headers)


def pdf_file(name="spec.pdf", data=None, media="application/pdf"):
    return {"file": (name, data if data is not None else pdf("Bracket RFQ-A1"), media)}


async def raw_post(app, path, body, headers):
    """Drive the ASGI app directly and count how often the body was requested."""
    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": body, "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
    }  # fmt: skip
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, reads


def multipart(data=b"x", name="file", filename="spec.pdf"):
    boundary = "b0undary"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, {"content-type": f"multipart/form-data; boundary={boundary}"}


async def test_authorized_pdf_upload_is_stored_linked_and_extracted(tmp_path, fakes):
    app = make_app(tmp_path)
    data = pdf("Bracket RFQ-A1", None)
    response = await upload(app, files=pdf_file(data=data))
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "attachment_id", "rfq_id", "filename", "document_type", "revision", "sha256",
        "size_bytes", "extraction",
    }  # fmt: skip
    assert body["rfq_id"] == "RFQ-A1" and body["filename"] == "spec.pdf"
    assert body["document_type"] == "pdf" and body["revision"] is None
    assert body["sha256"] == hashlib.sha256(data).hexdigest() and body["size_bytes"] == len(data)
    assert body["extraction"]["kind"] == "pdf" and body["extraction"]["status"] == "text"
    assert body["extraction"]["pages"][1] == {"page_number": 2, "text": ""}
    session = app.state.fake_session
    assert session.commits == 1
    [row] = session.added
    assert row["rfq_id"] == "RFQ-A1" and row["attachment_id"] == body["attachment_id"]
    assert stored(tmp_path) == [row["storage_ref"]] == [f"{body['attachment_id']}.pdf"]
    assert (tmp_path / "store" / row["storage_ref"]).read_bytes() == data
    # The storage reference and directory never reach the client.
    assert row["storage_ref"] not in response.text and str(tmp_path) not in response.text


async def test_authorized_bom_upload_returns_exact_rows(tmp_path, fakes):
    app = make_app(tmp_path)
    response = await upload(app, files={"file": ("RFQ-A1_revB.csv", bom(ROW), "text/csv")})
    assert response.status_code == 201
    [row] = response.json()["extraction"]["rows"]
    assert response.json()["extraction"]["kind"] == "bom"
    assert row["width_mm"] == "80.50" and row["tolerance_mm"] == "0.10"
    assert row["height_mm"] is None and row["quantity"] == 10 and row["material_id"] == "MAT-T01"
    assert stored(tmp_path)[0].endswith(".csv")


async def test_repeated_uploads_get_distinct_ids_and_files(tmp_path, fakes):
    app = make_app(tmp_path)
    first = await upload(app, files=pdf_file())
    second = await upload(app, files=pdf_file())
    assert first.json()["attachment_id"] != second.json()["attachment_id"]
    assert first.json()["sha256"] == second.json()["sha256"]
    assert len(stored(tmp_path)) == 2


async def test_missing_identity_reads_no_body_and_writes_nothing(tmp_path, fakes):
    app = make_app(tmp_path, principal=None)

    async def tripwire():
        raise AssertionError("no database without identity")

    app.dependency_overrides[get_write_database] = tripwire
    body, headers = multipart(pdf("x"))
    status, reads = await raw_post(app, URL.format("RFQ-A1"), body, headers)
    assert (status, reads) == (401, 0)
    response = await upload(app, files=pdf_file())
    assert response.status_code == 401 and response.json() == {"detail": "Authentication required."}
    assert stored(tmp_path) == []


@pytest.mark.parametrize(
    "spoof",
    [
        {"headers": {"X-Customer-Id": MINE, "Authorization": "Bearer forged"}},
        {"data": {"customer_id": MINE}},
    ],
)
async def test_request_supplied_identity_is_never_trusted(tmp_path, fakes, spoof):
    response = await upload(make_app(tmp_path, principal=None), files=pdf_file(), **spoof)
    assert response.status_code == 401
    assert stored(tmp_path) == []


async def test_customer_id_form_field_cannot_redirect_an_upload(tmp_path, fakes):
    app = make_app(tmp_path, principal=THEIRS)
    response = await upload(app, "RFQ-A1", files=pdf_file(), data={"customer_id": MINE})
    assert response.status_code == 404  # ownership comes from the principal only
    app = make_app(tmp_path, principal=MINE)
    response = await upload(app, "RFQ-A1", files=pdf_file(), data={"customer_id": THEIRS})
    assert response.status_code == 400 and response.json()["reason"] == "malformed_multipart"
    assert stored(tmp_path) == [] and app.state.fake_session.added == []


async def test_cross_customer_and_missing_rfqs_are_indistinguishable(tmp_path, fakes):
    app = make_app(tmp_path)
    body, headers = multipart(pdf("x"))
    for rfq_id in ("RFQ-B1", "RFQ-NOPE"):
        status, reads = await raw_post(app, URL.format(rfq_id), body, headers)
        assert (status, reads) == (404, 0)  # rejected before the body is read
    theirs = await upload(app, "RFQ-B1", files=pdf_file())
    absent = await upload(app, "RFQ-NOPE", files=pdf_file())
    assert theirs.status_code == absent.status_code == 404
    assert theirs.json() == absent.json() == {"detail": "Not found."}
    listing = await make_app_get(app, "RFQ-B1")
    assert listing.status_code == 404 and listing.json() == {"detail": "Not found."}
    assert stored(tmp_path) == [] and app.state.fake_session.added == []


async def make_app_get(app, rfq_id):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as http:
        return await http.get(URL.format(rfq_id))


async def test_unconfigured_storage_fails_closed(tmp_path, fakes):
    app = make_app(tmp_path, storage=False)
    response = await upload(app, files=pdf_file())
    assert response.status_code == 503 and response.json() == {"detail": "Service unavailable."}
    assert app.state.fake_session.added == []


async def test_declared_oversize_body_is_rejected_before_reading(tmp_path, fakes):
    app = make_app(tmp_path, max_bytes=1024)
    body, headers = multipart(b"%PDF-" + b"x" * 200_000)
    status, reads = await raw_post(
        app, URL.format("RFQ-A1"), body, {**headers, "content-length": str(len(body))}
    )
    assert (status, reads) == (413, 0)
    assert stored(tmp_path) == []


async def test_streamed_oversize_body_is_cut_off_without_content_length(tmp_path, fakes):
    app = make_app(tmp_path, max_bytes=1024)
    body, headers = multipart(b"%PDF-" + b"x" * 200_000)

    async def chunks():
        for start in range(0, len(body), 4096):
            yield body[start : start + 4096]

    response = await upload(app, content=chunks(), headers=headers)
    assert response.status_code == 413 and response.json()["reason"] == "request_too_large"
    assert stored(tmp_path) == [] and app.state.fake_session.added == []


async def test_file_over_the_limit_inside_the_body_allowance_is_rejected(tmp_path, fakes):
    app = make_app(tmp_path, max_bytes=1024)
    response = await upload(app, files=pdf_file(data=pdf("x" * 2000)))
    assert response.status_code == 413 and response.json()["reason"] == "file_too_large"
    assert stored(tmp_path) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"files": [("file", ("a.pdf", b"%PDF-1", "application/pdf"))] * 2},  # two files
        {"files": {"document": ("a.pdf", b"%PDF-1", "application/pdf")}},  # wrong field
        {"data": {"file": "not a file"}},  # field instead of file
        {"content": b"%PDF-1.4", "headers": {"content-type": "application/pdf"}},
        {"content": b"--x\r\n", "headers": {"content-type": "multipart/form-data"}},
        {"content": b"garbage", "headers": {"content-type": "multipart/form-data; boundary=z"}},
        {"content": b"", "headers": {"content-type": "multipart/form-data; boundary=z"}},
    ],
)
async def test_multipart_structure_is_limited(tmp_path, fakes, kwargs):
    app = make_app(tmp_path)
    response = await upload(app, **kwargs)
    assert response.status_code == 400
    assert response.json()["reason"] == "malformed_multipart"
    assert stored(tmp_path) == [] and app.state.fake_session.added == []


async def test_non_numeric_content_length_is_rejected(tmp_path, fakes):
    body, headers = multipart(pdf("x"))
    status, _ = await raw_post(
        make_app(tmp_path), URL.format("RFQ-A1"), body, {**headers, "content-length": "-1"}
    )
    assert status == 400


SECRET = "TOPSECRET-4242"  # matches the identifier shape on purpose


@pytest.mark.parametrize(
    "files, status, expected",
    [
        (pdf_file(name=f"../{SECRET}.pdf"), 422, "invalid_filename"),
        (pdf_file(name=f"{SECRET}.exe"), 415, "unsupported_type"),
        (pdf_file(name="x.csv", media="text/csv"), 415, "content_type_mismatch"),
        (pdf_file(data=b"%PDF-1.4\n" + SECRET.encode()), 422, "pdf_malformed"),
        (pdf_file(data=encrypted_pdf()), 422, "pdf_encrypted"),
        (pdf_file(data=b""), 422, "empty_file"),
        ({"file": ("b.csv", bom(header=f"{SECRET},x"), "text/csv")}, 422, "bom_columns"),
        (
            {"file": ("b.csv", bom(ROW.replace("MAT-T01", f"={SECRET}")), "text/csv")},
            422,
            "bom_rows",
        ),
    ],
)
async def test_rejections_are_fixed_vocabulary_and_never_echo_input(
    tmp_path, fakes, files, status, expected
):
    app = make_app(tmp_path)
    response = await upload(app, files=files)
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"code", "message", "reason", "issues"}
    assert body["code"] == "ATTACHMENT_REJECTED" and body["reason"] == expected
    assert SECRET not in response.text and "Traceback" not in response.text
    assert stored(tmp_path) == [] and app.state.fake_session.added == []
    if expected == "bom_rows":
        assert body["issues"] == [{"row": 1, "column": "material_id", "code": "formula_like"}]


async def test_database_failure_removes_the_file_and_answers_503(tmp_path, fakes):
    app = make_app(tmp_path, session=FakeSession(fail_commit=True))
    response = await upload(app, files=pdf_file())
    assert response.status_code == 503 and response.json() == {"detail": "Service unavailable."}
    assert "connection" not in response.text
    assert stored(tmp_path) == []


async def test_failure_after_storing_removes_the_file(tmp_path, fakes, monkeypatch):
    def broken_add(session, **row):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(repositories, "add_attachment", broken_add)
    app = make_app(tmp_path)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(URL.format("RFQ-A1"), files=pdf_file())
    assert response.status_code == 500 and response.text == "Internal Server Error"
    assert stored(tmp_path) == []


async def test_cancellation_after_storing_removes_the_file(tmp_path, fakes, monkeypatch):
    async def cancelled_commit(self):
        raise asyncio.CancelledError

    monkeypatch.setattr(FakeSession, "commit", cancelled_commit)
    app = make_app(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await upload(app, files=pdf_file())
    assert stored(tmp_path) == []


async def test_unsafe_storage_directory_answers_503_and_writes_nothing(tmp_path, fakes):
    (tmp_path / "store").mkdir()
    (tmp_path / "store").chmod(0o777)
    app = make_app(tmp_path)
    response = await upload(app, files=pdf_file())
    assert response.status_code == 503 and response.json() == {"detail": "Service unavailable."}
    assert str(tmp_path) not in response.text
    assert stored(tmp_path) == [] and app.state.fake_session.added == []
    assert app.state.fake_session.commits == 0


async def test_cancellation_while_the_write_thread_runs_leaves_no_file(
    tmp_path, fakes, monkeypatch
):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_store = router_module.store_file

    def paused_store(*args):
        started.set()
        try:
            assert release.wait(5), "test never released the worker"
            return real_store(*args)
        finally:
            finished.set()

    monkeypatch.setattr(router_module, "store_file", paused_store)
    app = make_app(tmp_path)
    request = asyncio.create_task(upload(app, files=pdf_file()))
    assert await asyncio.to_thread(started.wait, 5)  # worker paused before writing
    request.cancel()
    for _ in range(5):
        await asyncio.sleep(0)  # deliver the cancellation while the worker is blocked
    assert not request.done()  # the handler is waiting for the thread, not abandoning it
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert finished.is_set()  # the file was written, after the cancellation...
    assert stored(tmp_path) == []  # ...and removed before the request ended
    session = app.state.fake_session
    assert session.added == [] and session.commits == 0


async def test_invalid_rfq_id_uses_the_structured_422(tmp_path, fakes):
    response = await upload(make_app(tmp_path), " ", files=pdf_file())
    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_VALIDATION_ERROR"
    assert stored(tmp_path) == []


async def test_metadata_list_requires_identity(tmp_path, fakes):
    app = make_app(tmp_path, principal=None)
    response = await make_app_get(app, "RFQ-A1")
    assert response.status_code == 401
    ok = await make_app_get(make_app(tmp_path), "RFQ-A1")
    assert ok.status_code == 200 and ok.json() == {"items": []}


async def test_production_upload_is_unreachable_without_an_authenticator(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    app = create_app(ApiSettings(environment="production", attachment_dir=tmp_path / "store"))
    response = await upload(app, files=pdf_file())
    assert response.status_code == 401
    assert app.state.write_database is None and stored(tmp_path) == []


async def test_openapi_documents_the_upload_contract():
    schema = create_app(ApiSettings(environment="development")).openapi()
    post = schema["paths"]["/api/v1/rfqs/{rfq_id}/attachments"]["post"]
    assert post["tags"] == ["attachments"] and "201" in post["responses"]
    assert {"400", "404", "413", "415", "422", "503"} <= set(post["responses"])
    assert "requestBody" not in post  # the body is parsed by the bounded reader only
    components = schema["components"]["schemas"]
    assert "storage_ref" not in components["AttachmentUploadResponse"]["properties"]
    assert components["AttachmentRejection"]["additionalProperties"] is False
