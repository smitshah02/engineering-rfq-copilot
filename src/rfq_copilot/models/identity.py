"""Approved Day 4 identity mappings; metadata definitions only."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rfq_copilot.models.base import Base

if TYPE_CHECKING:
    from rfq_copilot.models.approvals import ApprovalHistory, ApprovalRequest
    from rfq_copilot.models.quotations import PricingCalculation, Quote, QuoteVersion
    from rfq_copilot.models.reliability import AuditEvent, IdempotencyRecord, WorkflowRun
    from rfq_copilot.models.rfqs import RFQ


class Role(Base):
    """Approved Day 4 table roles; database constraints are applied by later migrations."""

    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint(
            "role IN ('sales_representative','estimator','manufacturing_engineer','sales_manager','administrator','auditor')",
            name="rule_01",
        ),
        CheckConstraint("length(btrim(role)) > 0", name="role_nonblank"),
        {"schema": "business"},
    )

    role: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)

    grants: Mapped[list[RolePermission]] = relationship(
        "RolePermission",
        back_populates="role_record",
        primaryjoin="RolePermission.role == Role.role",
        foreign_keys="[RolePermission.role]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    users: Mapped[list[User]] = relationship(
        "User",
        back_populates="role_record",
        primaryjoin="User.role == Role.role",
        foreign_keys="[User.role]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    approval_requests: Mapped[list[ApprovalRequest]] = relationship(
        "ApprovalRequest",
        back_populates="required_role_record",
        primaryjoin="ApprovalRequest.required_role == Role.role",
        foreign_keys="[ApprovalRequest.required_role]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class Permission(Base):
    """Approved Day 4 table permissions; database constraints are applied by later migrations."""

    __tablename__ = "permissions"
    __table_args__ = (
        CheckConstraint("length(btrim(permission)) > 0", name="permission_nonblank"),
        {"schema": "business"},
    )

    permission: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)

    role_grants: Mapped[list[RolePermission]] = relationship(
        "RolePermission",
        back_populates="permission_record",
        primaryjoin="RolePermission.permission == Permission.permission",
        foreign_keys="[RolePermission.permission]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )


class RolePermission(Base):
    """Approved Day 4 table role_permissions; database constraints are applied by later migrations."""

    __tablename__ = "role_permissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["role"],
            ["business.roles.role"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["permission"],
            ["business.permissions.permission"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint("length(btrim(role)) > 0", name="role_nonblank"),
        CheckConstraint("length(btrim(permission)) > 0", name="permission_nonblank"),
        {"schema": "business"},
    )

    role: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    permission: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean(), nullable=False)

    role_record: Mapped[Role] = relationship(
        "Role",
        back_populates="grants",
        primaryjoin="RolePermission.role == Role.role",
        foreign_keys="[RolePermission.role]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    permission_record: Mapped[Permission] = relationship(
        "Permission",
        back_populates="role_grants",
        primaryjoin="RolePermission.permission == Permission.permission",
        foreign_keys="[RolePermission.permission]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )


class User(Base):
    """Approved Day 4 table users; database constraints are applied by later migrations."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("authentication_subject"),
        ForeignKeyConstraint(
            ["role"],
            ["business.roles.role"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "length(btrim(user_id)) > 0 AND length(user_id) <= 64", name="user_id_identity"
        ),
        CheckConstraint("length(btrim(role)) > 0", name="role_nonblank"),
        CheckConstraint(
            "length(btrim(authentication_subject)) > 0", name="authentication_subject_nonblank"
        ),
        Index(
            "ix_users_reviewers",
            "role",
            "assigned_region",
            "user_id",
            unique=False,
            postgresql_using="btree",
            postgresql_where=text("active"),
        ),
        {"schema": "business"},
    )

    user_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text(), nullable=False)
    role: Mapped[str] = mapped_column(Text(), nullable=False)
    email: Mapped[str] = mapped_column(Text(), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    assigned_region: Mapped[str] = mapped_column(Text(), nullable=False)
    authentication_subject: Mapped[str] = mapped_column(Text(), nullable=False)

    role_record: Mapped[Role] = relationship(
        "Role",
        back_populates="users",
        primaryjoin="User.role == Role.role",
        foreign_keys="[User.role]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    created_rfqs: Mapped[list[RFQ]] = relationship(
        "RFQ",
        back_populates="creator",
        primaryjoin="RFQ.created_by == User.user_id",
        foreign_keys="[RFQ.created_by]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    pricing_calculations: Mapped[list[PricingCalculation]] = relationship(
        "PricingCalculation",
        back_populates="creator",
        primaryjoin="PricingCalculation.created_by == User.user_id",
        foreign_keys="[PricingCalculation.created_by]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    created_quotes: Mapped[list[Quote]] = relationship(
        "Quote",
        back_populates="creator",
        primaryjoin="Quote.created_by == User.user_id",
        foreign_keys="[Quote.created_by]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    submitted_quotes: Mapped[list[Quote]] = relationship(
        "Quote",
        back_populates="submitter",
        primaryjoin="Quote.submitted_by == User.user_id",
        foreign_keys="[Quote.submitted_by]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    approved_quotes: Mapped[list[Quote]] = relationship(
        "Quote",
        back_populates="approver",
        primaryjoin="Quote.approver_id == User.user_id",
        foreign_keys="[Quote.approver_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    released_quotes: Mapped[list[Quote]] = relationship(
        "Quote",
        back_populates="releaser",
        primaryjoin="Quote.released_by == User.user_id",
        foreign_keys="[Quote.released_by]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    created_quote_versions: Mapped[list[QuoteVersion]] = relationship(
        "QuoteVersion",
        back_populates="creator",
        primaryjoin="QuoteVersion.created_by == User.user_id",
        foreign_keys="[QuoteVersion.created_by]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    requested_approvals: Mapped[list[ApprovalRequest]] = relationship(
        "ApprovalRequest",
        back_populates="requester",
        primaryjoin="ApprovalRequest.requested_by == User.user_id",
        foreign_keys="[ApprovalRequest.requested_by]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    resolved_approvals: Mapped[list[ApprovalRequest]] = relationship(
        "ApprovalRequest",
        back_populates="resolver",
        primaryjoin="ApprovalRequest.resolved_by == User.user_id",
        foreign_keys="[ApprovalRequest.resolved_by]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    approval_events: Mapped[list[ApprovalHistory]] = relationship(
        "ApprovalHistory",
        back_populates="actor",
        primaryjoin="ApprovalHistory.actor_id == User.user_id",
        foreign_keys="[ApprovalHistory.actor_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    idempotency_records: Mapped[list[IdempotencyRecord]] = relationship(
        "IdempotencyRecord",
        back_populates="actor",
        primaryjoin="IdempotencyRecord.actor_id == User.user_id",
        foreign_keys="[IdempotencyRecord.actor_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        "AuditEvent",
        back_populates="actor",
        primaryjoin="AuditEvent.actor_id == User.user_id",
        foreign_keys="[AuditEvent.actor_id]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
    workflow_runs: Mapped[list[WorkflowRun]] = relationship(
        "WorkflowRun",
        back_populates="initiator",
        primaryjoin="WorkflowRun.initiated_by == User.user_id",
        foreign_keys="[WorkflowRun.initiated_by]",
        lazy="raise",
        uselist=True,
        cascade="save-update, merge",
        passive_deletes="all",
    )
