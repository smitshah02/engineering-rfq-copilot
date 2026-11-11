"""rfq_assignments (approved; activated in code 2026-11-11, not applied to development)

Revision ID: 20261111_02
Revises: 20261110_01
Create Date: 2026-11-11

Decision B (TD-015). Explicit per-RFQ staff assignment, kept apart from customer
ownership: a read needs ownership (customer scope) AND, when enforced, an active
assignment. Nothing is backfilled; no assignment is inferred from created_by, region
or role. Downgrade refuses while any assignment exists.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic.util import CommandError

from alembic import op

revision: str = "20261111_02"
down_revision: Union[str, Sequence[str], None] = "20261110_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "rfq_assignments"


def _fk(column, target, name):
    return sa.ForeignKeyConstraint(
        [column],
        [target],
        name=op.f(name),
        onupdate="RESTRICT",
        ondelete="RESTRICT",
        match="SIMPLE",
    )


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("rfq_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("assigned_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64",
            name=op.f("ck_rfq_assignments_rfq_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(user_id)) > 0 AND length(user_id) <= 64",
            name=op.f("ck_rfq_assignments_user_id_identity"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name=op.f("ck_rfq_assignments_revoked_after_created"),
        ),
        _fk("rfq_id", "business.rfqs.rfq_id", "fk_rfq_assignments_rfq_id_rfqs"),
        _fk("user_id", "business.users.user_id", "fk_rfq_assignments_user_id_users"),
        _fk("assigned_by", "business.users.user_id", "fk_rfq_assignments_assigned_by_users"),
        sa.PrimaryKeyConstraint("rfq_id", "user_id", name=op.f("pk_rfq_assignments")),
        schema="business",
    )
    op.create_index(
        op.f("ix_rfq_assignments_active_user"),
        TABLE,
        ["user_id", "rfq_id"],
        schema="business",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM business.{TABLE})")).scalar():
        raise CommandError("rfq_assignments has rows; downgrade refused without removal.")
    op.drop_index(op.f("ix_rfq_assignments_active_user"), table_name=TABLE, schema="business")
    op.drop_table(TABLE, schema="business")
