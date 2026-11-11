"""Approved Day 4 identity mappings, plus approved later additions; metadata only.

TD-012 (20261110_01): user_customer_bindings. Phase 3.5 (20261111_02/03/06):
rfq_assignments, token_revocations and the identity_operator role value.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
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
            # identity_operator: approved Phase 3.5 D3 (20261111_06); the role row and its two
            # operator permissions come only from the pinned rbac-policy-v2 overlay.
            "role IN ('sales_representative','estimator','manufacturing_engineer','sales_manager','administrator','auditor','identity_operator')",
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


class UserCustomerBinding(Base):
    """Approved TD-012 table (revision 20261110_01): the customer account a user acts as.

    Rows are provisioned explicitly; nothing is inferred from email, name, region or token
    claims. ``revoked_at IS NULL`` means active, and a user has at most one active binding
    (partial unique index), matching the single-customer ``Principal``. Revoked rows stay
    as history; re-binding the same pair clears ``revoked_at``.
    """

    __tablename__ = "user_customer_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id"],
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
            "length(btrim(user_id)) > 0 AND length(user_id) <= 64", name="user_id_identity"
        ),
        CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name="customer_id_identity",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at", name="revoked_after_created"
        ),
        Index(
            "uq_user_customer_bindings_active_user",
            "user_id",
            unique=True,
            postgresql_using="btree",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(None, "customer_id", unique=False, postgresql_using="btree"),
        {"schema": "business"},
    )

    user_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RfqAssignment(Base):
    """Approved Phase 3.5 table (revision 20261111_02): per-RFQ staff assignment.

    Narrows customer-scoped RFQ reads when RFQ_API_RFQ_ASSIGNMENTS=required; never widens
    them. Rows are provisioned explicitly (nothing is backfilled or inferred). Active
    while ``revoked_at IS NULL``. No ORM relationships, like the TD-012 binding.
    """

    __tablename__ = "rfq_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
            name="fk_rfq_assignments_rfq_id_rfqs",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),  # fmt: skip
        ForeignKeyConstraint(
            ["user_id"],
            ["business.users.user_id"],
            name="fk_rfq_assignments_user_id_users",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),  # fmt: skip
        ForeignKeyConstraint(
            ["assigned_by"],
            ["business.users.user_id"],
            name="fk_rfq_assignments_assigned_by_users",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),  # fmt: skip
        CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64", name="rfq_id_identity"
        ),
        CheckConstraint(
            "length(btrim(user_id)) > 0 AND length(user_id) <= 64", name="user_id_identity"
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at", name="revoked_after_created"
        ),
        Index(
            "ix_rfq_assignments_active_user",
            "user_id",
            "rfq_id",
            unique=False,
            postgresql_using="btree",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        {"schema": "business"},
    )

    rfq_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    user_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    assigned_by: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TokenRevocation(Base):
    """Approved Phase 3.5 table (revision 20261111_03): relying-party token revocation.

    One row revokes either one token (SHA-256 of the exact token string; the token is
    never stored) or every token of a user issued before ``not_before``. Written only by
    ``rfq_copilot.identity_admin`` for holders of ``revoke_tokens``. No relationships.
    """

    __tablename__ = "token_revocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id"],
            ["business.users.user_id"],
            name="fk_token_revocations_user_id_users",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),  # fmt: skip
        ForeignKeyConstraint(
            ["created_by"],
            ["business.users.user_id"],
            name="fk_token_revocations_created_by_users",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),  # fmt: skip
        CheckConstraint(
            "length(btrim(user_id)) > 0 AND length(user_id) <= 64", name="user_id_identity"
        ),
        CheckConstraint(
            "length(btrim(created_by)) > 0 AND length(created_by) <= 64",
            name="created_by_identity",
        ),
        CheckConstraint("(token_sha256 IS NULL) <> (not_before IS NULL)", name="one_kind"),
        CheckConstraint("token_sha256 ~ '^[0-9a-f]{64}$'", name="token_sha256_hex"),
        CheckConstraint("length(btrim(reason)) > 0 AND length(reason) <= 200", name="reason"),
        Index(
            "uq_token_revocations_token_sha256",
            "token_sha256",
            unique=True,
            postgresql_using="btree",
            postgresql_where=text("token_sha256 IS NOT NULL"),
        ),
        Index(
            "ix_token_revocations_user_not_before",
            "user_id",
            "not_before",
            unique=False,
            postgresql_using="btree",
            postgresql_where=text("not_before IS NOT NULL"),
        ),
        {"schema": "business"},
    )

    revocation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), default=uuid4, primary_key=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(Text(), nullable=False)
    token_sha256: Mapped[str | None] = mapped_column(Text(), nullable=True)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(Text(), nullable=False)
    reason: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
