"""Approved Day 4 rfqs mappings; metadata definitions only."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rfq_copilot.models.base import Base

if TYPE_CHECKING:
    from rfq_copilot.models.customers import Customer
    from rfq_copilot.models.identity import User
    from rfq_copilot.models.manufacturing import Finish, Material
    from rfq_copilot.models.quotations import PricingCalculation, Quote
    from rfq_copilot.models.reliability import AuditEvent, WorkflowRun


class RFQ(Base):
    """Approved Day 4 table rfqs; database constraints are applied by later migrations."""

    __tablename__ = "rfqs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id"],
            ["business.customers.customer_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["business.users.user_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "status IN ('draft','intake','validated','blocked','closed','archived')", name="rule_01"
        ),
        CheckConstraint("row_version >= 1", name="rule_02"),
        CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64", name="rfq_id_identity"
        ),
        CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name="customer_id_identity",
        ),
        CheckConstraint("jsonb_typeof(part_ids) = 'array'", name="part_ids_shape"),
        CheckConstraint(
            "length(btrim(created_by)) > 0 AND length(created_by) <= 64", name="created_by_identity"
        ),
        Index(
            "ix_rfqs_customer_submitted",
            "customer_id",
            text("submitted_at DESC"),
            "rfq_id",
            unique=False,
            postgresql_using="btree",
        ),
        {"schema": "business"},
    )

    rfq_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text(), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    raw_request: Mapped[str | None] = mapped_column(Text(), nullable=True)
    source_type: Mapped[str] = mapped_column(Text(), nullable=False)
    part_ids: Mapped[list[Any]] = mapped_column(
        JSONB(none_as_null=True), server_default=text("'[]'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text(), server_default=text("'draft'"), nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text(), nullable=True)
    row_version: Mapped[int] = mapped_column(BigInteger(), server_default=text("1"), nullable=False)

    customer: Mapped[Customer] = relationship(
        "Customer",
        back_populates="rfqs",
        primaryjoin="RFQ.customer_id == Customer.customer_id",
        foreign_keys="[RFQ.customer_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    creator: Mapped[User | None] = relationship(
        "User",
        back_populates="created_rfqs",
        primaryjoin="RFQ.created_by == User.user_id",
        foreign_keys="[RFQ.created_by]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    line_items: Mapped[list[RFQLineItem]] = relationship(
        "RFQLineItem",
        back_populates="rfq",
        primaryjoin="RFQLineItem.rfq_id == RFQ.rfq_id",
        foreign_keys="[RFQLineItem.rfq_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    attachments: Mapped[list[RFQAttachment]] = relationship(
        "RFQAttachment",
        back_populates="rfq",
        primaryjoin="RFQAttachment.rfq_id == RFQ.rfq_id",
        foreign_keys="[RFQAttachment.rfq_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    pricing_calculations: Mapped[list[PricingCalculation]] = relationship(
        "PricingCalculation",
        back_populates="rfq",
        primaryjoin="PricingCalculation.rfq_id == RFQ.rfq_id",
        foreign_keys="[PricingCalculation.rfq_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    quote: Mapped[Quote | None] = relationship(
        "Quote",
        back_populates="rfq",
        primaryjoin="Quote.rfq_id == RFQ.rfq_id",
        foreign_keys="[Quote.rfq_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        "AuditEvent",
        back_populates="rfq",
        primaryjoin="AuditEvent.rfq_id == RFQ.rfq_id",
        foreign_keys="[AuditEvent.rfq_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    workflow_runs: Mapped[list[WorkflowRun]] = relationship(
        "WorkflowRun",
        back_populates="rfq",
        primaryjoin="WorkflowRun.rfq_id == RFQ.rfq_id",
        foreign_keys="[WorkflowRun.rfq_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class RFQLineItem(Base):
    """Approved Day 4 table rfq_line_items; database constraints are applied by later migrations."""

    __tablename__ = "rfq_line_items"
    __table_args__ = (
        UniqueConstraint("rfq_id", "line_number"),
        UniqueConstraint("rfq_id", "part_id"),
        ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["material_id"],
            ["business.materials.material_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["finish_id"],
            ["business.finishes.finish_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint("line_number > 0 AND requirement_revision > 0", name="rule_01"),
        CheckConstraint(
            "length_mm > 0 AND width_mm > 0 AND height_mm > 0 AND thickness_mm > 0 AND bend_radius_mm > 0",
            name="rule_02",
        ),
        CheckConstraint(
            "tolerance_mm >= 0 AND weld_length_mm >= 0 AND quantity > 0 AND hole_count >= 0",
            name="rule_03",
        ),
        CheckConstraint(
            "part_type IN ('flat_panel','mounting_plate','l_bracket','u_bracket','enclosure','fabricated_assembly')",
            name="rule_04",
        ),
        CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64", name="rfq_id_identity"
        ),
        CheckConstraint(
            "length(btrim(part_id)) > 0 AND length(part_id) <= 64", name="part_id_identity"
        ),
        CheckConstraint(
            "length(btrim(material_id)) > 0 AND length(material_id) <= 64",
            name="material_id_identity",
        ),
        CheckConstraint(
            "length(btrim(finish_id)) > 0 AND length(finish_id) <= 64", name="finish_id_identity"
        ),
        CheckConstraint("length_mm <> 'NaN'::numeric", name="length_mm_finite"),
        CheckConstraint("width_mm <> 'NaN'::numeric", name="width_mm_finite"),
        CheckConstraint("height_mm <> 'NaN'::numeric", name="height_mm_finite"),
        CheckConstraint("thickness_mm <> 'NaN'::numeric", name="thickness_mm_finite"),
        CheckConstraint("tolerance_mm <> 'NaN'::numeric", name="tolerance_mm_finite"),
        CheckConstraint("bend_radius_mm <> 'NaN'::numeric", name="bend_radius_mm_finite"),
        CheckConstraint("weld_length_mm <> 'NaN'::numeric", name="weld_length_mm_finite"),
        CheckConstraint("jsonb_typeof(source_evidence) = 'array'", name="source_evidence_shape"),
        {"schema": "business"},
    )

    rfq_line_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), default=uuid4, primary_key=True, nullable=False
    )
    rfq_id: Mapped[str] = mapped_column(Text(), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    part_id: Mapped[str] = mapped_column(Text(), nullable=False)
    part_type: Mapped[str | None] = mapped_column(Text(), nullable=True)
    material_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    finish_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    length_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 3, asdecimal=True), nullable=True)
    width_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 3, asdecimal=True), nullable=True)
    height_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 3, asdecimal=True), nullable=True)
    thickness_mm: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3, asdecimal=True), nullable=True
    )
    tolerance_mm: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3, asdecimal=True), nullable=True
    )
    bend_radius_mm: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3, asdecimal=True), nullable=True
    )
    weld_length_mm: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3, asdecimal=True), nullable=True
    )
    quantity: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    hole_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    source_evidence: Mapped[list[Any]] = mapped_column(
        JSONB(none_as_null=True), server_default=text("'[]'::jsonb"), nullable=False
    )
    requirement_revision: Mapped[int] = mapped_column(
        Integer(), server_default=text("1"), nullable=False
    )

    rfq: Mapped[RFQ] = relationship(
        "RFQ",
        back_populates="line_items",
        primaryjoin="RFQLineItem.rfq_id == RFQ.rfq_id",
        foreign_keys="[RFQLineItem.rfq_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    material: Mapped[Material | None] = relationship(
        "Material",
        back_populates="rfq_lines",
        primaryjoin="RFQLineItem.material_id == Material.material_id",
        foreign_keys="[RFQLineItem.material_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    finish: Mapped[Finish | None] = relationship(
        "Finish",
        back_populates="rfq_lines",
        primaryjoin="RFQLineItem.finish_id == Finish.finish_id",
        foreign_keys="[RFQLineItem.finish_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )


class RFQAttachment(Base):
    """Approved Day 4 table rfq_attachments; database constraints are applied by later migrations."""

    __tablename__ = "rfq_attachments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint("document_type IN ('pdf','csv','md')", name="rule_01"),
        CheckConstraint("is_trusted_instruction = FALSE", name="rule_02"),
        CheckConstraint(
            "length(btrim(attachment_id)) > 0 AND length(attachment_id) <= 64",
            name="attachment_id_identity",
        ),
        CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64", name="rfq_id_identity"
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        {"schema": "business"},
    )

    attachment_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    rfq_id: Mapped[str] = mapped_column(Text(), nullable=False)
    filename: Mapped[str] = mapped_column(Text(), nullable=False)
    storage_ref: Mapped[str] = mapped_column(Text(), nullable=False)
    revision: Mapped[str | None] = mapped_column(Text(), nullable=True)
    document_type: Mapped[str] = mapped_column(Text(), nullable=False)
    source_role: Mapped[str] = mapped_column(Text(), nullable=False)
    is_trusted_instruction: Mapped[bool] = mapped_column(
        Boolean(), server_default=text("FALSE"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(Text(), nullable=False)

    rfq: Mapped[RFQ] = relationship(
        "RFQ",
        back_populates="attachments",
        primaryjoin="RFQAttachment.rfq_id == RFQ.rfq_id",
        foreign_keys="[RFQAttachment.rfq_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
