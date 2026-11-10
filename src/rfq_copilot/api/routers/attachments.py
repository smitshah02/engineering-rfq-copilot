"""Attachments of the verified customer's RFQs: upload (PDF, CSV BOM) and metadata list.

Upload order, each step failing closed before the next:
1. ``get_principal`` (401). Nothing else runs, the body is not read, no file or row.
2. A storage directory is configured (503).
3. The RFQ belongs to the principal (404, identical for another customer's RFQ and a
   missing one). The connection is then released while the body streams in.
4. Content-Length, then a byte cap while the multipart body streams: one file part named
   ``file``, no other fields (400/413).
5. Name, declared type, real content and extraction (``rfq_copilot.attachments``).
6. A new server-named file (O_EXCL, 0600) in a verified private directory, then ownership
   is checked again and the row is inserted and committed in one transaction. Any
   failure, cancellation included, waits for the write thread and then removes the file.
There is no download route.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path as FilePath
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from rfq_copilot.api.auth import Principal, get_principal
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.db import get_read_session, get_write_session, unavailable
from rfq_copilot.api.errors import VALIDATION_ERROR_RESPONSES, ValidationErrorResponse
from rfq_copilot.api.schemas.attachments import (
    AttachmentList,
    AttachmentMetadata,
    AttachmentRejection,
    AttachmentUploadResponse,
    BomExtraction,
    PdfExtraction,
)
from rfq_copilot.api.schemas.common import Identifier
from rfq_copilot.attachments import (
    AttachmentRejected,
    RejectReason,
    check_file,
    new_attachment_id,
    remove_file,
    store_file,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/rfqs/{rfq_id}/attachments",
    tags=["attachments"],
    responses=VALIDATION_ERROR_RESPONSES,
)

MULTIPART_OVERHEAD = 64 * 1024  # boundaries and part headers on top of the file itself

STATUS: dict[str, int] = {
    "malformed_multipart": 400,
    "request_too_large": 413,
    "file_too_large": 413,
    "unsupported_type": 415,
    "content_type_mismatch": 415,
}  # every other reason is 422

# Static text; never built from the request, the file or an exception.
MESSAGES: dict[str, str] = {
    "malformed_multipart": "Send multipart/form-data with exactly one file field named 'file'.",
    "request_too_large": "Request body is too large.",
    "file_too_large": "File is too large.",
    "invalid_filename": "File name is not allowed.",
    "unsupported_type": "Only PDF and CSV bill-of-materials files are accepted.",
    "content_type_mismatch": "File content does not match its name or declared type.",
    "empty_file": "File is empty.",
    "pdf_malformed": "PDF could not be read.",
    "pdf_encrypted": "Encrypted PDFs are not accepted.",
    "pdf_too_many_pages": "PDF has too many pages.",
    "bom_encoding": "CSV must be UTF-8 text.",
    "bom_malformed": "CSV could not be parsed.",
    "bom_columns": "CSV header must list exactly the required BOM columns.",
    "bom_rows": "CSV rows are missing or invalid.",
    "bom_too_many_rows": "CSV has too many rows.",
}

_REJECTION = {"model": AttachmentRejection, "description": "Attachment rejected."}
UPLOAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: _REJECTION,
    404: {"description": "RFQ not found."},
    413: _REJECTION,
    415: _REJECTION,
    422: {
        "model": ValidationErrorResponse | AttachmentRejection,
        "description": "Invalid path parameter or rejected file content.",
    },
    503: {"description": "Attachment storage or database unavailable."},
}


def rejection(reason: RejectReason, issues: tuple = ()) -> JSONResponse:
    body = AttachmentRejection(
        code="ATTACHMENT_REJECTED",
        message=MESSAGES[reason],
        reason=reason,
        issues=list(issues),
    )
    return JSONResponse(status_code=STATUS.get(reason, 422), content=body.model_dump())


def get_settings(request: Request) -> ApiSettings:
    return request.app.state.settings


def get_storage_dir(
    principal: Annotated[Principal, Depends(get_principal)],
    settings: Annotated[ApiSettings, Depends(get_settings)],
) -> FilePath:
    if settings.attachment_dir is None:
        raise unavailable()
    return settings.attachment_dir


class _BodyTooLarge(MultiPartException):
    """A MultiPartException, so Starlette closes any spooled part file on the way out."""


async def _capped(stream: AsyncIterator[bytes], limit: int) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in stream:
        total += len(chunk)
        if total > limit:
            raise _BodyTooLarge("too large")
        yield chunk


async def read_upload(request: Request, max_bytes: int) -> tuple[Any, str | None, bytes]:
    """Parse the bounded multipart body; return the part's filename, type and bytes."""
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise AttachmentRejected("malformed_multipart")
    limit = max_bytes + MULTIPART_OVERHEAD
    length = request.headers.get("content-length")
    if length is not None and not (length.isascii() and length.isdigit()):
        raise AttachmentRejected("malformed_multipart")
    if length is not None and int(length) > limit:
        raise AttachmentRejected("request_too_large")
    parser = MultiPartParser(
        request.headers, _capped(request.stream(), limit), max_files=1, max_fields=0
    )
    try:
        form = await parser.parse()
    except _BodyTooLarge:
        raise AttachmentRejected("request_too_large") from None
    except MultiPartException:
        raise AttachmentRejected("malformed_multipart") from None
    try:
        items = form.multi_items()
        if len(items) != 1 or items[0][0] != "file" or not isinstance(items[0][1], UploadFile):
            raise AttachmentRejected("malformed_multipart")
        upload = items[0][1]
        return upload.filename, upload.content_type, await upload.read(max_bytes + 1)
    finally:
        await form.close()


_CLEANUPS: set[asyncio.Task] = set()  # strong references until each cleanup finishes


async def _discard(write: asyncio.Future, storage: FilePath) -> None:
    await asyncio.wait([write])  # the thread runs to the end even if the request is gone
    if not write.cancelled() and write.exception() is None:
        # Only a file this request created; a failed write already removed its own.
        await asyncio.to_thread(remove_file, storage, write.result())


async def discard_after(write: asyncio.Future, storage: FilePath) -> None:
    """Remove the stored file once the write finishes, even if this request is cancelled
    again: the cleanup is its own task, shielded and referenced until done."""
    cleanup = asyncio.ensure_future(_discard(write, storage))
    _CLEANUPS.add(cleanup)
    cleanup.add_done_callback(_CLEANUPS.discard)
    await asyncio.shield(cleanup)


@router.post(
    "",
    status_code=201,
    summary="Upload a PDF or CSV BOM to one of the verified customer's RFQs",
    responses=UPLOAD_RESPONSES,
)
async def upload_attachment(
    principal: Annotated[Principal, Depends(get_principal)],
    rfq_id: Annotated[Identifier, Path()],
    request: Request,
    storage: Annotated[FilePath, Depends(get_storage_dir)],
    settings: Annotated[ApiSettings, Depends(get_settings)],
    session: Annotated[Any, Depends(get_write_session)],
) -> AttachmentUploadResponse:
    from rfq_copilot.repositories import add_attachment, rfq_belongs_to

    customer_id = principal.customer_id
    owned = await rfq_belongs_to(session, rfq_id, customer_id=customer_id)
    await session.rollback()  # hand the connection back while the body streams in
    if not owned:
        raise HTTPException(status_code=404, detail="Not found.")
    try:
        filename, client_type, data = await read_upload(request, settings.max_upload_bytes)
        checked = await asyncio.to_thread(
            check_file, filename, client_type, data, settings.max_upload_bytes
        )
    except AttachmentRejected as exc:
        return rejection(exc.reason, exc.issues)

    attachment_id = new_attachment_id()
    # The worker thread cannot be cancelled, so the write is shielded and every failure
    # path (cancellation included) waits for it before removing what it wrote.
    write = asyncio.ensure_future(
        asyncio.to_thread(store_file, storage, attachment_id, checked.document_type, data)
    )
    try:
        storage_ref = await asyncio.shield(write)
        if not await rfq_belongs_to(session, rfq_id, customer_id=customer_id):
            raise HTTPException(status_code=404, detail="Not found.")
        add_attachment(
            session,
            attachment_id=attachment_id,
            rfq_id=rfq_id,
            filename=checked.filename,
            storage_ref=storage_ref,
            document_type=checked.document_type,
            sha256=checked.sha256,
        )
        await session.commit()
    except OSError:
        # Storage failure (unsafe directory, disk) or a driver error: no detail leaves.
        await discard_after(write, storage)
        logger.warning("Attachment upload failed: storage or database unavailable.")
        raise unavailable() from None
    except BaseException:
        # Includes cancellation. The session dependency rolls the transaction back.
        await discard_after(write, storage)
        raise

    extraction = checked.extraction
    model = PdfExtraction if extraction.kind == "pdf" else BomExtraction
    return AttachmentUploadResponse(
        attachment_id=attachment_id,
        rfq_id=rfq_id,
        filename=checked.filename,
        document_type=checked.document_type,
        revision=None,
        sha256=checked.sha256,
        size_bytes=checked.size_bytes,
        extraction=model.model_validate(extraction),
    )


@router.get("", summary="Attachment metadata of one of the verified customer's RFQs")
async def list_rfq_attachments(
    principal: Annotated[Principal, Depends(get_principal)],
    rfq_id: Annotated[Identifier, Path()],
    session: Annotated[Any, Depends(get_read_session)],
) -> AttachmentList:
    from rfq_copilot.repositories import list_attachments, rfq_belongs_to

    if not await rfq_belongs_to(session, rfq_id, customer_id=principal.customer_id):
        raise HTTPException(status_code=404, detail="Not found.")
    rows = await list_attachments(session, rfq_id, customer_id=principal.customer_id)
    return AttachmentList(items=[AttachmentMetadata.model_validate(row) for row in rows])
