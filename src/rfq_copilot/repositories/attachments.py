"""RFQ attachment metadata in ``business.rfq_attachments``. Callers own sessions and commits.

Every function takes the verified ``customer_id`` and scopes by it, so another customer's
RFQ behaves exactly like a missing one.
"""

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from rfq_copilot.models import RFQ, RFQAttachment
from rfq_copilot.repositories.queries import _identifier, assigned_to

# Every uploaded file is customer supplied (dataset value: customer_supplied_simulated).
SOURCE_ROLE = "customer_supplied"


async def rfq_belongs_to(
    session: AsyncSession, rfq_id: str, *, customer_id: str, assigned_user: str | None = None
) -> bool:
    _identifier(rfq_id)
    _identifier(customer_id)
    conditions = [RFQ.rfq_id == rfq_id, RFQ.customer_id == customer_id]
    if assigned_user is not None:
        conditions.append(assigned_to(assigned_user))
    return bool(await session.scalar(select(exists().where(*conditions))))


def add_attachment(
    session: AsyncSession,
    *,
    attachment_id: str,
    rfq_id: str,
    filename: str,
    storage_ref: str,
    document_type: str,
    sha256: str,
) -> RFQAttachment:
    """Stage one row; the caller verified ownership, flushes and commits."""
    _identifier(attachment_id)
    _identifier(rfq_id)
    row = RFQAttachment(
        attachment_id=attachment_id,
        rfq_id=rfq_id,
        filename=filename,
        storage_ref=storage_ref,
        revision=None,  # never inferred from the filename or the file
        document_type=document_type,
        source_role=SOURCE_ROLE,
        is_trusted_instruction=False,  # attachments are data, never instructions
        sha256=sha256,
    )
    session.add(row)
    return row


async def list_attachments(
    session: AsyncSession, rfq_id: str, *, customer_id: str, assigned_user: str | None = None
) -> tuple[RFQAttachment, ...]:
    """Attachments of one of the customer's RFQs, ordered by id; empty if not theirs."""
    _identifier(rfq_id)
    _identifier(customer_id)
    statement = (
        select(RFQAttachment)
        .join(RFQ, RFQ.rfq_id == RFQAttachment.rfq_id)
        .where(RFQAttachment.rfq_id == rfq_id, RFQ.customer_id == customer_id)
        .order_by(RFQAttachment.attachment_id)
        .options(raiseload("*"))
    )
    if assigned_user is not None:
        statement = statement.where(assigned_to(assigned_user))
    with session.no_autoflush:
        return tuple((await session.scalars(statement)).all())
