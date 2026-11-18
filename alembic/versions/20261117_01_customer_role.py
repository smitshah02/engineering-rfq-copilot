"""customer_role (Milestone 2 Step 1; written, not applied)

Revision ID: 20261117_01
Revises: 20261116_04
Create Date: 2026-11-17

Widens ``business.roles`` CHECK (``ck_roles_rule_01``) to admit the dedicated external
``customer`` role, exactly as 20261111_06 admitted ``identity_operator``. Schema only: the
role row, its permissions and grants come from the pinned ``rbac-policy-v3`` overlay
(``python -m rfq_copilot.seeding.policy``). No user is given this role here.

Same constraint name and type, so the managed shape (names/types, ``migration_safety``) and
the catalog counts are unchanged by construction; ``verify_schema`` compares the CHECK text
with the ORM model. No trigger or function: not a guard revision.

Downgrade refuses while any role row or user still uses ``customer``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic.util import CommandError

from alembic import op

revision: str = "20261117_01"
down_revision: Union[str, Sequence[str], None] = "20261116_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PREVIOUS_ROLES = (
    "'sales_representative','estimator','manufacturing_engineer','sales_manager',"
    "'administrator','auditor','identity_operator'"
)
NAME = "ck_roles_rule_01"  # wrapped in op.f() at use: a final name


def upgrade() -> None:
    op.drop_constraint(op.f(NAME), "roles", schema="business", type_="check")
    op.create_check_constraint(
        op.f(NAME), "roles", f"role IN ({PREVIOUS_ROLES},'customer')", schema="business"
    )


def downgrade() -> None:
    in_use = op.get_bind().execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM business.roles WHERE role = 'customer') "
            "OR EXISTS (SELECT 1 FROM business.users WHERE role = 'customer')"
        )
    )
    if in_use.scalar():
        raise CommandError("customer is in use; remove the overlay and accounts first.")
    op.drop_constraint(op.f(NAME), "roles", schema="business", type_="check")
    op.create_check_constraint(
        op.f(NAME), "roles", f"role IN ({PREVIOUS_ROLES})", schema="business"
    )
