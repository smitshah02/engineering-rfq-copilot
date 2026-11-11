"""multi_customer_bindings (PROPOSED, not applied)

Revision ID: 20261111_05
Revises: 20261111_06
Create Date: 2026-11-11

Decision B (TD-017). Drops the one-active-binding index so staff may hold several
active customer bindings. Must be applied together with RFQ_API_CUSTOMER_CONTEXT=
selectable. Without a context header, a user with several bindings is refused (403)
on every route, never given a default customer. Downgrade refuses while any user has
more than one active binding, then recreates the index.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic.util import CommandError

from alembic import op

revision: str = "20261111_05"
down_revision: Union[str, Sequence[str], None] = "20261111_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX = "uq_user_customer_bindings_active_user"  # wrapped in op.f() at use: a final name


def upgrade() -> None:
    op.drop_index(op.f(INDEX), table_name="user_customer_bindings", schema="business")


def downgrade() -> None:
    several = op.get_bind().execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM business.user_customer_bindings "
            "WHERE revoked_at IS NULL GROUP BY user_id HAVING count(*) > 1)"
        )
    )
    if several.scalar():
        raise CommandError("users hold several active bindings; downgrade refused.")
    op.create_index(
        op.f(INDEX),
        "user_customer_bindings",
        ["user_id"],
        unique=True,
        schema="business",
        postgresql_using="btree",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
