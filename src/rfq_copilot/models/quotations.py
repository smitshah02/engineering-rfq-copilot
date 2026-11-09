"""Approved Day 4 quotations mappings; metadata definitions only."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rfq_copilot.models.base import Base

if TYPE_CHECKING:
    from rfq_copilot.models.approvals import ApprovalHistory, ApprovalRequest
    from rfq_copilot.models.identity import User
    from rfq_copilot.models.reliability import AuditEvent
    from rfq_copilot.models.rfqs import RFQ
    from rfq_copilot.models.rules import BusinessRuleVersion


class PricingCalculation(Base):
    """Approved Day 4 table pricing_calculations; database constraints are applied by later migrations."""

    __tablename__ = "pricing_calculations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
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
        ForeignKeyConstraint(
            ["pricing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["manufacturing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint("quote_total_usd >= 0 AND expires_at > created_at", name="rule_01"),
        CheckConstraint(
            "length(btrim(calculation_id)) > 0 AND length(calculation_id) <= 64",
            name="calculation_id_identity",
        ),
        CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64", name="rfq_id_identity"
        ),
        CheckConstraint(
            "length(btrim(created_by)) > 0 AND length(created_by) <= 64", name="created_by_identity"
        ),
        CheckConstraint("requirement_sha256 ~ '^[0-9a-f]{64}$'", name="requirement_sha256_hex"),
        CheckConstraint("jsonb_typeof(parts) = 'array'", name="parts_shape"),
        CheckConstraint("jsonb_typeof(lines) = 'array'", name="lines_shape"),
        CheckConstraint("quote_total_usd <> 'NaN'::numeric", name="quote_total_usd_finite"),
        CheckConstraint(
            "length(btrim(pricing_rule_version)) > 0", name="pricing_rule_version_nonblank"
        ),
        CheckConstraint(
            "length(btrim(manufacturing_rule_version)) > 0",
            name="manufacturing_rule_version_nonblank",
        ),
        CheckConstraint(
            "jsonb_typeof(source_snapshot_versions) = 'object'",
            name="source_snapshot_versions_shape",
        ),
        {"schema": "business"},
    )

    calculation_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    rfq_id: Mapped[str] = mapped_column(Text(), nullable=False)
    created_by: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requirement_sha256: Mapped[str] = mapped_column(Text(), nullable=False)
    parts: Mapped[list[Any]] = mapped_column(JSONB(none_as_null=True), nullable=False)
    lines: Mapped[list[Any]] = mapped_column(JSONB(none_as_null=True), nullable=False)
    quote_total_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2, asdecimal=True), nullable=False)
    pricing_rule_version: Mapped[str] = mapped_column(Text(), nullable=False)
    manufacturing_rule_version: Mapped[str] = mapped_column(Text(), nullable=False)
    source_snapshot_versions: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )

    rfq: Mapped[RFQ] = relationship(
        "RFQ",
        back_populates="pricing_calculations",
        primaryjoin="PricingCalculation.rfq_id == RFQ.rfq_id",
        foreign_keys="[PricingCalculation.rfq_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    creator: Mapped[User] = relationship(
        "User",
        back_populates="pricing_calculations",
        primaryjoin="PricingCalculation.created_by == User.user_id",
        foreign_keys="[PricingCalculation.created_by]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    pricing_rule: Mapped[BusinessRuleVersion] = relationship(
        "BusinessRuleVersion",
        back_populates="pricing_calculations_pricing",
        primaryjoin="PricingCalculation.pricing_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[PricingCalculation.pricing_rule_version]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    manufacturing_rule: Mapped[BusinessRuleVersion] = relationship(
        "BusinessRuleVersion",
        back_populates="pricing_calculations_manufacturing",
        primaryjoin="PricingCalculation.manufacturing_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[PricingCalculation.manufacturing_rule_version]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    quote_versions: Mapped[list[QuoteVersion]] = relationship(
        "QuoteVersion",
        back_populates="calculation",
        primaryjoin="QuoteVersion.calculation_id == PricingCalculation.calculation_id",
        foreign_keys="[QuoteVersion.calculation_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class Quote(Base):
    """Approved Day 4 table quotes; database constraints are applied by later migrations."""

    __tablename__ = "quotes"
    __table_args__ = (
        UniqueConstraint("rfq_id"),
        ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
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
        ForeignKeyConstraint(
            ["submitted_by"],
            ["business.users.user_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["approver_id"],
            ["business.users.user_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["released_by"],
            ["business.users.user_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["quote_id", "current_version"],
            ["business.quote_versions.quote_id", "business.quote_versions.version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["quote_id", "approved_version"],
            ["business.quote_versions.quote_id", "business.quote_versions.version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint("current_version >= 1 AND row_version >= 1", name="rule_01"),
        CheckConstraint(
            "approved_version IS NULL OR approved_version = current_version", name="rule_02"
        ),
        CheckConstraint(
            "approved_version IS NULL OR (approver_id IS NOT NULL AND approval_timestamp IS NOT NULL AND approver_id <> created_by)",
            name="rule_03",
        ),
        CheckConstraint(
            "status NOT IN ('approved','released') OR (approved_version IS NOT NULL AND approver_id IS NOT NULL AND approval_timestamp IS NOT NULL)",
            name="rule_04",
        ),
        CheckConstraint("(released_by IS NULL) = (released_at IS NULL)", name="rule_05"),
        CheckConstraint("status <> 'released' OR released_by IS NOT NULL", name="rule_06"),
        CheckConstraint(
            "status IN ('draft','revised_draft','pending_approval','approved','rejected','expired','clarification_required','blocked','released')",
            name="rule_07",
        ),
        CheckConstraint(
            "(approved_version IS NULL) = (approval_timestamp IS NULL)", name="rule_08"
        ),
        CheckConstraint(
            "length(btrim(quote_id)) > 0 AND length(quote_id) <= 64", name="quote_id_identity"
        ),
        CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64", name="rfq_id_identity"
        ),
        CheckConstraint(
            "length(btrim(created_by)) > 0 AND length(created_by) <= 64", name="created_by_identity"
        ),
        CheckConstraint(
            "length(btrim(submitted_by)) > 0 AND length(submitted_by) <= 64",
            name="submitted_by_identity",
        ),
        CheckConstraint(
            "length(btrim(approver_id)) > 0 AND length(approver_id) <= 64",
            name="approver_id_identity",
        ),
        CheckConstraint(
            "length(btrim(released_by)) > 0 AND length(released_by) <= 64",
            name="released_by_identity",
        ),
        {"schema": "business"},
    )

    quote_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    rfq_id: Mapped[str] = mapped_column(Text(), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    status: Mapped[str] = mapped_column(Text(), nullable=False)
    created_by: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_by: Mapped[str | None] = mapped_column(Text(), nullable=True)
    approved_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    approver_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    approval_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    legacy_idempotency_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    row_version: Mapped[int] = mapped_column(BigInteger(), server_default=text("1"), nullable=False)
    released_by: Mapped[str | None] = mapped_column(Text(), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    rfq: Mapped[RFQ] = relationship(
        "RFQ",
        back_populates="quote",
        primaryjoin="Quote.rfq_id == RFQ.rfq_id",
        foreign_keys="[Quote.rfq_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    creator: Mapped[User] = relationship(
        "User",
        back_populates="created_quotes",
        primaryjoin="Quote.created_by == User.user_id",
        foreign_keys="[Quote.created_by]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    submitter: Mapped[User | None] = relationship(
        "User",
        back_populates="submitted_quotes",
        primaryjoin="Quote.submitted_by == User.user_id",
        foreign_keys="[Quote.submitted_by]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    approver: Mapped[User | None] = relationship(
        "User",
        back_populates="approved_quotes",
        primaryjoin="Quote.approver_id == User.user_id",
        foreign_keys="[Quote.approver_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    releaser: Mapped[User | None] = relationship(
        "User",
        back_populates="released_quotes",
        primaryjoin="Quote.released_by == User.user_id",
        foreign_keys="[Quote.released_by]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    current_version_record: Mapped[QuoteVersion] = relationship(
        "QuoteVersion",
        back_populates="current_for_quote",
        primaryjoin="and_(Quote.quote_id == QuoteVersion.quote_id, Quote.current_version == QuoteVersion.version)",
        foreign_keys="[Quote.quote_id, Quote.current_version]",
        lazy="raise",
        uselist=False,
        viewonly=True,
    )
    approved_version_record: Mapped[QuoteVersion | None] = relationship(
        "QuoteVersion",
        back_populates="approved_for_quote",
        primaryjoin="and_(Quote.quote_id == QuoteVersion.quote_id, Quote.approved_version == QuoteVersion.version)",
        foreign_keys="[Quote.quote_id, Quote.approved_version]",
        lazy="raise",
        uselist=False,
        viewonly=True,
    )
    versions: Mapped[list[QuoteVersion]] = relationship(
        "QuoteVersion",
        back_populates="quote",
        primaryjoin="QuoteVersion.quote_id == Quote.quote_id",
        foreign_keys="[QuoteVersion.quote_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        "AuditEvent",
        back_populates="quote",
        primaryjoin="AuditEvent.quote_id == Quote.quote_id",
        foreign_keys="[AuditEvent.quote_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class QuoteVersion(Base):
    """Approved Day 4 table quote_versions; database constraints are applied by later migrations."""

    __tablename__ = "quote_versions"
    __table_args__ = (
        UniqueConstraint("quote_id", "version"),
        ForeignKeyConstraint(
            ["quote_id"],
            ["business.quotes.quote_id"],
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
        ForeignKeyConstraint(
            ["pricing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["manufacturing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["approval_rule_version"],
            ["business.business_rule_versions.rule_version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["workflow_rule_version"],
            ["business.business_rule_versions.rule_version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["calculation_id"],
            ["business.pricing_calculations.calculation_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint("version >= 1", name="rule_01"),
        CheckConstraint("currency = 'USD'", name="rule_02"),
        CheckConstraint("total_usd >= 0 AND estimated_lead_business_days >= 0", name="rule_03"),
        CheckConstraint(
            "snapshot_completeness IN ('metadata_only','priced_partial','complete')", name="rule_04"
        ),
        CheckConstraint(
            "snapshot_completeness <> 'metadata_only' OR total_usd IS NULL", name="rule_05"
        ),
        CheckConstraint(
            "snapshot_completeness = 'metadata_only' OR (total_usd IS NOT NULL AND pricing_rule_version IS NOT NULL)",
            name="rule_06",
        ),
        CheckConstraint(
            "snapshot_completeness <> 'complete' OR (manufacturing_rule_version IS NOT NULL AND approval_rule_version IS NOT NULL AND workflow_rule_version IS NOT NULL AND source_snapshot_versions IS NOT NULL AND calculation_id IS NOT NULL AND expiry_date IS NOT NULL AND estimated_lead_business_days IS NOT NULL)",
            name="rule_07",
        ),
        CheckConstraint("NOT approval_valid OR is_current", name="rule_08"),
        CheckConstraint(
            "length(btrim(quote_version_id)) > 0 AND length(quote_version_id) <= 64",
            name="quote_version_id_identity",
        ),
        CheckConstraint(
            "length(btrim(quote_id)) > 0 AND length(quote_id) <= 64", name="quote_id_identity"
        ),
        CheckConstraint(
            "length(btrim(created_by)) > 0 AND length(created_by) <= 64", name="created_by_identity"
        ),
        CheckConstraint("total_usd <> 'NaN'::numeric", name="total_usd_finite"),
        CheckConstraint(
            "length(btrim(pricing_rule_version)) > 0", name="pricing_rule_version_nonblank"
        ),
        CheckConstraint(
            "length(btrim(manufacturing_rule_version)) > 0",
            name="manufacturing_rule_version_nonblank",
        ),
        CheckConstraint(
            "length(btrim(approval_rule_version)) > 0", name="approval_rule_version_nonblank"
        ),
        CheckConstraint(
            "length(btrim(workflow_rule_version)) > 0", name="workflow_rule_version_nonblank"
        ),
        CheckConstraint(
            "jsonb_typeof(source_snapshot_versions) = 'object'",
            name="source_snapshot_versions_shape",
        ),
        CheckConstraint(
            "length(btrim(calculation_id)) > 0 AND length(calculation_id) <= 64",
            name="calculation_id_identity",
        ),
        Index(
            "uq_quote_versions_current",
            "quote_id",
            unique=True,
            postgresql_using="btree",
            postgresql_where=text("is_current"),
        ),
        {"schema": "business"},
    )

    quote_version_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    quote_id: Mapped[str] = mapped_column(Text(), nullable=False)
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    created_by: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    approval_valid: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    currency: Mapped[str] = mapped_column(Text(), server_default=text("'USD'"), nullable=False)
    total_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2, asdecimal=True), nullable=True)
    pricing_rule_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    manufacturing_rule_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    approval_rule_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    workflow_rule_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    estimated_lead_business_days: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    source_snapshot_versions: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    calculation_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    snapshot_completeness: Mapped[str] = mapped_column(Text(), nullable=False)

    current_for_quote: Mapped[Quote | None] = relationship(
        "Quote",
        back_populates="current_version_record",
        primaryjoin="and_(Quote.quote_id == QuoteVersion.quote_id, Quote.current_version == QuoteVersion.version)",
        foreign_keys="[Quote.quote_id, Quote.current_version]",
        lazy="raise",
        uselist=False,
        viewonly=True,
    )
    approved_for_quote: Mapped[Quote | None] = relationship(
        "Quote",
        back_populates="approved_version_record",
        primaryjoin="and_(Quote.quote_id == QuoteVersion.quote_id, Quote.approved_version == QuoteVersion.version)",
        foreign_keys="[Quote.quote_id, Quote.approved_version]",
        lazy="raise",
        uselist=False,
        viewonly=True,
    )
    quote: Mapped[Quote] = relationship(
        "Quote",
        back_populates="versions",
        primaryjoin="QuoteVersion.quote_id == Quote.quote_id",
        foreign_keys="[QuoteVersion.quote_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    creator: Mapped[User] = relationship(
        "User",
        back_populates="created_quote_versions",
        primaryjoin="QuoteVersion.created_by == User.user_id",
        foreign_keys="[QuoteVersion.created_by]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    pricing_rule: Mapped[BusinessRuleVersion | None] = relationship(
        "BusinessRuleVersion",
        back_populates="quote_versions_pricing",
        primaryjoin="QuoteVersion.pricing_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[QuoteVersion.pricing_rule_version]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    manufacturing_rule: Mapped[BusinessRuleVersion | None] = relationship(
        "BusinessRuleVersion",
        back_populates="quote_versions_manufacturing",
        primaryjoin="QuoteVersion.manufacturing_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[QuoteVersion.manufacturing_rule_version]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    approval_rule: Mapped[BusinessRuleVersion | None] = relationship(
        "BusinessRuleVersion",
        back_populates="quote_versions_approval",
        primaryjoin="QuoteVersion.approval_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[QuoteVersion.approval_rule_version]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    workflow_rule: Mapped[BusinessRuleVersion | None] = relationship(
        "BusinessRuleVersion",
        back_populates="quote_versions_workflow",
        primaryjoin="QuoteVersion.workflow_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[QuoteVersion.workflow_rule_version]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    calculation: Mapped[PricingCalculation | None] = relationship(
        "PricingCalculation",
        back_populates="quote_versions",
        primaryjoin="QuoteVersion.calculation_id == PricingCalculation.calculation_id",
        foreign_keys="[QuoteVersion.calculation_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    line_items: Mapped[list[QuoteLineItem]] = relationship(
        "QuoteLineItem",
        back_populates="version_record",
        primaryjoin="and_(QuoteLineItem.quote_id == QuoteVersion.quote_id, QuoteLineItem.quote_version == QuoteVersion.version)",
        foreign_keys="[QuoteLineItem.quote_id, QuoteLineItem.quote_version]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    approval_requests: Mapped[list[ApprovalRequest]] = relationship(
        "ApprovalRequest",
        back_populates="version_record",
        primaryjoin="and_(ApprovalRequest.quote_id == QuoteVersion.quote_id, ApprovalRequest.quote_version == QuoteVersion.version)",
        foreign_keys="[ApprovalRequest.quote_id, ApprovalRequest.quote_version]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    approval_events: Mapped[list[ApprovalHistory]] = relationship(
        "ApprovalHistory",
        back_populates="version_record",
        primaryjoin="and_(ApprovalHistory.quote_id == QuoteVersion.quote_id, ApprovalHistory.quote_version == QuoteVersion.version)",
        foreign_keys="[ApprovalHistory.quote_id, ApprovalHistory.quote_version]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        "AuditEvent",
        back_populates="version_record",
        primaryjoin="and_(AuditEvent.quote_id == QuoteVersion.quote_id, AuditEvent.quote_version == QuoteVersion.version)",
        foreign_keys="[AuditEvent.quote_id, AuditEvent.quote_version]",
        lazy="raise",
        uselist=True,
        viewonly=True,
    )


class QuoteLineItem(Base):
    """Approved Day 4 table quote_line_items; database constraints are applied by later migrations."""

    __tablename__ = "quote_line_items"
    __table_args__ = (
        UniqueConstraint("quote_id", "quote_version", "line_number"),
        ForeignKeyConstraint(
            ["quote_id", "quote_version"],
            ["business.quote_versions.quote_id", "business.quote_versions.version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "quote_version >= 1 AND line_number > 0 AND quantity > 0 AND line_total_usd >= 0",
            name="rule_01",
        ),
        CheckConstraint(
            "jsonb_typeof(part_json) = 'object' AND jsonb_typeof(cost_breakdown_json) = 'object'",
            name="rule_02",
        ),
        CheckConstraint(
            "length(btrim(quote_line_id)) > 0 AND length(quote_line_id) <= 64",
            name="quote_line_id_identity",
        ),
        CheckConstraint(
            "length(btrim(quote_id)) > 0 AND length(quote_id) <= 64", name="quote_id_identity"
        ),
        CheckConstraint(
            "length(btrim(part_id)) > 0 AND length(part_id) <= 64", name="part_id_identity"
        ),
        CheckConstraint("line_total_usd <> 'NaN'::numeric", name="line_total_usd_finite"),
        {"schema": "business"},
    )

    quote_line_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    quote_id: Mapped[str] = mapped_column(Text(), nullable=False)
    quote_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    part_id: Mapped[str] = mapped_column(Text(), nullable=False)
    part_json: Mapped[dict[str, Any]] = mapped_column(JSONB(none_as_null=True), nullable=False)
    cost_breakdown_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer(), nullable=False)
    line_total_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2, asdecimal=True), nullable=False)

    version_record: Mapped[QuoteVersion] = relationship(
        "QuoteVersion",
        back_populates="line_items",
        primaryjoin="and_(QuoteLineItem.quote_id == QuoteVersion.quote_id, QuoteLineItem.quote_version == QuoteVersion.version)",
        foreign_keys="[QuoteLineItem.quote_id, QuoteLineItem.quote_version]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
