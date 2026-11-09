"""Approved Day 4 rules mappings; metadata definitions only."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, Date, DateTime, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rfq_copilot.models.base import Base

if TYPE_CHECKING:
    from rfq_copilot.models.approvals import ApprovalRequest
    from rfq_copilot.models.manufacturing import Finish, Material
    from rfq_copilot.models.quotations import PricingCalculation, QuoteVersion
    from rfq_copilot.models.supply import MaterialInventory, ProductionCapacity


class BusinessRuleVersion(Base):
    """Approved Day 4 table business_rule_versions; database constraints are applied by later migrations."""

    __tablename__ = "business_rule_versions"
    __table_args__ = (
        UniqueConstraint("rule_kind", "sha256"),
        CheckConstraint(
            "rule_kind IN ('manufacturing','pricing','approval','workflow','entity_contract','tool_contract','business_constraints','coordination','handoff')",
            name="rule_01",
        ),
        CheckConstraint("length(btrim(rule_version)) > 0", name="rule_version_nonblank"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint("jsonb_typeof(document) = 'object'", name="document_shape"),
        {"schema": "business"},
    )

    rule_version: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    rule_kind: Mapped[str] = mapped_column(Text(), nullable=False)
    sha256: Mapped[str] = mapped_column(Text(), nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB(none_as_null=True), nullable=False)
    source_path: Mapped[str] = mapped_column(Text(), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    materials: Mapped[list[Material]] = relationship(
        "Material",
        back_populates="pricing_rule",
        primaryjoin="Material.pricing_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[Material.pricing_rule_version]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    finishes: Mapped[list[Finish]] = relationship(
        "Finish",
        back_populates="pricing_rule",
        primaryjoin="Finish.pricing_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[Finish.pricing_rule_version]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    pricing_calculations_pricing: Mapped[list[PricingCalculation]] = relationship(
        "PricingCalculation",
        back_populates="pricing_rule",
        primaryjoin="PricingCalculation.pricing_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[PricingCalculation.pricing_rule_version]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    pricing_calculations_manufacturing: Mapped[list[PricingCalculation]] = relationship(
        "PricingCalculation",
        back_populates="manufacturing_rule",
        primaryjoin="PricingCalculation.manufacturing_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[PricingCalculation.manufacturing_rule_version]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    quote_versions_pricing: Mapped[list[QuoteVersion]] = relationship(
        "QuoteVersion",
        back_populates="pricing_rule",
        primaryjoin="QuoteVersion.pricing_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[QuoteVersion.pricing_rule_version]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    quote_versions_manufacturing: Mapped[list[QuoteVersion]] = relationship(
        "QuoteVersion",
        back_populates="manufacturing_rule",
        primaryjoin="QuoteVersion.manufacturing_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[QuoteVersion.manufacturing_rule_version]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    quote_versions_approval: Mapped[list[QuoteVersion]] = relationship(
        "QuoteVersion",
        back_populates="approval_rule",
        primaryjoin="QuoteVersion.approval_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[QuoteVersion.approval_rule_version]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    quote_versions_workflow: Mapped[list[QuoteVersion]] = relationship(
        "QuoteVersion",
        back_populates="workflow_rule",
        primaryjoin="QuoteVersion.workflow_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[QuoteVersion.workflow_rule_version]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    approval_requests: Mapped[list[ApprovalRequest]] = relationship(
        "ApprovalRequest",
        back_populates="approval_rule",
        primaryjoin="ApprovalRequest.approval_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[ApprovalRequest.approval_rule_version]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class DataSnapshot(Base):
    """Approved Day 4 table data_snapshots; database constraints are applied by later migrations."""

    __tablename__ = "data_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_kind", "sha256"),
        CheckConstraint("snapshot_kind IN ('inventory','capacity')", name="rule_01"),
        CheckConstraint(
            "length(btrim(snapshot_id)) > 0 AND length(snapshot_id) <= 64",
            name="snapshot_id_identity",
        ),
        CheckConstraint("length(btrim(dataset_version)) > 0", name="dataset_version_nonblank"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        {"schema": "business"},
    )

    snapshot_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    snapshot_kind: Mapped[str] = mapped_column(Text(), nullable=False)
    dataset_version: Mapped[str] = mapped_column(Text(), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date(), nullable=False)
    sha256: Mapped[str] = mapped_column(Text(), nullable=False)
    artifact_ref: Mapped[str] = mapped_column(Text(), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    inventory_records: Mapped[list[MaterialInventory]] = relationship(
        "MaterialInventory",
        back_populates="snapshot",
        primaryjoin="MaterialInventory.snapshot_id == DataSnapshot.snapshot_id",
        foreign_keys="[MaterialInventory.snapshot_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    capacity_records: Mapped[list[ProductionCapacity]] = relationship(
        "ProductionCapacity",
        back_populates="snapshot",
        primaryjoin="ProductionCapacity.snapshot_id == DataSnapshot.snapshot_id",
        foreign_keys="[ProductionCapacity.snapshot_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
