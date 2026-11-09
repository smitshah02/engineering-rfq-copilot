"""capacity_unique_per_snapshot

Revision ID: 20261109_01
Revises: 20261108_01
Create Date: 2026-11-09

Approved TD-005: a capacity week is unique per snapshot, so several capacity snapshots can
hold the same workcentre-week. The new key also serves the snapshot-filtered capacity read
(workcentre_id, week range, snapshot_id). No data changes; existing rows satisfy the new
key because it is strictly weaker than the old one.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20261109_01"
down_revision: Union[str, Sequence[str], None] = "20261108_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = ("workcentre_id", "week_start")
NEW = ("workcentre_id", "week_start", "snapshot_id")


def upgrade() -> None:
    op.drop_constraint(
        op.f("uq_production_capacity_workcentre_id_week_start"),
        "production_capacity",
        schema="business",
        type_="unique",
    )
    op.create_unique_constraint(
        op.f("uq_production_capacity_workcentre_id_week_start_snapshot_id"),
        "production_capacity",
        list(NEW),
        schema="business",
    )


def downgrade() -> None:
    # Restoring the old key fails (and rolls back) while two snapshots share a week;
    # nothing is ever deleted to make it fit.
    op.drop_constraint(
        op.f("uq_production_capacity_workcentre_id_week_start_snapshot_id"),
        "production_capacity",
        schema="business",
        type_="unique",
    )
    op.create_unique_constraint(
        op.f("uq_production_capacity_workcentre_id_week_start"),
        "production_capacity",
        list(OLD),
        schema="business",
    )
