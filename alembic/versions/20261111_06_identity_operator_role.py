"""identity_operator_role (approved; activated in code 2026-11-11, not applied to development)

Revision ID: 20261111_06
Revises: 20261111_04
Create Date: 2026-11-11

Decision D3. Widens ``business.roles`` CHECK (``ck_roles_rule_01``) to admit the
dedicated ``identity_operator`` role for separately provisioned human operator accounts.
Schema only: the role row, its two permissions and their grants come from the pinned
``rbac-policy-v2`` overlay (``python -m rfq_copilot.seeding.policy``), applied after the
baseline seed. No user is given this role here or by the overlay.

Downgrade refuses while any role row or user still uses ``identity_operator``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic.util import CommandError

from alembic import op

revision: str = "20261111_06"
down_revision: Union[str, Sequence[str], None] = "20261111_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BASELINE_ROLES = (
    "'sales_representative','estimator','manufacturing_engineer','sales_manager',"
    "'administrator','auditor'"
)
NAME = "ck_roles_rule_01"  # wrapped in op.f() at use: a final name


def upgrade() -> None:
    op.drop_constraint(op.f(NAME), "roles", schema="business", type_="check")
    op.create_check_constraint(
        op.f(NAME), "roles", f"role IN ({BASELINE_ROLES},'identity_operator')", schema="business"
    )


def downgrade() -> None:
    in_use = op.get_bind().execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM business.roles WHERE role = 'identity_operator') "
            "OR EXISTS (SELECT 1 FROM business.users WHERE role = 'identity_operator')"
        )
    )
    if in_use.scalar():
        raise CommandError("identity_operator is in use; remove the overlay first.")
    op.drop_constraint(op.f(NAME), "roles", schema="business", type_="check")
    op.create_check_constraint(
        op.f(NAME), "roles", f"role IN ({BASELINE_ROLES})", schema="business"
    )
