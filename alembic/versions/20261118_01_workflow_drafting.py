"""workflow_drafting (Checkpoint A4: automated internal quotation drafting)

Revision ID: 20261118_01
Revises: 20261117_02
Create Date: 2026-11-18

Additive only, on ``orchestration.workflow_runs``; no K2-K5 object, RFQ or quote is touched.
Every existing row stays valid: the new columns default to 0 / NULL and every replaced CHECK
is a superset of the one it replaces.

- ``status`` (rule_01) admits ``drafting``: a ``completed`` / ``ready_for_draft`` run claimed by
  the quotation drafter (``workflow_drafter``). Only ``running`` or ``drafting`` may hold a
  lease (rule_03).
- ``draft_attempt_count`` (0..3, rule_08): drafting attempts, separate from the Supervisor's
  ``attempt_count``. Every claim of either phase advances ``claim_generation`` (rule_05).
- ``disposition`` (rule_06) admits ``draft_created`` and ``draft_refused``.
- ``quote_id``: the internal draft this run created, a foreign key to ``business.quotes``
  (RESTRICT). Linked only on a ``completed`` run (rule_09), and exactly when the disposition
  is ``draft_created`` (rule_10). ``uq_workflow_runs_quote_id``: one run per quote.
- ``ix_workflow_runs_claimable``: the runnable rows of both claim queries, oldest first.

No trigger or function: not a guard revision. Downgrade runs only on the verified test
target with ``allow_test_downgrade=true`` and only while no run is drafting, linked to a
quote or carries a drafting disposition or attempt.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic.util import CommandError

from alembic import op

revision: str = "20261118_01"
down_revision: Union[str, Sequence[str], None] = "20261117_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE, SCHEMA = "workflow_runs", "orchestration"
A1_DISPOSITIONS = (
    "'clarification_required','review_required','not_quotable','not_evaluated',"
    "'source_mismatch','ready_for_draft','technical_failure','configuration_error'"
)
TERMINAL = "status IN ('waiting_human','blocked','failed','completed')"
# name -> (A1 definition, A4 definition): each A4 rule admits everything the A1 rule did.
REPLACED = {
    "rule_01": (
        "status IN ('queued','running','waiting_human','blocked','failed','completed')",
        "status IN ('queued','running','drafting','waiting_human','blocked','failed','completed')",
    ),
    "rule_03": (
        "(lease_owner IS NULL) = (lease_expires_at IS NULL) "
        "AND (status = 'running' OR lease_owner IS NULL)",
        "(lease_owner IS NULL) = (lease_expires_at IS NULL) "
        "AND (status IN ('running','drafting') OR lease_owner IS NULL)",
    ),
    "rule_05": (
        "claim_generation >= attempt_count",
        "claim_generation >= attempt_count + draft_attempt_count",
    ),
    "rule_06": (
        f"disposition IS NULL OR (disposition IN ({A1_DISPOSITIONS}) AND {TERMINAL})",
        f"disposition IS NULL OR (disposition IN ({A1_DISPOSITIONS},'draft_created','draft_refused') AND {TERMINAL})",  # fmt: skip
    ),
}
ADDED = {
    "rule_08": "draft_attempt_count BETWEEN 0 AND 3",
    "rule_09": "quote_id IS NULL OR status = 'completed'",
    "rule_10": "(quote_id IS NOT NULL) = (COALESCE(disposition, '') = 'draft_created')",
}
QUOTE_FK = "fk_workflow_runs_quote_id_quotes"
QUOTE_INDEX = "uq_workflow_runs_quote_id"
CLAIM_INDEX = "ix_workflow_runs_claimable"
CLAIMABLE = (
    "status IN ('queued','running','drafting') "
    "OR (status = 'completed' AND disposition = 'ready_for_draft')"
)


def _replace(name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, schema=SCHEMA, type_="check")
    op.create_check_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, condition, schema=SCHEMA)


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "draft_attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),  # fmt: skip
        schema=SCHEMA,
    )
    op.add_column(TABLE, sa.Column("quote_id", sa.Text(), nullable=True), schema=SCHEMA)
    op.create_foreign_key(
        op.f(QUOTE_FK), TABLE, "quotes", ["quote_id"], ["quote_id"],
        source_schema=SCHEMA, referent_schema="business",
        ondelete="RESTRICT", onupdate="RESTRICT", match="SIMPLE",
    )  # fmt: skip
    for name, (_, condition) in REPLACED.items():
        _replace(name, condition)
    for name, condition in ADDED.items():
        op.create_check_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, condition, schema=SCHEMA)
    op.create_index(
        op.f(QUOTE_INDEX), TABLE, ["quote_id"], unique=True, schema=SCHEMA,
        postgresql_where=sa.text("quote_id IS NOT NULL"),
    )  # fmt: skip
    op.create_index(
        op.f(CLAIM_INDEX), TABLE, ["created_at", "workflow_run_id"], unique=False, schema=SCHEMA,
        postgresql_where=sa.text(CLAIMABLE),
    )  # fmt: skip


def downgrade() -> None:
    settings = op.get_bind().info.get("rfq_migration_settings")
    if settings is None or not settings.allow_test_downgrade or settings.database.target != "test":
        raise CommandError(
            "The workflow drafting downgrade drops run-to-quote links: only the verified test "
            "target with allow_test_downgrade=true may run it. Repair other databases forward."
        )
    in_use = op.get_bind().execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM orchestration.workflow_runs WHERE status = 'drafting' "
            "OR quote_id IS NOT NULL OR draft_attempt_count > 0 "
            "OR disposition IN ('draft_created','draft_refused'))"
        )
    )
    if in_use.scalar():
        raise CommandError("Workflow runs hold drafting state; downgrade refused.")
    op.drop_index(op.f(CLAIM_INDEX), table_name=TABLE, schema=SCHEMA)
    op.drop_index(op.f(QUOTE_INDEX), table_name=TABLE, schema=SCHEMA)
    for name in reversed(ADDED):
        op.drop_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, schema=SCHEMA, type_="check")
    for name, (condition, _) in reversed(REPLACED.items()):
        _replace(name, condition)
    op.drop_constraint(op.f(QUOTE_FK), TABLE, schema=SCHEMA, type_="foreignkey")
    op.drop_column(TABLE, "quote_id", schema=SCHEMA)
    op.drop_column(TABLE, "draft_attempt_count", schema=SCHEMA)
