"""upload_idempotency_operation (approved; activated in code 2026-11-11, not applied to development)

Revision ID: 20261111_04
Revises: 20261111_03
Create Date: 2026-11-11

Decision F (TD-019 residual). Lets ``business.idempotency_records`` hold attachment
upload receipts under their own operation name instead of reusing create_rfq. Only
the operation CHECK (rule_01) changes. Downgrade refuses while upload receipts exist.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic.util import CommandError

from alembic import op

revision: str = "20261111_04"
down_revision: Union[str, Sequence[str], None] = "20261111_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APPROVED = (
    "'create_rfq','create_quote_draft','request_approval','approve_quote',"
    "'reject_quote','release_quote','edit_quote_draft'"
)
NAME = "ck_idempotency_records_rule_01"  # wrapped in op.f() at use: a final name


def upgrade() -> None:
    op.drop_constraint(op.f(NAME), "idempotency_records", schema="business", type_="check")
    op.create_check_constraint(
        op.f(NAME),
        "idempotency_records",
        f"operation IN ({APPROVED},'attach_rfq_document')",
        schema="business",
    )


def downgrade() -> None:
    receipts = op.get_bind().execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM business.idempotency_records "
            "WHERE operation = 'attach_rfq_document')"
        )
    )
    if receipts.scalar():
        raise CommandError("upload receipts exist; downgrade refused without removal.")
    op.drop_constraint(op.f(NAME), "idempotency_records", schema="business", type_="check")
    op.create_check_constraint(
        op.f(NAME), "idempotency_records", f"operation IN ({APPROVED})", schema="business"
    )
