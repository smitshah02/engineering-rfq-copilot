"""Untrusted RFQ attachments: content checks, deterministic extraction and file storage.

Supported, as in dataset v1.1.0 (``attachments/``, table ``rfq_attachments``):
- ``pdf``: machine-readable text through pypdf. No OCR, no rendering, no link following,
  no JavaScript or embedded files; only ``extract_text`` is called. Encrypted and corrupt
  files are rejected; a PDF without extractable text is accepted with status ``no_text``.
- ``csv``: bills of materials with exactly the dataset's eleven columns. Cells are kept
  verbatim; numbers become exact ``Decimal``/``int`` or the file is rejected.
The table also allows ``md``; Markdown drawing descriptions are not accepted yet.

Extraction is not manufacturing validation. Nothing here reads the network, a database or
a user-supplied path, and no error message carries file content.
"""

import csv
import hashlib
import io
import os
import secrets
import stat
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal

from rfq_copilot.api.schemas.common import _PLAIN_DECIMAL, INT32_MAX

DocumentType = Literal["pdf", "csv"]

MAX_FILENAME_CHARS = 255
MAX_PDF_PAGES = 100
MAX_PDF_TEXT_CHARS = 200_000  # across all pages; the rest is cut and flagged
MAX_BOM_ROWS = 500  # ponytail: dataset BOMs have at most 2 rows; raise with evidence
MAX_BOM_CELL_CHARS = 64  # dataset identifier length
MAX_ISSUES = 20

BOM_COLUMNS = (
    "rfq_id",
    "revision",
    "line",
    "part_type",
    "length_mm",
    "width_mm",
    "height_mm",
    "material_id",
    "quantity",
    "tolerance_mm",
    "finish_id",
)
_TEXT_COLUMNS = ("rfq_id", "revision", "part_type", "material_id", "finish_id")
_DECIMAL_COLUMNS = ("length_mm", "width_mm", "height_mm", "tolerance_mm")
_INTEGER_COLUMNS = ("line", "quantity")
_OPTIONAL_COLUMNS = frozenset({"height_mm"})  # blank for flat parts in the dataset
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_SUFFIXES: dict[str, DocumentType] = {".pdf": "pdf", ".csv": "csv"}
_CLIENT_TYPES: dict[DocumentType, frozenset[str]] = {
    "pdf": frozenset({"application/pdf", "application/octet-stream"}),
    "csv": frozenset(
        {
            "text/csv",
            "application/csv",
            "text/plain",
            "application/vnd.ms-excel",
            "application/octet-stream",
        }  # fmt: skip
    ),
}

RejectReason = Literal[
    "malformed_multipart",
    "request_too_large",
    "invalid_filename",
    "unsupported_type",
    "content_type_mismatch",
    "empty_file",
    "file_too_large",
    "pdf_malformed",
    "pdf_encrypted",
    "pdf_too_many_pages",
    "bom_encoding",
    "bom_malformed",
    "bom_columns",
    "bom_rows",
    "bom_too_many_rows",
]
BomIssueCode = Literal[
    "missing_value", "invalid_number", "invalid_integer", "invalid_text", "formula_like",
    "too_long", "wrong_field_count",
]  # fmt: skip


@dataclass(frozen=True)
class BomIssue:
    row: int | None  # 1-based data row; None for the header
    column: str | None  # always one of BOM_COLUMNS
    code: BomIssueCode


class AttachmentRejected(Exception):
    """A rejection whose ``reason`` and ``issues`` are fixed vocabulary, safe to publish."""

    def __init__(self, reason: RejectReason, issues: tuple[BomIssue, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.issues = issues


@dataclass(frozen=True)
class PdfPage:
    page_number: int
    text: str


@dataclass(frozen=True)
class PdfExtraction:
    status: Literal["text", "no_text"]
    page_count: int
    pages: tuple[PdfPage, ...]
    truncated: bool
    kind: Literal["pdf"] = "pdf"


@dataclass(frozen=True)
class BomRow:
    rfq_id: str
    revision: str
    line: int
    part_type: str
    length_mm: Decimal
    width_mm: Decimal
    height_mm: Decimal | None
    material_id: str
    quantity: int
    tolerance_mm: Decimal
    finish_id: str


@dataclass(frozen=True)
class BomExtraction:
    rows: tuple[BomRow, ...]
    kind: Literal["bom"] = "bom"


@dataclass(frozen=True)
class CheckedFile:
    filename: str
    document_type: DocumentType
    size_bytes: int
    sha256: str
    extraction: PdfExtraction | BomExtraction = field(repr=False)


def check_filename(name: object) -> DocumentType:
    """The display name is metadata only, never a path. Unsafe names are rejected, not fixed."""
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= MAX_FILENAME_CHARS
        or name != name.strip()
        or name.startswith(".")
        or any(c in "/\\" or unicodedata.category(c) in ("Cc", "Cf") for c in name)
    ):
        raise AttachmentRejected("invalid_filename")
    document_type = _SUFFIXES.get(os.path.splitext(name)[1].lower())
    if document_type is None or os.path.splitext(name)[0] in ("", "."):
        raise AttachmentRejected("unsupported_type")
    return document_type


def check_file(
    filename: object, client_type: str | None, data: bytes, max_bytes: int
) -> CheckedFile:
    """Validate name, declared type, size and actual content, then extract. Deterministic."""
    document_type = check_filename(filename)
    if not data:
        raise AttachmentRejected("empty_file")
    if len(data) > max_bytes:
        raise AttachmentRejected("file_too_large")
    if data.startswith(_ZIP_MAGIC):  # spreadsheets and archives are never expanded
        raise AttachmentRejected("unsupported_type")
    if (document_type == "pdf") != data.startswith(_PDF_MAGIC):
        raise AttachmentRejected("content_type_mismatch")
    declared = (client_type or "application/octet-stream").split(";")[0].strip().lower()
    if declared not in _CLIENT_TYPES[document_type]:
        raise AttachmentRejected("content_type_mismatch")
    extraction = extract_pdf(data) if document_type == "pdf" else extract_bom(data)
    return CheckedFile(
        filename=filename,  # type: ignore[arg-type]  # checked by check_filename
        document_type=document_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        extraction=extraction,
    )


def extract_pdf(data: bytes) -> PdfExtraction:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise AttachmentRejected("pdf_encrypted")
        page_count = len(reader.pages)
        if page_count == 0:
            raise AttachmentRejected("pdf_malformed")
        if page_count > MAX_PDF_PAGES:
            raise AttachmentRejected("pdf_too_many_pages")
        pages, budget, truncated = [], MAX_PDF_TEXT_CHARS, False
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if len(text) > budget:
                text, truncated = text[:budget], True
            budget -= len(text)
            pages.append(PdfPage(number, text))
    except AttachmentRejected:
        raise
    # pypdf raises its own errors and, on hostile input, plain Python ones (bad indexes,
    # recursion, decode errors). None is reported; the file is simply malformed.
    except (PyPdfError, ValueError, TypeError, KeyError, IndexError, AttributeError,
            AssertionError, RecursionError, OSError, ArithmeticError):  # fmt: skip
        raise AttachmentRejected("pdf_malformed") from None
    has_text = any(page.text.strip() for page in pages)
    return PdfExtraction("text" if has_text else "no_text", page_count, tuple(pages), truncated)


def extract_bom(data: bytes) -> BomExtraction:
    try:
        text = data.decode("utf-8-sig")  # strict: no replacement characters are invented
    except UnicodeDecodeError:
        raise AttachmentRejected("bom_encoding") from None
    if "\x00" in text:
        raise AttachmentRejected("bom_encoding")
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error:
        raise AttachmentRejected("bom_malformed") from None
    if not records or tuple(records[0]) != BOM_COLUMNS:
        # Missing, renamed, reordered, duplicated or extra columns are all rejected: a
        # column is never guessed from position or similar spelling.
        raise AttachmentRejected("bom_columns")
    body = records[1:]
    if not body:
        raise AttachmentRejected("bom_rows")
    if len(body) > MAX_BOM_ROWS:
        raise AttachmentRejected("bom_too_many_rows")
    rows, issues = [], []
    for number, record in enumerate(body, start=1):
        if len(record) != len(BOM_COLUMNS):
            issues.append(BomIssue(number, None, "wrong_field_count"))
            continue
        values, row_issues = {}, []
        for column, cell in zip(BOM_COLUMNS, record, strict=True):
            value, code = _cell(column, cell)
            if code is None:
                values[column] = value
            else:
                row_issues.append(BomIssue(number, column, code))
        if row_issues:
            issues.extend(row_issues)
        else:
            rows.append(BomRow(**values))
    if issues:
        raise AttachmentRejected("bom_rows", tuple(issues[:MAX_ISSUES]))
    return BomExtraction(tuple(rows))


def _cell(column: str, cell: str) -> tuple[object, BomIssueCode | None]:
    if cell == "":
        return (None, None) if column in _OPTIONAL_COLUMNS else (None, "missing_value")
    if len(cell) > MAX_BOM_CELL_CHARS:
        return None, "too_long"
    if column in _TEXT_COLUMNS:
        if cell.startswith(_FORMULA_PREFIXES):
            return None, "formula_like"
        if cell != cell.strip() or any(unicodedata.category(c) in ("Cc", "Cf") for c in cell):
            return None, "invalid_text"
        return cell, None
    if column in _INTEGER_COLUMNS:
        if cell.isascii() and cell.isdigit() and 1 <= int(cell) <= INT32_MAX:
            return int(cell), None
        return None, "invalid_integer"
    # Decimal columns: a plain non-negative decimal, kept at the scale it was written.
    if _PLAIN_DECIMAL.fullmatch(cell) and cell.isascii():
        return Decimal(cell), None
    return None, "invalid_number"


# --- Storage ---------------------------------------------------------------------------


def new_attachment_id() -> str:
    return f"ATT-{secrets.token_hex(16).upper()}"  # 36 characters, 128 random bits


class UnsafeStorageError(OSError):
    """The configured storage directory is a symlink, not ours, or not private (0700)."""


def _open_directory(directory: Path, *, create: bool) -> int:
    """Open the storage directory itself and verify it; return a directory descriptor.

    ``O_NOFOLLOW`` refuses a symlink as the last component, and the checks run on the
    opened descriptor, so a directory swapped in after the check is never written to.
    The directory's mode is never changed: an unsafe one is rejected, as seeding does.
    POSIX only (``dir_fd``), like the rest of the deployment.
    """
    if create:
        try:
            directory.mkdir(mode=0o700, parents=True)
        except FileExistsError:
            pass
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        if os.path.islink(directory):
            raise UnsafeStorageError("storage directory must not be a symlink") from None
        raise
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(descriptor)
        raise UnsafeStorageError("storage directory must be owned by this user, mode 0700")
    return descriptor


def store_file(
    directory: Path, attachment_id: str, document_type: DocumentType, data: bytes
) -> str:
    """Write ``data`` to a new server-named file (mode 0600) and return its storage ref.

    The name comes only from the server-generated id. ``O_EXCL`` refuses to replace any
    existing file, and ``O_NOFOLLOW`` refuses a planted symlink. The file is created
    relative to the verified directory descriptor. A partial file is removed.
    """
    if not attachment_id.startswith("ATT-") or not attachment_id[4:].isalnum():
        raise ValueError("attachment_id must be server generated.")
    storage_ref = f"{attachment_id}.{document_type}"
    folder = _open_directory(directory, create=True)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(storage_ref, flags, 0o600, dir_fd=folder)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            os.unlink(storage_ref, dir_fd=folder)
            raise
    finally:
        os.close(folder)
    return storage_ref


def remove_file(directory: Path, storage_ref: str) -> None:
    folder = _open_directory(directory, create=False)
    try:
        os.unlink(Path(storage_ref).name, dir_fd=folder)
    except FileNotFoundError:
        pass
    finally:
        os.close(folder)
