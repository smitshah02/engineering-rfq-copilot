"""Approved schema baseline quotations mappings; metadata definitions only."""

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
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import UserDefinedType

from rfq_copilot.models.base import Base

if TYPE_CHECKING:
    from rfq_copilot.models.approvals import ApprovalHistory, ApprovalRequest
    from rfq_copilot.models.identity import User
    from rfq_copilot.models.reliability import AuditEvent, WorkflowRun
    from rfq_copilot.models.rfqs import RFQ
    from rfq_copilot.models.rules import BusinessRuleVersion


class Xid8(UserDefinedType):
    """PostgreSQL ``xid8`` (64-bit transaction id); asyncpg decodes it as ``int``."""

    cache_ok = True

    def get_col_spec(self, **kw) -> str:
        return "XID8"


# Reflection (Alembic compare, schema preflight) then knows the type instead of NullType.
ischema_names.setdefault("xid8", Xid8)


class PricingCalculation(Base):
    """Approved schema baseline table pricing_calculations, re-keyed by K3 (20261116_01).

    One persisted calculation record per (RFQ, RFQ source digest, calculation content):
    ``calculation_record_id`` is the record's identity, ``calculation_id`` the unchanged
    content-derived fingerprint (parts + pricing version; not unique: the same parts priced
    for another RFQ get another record), ``content_sha256`` the reusable pricing-content
    fingerprint. Calculations do not expire; quotation versions do."""

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
        UniqueConstraint(
            "rfq_id",
            "requirement_sha256",
            "content_sha256",
            name="uq_pricing_calculations_reuse_key",
        ),
        UniqueConstraint("calculation_record_id", "rfq_id"),
        CheckConstraint("quote_total_usd >= 0", name="rule_01"),
        CheckConstraint(
            "length(btrim(calculation_id)) > 0 AND length(calculation_id) <= 64",
            name="calculation_id_identity",
        ),
        CheckConstraint(
            "length(btrim(calculation_record_id)) > 0 AND length(calculation_record_id) <= 64",
            name="calculation_record_id_identity",
        ),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256_hex"),
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

    calculation_id: Mapped[str] = mapped_column(Text(), nullable=False)
    rfq_id: Mapped[str] = mapped_column(Text(), nullable=False)
    created_by: Mapped[str] = mapped_column(Text(), nullable=False)
    # K3: must equal transaction_timestamp() (k3_pricing_calculations_seal).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    # K3: the RFQ source digest (canonical runtime lines with their requirement revisions).
    requirement_sha256: Mapped[str] = mapped_column(Text(), nullable=False)
    parts: Mapped[list[Any]] = mapped_column(JSONB(none_as_null=True), nullable=False)
    lines: Mapped[list[Any]] = mapped_column(JSONB(none_as_null=True), nullable=False)
    quote_total_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2, asdecimal=True), nullable=False)
    pricing_rule_version: Mapped[str] = mapped_column(Text(), nullable=False)
    manufacturing_rule_version: Mapped[str] = mapped_column(Text(), nullable=False)
    # Snapshot pins of the evaluation that first recorded the content (provenance only: the
    # version-specific pins live on each quote version, never in the content identity).
    source_snapshot_versions: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    calculation_record_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text(), nullable=False)

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
        primaryjoin="and_(QuoteVersion.calculation_record_id == PricingCalculation.calculation_record_id, QuoteVersion.rfq_id == PricingCalculation.rfq_id)",
        foreign_keys="[QuoteVersion.calculation_record_id, QuoteVersion.rfq_id]",
        lazy="raise",
        uselist=True,
        viewonly=True,
    )


class Quote(Base):
    """Approved schema baseline table quotes; database constraints are applied by later migrations."""

    __tablename__ = "quotes"
    __table_args__ = (
        UniqueConstraint("rfq_id"),
        UniqueConstraint("quote_id", "rfq_id"),  # K3: target of the version's RFQ binding
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
    evidenced_versions: Mapped[list[QuoteVersion]] = relationship(
        "QuoteVersion",
        back_populates="evidence_quote",
        primaryjoin="and_(QuoteVersion.quote_id == Quote.quote_id, QuoteVersion.rfq_id == Quote.rfq_id)",
        foreign_keys="[QuoteVersion.quote_id, QuoteVersion.rfq_id]",
        lazy="raise",
        uselist=True,
        viewonly=True,
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
    # Checkpoint A4 (20261118_01): the workflow run that created this internal draft.
    workflow_runs: Mapped[list[WorkflowRun]] = relationship(
        "WorkflowRun",
        back_populates="quote",
        primaryjoin="WorkflowRun.quote_id == Quote.quote_id",
        foreign_keys="[WorkflowRun.quote_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class QuoteVersion(Base):
    """Approved schema baseline table quote_versions; database constraints are applied by later migrations."""

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
        # K3: the version's RFQ is its quotation's RFQ, and its calculation record belongs to
        # that RFQ (no cross-RFQ or cross-customer record). rule_07/rule_09 make the evidence
        # columns all-or-nothing, so MATCH SIMPLE (the repository convention) suffices.
        ForeignKeyConstraint(
            ["quote_id", "rfq_id"],
            ["business.quotes.quote_id", "business.quotes.rfq_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["calculation_record_id", "rfq_id"],
            [
                "business.pricing_calculations.calculation_record_id",
                "business.pricing_calculations.rfq_id",
            ],
            name="fk_quote_versions_calculation_record_rfq",  # the convention exceeds 63 chars
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
            "snapshot_completeness <> 'complete' OR (manufacturing_rule_version IS NOT NULL AND approval_rule_version IS NOT NULL AND workflow_rule_version IS NOT NULL AND source_snapshot_versions IS NOT NULL AND calculation_id IS NOT NULL AND expiry_date IS NOT NULL AND estimated_lead_business_days IS NOT NULL AND rfq_id IS NOT NULL AND calculation_record_id IS NOT NULL AND rfq_source_sha256 IS NOT NULL AND evaluation_fingerprint IS NOT NULL AND rules_sha256 IS NOT NULL AND horizon_start IS NOT NULL)",
            name="rule_07",
        ),
        CheckConstraint(
            "snapshot_completeness = 'complete' OR (rfq_id IS NULL AND calculation_record_id IS NULL AND rfq_source_sha256 IS NULL AND evaluation_fingerprint IS NULL AND rules_sha256 IS NULL AND horizon_start IS NULL AND requested_delivery_date IS NULL)",
            name="rule_09",
        ),
        CheckConstraint(
            "snapshot_completeness <> 'complete' OR expiry_date >= (created_at AT TIME ZONE 'UTC')::date",
            name="rule_10",
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
        CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64", name="rfq_id_identity"
        ),
        CheckConstraint(
            "length(btrim(calculation_record_id)) > 0 AND length(calculation_record_id) <= 64",
            name="calculation_record_id_identity",
        ),
        CheckConstraint("rfq_source_sha256 ~ '^[0-9a-f]{64}$'", name="rfq_source_sha256_hex"),
        CheckConstraint(
            "evaluation_fingerprint ~ '^[0-9a-f]{64}$'", name="evaluation_fingerprint_hex"
        ),
        CheckConstraint("rules_sha256 ~ '^[0-9a-f]{64}$'", name="rules_sha256_hex"),
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
    # K2 (20261115_01): stamped by the database trigger with the creating top-level
    # transaction; never set by the application (a supplied value is overwritten). NULL means
    # a version created before K2: its transaction is unknown, so it is legacy and sealed.
    created_xact: Mapped[int | None] = mapped_column(Xid8(), nullable=True)
    # K3 (20261116_01) version evidence: NULL unless snapshot_completeness = 'complete'
    # (rule_07/rule_09), written with the version and immutable afterwards (K2). Consistency
    # evidence only; nothing here authenticates the RFQ source or the evaluation context.
    rfq_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    calculation_record_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    rfq_source_sha256: Mapped[str | None] = mapped_column(Text(), nullable=True)
    evaluation_fingerprint: Mapped[str | None] = mapped_column(Text(), nullable=True)
    rules_sha256: Mapped[str | None] = mapped_column(Text(), nullable=True)
    horizon_start: Mapped[date | None] = mapped_column(Date(), nullable=True)
    requested_delivery_date: Mapped[date | None] = mapped_column(Date(), nullable=True)

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
    evidence_quote: Mapped[Quote | None] = relationship(
        "Quote",
        back_populates="evidenced_versions",
        primaryjoin="and_(QuoteVersion.quote_id == Quote.quote_id, QuoteVersion.rfq_id == Quote.rfq_id)",
        foreign_keys="[QuoteVersion.quote_id, QuoteVersion.rfq_id]",
        lazy="raise",
        uselist=False,
        viewonly=True,
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
        primaryjoin="and_(QuoteVersion.calculation_record_id == PricingCalculation.calculation_record_id, QuoteVersion.rfq_id == PricingCalculation.rfq_id)",
        foreign_keys="[QuoteVersion.calculation_record_id, QuoteVersion.rfq_id]",
        lazy="raise",
        uselist=False,
        viewonly=True,
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
    """Approved schema baseline table quote_line_items; database constraints are applied by later migrations."""

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
