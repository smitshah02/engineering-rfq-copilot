"""k4_approval_lifecycle (K4, Day 54D; written, not applied)

Revision ID: 20261116_03
Revises: 20261116_01
Create Date: 2026-11-16

The approved human lifecycle transitions on top of K2/K3 (docs/dev_log/day_54.md, §K and
the Day 54D decisions 1-4). K2's and K3's revision files, functions and pinned SHA-256 are
untouched. This revision:

- drops three K2 triggers (``k2_quotes_guard``, ``k2_quote_versions_guard`` and
  ``k2_approval_requests_immutable``; their functions stay defined and unused, so the
  downgrade recreates the triggers from them) and declares that in ``DROPPED_TRIGGERS``,
  which ``schema_contract.contract_guards`` applies in revision order;
- creates ``k4_quotes_guard``: K2's INSERT rule and revision transition, plus the human
  lifecycle: submit (draft/revised_draft -> pending_approval), approve (pending_approval ->
  approved), reject (pending_approval -> rejected), re-revise (rejected -> revised_draft)
  and release (approved -> released). Every transition changes only its named columns,
  bumps ``row_version`` by one and is bound to an ``approval_requests`` (or, for release,
  ``approval_history``) row written in the same transaction for the same version, dated
  ``transaction_timestamp()``. Nothing else ever changes a quote row;
- creates ``k4_quote_versions_guard``: K2's rules, plus ``approval_valid`` false -> true on
  the current version only when its approval request resolved ``approved`` in the same
  transaction;
- creates ``k4_approval_requests_guard``: a request resolves once (pending -> approved,
  rejected or superseded, ``resolved_by``/``resolved_at`` set, nothing else changed); UPDATE
  of anything else and DELETE are refused.

Trigger functions are SECURITY INVOKER with a fixed ``search_path`` and no PUBLIC EXECUTE;
triggers are ENABLE ALWAYS. ``FUNCTIONS``/``TRIGGERS`` are K4's own allowlist, pinned
separately in ``schema_contract.GUARD_FILE_SHA256``. Downgrade is test-only (as K3).
"""

import re
from typing import Sequence, Union

from alembic.util import CommandError

from alembic import op

revision: str = "20261116_03"
down_revision: Union[str, Sequence[str], None] = "20261116_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEARCH_PATH = "pg_catalog, pg_temp"

# K2 triggers this revision replaces (the K2 functions remain for the downgrade).
# fmt: off
DROPPED_TRIGGERS = (
    ("k2_quotes_guard", "quotes", "k2_quotes_guard", ("INSERT", "UPDATE", "DELETE")),
    ("k2_quote_versions_guard", "quote_versions", "k2_quote_versions_guard", ("INSERT", "UPDATE", "DELETE")),
    ("k2_approval_requests_immutable", "approval_requests", "k2_refuse_change", ("UPDATE", "DELETE")),
)
# fmt: on

FUNCTIONS = {
    "k4_quotes_guard": """
DECLARE
    bump boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- K2: a new quotation is an unsubmitted, unapproved, unreleased version-1 draft.
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
        bump := NEW.row_version = OLD.row_version + 1;
        -- Revision (K2, extended to a rejected quotation): the pointer moves by one.
        IF bump AND NEW.current_version = OLD.current_version + 1
           AND OLD.status IN ('draft', 'revised_draft', 'rejected')
           AND NEW.status = 'revised_draft'
           AND OLD.approved_version IS NULL AND OLD.released_at IS NULL
           AND (to_jsonb(NEW) - ARRAY['current_version', 'row_version', 'status'])
             = (to_jsonb(OLD) - ARRAY['current_version', 'row_version', 'status']) THEN
            RETURN NEW;
        END IF;
        -- Submit for review: bound to the pending request written in this transaction.
        IF bump AND OLD.status IN ('draft', 'revised_draft')
           AND NEW.status = 'pending_approval'
           AND NEW.submitted_by IS NOT NULL
           AND OLD.approved_version IS NULL AND OLD.released_at IS NULL
           AND (to_jsonb(NEW) - ARRAY['row_version', 'status', 'submitted_by'])
             = (to_jsonb(OLD) - ARRAY['row_version', 'status', 'submitted_by'])
           AND EXISTS (
               SELECT 1 FROM business.approval_requests r
               WHERE r.quote_id = NEW.quote_id AND r.quote_version = NEW.current_version
                 AND r.status = 'pending' AND r.requested_by = NEW.submitted_by
                 AND r.requested_at = transaction_timestamp()) THEN
            RETURN NEW;
        END IF;
        -- Approve: bound to the request this transaction resolved for this exact version.
        IF bump AND OLD.status = 'pending_approval' AND NEW.status = 'approved'
           AND NEW.approved_version = OLD.current_version
           AND NEW.approver_id IS NOT NULL AND NEW.approval_timestamp IS NOT NULL
           AND NEW.rejection_reason IS NULL AND OLD.released_at IS NULL
           AND (to_jsonb(NEW) - ARRAY['row_version', 'status', 'approved_version',
                                      'approver_id', 'approval_timestamp'])
             = (to_jsonb(OLD) - ARRAY['row_version', 'status', 'approved_version',
                                      'approver_id', 'approval_timestamp'])
           AND EXISTS (
               SELECT 1 FROM business.approval_requests r
               WHERE r.quote_id = NEW.quote_id AND r.quote_version = NEW.current_version
                 AND r.status = 'approved' AND r.resolved_by = NEW.approver_id
                 AND r.resolved_at = transaction_timestamp()) THEN
            RETURN NEW;
        END IF;
        -- Reject: the same binding; the reason is recorded, nothing else moves.
        IF bump AND OLD.status = 'pending_approval' AND NEW.status = 'rejected'
           AND NEW.rejection_reason IS NOT NULL
           AND OLD.approved_version IS NULL AND OLD.released_at IS NULL
           AND (to_jsonb(NEW) - ARRAY['row_version', 'status', 'rejection_reason'])
             = (to_jsonb(OLD) - ARRAY['row_version', 'status', 'rejection_reason'])
           AND EXISTS (
               SELECT 1 FROM business.approval_requests r
               WHERE r.quote_id = NEW.quote_id AND r.quote_version = NEW.current_version
                 AND r.status = 'rejected' AND r.resolved_at = transaction_timestamp()) THEN
            RETURN NEW;
        END IF;
        -- Release: bound to the 'released' history row written in this transaction.
        IF bump AND OLD.status = 'approved' AND NEW.status = 'released'
           AND OLD.released_at IS NULL AND NEW.released_by IS NOT NULL
           AND NEW.released_at IS NOT NULL
           AND (to_jsonb(NEW) - ARRAY['row_version', 'status', 'released_by', 'released_at'])
             = (to_jsonb(OLD) - ARRAY['row_version', 'status', 'released_by', 'released_at'])
           AND EXISTS (
               SELECT 1 FROM business.approval_history h
               WHERE h.quote_id = NEW.quote_id AND h.quote_version = OLD.approved_version
                 AND h.action = 'released' AND h.actor_id = NEW.released_by
                 AND h.action_at = transaction_timestamp()) THEN
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
    "k4_quote_versions_guard": """
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.created_xact := pg_current_xact_id();
        IF NOT NEW.approval_valid
           OR pg_has_role(current_user,
                          (SELECT c.relowner FROM pg_class c WHERE c.oid = TG_RELID),
                          'MEMBER') THEN
            RETURN NEW;
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        -- K2: superseded by a newer version.
        IF OLD.is_current AND NOT NEW.is_current
           AND (to_jsonb(NEW) - 'is_current') = (to_jsonb(OLD) - 'is_current') THEN
            RETURN NEW;
        END IF;
        -- K4: the current version becomes the approved one, once, with its request.
        IF OLD.is_current AND NEW.is_current AND NOT OLD.approval_valid AND NEW.approval_valid
           AND (to_jsonb(NEW) - 'approval_valid') = (to_jsonb(OLD) - 'approval_valid')
           AND EXISTS (
               SELECT 1 FROM business.approval_requests r
               WHERE r.quote_id = NEW.quote_id AND r.quote_version = NEW.version
                 AND r.status = 'approved' AND r.resolved_at = transaction_timestamp()) THEN
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
    "k4_approval_requests_guard": """
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.status = 'pending'
       AND NEW.status IN ('approved', 'rejected', 'superseded')
       AND NEW.resolved_by IS NOT NULL AND NEW.resolved_at IS NOT NULL
       AND (to_jsonb(NEW) - ARRAY['status', 'resolved_by', 'resolved_at'])
         = (to_jsonb(OLD) - ARRAY['status', 'resolved_by', 'resolved_at']) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING
        ERRCODE = 'integrity_constraint_violation',
        MESSAGE = format('%s on business.approval_requests is refused: a request resolves once',
                         TG_OP),
        CONSTRAINT = TG_NAME;
END;
""",
}

ALL_EVENTS = ("INSERT", "UPDATE", "DELETE")
# (trigger, business table, function, events); each is BEFORE <events> ... FOR EACH ROW.
# fmt: off
TRIGGERS = (
    ("k4_quotes_guard", "quotes", "k4_quotes_guard", ALL_EVENTS),
    ("k4_quote_versions_guard", "quote_versions", "k4_quote_versions_guard", ALL_EVENTS),
    ("k4_approval_requests_guard", "approval_requests", "k4_approval_requests_guard", ("UPDATE", "DELETE")),
)
# fmt: on
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def _checked(*names: str) -> None:
    # Static module constants only; asserted anyway before they reach DDL text.
    if not all(_IDENTIFIER.fullmatch(name) for name in names):
        raise CommandError("Unexpected identifier in a K4 definition.")


def _create(trigger: str, table: str, function: str, events: Sequence[str]) -> None:
    _checked(trigger, table, function)
    op.execute(
        f"CREATE TRIGGER {trigger} BEFORE {' OR '.join(events)} ON business.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION business.{function}()"
    )
    # Fire also under session_replication_role = replica (tgenabled 'A').
    op.execute(f"ALTER TABLE business.{table} ENABLE ALWAYS TRIGGER {trigger}")


def upgrade() -> None:
    for trigger, table, _, _ in DROPPED_TRIGGERS:
        _checked(trigger, table)
        op.execute(f"DROP TRIGGER {trigger} ON business.{table}")
    for name, body in FUNCTIONS.items():
        _checked(name)
        op.execute(
            f"CREATE FUNCTION business.{name}() RETURNS trigger LANGUAGE plpgsql "
            f"SECURITY INVOKER SET search_path = {SEARCH_PATH} AS $k4${body}$k4$"
        )
        op.execute(f"REVOKE ALL ON FUNCTION business.{name}() FROM PUBLIC")
        op.execute(f"COMMENT ON FUNCTION business.{name}() IS 'rfq-copilot:{revision}'")
    for trigger, table, function, events in TRIGGERS:
        _create(trigger, table, function, events)


def downgrade() -> None:
    settings = op.get_bind().info.get("rfq_migration_settings")
    allowed = settings is not None and settings.allow_test_downgrade
    if not allowed or settings.database.target != "test":
        raise CommandError(
            "K4 downgrade removes the lifecycle guards: only the verified test target with "
            "allow_test_downgrade=true may run it. Repair other databases forward."
        )
    for trigger, table, _, _ in reversed(TRIGGERS):
        _checked(trigger, table)
        op.execute(f"DROP TRIGGER {trigger} ON business.{table}")
    for name in reversed(FUNCTIONS):
        _checked(name)
        op.execute(f"DROP FUNCTION business.{name}()")
    for trigger, table, function, events in DROPPED_TRIGGERS:
        _create(trigger, table, function, events)  # K2's functions were never dropped
