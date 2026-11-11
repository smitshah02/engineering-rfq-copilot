"""Attachment upload, metadata and extraction contracts.

Never exposed: the storage reference or directory, the source role, file bytes beyond the
extracted text, exception text. Decimals keep the scale written in the file.
"""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from rfq_copilot.attachments import BomIssueCode, RejectReason

_STRICT = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

BomColumn = Literal[
    "rfq_id", "revision", "line", "part_type", "length_mm", "width_mm", "height_mm",
    "material_id", "quantity", "tolerance_mm", "finish_id",
]  # fmt: skip


class AttachmentMetadata(BaseModel):
    model_config = _STRICT

    attachment_id: str
    rfq_id: str
    filename: str
    document_type: Literal["pdf", "csv", "md"]
    revision: str | None
    sha256: str


class AttachmentList(BaseModel):
    model_config = _STRICT

    items: tuple[AttachmentMetadata, ...]


class PdfPage(BaseModel):
    model_config = _STRICT

    page_number: int
    text: str


class PdfExtraction(BaseModel):
    """Machine-readable text only. ``no_text`` (for example a scanned image) is not OCRed."""

    model_config = _STRICT

    kind: Literal["pdf"]
    status: Literal["text", "no_text"]
    page_count: int
    pages: tuple[PdfPage, ...]
    truncated: bool


class BomRow(BaseModel):
    """One BOM row as written. Not checked against the catalog or manufacturing rules."""

    model_config = _STRICT

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


class BomExtraction(BaseModel):
    model_config = _STRICT

    kind: Literal["bom"]
    rows: tuple[BomRow, ...]


class AttachmentUploadResponse(AttachmentMetadata):
    size_bytes: int
    extraction: Annotated[PdfExtraction | BomExtraction, Field(discriminator="kind")]


class BomIssue(BaseModel):
    model_config = _STRICT

    row: int | None
    column: BomColumn | None
    code: BomIssueCode


class AttachmentRejection(BaseModel):
    model_config = _STRICT

    code: Literal["ATTACHMENT_REJECTED"]
    message: str
    reason: RejectReason
    issues: tuple[BomIssue, ...]
