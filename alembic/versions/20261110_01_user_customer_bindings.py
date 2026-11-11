"""user_customer_bindings

Revision ID: 20261110_01
Revises: 20261109_01
Create Date: 2026-11-10

Approved TD-012: an explicit, authoritative binding from an authenticated user to the one
customer account they act as. Additive only: one new table, no change to existing tables
or data. ``revoked_at IS NULL`` marks the active binding; a partial unique index allows at
most one active binding per user. Foreign keys are RESTRICT, like every other table. Downgrade refuses while any binding
exists, so provisioned records are never dropped silently.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic.util import CommandError

from alembic import op

revision: str = "20261110_01"
down_revision: Union[str, Sequence[str], None] = "20261109_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "user_customer_bindings"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(btrim(user_id)) > 0 AND length(user_id) <= 64",
            name=op.f("ck_user_customer_bindings_user_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(customer_id)) > 0 AND length(customer_id) <= 64",
            name=op.f("ck_user_customer_bindings_customer_id_identity"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name=op.f("ck_user_customer_bindings_revoked_after_created"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["business.customers.customer_id"],
            name=op.f("fk_user_customer_bindings_customer_id_customers"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["business.users.user_id"],
            name=op.f("fk_user_customer_bindings_user_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("user_id", "customer_id", name=op.f("pk_user_customer_bindings")),
        schema="business",
    )
    op.create_index(
        op.f("ix_user_customer_bindings_customer_id"),
        TABLE,
        ["customer_id"],
        unique=False,
        schema="business",
        postgresql_using="btree",
    )
    op.create_index(
        "uq_user_customer_bindings_active_user",
        TABLE,
        ["user_id"],
        unique=True,
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    # Bindings are authoritative records: refuse (and roll back) rather than delete them.
    if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM business.{TABLE})")).scalar():
        raise CommandError("user_customer_bindings has rows; downgrade refused without removal.")
    op.drop_index("uq_user_customer_bindings_active_user", table_name=TABLE, schema="business")
    op.drop_index(
        op.f("ix_user_customer_bindings_customer_id"), table_name=TABLE, schema="business"
    )
    op.drop_table(TABLE, schema="business")
