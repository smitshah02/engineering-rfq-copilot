"""Approved Day 4 customers mappings; metadata definitions only."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rfq_copilot.models.base import Base

if TYPE_CHECKING:
    from rfq_copilot.models.reliability import AuditEvent, IdempotencyRecord, WorkflowRun
    from rfq_copilot.models.rfqs import RFQ


class Customer(Base):
    """Approved Day 4 table customers; database constraints are applied by later migrations."""

    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint(
            "account_status IN ('active','pending_verification','on_hold','credit_restricted','inactive')",
            name="rule_01",
        ),
        CheckConstraint("credit_limit_usd >= 0 AND credit_exposure_usd >= 0", name="rule_02"),
        CheckConstraint(
            "approval_requirement IN ('threshold_based','commercial_review')", name="rule_03"
        ),
        CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name="customer_id_identity",
        ),
        CheckConstraint("credit_limit_usd <> 'NaN'::numeric", name="credit_limit_usd_finite"),
        CheckConstraint("credit_exposure_usd <> 'NaN'::numeric", name="credit_exposure_usd_finite"),
        {"schema": "business"},
    )

    customer_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    company_name: Mapped[str] = mapped_column(Text(), nullable=False)
    industry: Mapped[str] = mapped_column(Text(), nullable=False)
    city: Mapped[str] = mapped_column(Text(), nullable=False)
    state: Mapped[str] = mapped_column(Text(), nullable=False)
    country: Mapped[str] = mapped_column(Text(), nullable=False)
    region: Mapped[str] = mapped_column(Text(), nullable=False)
    account_status: Mapped[str] = mapped_column(Text(), nullable=False)
    payment_terms: Mapped[str] = mapped_column(Text(), nullable=False)
    credit_limit_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 2, asdecimal=True), nullable=False
    )
    credit_exposure_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 2, asdecimal=True), nullable=False
    )
    preferred_shipping: Mapped[str] = mapped_column(Text(), nullable=False)
    restrictions: Mapped[str] = mapped_column(Text(), nullable=False)
    created_date: Mapped[date] = mapped_column(Date(), nullable=False)
    approval_requirement: Mapped[str] = mapped_column(Text(), nullable=False)

    contacts: Mapped[list[Contact]] = relationship(
        "Contact",
        back_populates="customer",
        primaryjoin="Contact.customer_id == Customer.customer_id",
        foreign_keys="[Contact.customer_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    rfqs: Mapped[list[RFQ]] = relationship(
        "RFQ",
        back_populates="customer",
        primaryjoin="RFQ.customer_id == Customer.customer_id",
        foreign_keys="[RFQ.customer_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    idempotency_records: Mapped[list[IdempotencyRecord]] = relationship(
        "IdempotencyRecord",
        back_populates="customer",
        primaryjoin="IdempotencyRecord.customer_id == Customer.customer_id",
        foreign_keys="[IdempotencyRecord.customer_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        "AuditEvent",
        back_populates="customer",
        primaryjoin="AuditEvent.customer_id == Customer.customer_id",
        foreign_keys="[AuditEvent.customer_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    workflow_runs: Mapped[list[WorkflowRun]] = relationship(
        "WorkflowRun",
        back_populates="customer",
        primaryjoin="WorkflowRun.customer_id == Customer.customer_id",
        foreign_keys="[WorkflowRun.customer_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class Contact(Base):
    """Approved Day 4 table contacts; database constraints are applied by later migrations."""

    __tablename__ = "contacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id"],
            ["business.customers.customer_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "length(btrim(contact_id)) > 0 AND length(contact_id) <= 64", name="contact_id_identity"
        ),
        CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name="customer_id_identity",
        ),
        Index(
            "uq_contacts_primary",
            "customer_id",
            unique=True,
            postgresql_using="btree",
            postgresql_where=text("is_primary"),
        ),
        Index(
            "ix_contacts_customer_name",
            "customer_id",
            "contact_name",
            "contact_id",
            unique=False,
            postgresql_using="btree",
        ),
        {"schema": "business"},
    )

    contact_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text(), nullable=False)
    contact_name: Mapped[str] = mapped_column(Text(), nullable=False)
    email: Mapped[str] = mapped_column(Text(), nullable=False)
    phone: Mapped[str] = mapped_column(Text(), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean(), nullable=False)

    customer: Mapped[Customer] = relationship(
        "Customer",
        back_populates="contacts",
        primaryjoin="Contact.customer_id == Customer.customer_id",
        foreign_keys="[Contact.customer_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
