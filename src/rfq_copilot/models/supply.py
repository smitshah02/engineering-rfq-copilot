"""Approved Day 4 supply mappings; metadata definitions only."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rfq_copilot.models.base import Base

if TYPE_CHECKING:
    from rfq_copilot.models.manufacturing import MaterialThicknessOption, Supplier, Workcentre
    from rfq_copilot.models.rules import DataSnapshot


class MaterialInventory(Base):
    """Approved Day 4 table material_inventory; database constraints are applied by later migrations."""

    __tablename__ = "material_inventory"
    __table_args__ = (
        ForeignKeyConstraint(
            ["material_id", "thickness_mm"],
            [
                "business.material_thickness_options.material_id",
                "business.material_thickness_options.thickness_mm",
            ],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["supplier_id"],
            ["business.suppliers.supplier_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["snapshot_id"],
            ["business.data_snapshots.snapshot_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "quantity_kg >= 0 AND reserved_kg >= 0 AND reserved_kg <= quantity_kg", name="rule_01"
        ),
        CheckConstraint(
            "reorder_threshold_kg >= 0 AND supplier_lead_business_days >= 0", name="rule_02"
        ),
        CheckConstraint("unit = 'kg'", name="rule_03"),
        CheckConstraint("stock_form = 'sheet'", name="rule_04"),
        CheckConstraint("row_version >= 1", name="rule_05"),
        CheckConstraint(
            "length(btrim(inventory_id)) > 0 AND length(inventory_id) <= 64",
            name="inventory_id_identity",
        ),
        CheckConstraint(
            "length(btrim(material_id)) > 0 AND length(material_id) <= 64",
            name="material_id_identity",
        ),
        CheckConstraint("thickness_mm <> 'NaN'::numeric", name="thickness_mm_finite"),
        CheckConstraint("quantity_kg <> 'NaN'::numeric", name="quantity_kg_finite"),
        CheckConstraint("reserved_kg <> 'NaN'::numeric", name="reserved_kg_finite"),
        CheckConstraint(
            "reorder_threshold_kg <> 'NaN'::numeric", name="reorder_threshold_kg_finite"
        ),
        CheckConstraint(
            "length(btrim(supplier_id)) > 0 AND length(supplier_id) <= 64",
            name="supplier_id_identity",
        ),
        CheckConstraint("available_kg <> 'NaN'::numeric", name="available_kg_finite"),
        CheckConstraint(
            "length(btrim(snapshot_id)) > 0 AND length(snapshot_id) <= 64",
            name="snapshot_id_identity",
        ),
        Index(
            "ix_inventory_material_thickness",
            "material_id",
            "thickness_mm",
            "warehouse",
            unique=False,
            postgresql_using="btree",
        ),
        {"schema": "business"},
    )

    inventory_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    material_id: Mapped[str] = mapped_column(Text(), nullable=False)
    thickness_mm: Mapped[Decimal] = mapped_column(Numeric(12, 3, asdecimal=True), nullable=False)
    stock_form: Mapped[str] = mapped_column(Text(), nullable=False)
    quantity_kg: Mapped[Decimal] = mapped_column(Numeric(18, 6, asdecimal=True), nullable=False)
    reserved_kg: Mapped[Decimal] = mapped_column(Numeric(18, 6, asdecimal=True), nullable=False)
    unit: Mapped[str] = mapped_column(Text(), nullable=False)
    warehouse: Mapped[str] = mapped_column(Text(), nullable=False)
    reorder_threshold_kg: Mapped[Decimal] = mapped_column(
        Numeric(18, 6, asdecimal=True), nullable=False
    )
    supplier_id: Mapped[str] = mapped_column(Text(), nullable=False)
    supplier_lead_business_days: Mapped[int] = mapped_column(Integer(), nullable=False)
    expected_replenishment_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    snapshot_date: Mapped[date] = mapped_column(Date(), nullable=False)
    available_kg: Mapped[Decimal] = mapped_column(
        Numeric(18, 6, asdecimal=True),
        Computed("quantity_kg - reserved_kg", persisted=True),
        nullable=False,
    )
    snapshot_id: Mapped[str] = mapped_column(Text(), nullable=False)
    row_version: Mapped[int] = mapped_column(BigInteger(), server_default=text("1"), nullable=False)

    thickness_option: Mapped[MaterialThicknessOption] = relationship(
        "MaterialThicknessOption",
        back_populates="inventory_records",
        primaryjoin="and_(MaterialInventory.material_id == MaterialThicknessOption.material_id, MaterialInventory.thickness_mm == MaterialThicknessOption.thickness_mm)",
        foreign_keys="[MaterialInventory.material_id, MaterialInventory.thickness_mm]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    supplier: Mapped[Supplier] = relationship(
        "Supplier",
        back_populates="inventory_records",
        primaryjoin="MaterialInventory.supplier_id == Supplier.supplier_id",
        foreign_keys="[MaterialInventory.supplier_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    snapshot: Mapped[DataSnapshot] = relationship(
        "DataSnapshot",
        back_populates="inventory_records",
        primaryjoin="MaterialInventory.snapshot_id == DataSnapshot.snapshot_id",
        foreign_keys="[MaterialInventory.snapshot_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    procurements: Mapped[list[ProcurementRecord]] = relationship(
        "ProcurementRecord",
        back_populates="inventory",
        primaryjoin="ProcurementRecord.inventory_id == MaterialInventory.inventory_id",
        foreign_keys="[ProcurementRecord.inventory_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class ProcurementRecord(Base):
    """Approved Day 4 table procurement_records; database constraints are applied by later migrations."""

    __tablename__ = "procurement_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["inventory_id"],
            ["business.material_inventory.inventory_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["supplier_id"],
            ["business.suppliers.supplier_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "quantity_kg > 0 AND expected_receipt_date >= ordered_date", name="rule_01"
        ),
        CheckConstraint("status IN ('ordered','delayed')", name="rule_02"),
        CheckConstraint(
            "length(btrim(procurement_id)) > 0 AND length(procurement_id) <= 64",
            name="procurement_id_identity",
        ),
        CheckConstraint(
            "length(btrim(inventory_id)) > 0 AND length(inventory_id) <= 64",
            name="inventory_id_identity",
        ),
        CheckConstraint(
            "length(btrim(supplier_id)) > 0 AND length(supplier_id) <= 64",
            name="supplier_id_identity",
        ),
        CheckConstraint("quantity_kg <> 'NaN'::numeric", name="quantity_kg_finite"),
        Index(
            "ix_procurement_supplier_status_eta",
            "supplier_id",
            "status",
            "expected_receipt_date",
            unique=False,
            postgresql_using="btree",
        ),
        Index(
            "ix_procurement_inventory_status",
            "inventory_id",
            "status",
            unique=False,
            postgresql_using="btree",
        ),
        {"schema": "business"},
    )

    procurement_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    inventory_id: Mapped[str] = mapped_column(Text(), nullable=False)
    supplier_id: Mapped[str] = mapped_column(Text(), nullable=False)
    quantity_kg: Mapped[Decimal] = mapped_column(Numeric(18, 6, asdecimal=True), nullable=False)
    ordered_date: Mapped[date] = mapped_column(Date(), nullable=False)
    expected_receipt_date: Mapped[date] = mapped_column(Date(), nullable=False)
    status: Mapped[str] = mapped_column(Text(), nullable=False)

    inventory: Mapped[MaterialInventory] = relationship(
        "MaterialInventory",
        back_populates="procurements",
        primaryjoin="ProcurementRecord.inventory_id == MaterialInventory.inventory_id",
        foreign_keys="[ProcurementRecord.inventory_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    supplier: Mapped[Supplier] = relationship(
        "Supplier",
        back_populates="procurements",
        primaryjoin="ProcurementRecord.supplier_id == Supplier.supplier_id",
        foreign_keys="[ProcurementRecord.supplier_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )


class ProductionCapacity(Base):
    """Approved Day 4 table production_capacity; database constraints are applied by later migrations."""

    __tablename__ = "production_capacity"
    __table_args__ = (
        UniqueConstraint("workcentre_id", "week_start"),
        ForeignKeyConstraint(
            ["workcentre_id"],
            ["business.workcentres.workcentre_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["snapshot_id"],
            ["business.data_snapshots.snapshot_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "nominal_hours >= 0 AND downtime_hours >= 0 AND downtime_hours <= nominal_hours",
            name="rule_01",
        ),
        CheckConstraint(
            "scheduled_hours >= 0 AND scheduled_hours <= nominal_hours - downtime_hours",
            name="rule_02",
        ),
        CheckConstraint("EXTRACT(ISODOW FROM week_start) = 1", name="rule_03"),
        CheckConstraint("row_version >= 1", name="rule_04"),
        CheckConstraint(
            "length(btrim(capacity_id)) > 0 AND length(capacity_id) <= 64",
            name="capacity_id_identity",
        ),
        CheckConstraint(
            "length(btrim(workcentre_id)) > 0 AND length(workcentre_id) <= 64",
            name="workcentre_id_identity",
        ),
        CheckConstraint("nominal_hours <> 'NaN'::numeric", name="nominal_hours_finite"),
        CheckConstraint("downtime_hours <> 'NaN'::numeric", name="downtime_hours_finite"),
        CheckConstraint("scheduled_hours <> 'NaN'::numeric", name="scheduled_hours_finite"),
        CheckConstraint("available_hours <> 'NaN'::numeric", name="available_hours_finite"),
        CheckConstraint("remaining_hours <> 'NaN'::numeric", name="remaining_hours_finite"),
        CheckConstraint(
            "utilization_fraction <> 'NaN'::numeric", name="utilization_fraction_finite"
        ),
        CheckConstraint(
            "length(btrim(snapshot_id)) > 0 AND length(snapshot_id) <= 64",
            name="snapshot_id_identity",
        ),
        {"schema": "business"},
    )

    capacity_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    workcentre_id: Mapped[str] = mapped_column(Text(), nullable=False)
    week_start: Mapped[date] = mapped_column(Date(), nullable=False)
    nominal_hours: Mapped[Decimal] = mapped_column(Numeric(12, 4, asdecimal=True), nullable=False)
    downtime_hours: Mapped[Decimal] = mapped_column(Numeric(12, 4, asdecimal=True), nullable=False)
    scheduled_hours: Mapped[Decimal] = mapped_column(Numeric(12, 4, asdecimal=True), nullable=False)
    available_hours: Mapped[Decimal] = mapped_column(
        Numeric(12, 4, asdecimal=True),
        Computed("nominal_hours - downtime_hours", persisted=True),
        nullable=False,
    )
    remaining_hours: Mapped[Decimal] = mapped_column(
        Numeric(12, 4, asdecimal=True),
        Computed("nominal_hours - downtime_hours - scheduled_hours", persisted=True),
        nullable=False,
    )
    utilization_fraction: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 4, asdecimal=True),
        Computed(
            "CASE WHEN nominal_hours > downtime_hours THEN round(scheduled_hours / (nominal_hours - downtime_hours),4) ELSE NULL END",
            persisted=True,
        ),
        nullable=True,
    )
    snapshot_id: Mapped[str] = mapped_column(Text(), nullable=False)
    row_version: Mapped[int] = mapped_column(BigInteger(), server_default=text("1"), nullable=False)

    workcentre: Mapped[Workcentre] = relationship(
        "Workcentre",
        back_populates="capacity_records",
        primaryjoin="ProductionCapacity.workcentre_id == Workcentre.workcentre_id",
        foreign_keys="[ProductionCapacity.workcentre_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    snapshot: Mapped[DataSnapshot] = relationship(
        "DataSnapshot",
        back_populates="capacity_records",
        primaryjoin="ProductionCapacity.snapshot_id == DataSnapshot.snapshot_id",
        foreign_keys="[ProductionCapacity.snapshot_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
