"""Approved Day 4 approvals mappings; metadata definitions only."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rfq_copilot.models.base import Base

if TYPE_CHECKING:
    from rfq_copilot.models.identity import Role, User
    from rfq_copilot.models.quotations import QuoteVersion
    from rfq_copilot.models.rules import BusinessRuleVersion


class ApprovalRequest(Base):
    """Approved Day 4 table approval_requests; database constraints are applied by later migrations."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint("approval_request_id", "quote_id", "quote_version"),
        ForeignKeyConstraint(
            ["quote_id", "quote_version"],
            ["business.quote_versions.quote_id", "business.quote_versions.version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["requested_by"],
            ["business.users.user_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["resolved_by"],
            ["business.users.user_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["required_role"],
            ["business.roles.role"],
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
        CheckConstraint("status IN ('pending','approved','rejected','superseded')", name="rule_01"),
        CheckConstraint("required_role = 'sales_manager'", name="rule_02"),
        CheckConstraint(
            "(status = 'pending' AND resolved_by IS NULL AND resolved_at IS NULL) OR (status <> 'pending' AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name="rule_03",
        ),
        CheckConstraint(
            "length(btrim(approval_request_id)) > 0 AND length(approval_request_id) <= 64",
            name="approval_request_id_identity",
        ),
        CheckConstraint(
            "length(btrim(quote_id)) > 0 AND length(quote_id) <= 64", name="quote_id_identity"
        ),
        CheckConstraint(
            "length(btrim(requested_by)) > 0 AND length(requested_by) <= 64",
            name="requested_by_identity",
        ),
        CheckConstraint(
            "length(btrim(approval_rule_version)) > 0", name="approval_rule_version_nonblank"
        ),
        CheckConstraint(
            "length(btrim(resolved_by)) > 0 AND length(resolved_by) <= 64",
            name="resolved_by_identity",
        ),
        Index(
            "uq_approval_requests_pending",
            "quote_id",
            "quote_version",
            unique=True,
            postgresql_using="btree",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_approval_requests_queue",
            "required_role",
            "requested_at",
            "quote_id",
            "quote_version",
            unique=False,
            postgresql_using="btree",
            postgresql_where=text("status = 'pending'"),
        ),
        {"schema": "business"},
    )

    approval_request_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    quote_id: Mapped[str] = mapped_column(Text(), nullable=False)
    quote_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    requested_by: Mapped[str] = mapped_column(Text(), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    required_role: Mapped[str] = mapped_column(
        Text(), server_default=text("'sales_manager'"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text(), server_default=text("'pending'"), nullable=False)
    approval_rule_version: Mapped[str] = mapped_column(Text(), nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(Text(), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    version_record: Mapped[QuoteVersion] = relationship(
        "QuoteVersion",
        back_populates="approval_requests",
        primaryjoin="and_(ApprovalRequest.quote_id == QuoteVersion.quote_id, ApprovalRequest.quote_version == QuoteVersion.version)",
        foreign_keys="[ApprovalRequest.quote_id, ApprovalRequest.quote_version]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    requester: Mapped[User] = relationship(
        "User",
        back_populates="requested_approvals",
        primaryjoin="ApprovalRequest.requested_by == User.user_id",
        foreign_keys="[ApprovalRequest.requested_by]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    resolver: Mapped[User | None] = relationship(
        "User",
        back_populates="resolved_approvals",
        primaryjoin="ApprovalRequest.resolved_by == User.user_id",
        foreign_keys="[ApprovalRequest.resolved_by]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    required_role_record: Mapped[Role] = relationship(
        "Role",
        back_populates="approval_requests",
        primaryjoin="ApprovalRequest.required_role == Role.role",
        foreign_keys="[ApprovalRequest.required_role]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    approval_rule: Mapped[BusinessRuleVersion] = relationship(
        "BusinessRuleVersion",
        back_populates="approval_requests",
        primaryjoin="ApprovalRequest.approval_rule_version == BusinessRuleVersion.rule_version",
        foreign_keys="[ApprovalRequest.approval_rule_version]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    history_events: Mapped[list[ApprovalHistory]] = relationship(
        "ApprovalHistory",
        back_populates="request",
        primaryjoin="and_(ApprovalHistory.approval_request_id == ApprovalRequest.approval_request_id, ApprovalHistory.quote_id == ApprovalRequest.quote_id, ApprovalHistory.quote_version == ApprovalRequest.quote_version)",
        foreign_keys="[ApprovalHistory.approval_request_id, ApprovalHistory.quote_id, ApprovalHistory.quote_version]",
        lazy="raise",
        uselist=True,
        viewonly=True,
    )


class ApprovalHistory(Base):
    """Approved Day 4 table approval_history; database constraints are applied by later migrations."""

    __tablename__ = "approval_history"
    __table_args__ = (
        ForeignKeyConstraint(
            ["quote_id", "quote_version"],
            ["business.quote_versions.quote_id", "business.quote_versions.version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["actor_id"],
            ["business.users.user_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["approval_request_id", "quote_id", "quote_version"],
            [
                "business.approval_requests.approval_request_id",
                "business.approval_requests.quote_id",
                "business.approval_requests.quote_version",
            ],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            match="SIMPLE",
        ),
        CheckConstraint(
            "action IN ('submitted','approved','rejected','released','superseded')", name="rule_01"
        ),
        CheckConstraint("quote_version >= 1", name="rule_02"),
        CheckConstraint(
            "length(btrim(approval_id)) > 0 AND length(approval_id) <= 64",
            name="approval_id_identity",
        ),
        CheckConstraint(
            "length(btrim(quote_id)) > 0 AND length(quote_id) <= 64", name="quote_id_identity"
        ),
        CheckConstraint(
            "length(btrim(actor_id)) > 0 AND length(actor_id) <= 64", name="actor_id_identity"
        ),
        CheckConstraint(
            "length(btrim(approval_request_id)) > 0 AND length(approval_request_id) <= 64",
            name="approval_request_id_identity",
        ),
        Index(
            "ix_approval_history_version_time",
            "quote_id",
            "quote_version",
            "action_at",
            "approval_id",
            unique=False,
            postgresql_using="btree",
        ),
        {"schema": "business"},
    )

    approval_id: Mapped[str] = mapped_column(Text(), primary_key=True, nullable=False)
    quote_id: Mapped[str] = mapped_column(Text(), nullable=False)
    quote_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    action: Mapped[str] = mapped_column(Text(), nullable=False)
    actor_id: Mapped[str] = mapped_column(Text(), nullable=False)
    action_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text(), nullable=False)
    approval_request_id: Mapped[str | None] = mapped_column(Text(), nullable=True)

    version_record: Mapped[QuoteVersion] = relationship(
        "QuoteVersion",
        back_populates="approval_events",
        primaryjoin="and_(ApprovalHistory.quote_id == QuoteVersion.quote_id, ApprovalHistory.quote_version == QuoteVersion.version)",
        foreign_keys="[ApprovalHistory.quote_id, ApprovalHistory.quote_version]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    actor: Mapped[User] = relationship(
        "User",
        back_populates="approval_events",
        primaryjoin="ApprovalHistory.actor_id == User.user_id",
        foreign_keys="[ApprovalHistory.actor_id]",
        lazy="raise",
        uselist=False,
        cascade="save-update, merge",
    )
    request: Mapped[ApprovalRequest | None] = relationship(
        "ApprovalRequest",
        back_populates="history_events",
        primaryjoin="and_(ApprovalHistory.approval_request_id == ApprovalRequest.approval_request_id, ApprovalHistory.quote_id == ApprovalRequest.quote_id, ApprovalHistory.quote_version == ApprovalRequest.quote_version)",
        foreign_keys="[ApprovalHistory.approval_request_id, ApprovalHistory.quote_id, ApprovalHistory.quote_version]",
        lazy="raise",
        uselist=False,
        viewonly=True,
    )
