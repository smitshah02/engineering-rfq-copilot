"""workflow_queue (Checkpoints A1/A2: durable RFQ processing queue, workflow service role)

Revision ID: 20261117_02
Revises: 20261117_01
Create Date: 2026-11-17

Additive only. Nothing wrote ``orchestration.workflow_runs`` before this revision, but any
existing row is preserved: every new column is nullable or has a default, and every new
CHECK holds for a legacy row (a ``running`` row without a lease stays valid).

- ``workflow_runs.status`` (``ck_workflow_runs_rule_01``) admits ``queued``.
- Lease and fencing: ``lease_owner``, ``lease_expires_at`` (both or neither; only ``running``
  may hold one, rule_03), ``attempt_count`` (0..3, rule_04) and ``claim_generation`` (the
  fencing token, incremented by every claim; never below ``attempt_count``, rule_05).
- ``disposition`` (rule_06, fixed vocabulary, terminal statuses only) and ``last_error``
  (rule_07, a fixed lowercase code): allow-listed outcome metadata, never text or evidence.
- ``uq_workflow_runs_active_rfq``: at most one ``queued``/``running`` run per RFQ.
- ``business.roles`` (``ck_roles_rule_01``) admits the internal ``rfq_workflow_agent``
  service role, exactly as 20261117_01 admitted ``customer``. Its row and grants come only
  from the pinned ``rbac-policy-v4`` overlay; no account is created here.

No trigger or function: not a guard revision. Downgrade runs only on the verified test
target with ``allow_test_downgrade=true`` and only while no workflow run and no
``rfq_workflow_agent`` role row or user exist.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic.util import CommandError

from alembic import op

revision: str = "20261117_02"
down_revision: Union[str, Sequence[str], None] = "20261117_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE, SCHEMA = "workflow_runs", "orchestration"
STATUSES = "'running','waiting_human','blocked','failed','completed'"
ROLES = (
    "'sales_representative','estimator','manufacturing_engineer','sales_manager',"
    "'administrator','auditor','identity_operator','customer'"
)
DISPOSITIONS = (
    "'clarification_required','review_required','not_quotable','not_evaluated',"
    "'source_mismatch','ready_for_draft','technical_failure','configuration_error'"
)
CHECKS = {
    "rule_03": (
        "(lease_owner IS NULL) = (lease_expires_at IS NULL) "
        "AND (status = 'running' OR lease_owner IS NULL)"
    ),
    "rule_04": "attempt_count BETWEEN 0 AND 3",
    "rule_05": "claim_generation >= attempt_count",
    "rule_06": (
        f"disposition IS NULL OR (disposition IN ({DISPOSITIONS}) "
        "AND status IN ('waiting_human','blocked','failed','completed'))"
    ),
    "rule_07": "last_error IS NULL OR last_error ~ '^[a-z_]{1,64}$'",
    "lease_owner_identity": "length(btrim(lease_owner)) > 0 AND length(lease_owner) <= 128",
}
ACTIVE_INDEX = "uq_workflow_runs_active_rfq"


def _check(name: str, condition: str, table=TABLE, schema=SCHEMA) -> None:
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition, schema=schema)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_workflow_runs_rule_01"), TABLE, schema=SCHEMA, type_="check")
    _check("rule_01", f"status IN ('queued',{STATUSES})")
    op.add_column(TABLE, sa.Column("lease_owner", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column(
        TABLE,
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        TABLE,
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema=SCHEMA,
    )
    op.add_column(
        TABLE,
        sa.Column("claim_generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        schema=SCHEMA,
    )
    op.add_column(TABLE, sa.Column("disposition", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column(TABLE, sa.Column("last_error", sa.Text(), nullable=True), schema=SCHEMA)
    for name, condition in CHECKS.items():
        _check(name, condition)
    op.create_index(
        op.f(ACTIVE_INDEX),
        TABLE,
        ["rfq_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.drop_constraint(op.f("ck_roles_rule_01"), "roles", schema="business", type_="check")
    _check("rule_01", f"role IN ({ROLES},'rfq_workflow_agent')", "roles", "business")


def downgrade() -> None:
    settings = op.get_bind().info.get("rfq_migration_settings")
    if settings is None or not settings.allow_test_downgrade or settings.database.target != "test":
        raise CommandError(
            "The workflow queue downgrade drops queue state: only the verified test target "
            "with allow_test_downgrade=true may run it. Repair other databases forward."
        )
    in_use = op.get_bind().execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM orchestration.workflow_runs) "
            "OR EXISTS (SELECT 1 FROM business.roles WHERE role = 'rfq_workflow_agent') "
            "OR EXISTS (SELECT 1 FROM business.users WHERE role = 'rfq_workflow_agent')"
        )
    )
    if in_use.scalar():
        raise CommandError("Workflow runs or the service role exist; downgrade refused.")
    op.drop_constraint(op.f("ck_roles_rule_01"), "roles", schema="business", type_="check")
    _check("rule_01", f"role IN ({ROLES})", "roles", "business")
    op.drop_index(op.f(ACTIVE_INDEX), table_name=TABLE, schema=SCHEMA)
    for name in reversed(CHECKS):
        op.drop_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, schema=SCHEMA, type_="check")
    for column in ("last_error", "disposition", "claim_generation", "attempt_count", "lease_expires_at", "lease_owner"):  # fmt: skip
        op.drop_column(TABLE, column, schema=SCHEMA)
    op.drop_constraint(op.f("ck_workflow_runs_rule_01"), TABLE, schema=SCHEMA, type_="check")
    _check("rule_01", f"status IN ({STATUSES})")
