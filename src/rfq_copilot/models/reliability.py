"""Approved Day 4 reliability mappings; metadata definitions only."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
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
    from rfq_copilot.models.quotations import Quote, QuoteVersion
    from rfq_copilot.models.rfqs import RFQ


class IdempotencyRecord(Base):
    """Approved Day 4 table idempotency_records; database constraints are applied by later migrations."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("actor_id", "customer_id", "operation", "idempotency_key"),
        ForeignKeyConstraint(
            ["actor_id"],
            ["business.users.user_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["customer_id"],
            ["business.customers.customer_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            # attach_rfq_document: approved Phase 3.5 upload idempotency (20261111_04).
            "operation IN ('create_rfq','create_quote_draft','request_approval','approve_quote','reject_quote','release_quote','edit_quote_draft','attach_rfq_document')",
            name="rule_01",
        ),
        CheckConstraint(
            "length(btrim(actor_id)) > 0 AND length(actor_id) <= 64", name="actor_id_identity"
        ),
        CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name="customer_id_identity",
        ),
        CheckConstraint("length(btrim(idempotency_key)) > 0", name="idempotency_key_nonblank"),
        CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="request_sha256_hex"),
        CheckConstraint("jsonb_typeof(result_json) = 'object'", name="result_json_shape"),
        CheckConstraint("length(btrim(correlation_id)) > 0", name="correlation_id_nonblank"),
        {"schema": "business"},
    )

    receipt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), default=uuid4, primary_key=True, nullable=False
    )
    actor_id: Mapped[str] = mapped_column(Text(), nullable=False)
    customer_id: Mapped[str] = mapped_column(Text(), nullable=False)
    operation: Mapped[str] = mapped_column(Text(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text(), nullable=False)
    request_sha256: Mapped[str] = mapped_column(Text(), nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSONB(none_as_null=True), nullable=False)
    correlation_id: Mapped[str] = mapped_column(Text(), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    actor: Mapped[User] = relationship(
        "User",
        back_populates="idempotency_records",
        primaryjoin="IdempotencyRecord.actor_id == User.user_id",
        foreign_keys="[IdempotencyRecord.actor_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    customer: Mapped[Customer] = relationship(
        "Customer",
        back_populates="idempotency_records",
        primaryjoin="IdempotencyRecord.customer_id == Customer.customer_id",
        foreign_keys="[IdempotencyRecord.customer_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )


class AuditEvent(Base):
    """Approved Day 4 table audit_events; database constraints are applied by later migrations."""

    __tablename__ = "audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["actor_id"],
            ["business.users.user_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["customer_id"],
            ["business.customers.customer_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["quote_id"],
            ["business.quotes.quote_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["quote_id", "quote_version"],
            ["business.quote_versions.quote_id", "business.quote_versions.version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint("outcome IN ('success','denied','error')", name="rule_01"),
        CheckConstraint(
            "quote_version IS NULL OR (quote_id IS NOT NULL AND quote_version >= 1)", name="rule_02"
        ),
        CheckConstraint(
            "length(btrim(actor_id)) > 0 AND length(actor_id) <= 64", name="actor_id_identity"
        ),
        CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name="customer_id_identity",
        ),
        CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64", name="rfq_id_identity"
        ),
        CheckConstraint(
            "length(btrim(quote_id)) > 0 AND length(quote_id) <= 64", name="quote_id_identity"
        ),
        CheckConstraint("length(btrim(correlation_id)) > 0", name="correlation_id_nonblank"),
        CheckConstraint("jsonb_typeof(event_data) = 'object'", name="event_data_shape"),
        Index(
            "ix_audit_correlation_time",
            "correlation_id",
            "occurred_at",
            "event_id",
            unique=False,
            postgresql_using="btree",
        ),
        Index(
            "ix_audit_rfq_time",
            "rfq_id",
            "occurred_at",
            "event_id",
            unique=False,
            postgresql_using="btree",
            postgresql_where=text("rfq_id IS NOT NULL"),
        ),
        {"schema": "business"},
    )

    event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), default=uuid4, primary_key=True, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    actor_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    agent_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    customer_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    rfq_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    quote_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    quote_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    operation: Mapped[str] = mapped_column(Text(), nullable=False)
    outcome: Mapped[str] = mapped_column(Text(), nullable=False)
    correlation_id: Mapped[str] = mapped_column(Text(), nullable=False)
    event_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), server_default=text("'{}'::jsonb"), nullable=False
    )

    actor: Mapped[User | None] = relationship(
        "User",
        back_populates="audit_events",
        primaryjoin="AuditEvent.actor_id == User.user_id",
        foreign_keys="[AuditEvent.actor_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    customer: Mapped[Customer | None] = relationship(
        "Customer",
        back_populates="audit_events",
        primaryjoin="AuditEvent.customer_id == Customer.customer_id",
        foreign_keys="[AuditEvent.customer_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    rfq: Mapped[RFQ | None] = relationship(
        "RFQ",
        back_populates="audit_events",
        primaryjoin="AuditEvent.rfq_id == RFQ.rfq_id",
        foreign_keys="[AuditEvent.rfq_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    quote: Mapped[Quote | None] = relationship(
        "Quote",
        back_populates="audit_events",
        primaryjoin="AuditEvent.quote_id == Quote.quote_id",
        foreign_keys="[AuditEvent.quote_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    version_record: Mapped[QuoteVersion | None] = relationship(
        "QuoteVersion",
        back_populates="audit_events",
        primaryjoin="and_(AuditEvent.quote_id == QuoteVersion.quote_id, AuditEvent.quote_version == QuoteVersion.version)",
        foreign_keys="[AuditEvent.quote_id, AuditEvent.quote_version]",
        lazy="raise",
        uselist=False,
        viewonly=True,
    )


class WorkflowRun(Base):
    """Approved Day 4 table workflow_runs; database constraints are applied by later migrations."""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("correlation_id"),
        ForeignKeyConstraint(
            ["customer_id"],
            ["business.customers.customer_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["initiated_by"],
            ["business.users.user_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "status IN ('running','waiting_human','blocked','failed','completed')", name="rule_01"
        ),
        CheckConstraint("updated_at >= created_at", name="rule_02"),
        CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name="customer_id_identity",
        ),
        CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64", name="rfq_id_identity"
        ),
        CheckConstraint(
            "length(btrim(initiated_by)) > 0 AND length(initiated_by) <= 64",
            name="initiated_by_identity",
        ),
        CheckConstraint("length(btrim(correlation_id)) > 0", name="correlation_id_nonblank"),
        CheckConstraint(
            "length(btrim(external_workflow_id)) > 0", name="external_workflow_id_nonblank"
        ),
        CheckConstraint("length(btrim(graph_version)) > 0", name="graph_version_nonblank"),
        CheckConstraint("jsonb_typeof(rule_versions) = 'object'", name="rule_versions_shape"),
        CheckConstraint(
            "jsonb_typeof(source_snapshot_versions) = 'object'",
            name="source_snapshot_versions_shape",
        ),
        Index(
            "ix_workflow_runs_rfq_time",
            "rfq_id",
            "created_at",
            "workflow_run_id",
            unique=False,
            postgresql_using="btree",
            postgresql_where=text("rfq_id IS NOT NULL"),
        ),
        {"schema": "orchestration"},
    )

    workflow_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), default=uuid4, primary_key=True, nullable=False
    )
    customer_id: Mapped[str] = mapped_column(Text(), nullable=False)
    rfq_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    initiated_by: Mapped[str] = mapped_column(Text(), nullable=False)
    correlation_id: Mapped[str] = mapped_column(Text(), nullable=False)
    external_workflow_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(Text(), server_default=text("'running'"), nullable=False)
    graph_version: Mapped[str] = mapped_column(Text(), nullable=False)
    rule_versions: Mapped[dict[str, Any]] = mapped_column(JSONB(none_as_null=True), nullable=False)
    source_snapshot_versions: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    checkpoint_ref: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    customer: Mapped[Customer] = relationship(
        "Customer",
        back_populates="workflow_runs",
        primaryjoin="WorkflowRun.customer_id == Customer.customer_id",
        foreign_keys="[WorkflowRun.customer_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    rfq: Mapped[RFQ | None] = relationship(
        "RFQ",
        back_populates="workflow_runs",
        primaryjoin="WorkflowRun.rfq_id == RFQ.rfq_id",
        foreign_keys="[WorkflowRun.rfq_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    initiator: Mapped[User] = relationship(
        "User",
        back_populates="workflow_runs",
        primaryjoin="WorkflowRun.initiated_by == User.user_id",
        foreign_keys="[WorkflowRun.initiated_by]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
