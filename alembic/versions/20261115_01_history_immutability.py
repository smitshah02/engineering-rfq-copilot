"""history_immutability (K2, Day 54A; written, not applied)

Revision ID: 20261115_01
Revises: 20261111_06
Create Date: 2026-11-15

Database-enforced quotation history. The approved Day 54 design (docs/dev_log/day_54.md),
decisions N1 (version creation seal) and N5 (append-only receipts).

- ``quote_versions.created_xact xid8`` (nullable): the top-level transaction that inserted
  the version, stamped by the BEFORE INSERT trigger from ``pg_current_xact_id()`` (the
  top-level id also inside SAVEPOINTs). A supplied value is overwritten and the column can
  never change. Existing rows stay NULL: their creating transaction is unknown and is not
  invented, so they remain legacy, unverified and sealed (NULL never equals the current
  transaction).
- ``quote_versions``: INSERT with ``approval_valid`` only by the table owner (seeding of
  history); UPDATE only clears ``is_current`` (true -> false) with every other column equal;
  DELETE refused.
- ``quote_line_items``: INSERT only inside the transaction that created the parent version;
  UPDATE and DELETE refused.
- ``quotes``: INSERT only as an unsubmitted, unapproved, unreleased version-1 draft, except by
  the table owner (seeding of history); UPDATE only as a Day 52 revision (pointer + 1,
  row_version + 1, draft or revised_draft -> revised_draft, never approved or released, all
  else equal); DELETE refused. Approval and release transitions are added by their own
  later revisions. The owner exemption on INSERT grants nothing new: the owner can disable
  triggers anyway, which is why the application must never use it.
- Every trigger is ``ENABLE ALWAYS`` (``tgenabled = 'A'``): it fires even under
  ``session_replication_role = replica``.
- ``audit_events``, ``idempotency_records``, ``approval_history``, ``approval_requests``,
  ``pricing_calculations``, ``rfqs``, ``rfq_line_items``: UPDATE and DELETE refused.

Every refusal is SQLSTATE 23000 (integrity_constraint_violation) naming the trigger as its
constraint, so SQLAlchemy raises IntegrityError and callers classify by name. Functions are
SECURITY INVOKER with a fixed ``search_path``, owned by the migration owner, and EXECUTE is
revoked from PUBLIC (firing a trigger does not check it). No TRUNCATE trigger: TRUNCATE is
withheld from the runtime role by privileges (docs/database/k2_runtime_roles.sql), and the
guarded test teardown truncates as the owner. Triggers do not bind the table owner or a
superuser, who can disable them; that is why the application must use the runtime role.

``FUNCTIONS`` and ``TRIGGERS`` are also the exact allowlist the schema preflight compares the
live catalog with (``schema_contract.history_guards``). Downgrade removes everything again,
but only on the verified test target with explicit opt-in: it lowers protection.
"""

import re
from typing import Sequence, Union

from alembic.util import CommandError

from alembic import op

revision: str = "20261115_01"
down_revision: Union[str, Sequence[str], None] = "20261111_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEARCH_PATH = "pg_catalog, pg_temp"

# name -> PL/pgSQL body, stored verbatim as pg_proc.prosrc.
FUNCTIONS = {
    "k2_refuse_change": """
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'integrity_constraint_violation',
        MESSAGE = format('%s on business.%s is refused: recorded history is immutable',
                         TG_OP, TG_TABLE_NAME),
        CONSTRAINT = TG_NAME;
END;
""",
    "k2_quote_versions_guard": """
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- The creating top-level transaction; a supplied value is never kept.
        NEW.created_xact := pg_current_xact_id();
        -- Only the table owner (migrations, seeding of history) may insert an approved
        -- version; every other role inserts unapproved versions only.
        IF NOT NEW.approval_valid
           OR pg_has_role(current_user,
                          (SELECT c.relowner FROM pg_class c WHERE c.oid = TG_RELID),
                          'MEMBER') THEN
            RETURN NEW;
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        -- Superseded by a newer version: the only permitted change.
        IF OLD.is_current AND NOT NEW.is_current
           AND (to_jsonb(NEW) - 'is_current') = (to_jsonb(OLD) - 'is_current') THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION USING
        ERRCODE = 'integrity_constraint_violation',
        MESSAGE = format('%s on business.quote_versions is refused: version history is immutable',
                         TG_OP),
        CONSTRAINT = TG_NAME;
END;
""",
    "k2_quote_line_items_guard": """
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- Only the transaction that created the version may add its lines.
        IF EXISTS (
            SELECT 1 FROM business.quote_versions v
            WHERE v.quote_id = NEW.quote_id
              AND v.version = NEW.quote_version
              AND v.created_xact = pg_current_xact_id()
        ) THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION USING
        ERRCODE = 'integrity_constraint_violation',
        MESSAGE = format('%s on business.quote_line_items is refused: lines are sealed with their version',
                         TG_OP),
        CONSTRAINT = TG_NAME;
END;
""",
    "k2_quotes_guard": """
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- A new quotation is an unsubmitted, unapproved, unreleased version-1 draft.
        -- Historical states are imported only by the table owner (seeding).
        IF (NEW.status = 'draft' AND NEW.current_version = 1 AND NEW.row_version = 1
            AND NEW.submitted_by IS NULL AND NEW.approved_version IS NULL
            AND NEW.approver_id IS NULL AND NEW.approval_timestamp IS NULL
            AND NEW.rejection_reason IS NULL AND NEW.released_by IS NULL
            AND NEW.released_at IS NULL)
           OR pg_has_role(current_user,
                          (SELECT c.relowner FROM pg_class c WHERE c.oid = TG_RELID),
                          'MEMBER') THEN
            RETURN NEW;
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        -- A Day 52 revision moves the pointer of an unapproved, unreleased draft.
        IF NEW.current_version = OLD.current_version + 1
           AND NEW.row_version = OLD.row_version + 1
           AND OLD.status IN ('draft', 'revised_draft')
           AND NEW.status = 'revised_draft'
           AND OLD.approved_version IS NULL
           AND OLD.released_at IS NULL
           AND (to_jsonb(NEW) - ARRAY['current_version', 'row_version', 'status'])
             = (to_jsonb(OLD) - ARRAY['current_version', 'row_version', 'status']) THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION USING
        ERRCODE = 'integrity_constraint_violation',
        MESSAGE = format('%s on business.quotes is refused: no approved lifecycle transition',
                         TG_OP),
        CONSTRAINT = TG_NAME;
END;
""",
}

ALL_EVENTS = ("INSERT", "UPDATE", "DELETE")
CHANGES = ("UPDATE", "DELETE")
APPEND_ONLY = (
    "audit_events",
    "idempotency_records",
    "approval_history",
    "approval_requests",
    "pricing_calculations",
    "rfqs",
    "rfq_line_items",
)
# (trigger, business table, function, events); each is BEFORE <events> ... FOR EACH ROW.
TRIGGERS = (
    ("k2_quote_versions_guard", "quote_versions", "k2_quote_versions_guard", ALL_EVENTS),
    ("k2_quote_line_items_guard", "quote_line_items", "k2_quote_line_items_guard", ALL_EVENTS),
    ("k2_quotes_guard", "quotes", "k2_quotes_guard", ALL_EVENTS),
    *((f"k2_{table}_immutable", table, "k2_refuse_change", CHANGES) for table in APPEND_ONLY),
)
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def _checked(*names: str) -> None:
    # Static module constants only; asserted anyway before they reach DDL text.
    if not all(_IDENTIFIER.fullmatch(name) for name in names):
        raise CommandError("Unexpected identifier in a K2 definition.")


def upgrade() -> None:
    op.execute("ALTER TABLE business.quote_versions ADD COLUMN created_xact xid8")
    for name, body in FUNCTIONS.items():
        _checked(name)
        op.execute(
            f"CREATE FUNCTION business.{name}() RETURNS trigger LANGUAGE plpgsql "
            f"SECURITY INVOKER SET search_path = {SEARCH_PATH} AS $k2${body}$k2$"
        )
        op.execute(f"REVOKE ALL ON FUNCTION business.{name}() FROM PUBLIC")
        op.execute(f"COMMENT ON FUNCTION business.{name}() IS 'rfq-copilot:{revision}'")
    for trigger, table, function, events in TRIGGERS:
        _checked(trigger, table, function)
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE {' OR '.join(events)} ON business.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION business.{function}()"
        )
        # Fire also under session_replication_role = replica (tgenabled 'A').
        op.execute(f"ALTER TABLE business.{table} ENABLE ALWAYS TRIGGER {trigger}")


def downgrade() -> None:
    settings = op.get_bind().info.get("rfq_migration_settings")
    allowed = settings is not None and settings.allow_test_downgrade
    if not allowed or settings.database.target != "test":
        raise CommandError(
            "K2 downgrade removes history protection: only the verified test target with "
            "allow_test_downgrade=true may run it. Repair other databases forward."
        )
    for trigger, table, _, _ in reversed(TRIGGERS):
        _checked(trigger, table)
        op.execute(f"DROP TRIGGER {trigger} ON business.{table}")
    for name in reversed(FUNCTIONS):
        _checked(name)
        op.execute(f"DROP FUNCTION business.{name}()")
    op.execute("ALTER TABLE business.quote_versions DROP COLUMN created_xact")
