"""Approved Day 4 manufacturing mappings; metadata definitions only."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rfq_copilot.models.base import Base

if TYPE_CHECKING:
    from rfq_copilot.models.rfqs import RFQLineItem
    from rfq_copilot.models.rules import BusinessRuleVersion
    from rfq_copilot.models.supply import MaterialInventory, ProcurementRecord, ProductionCapacity


class Material(Base):
    """Approved Day 4 table materials; database constraints are applied by later migrations."""

    __tablename__ = "materials"
    __table_args__ = (
        ForeignKeyConstraint(
            ["pricing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "density_kg_m3 > 0 AND cost_usd_per_kg >= 0 AND min_bend_radius_factor > 0",
            name="rule_01",
        ),
        CheckConstraint("availability_class IN ('stocked','procure')", name="rule_02"),
        CheckConstraint(
            "category IN ('aluminum','mild_steel','stainless_steel','galvanized_steel','copper','brass')",
            name="rule_03",
        ),
        CheckConstraint(
            "length(btrim(material_id)) > 0 AND length(material_id) <= 64",
            name="material_id_identity",
        ),
        CheckConstraint("density_kg_m3 <> 'NaN'::numeric", name="density_kg_m3_finite"),
        CheckConstraint("cost_usd_per_kg <> 'NaN'::numeric", name="cost_usd_per_kg_finite"),
        CheckConstraint(
            "min_bend_radius_factor <> 'NaN'::numeric", name="min_bend_radius_factor_finite"
        ),
        CheckConstraint(
            "length(btrim(pricing_rule_version)) > 0", name="pricing_rule_version_nonblank"
        ),
        {"schema": "business"},
    )

    material_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    category: Mapped[str] = mapped_column(Text(), nullable=False)
    grade: Mapped[str] = mapped_column(Text(), nullable=False)
    density_kg_m3: Mapped[Decimal] = mapped_column(Numeric(18, 6, asdecimal=True), nullable=False)
    cost_usd_per_kg: Mapped[Decimal] = mapped_column(Numeric(18, 4, asdecimal=True), nullable=False)
    min_bend_radius_factor: Mapped[Decimal] = mapped_column(
        Numeric(12, 3, asdecimal=True), nullable=False
    )
    availability_class: Mapped[str] = mapped_column(Text(), nullable=False)
    pricing_rule_version: Mapped[str] = mapped_column(Text(), nullable=False)

    pricing_rule: Mapped[BusinessRuleVersion] = relationship(
        "BusinessRuleVersion",
        back_populates="materials",
        primaryjoin="Material.pricing_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[Material.pricing_rule_version]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    thickness_options: Mapped[list[MaterialThicknessOption]] = relationship(
        "MaterialThicknessOption",
        back_populates="material",
        primaryjoin="MaterialThicknessOption.material_id == Material.material_id",
        foreign_keys="[MaterialThicknessOption.material_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    finish_compatibilities: Mapped[list[MaterialFinishCompatibility]] = relationship(
        "MaterialFinishCompatibility",
        back_populates="material",
        primaryjoin="MaterialFinishCompatibility.material_id == Material.material_id",
        foreign_keys="[MaterialFinishCompatibility.material_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    supported_operations: Mapped[list[MaterialSupportedOperation]] = relationship(
        "MaterialSupportedOperation",
        back_populates="material",
        primaryjoin="MaterialSupportedOperation.material_id == Material.material_id",
        foreign_keys="[MaterialSupportedOperation.material_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    rfq_lines: Mapped[list[RFQLineItem]] = relationship(
        "RFQLineItem",
        back_populates="material",
        primaryjoin="RFQLineItem.material_id == Material.material_id",
        foreign_keys="[RFQLineItem.material_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class Finish(Base):
    """Approved Day 4 table finishes; database constraints are applied by later migrations."""

    __tablename__ = "finishes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["pricing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "length(btrim(finish_id)) > 0 AND length(finish_id) <= 64", name="finish_id_identity"
        ),
        CheckConstraint(
            "length(btrim(pricing_rule_version)) > 0", name="pricing_rule_version_nonblank"
        ),
        {"schema": "business"},
    )

    finish_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text(), nullable=False)
    pricing_rule_version: Mapped[str] = mapped_column(Text(), nullable=False)

    pricing_rule: Mapped[BusinessRuleVersion] = relationship(
        "BusinessRuleVersion",
        back_populates="finishes",
        primaryjoin="Finish.pricing_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[Finish.pricing_rule_version]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    material_compatibilities: Mapped[list[MaterialFinishCompatibility]] = relationship(
        "MaterialFinishCompatibility",
        back_populates="finish",
        primaryjoin="MaterialFinishCompatibility.finish_id == Finish.finish_id",
        foreign_keys="[MaterialFinishCompatibility.finish_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    material_categories: Mapped[list[FinishMaterialCategory]] = relationship(
        "FinishMaterialCategory",
        back_populates="finish",
        primaryjoin="FinishMaterialCategory.finish_id == Finish.finish_id",
        foreign_keys="[FinishMaterialCategory.finish_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    rfq_lines: Mapped[list[RFQLineItem]] = relationship(
        "RFQLineItem",
        back_populates="finish",
        primaryjoin="RFQLineItem.finish_id == Finish.finish_id",
        foreign_keys="[RFQLineItem.finish_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class Operation(Base):
    """Approved Day 4 table operations; database constraints are applied by later migrations."""

    __tablename__ = "operations"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(operation_id)) > 0 AND length(operation_id) <= 64",
            name="operation_id_identity",
        ),
        {"schema": "business"},
    )

    operation_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text(), nullable=False)
    is_quoted: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    notes: Mapped[str] = mapped_column(Text(), nullable=False)

    workcentres: Mapped[list[Workcentre]] = relationship(
        "Workcentre",
        back_populates="operation",
        primaryjoin="Workcentre.operation_id == Operation.operation_id",
        foreign_keys="[Workcentre.operation_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    material_support: Mapped[list[MaterialSupportedOperation]] = relationship(
        "MaterialSupportedOperation",
        back_populates="operation",
        primaryjoin="MaterialSupportedOperation.operation_id == Operation.operation_id",
        foreign_keys="[MaterialSupportedOperation.operation_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class Workcentre(Base):
    """Approved Day 4 table workcentres; database constraints are applied by later migrations."""

    __tablename__ = "workcentres"
    __table_args__ = (
        UniqueConstraint("workcentre_id", "operation_id"),
        ForeignKeyConstraint(
            ["operation_id"],
            ["business.operations.operation_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "weekly_nominal_hours >= 0 AND downtime_hours_per_week >= 0 AND downtime_hours_per_week <= weekly_nominal_hours",
            name="rule_01",
        ),
        CheckConstraint("planning_horizon_weeks > 0", name="rule_02"),
        CheckConstraint(
            "length(btrim(workcentre_id)) > 0 AND length(workcentre_id) <= 64",
            name="workcentre_id_identity",
        ),
        CheckConstraint(
            "length(btrim(operation_id)) > 0 AND length(operation_id) <= 64",
            name="operation_id_identity",
        ),
        CheckConstraint(
            "weekly_nominal_hours <> 'NaN'::numeric", name="weekly_nominal_hours_finite"
        ),
        CheckConstraint(
            "downtime_hours_per_week <> 'NaN'::numeric", name="downtime_hours_per_week_finite"
        ),
        {"schema": "business"},
    )

    workcentre_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    operation_id: Mapped[str] = mapped_column(Text(), nullable=False)
    shift_calendar: Mapped[str] = mapped_column(Text(), nullable=False)
    weekly_nominal_hours: Mapped[Decimal] = mapped_column(
        Numeric(12, 4, asdecimal=True), nullable=False
    )
    downtime_hours_per_week: Mapped[Decimal] = mapped_column(
        Numeric(12, 4, asdecimal=True), nullable=False
    )
    planning_horizon_weeks: Mapped[int] = mapped_column(Integer(), nullable=False)

    operation: Mapped[Operation] = relationship(
        "Operation",
        back_populates="workcentres",
        primaryjoin="Workcentre.operation_id == Operation.operation_id",
        foreign_keys="[Workcentre.operation_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    routes: Mapped[list[OperationRouting]] = relationship(
        "OperationRouting",
        back_populates="workcentre",
        primaryjoin="and_(OperationRouting.workcentre_id == Workcentre.workcentre_id, OperationRouting.operation_id == Workcentre.operation_id)",
        foreign_keys="[OperationRouting.workcentre_id, OperationRouting.operation_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    capacity_records: Mapped[list[ProductionCapacity]] = relationship(
        "ProductionCapacity",
        back_populates="workcentre",
        primaryjoin="ProductionCapacity.workcentre_id == Workcentre.workcentre_id",
        foreign_keys="[ProductionCapacity.workcentre_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class Supplier(Base):
    """Approved Day 4 table suppliers; database constraints are applied by later migrations."""

    __tablename__ = "suppliers"
    __table_args__ = (
        CheckConstraint("normal_lead_business_days >= 0", name="rule_01"),
        CheckConstraint(
            "length(btrim(supplier_id)) > 0 AND length(supplier_id) <= 64",
            name="supplier_id_identity",
        ),
        {"schema": "business"},
    )

    supplier_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    company_name: Mapped[str] = mapped_column(Text(), nullable=False)
    email: Mapped[str] = mapped_column(Text(), nullable=False)
    normal_lead_business_days: Mapped[int] = mapped_column(Integer(), nullable=False)

    inventory_records: Mapped[list[MaterialInventory]] = relationship(
        "MaterialInventory",
        back_populates="supplier",
        primaryjoin="MaterialInventory.supplier_id == Supplier.supplier_id",
        foreign_keys="[MaterialInventory.supplier_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    procurements: Mapped[list[ProcurementRecord]] = relationship(
        "ProcurementRecord",
        back_populates="supplier",
        primaryjoin="ProcurementRecord.supplier_id == Supplier.supplier_id",
        foreign_keys="[ProcurementRecord.supplier_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class MaterialThicknessOption(Base):
    """Approved Day 4 table material_thickness_options; database constraints are applied by later migrations."""

    __tablename__ = "material_thickness_options"
    __table_args__ = (
        ForeignKeyConstraint(
            ["material_id"],
            ["business.materials.material_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint("thickness_mm > 0", name="rule_01"),
        CheckConstraint(
            "length(btrim(material_id)) > 0 AND length(material_id) <= 64",
            name="material_id_identity",
        ),
        CheckConstraint("thickness_mm <> 'NaN'::numeric", name="thickness_mm_finite"),
        {"schema": "business"},
    )

    material_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    thickness_mm: Mapped[Decimal] = mapped_column(
        Numeric(12, 3, asdecimal=True), primary_key=True, nullable=False
    )

    material: Mapped[Material] = relationship(
        "Material",
        back_populates="thickness_options",
        primaryjoin="MaterialThicknessOption.material_id == Material.material_id",
        foreign_keys="[MaterialThicknessOption.material_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    inventory_records: Mapped[list[MaterialInventory]] = relationship(
        "MaterialInventory",
        back_populates="thickness_option",
        primaryjoin="and_(MaterialInventory.material_id == MaterialThicknessOption.material_id, MaterialInventory.thickness_mm == MaterialThicknessOption.thickness_mm)",
        foreign_keys="[MaterialInventory.material_id, MaterialInventory.thickness_mm]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class MaterialFinishCompatibility(Base):
    """Approved Day 4 table material_finish_compatibility; database constraints are applied by later migrations."""

    __tablename__ = "material_finish_compatibility"
    __table_args__ = (
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
        CheckConstraint(
            "length(btrim(material_id)) > 0 AND length(material_id) <= 64",
            name="material_id_identity",
        ),
        CheckConstraint(
            "length(btrim(finish_id)) > 0 AND length(finish_id) <= 64", name="finish_id_identity"
        ),
        {"schema": "business"},
    )

    material_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    finish_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)

    material: Mapped[Material] = relationship(
        "Material",
        back_populates="finish_compatibilities",
        primaryjoin="MaterialFinishCompatibility.material_id == Material.material_id",
        foreign_keys="[MaterialFinishCompatibility.material_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    finish: Mapped[Finish] = relationship(
        "Finish",
        back_populates="material_compatibilities",
        primaryjoin="MaterialFinishCompatibility.finish_id == Finish.finish_id",
        foreign_keys="[MaterialFinishCompatibility.finish_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )


class MaterialSupportedOperation(Base):
    """Approved Day 4 table material_supported_operations; database constraints are applied by later migrations."""

    __tablename__ = "material_supported_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["material_id"],
            ["business.materials.material_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["operation_id"],
            ["business.operations.operation_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "length(btrim(material_id)) > 0 AND length(material_id) <= 64",
            name="material_id_identity",
        ),
        CheckConstraint(
            "length(btrim(operation_id)) > 0 AND length(operation_id) <= 64",
            name="operation_id_identity",
        ),
        {"schema": "business"},
    )

    material_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    operation_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)

    material: Mapped[Material] = relationship(
        "Material",
        back_populates="supported_operations",
        primaryjoin="MaterialSupportedOperation.material_id == Material.material_id",
        foreign_keys="[MaterialSupportedOperation.material_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    operation: Mapped[Operation] = relationship(
        "Operation",
        back_populates="material_support",
        primaryjoin="MaterialSupportedOperation.operation_id == Operation.operation_id",
        foreign_keys="[MaterialSupportedOperation.operation_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )


class FinishMaterialCategory(Base):
    """Approved Day 4 table finish_material_categories; database constraints are applied by later migrations."""

    __tablename__ = "finish_material_categories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["finish_id"],
            ["business.finishes.finish_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "material_category IN ('aluminum','mild_steel','stainless_steel','galvanized_steel','copper','brass')",
            name="rule_01",
        ),
        CheckConstraint(
            "length(btrim(finish_id)) > 0 AND length(finish_id) <= 64", name="finish_id_identity"
        ),
        CheckConstraint("length(btrim(material_category)) > 0", name="material_category_nonblank"),
        {"schema": "business"},
    )

    finish_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    material_category: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)

    finish: Mapped[Finish] = relationship(
        "Finish",
        back_populates="material_categories",
        primaryjoin="FinishMaterialCategory.finish_id == Finish.finish_id",
        foreign_keys="[FinishMaterialCategory.finish_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )


class OperationRouting(Base):
    """Approved Day 4 table operation_routing; database constraints are applied by later migrations."""

    __tablename__ = "operation_routing"
    __table_args__ = (
        UniqueConstraint("part_type", "sequence"),
        ForeignKeyConstraint(
            ["workcentre_id", "operation_id"],
            ["business.workcentres.workcentre_id", "business.workcentres.operation_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint("sequence > 0", name="rule_01"),
        CheckConstraint("required_when IN ('always','finish_not_none')", name="rule_02"),
        CheckConstraint(
            "part_type IN ('flat_panel','mounting_plate','l_bracket','u_bracket','enclosure','fabricated_assembly')",
            name="rule_03",
        ),
        CheckConstraint(
            "length(btrim(routing_id)) > 0 AND length(routing_id) <= 64", name="routing_id_identity"
        ),
        CheckConstraint(
            "length(btrim(operation_id)) > 0 AND length(operation_id) <= 64",
            name="operation_id_identity",
        ),
        CheckConstraint(
            "length(btrim(workcentre_id)) > 0 AND length(workcentre_id) <= 64",
            name="workcentre_id_identity",
        ),
        {"schema": "business"},
    )

    routing_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    part_type: Mapped[str] = mapped_column(Text(), nullable=False)
    operation_id: Mapped[str] = mapped_column(Text(), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer(), nullable=False)
    required_when: Mapped[str] = mapped_column(Text(), nullable=False)
    workcentre_id: Mapped[str] = mapped_column(Text(), nullable=False)

    workcentre: Mapped[Workcentre] = relationship(
        "Workcentre",
        back_populates="routes",
        primaryjoin="and_(OperationRouting.workcentre_id == Workcentre.workcentre_id, OperationRouting.operation_id == Workcentre.operation_id)",
        foreign_keys="[OperationRouting.workcentre_id, OperationRouting.operation_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
