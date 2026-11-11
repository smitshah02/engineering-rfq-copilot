"""token_revocations (approved; activated in code 2026-11-11, not applied to development)

Revision ID: 20261111_03
Revises: 20261111_02
Create Date: 2026-11-11

Decision E (TD-024). Relying-party revocation for an identity provider without
introspection. A row revokes either one token, by the SHA-256 of the exact token string
(the raw token is never stored), or every token of a user issued before ``not_before``.
Rows are written only by the operator CLI, by holders of revoke_tokens (granted
only to identity_operator by the rbac-policy-v2 overlay). Downgrade refuses while any row exists.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic.util import CommandError
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261111_03"
down_revision: Union[str, Sequence[str], None] = "20261111_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "token_revocations"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("revocation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("token_sha256", sa.Text(), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(user_id)) > 0 AND length(user_id) <= 64",
            name=op.f("ck_token_revocations_user_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(created_by)) > 0 AND length(created_by) <= 64",
            name=op.f("ck_token_revocations_created_by_identity"),
        ),
        sa.CheckConstraint(
            "(token_sha256 IS NULL) <> (not_before IS NULL)",
            name=op.f("ck_token_revocations_one_kind"),
        ),
        sa.CheckConstraint(
            "token_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_token_revocations_token_sha256_hex"),
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0 AND length(reason) <= 200",
            name=op.f("ck_token_revocations_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["business.users.user_id"],
            name=op.f("fk_token_revocations_user_id_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),  # fmt: skip
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["business.users.user_id"],
            name=op.f("fk_token_revocations_created_by_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),  # fmt: skip
        sa.PrimaryKeyConstraint("revocation_id", name=op.f("pk_token_revocations")),
        schema="business",
    )
    op.create_index(
        op.f("uq_token_revocations_token_sha256"),
        TABLE,
        ["token_sha256"],
        unique=True,
        schema="business",
        postgresql_where=sa.text("token_sha256 IS NOT NULL"),
    )
    op.create_index(
        op.f("ix_token_revocations_user_not_before"),
        TABLE,
        ["user_id", "not_before"],
        schema="business",
        postgresql_where=sa.text("not_before IS NOT NULL"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM business.{TABLE})")).scalar():
        raise CommandError("token_revocations has rows; downgrade refused without removal.")
    op.drop_index(op.f("ix_token_revocations_user_not_before"), table_name=TABLE, schema="business")
    op.drop_index(op.f("uq_token_revocations_token_sha256"), table_name=TABLE, schema="business")
    op.drop_table(TABLE, schema="business")
