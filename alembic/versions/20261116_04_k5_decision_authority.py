"""k5_decision_authority (K5, Day 55; written, not applied)

Revision ID: 20261116_04
Revises: 20261116_03
Create Date: 2026-11-16

Human decisions get their own database authority (docs/dev_log/day_55.md, K5). K2, K3 and
K4 revision files, functions and pinned SHA-256 are untouched. This revision:

- drops K4's three triggers (``k4_quotes_guard``, ``k4_quote_versions_guard``,
  ``k4_approval_requests_guard``; their functions stay defined and unused, so the downgrade
  recreates the triggers from them) and declares that in ``DROPPED_TRIGGERS``;
- binds approve, reject, release, ``approval_valid``, request resolution and the decision
  history rows to sessions that are members of the NOLOGIN group ``rfq_quote_decision``
  (``docs/database/k5_decision_roles.sql``). The ordinary runtime role is not a member and
  also loses the column grants, so a direct UPDATE by it is refused twice (42501 / 23000).
  A missing group role makes every decision refused (``to_regrole`` is NULL): fail closed;
- re-checks, in the same transaction, what a decision rests on: the resolver/approver is an
  active user whose role is the request's required role and is granted the operation, and
  differs from the requester, the quote creator and the version creator; the version is
  current, ``complete`` and unexpired (database time, UTC); the releaser holds
  ``release_quote`` and differs from the approver (Day 55 policy); every decision is bound
  to its history row, request and quote written at ``transaction_timestamp()``;
- lets a genuinely rejected quotation (a ``rejected`` request for its current version) be
  revised, clearing ``submitted_by`` and ``rejection_reason`` (the reason stays in
  ``approval_history``), so the new version can be submitted and freshly approved;
- guards INSERT on ``approval_requests`` (pending, now, a requester granted
  ``request_approval``) and on ``approval_history`` (each action bound to its request or
  quote state; owner-written seed history exempt, as K2);
- binds at COMMIT (two deferred constraint triggers, as K3's line checks): every decision
  history row and every request resolution must end the transaction matched by the
  quotation's finalized state (submitted -> pending_approval by that actor; approved ->
  approved by that actor at that time with a valid version; rejected -> rejected;
  released -> released by that actor at that time). A row the BEFORE guards accept but whose
  transition never happens cannot commit. The services force the check before finishing.

What it cannot do: authenticate a person. The database sees a role, not a human. Human
authority is the decision process (``rfq_copilot.api.decision_app``: verified bearer token,
users/grants re-read) holding the only decision credential. Whoever holds that credential
can still record decisions for any valid sales manager.

Trigger functions are SECURITY INVOKER with a fixed ``search_path`` and no PUBLIC EXECUTE;
triggers are ENABLE ALWAYS. ``FUNCTIONS``/``TRIGGERS`` are K5's allowlist, pinned in
``schema_contract.GUARD_FILE_SHA256``. No table, column, CHECK, key or index changes; the two
constraint triggers add two ``pg_constraint`` rows (contype 't'), so the shape digest is
re-measured. Downgrade is test-only (as K3/K4).
"""

import re
from typing import Sequence, Union

from alembic.util import CommandError

from alembic import op

revision: str = "20261116_04"
down_revision: Union[str, Sequence[str], None] = "20261116_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEARCH_PATH = "pg_catalog, pg_temp"

# K4 triggers this revision replaces (the K4 functions remain for the downgrade).
# fmt: off
DROPPED_TRIGGERS = (
    ("k4_quotes_guard", "quotes", "k4_quotes_guard", ("INSERT", "UPDATE", "DELETE")),
    ("k4_quote_versions_guard", "quote_versions", "k4_quote_versions_guard", ("INSERT", "UPDATE", "DELETE")),
    ("k4_approval_requests_guard", "approval_requests", "k4_approval_requests_guard", ("UPDATE", "DELETE")),
)
# fmt: on

FUNCTIONS = {
    "k5_quotes_guard": """
DECLARE
    bump boolean;
    decision boolean := COALESCE(
        pg_has_role(current_user, to_regrole('rfq_quote_decision')::oid, 'MEMBER'), false);
    today date := (transaction_timestamp() AT TIME ZONE 'UTC')::date;
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
        -- Revision (K2/K4): the pointer moves by one, nothing else changes.
        IF bump AND NEW.current_version = OLD.current_version + 1
           AND OLD.status IN ('draft', 'revised_draft') AND NEW.status = 'revised_draft'
           AND OLD.approved_version IS NULL AND OLD.released_at IS NULL
           AND (to_jsonb(NEW) - ARRAY['current_version', 'row_version', 'status'])
             = (to_jsonb(OLD) - ARRAY['current_version', 'row_version', 'status']) THEN
            RETURN NEW;
        END IF;
        -- K5: revision of a genuinely rejected quotation clears submission and rejection.
        IF bump AND NEW.current_version = OLD.current_version + 1
           AND OLD.status = 'rejected' AND NEW.status = 'revised_draft'
           AND NEW.submitted_by IS NULL AND NEW.rejection_reason IS NULL
           AND OLD.approved_version IS NULL AND OLD.released_at IS NULL
           AND (to_jsonb(NEW) - ARRAY['current_version', 'row_version', 'status',
                                      'submitted_by', 'rejection_reason'])
             = (to_jsonb(OLD) - ARRAY['current_version', 'row_version', 'status',
                                      'submitted_by', 'rejection_reason'])
           AND EXISTS (
               SELECT 1 FROM business.approval_requests r
               WHERE r.quote_id = OLD.quote_id AND r.quote_version = OLD.current_version
                 AND r.status = 'rejected') THEN
            RETURN NEW;
        END IF;
        -- Submit (application role): bound to the pending request written in this transaction.
        IF bump AND OLD.status IN ('draft', 'revised_draft')
           AND NEW.status = 'pending_approval'
           AND NEW.submitted_by IS NOT NULL AND OLD.rejection_reason IS NULL
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
        -- Approve (decision role): an entitled, separate approver; current, complete,
        -- unexpired, flagged version; request and attested history row of this transaction.
        IF decision AND bump AND OLD.status = 'pending_approval' AND NEW.status = 'approved'
           AND NEW.approved_version = OLD.current_version
           AND NEW.approver_id IS NOT NULL
           AND NEW.approval_timestamp = transaction_timestamp()
           AND NEW.rejection_reason IS NULL AND OLD.released_at IS NULL
           AND NEW.approver_id <> OLD.created_by
           AND (to_jsonb(NEW) - ARRAY['row_version', 'status', 'approved_version',
                                      'approver_id', 'approval_timestamp'])
             = (to_jsonb(OLD) - ARRAY['row_version', 'status', 'approved_version',
                                      'approver_id', 'approval_timestamp'])
           AND EXISTS (
               SELECT 1 FROM business.approval_requests r
               JOIN business.quote_versions v
                 ON v.quote_id = r.quote_id AND v.version = r.quote_version
               JOIN business.users u
                 ON u.user_id = r.resolved_by AND u.active AND u.role = r.required_role
               JOIN business.role_permissions p
                 ON p.role = u.role AND p.permission = 'approve_quote' AND p.allowed
               JOIN business.approval_history h
                 ON h.approval_request_id = r.approval_request_id
                AND h.quote_id = r.quote_id AND h.quote_version = r.quote_version
                AND h.action = 'approved' AND h.actor_id = r.resolved_by
                AND h.action_at = transaction_timestamp()
               WHERE r.quote_id = NEW.quote_id AND r.quote_version = NEW.current_version
                 AND r.status = 'approved' AND r.resolved_by = NEW.approver_id
                 AND r.resolved_at = transaction_timestamp()
                 AND r.requested_by <> NEW.approver_id AND v.created_by <> NEW.approver_id
                 AND v.is_current AND v.approval_valid
                 AND v.snapshot_completeness = 'complete' AND v.expiry_date >= today) THEN
            RETURN NEW;
        END IF;
        -- Reject (decision role): an entitled, separate resolver and its history row.
        IF decision AND bump AND OLD.status = 'pending_approval' AND NEW.status = 'rejected'
           AND NEW.rejection_reason IS NOT NULL
           AND OLD.approved_version IS NULL AND OLD.released_at IS NULL
           AND (to_jsonb(NEW) - ARRAY['row_version', 'status', 'rejection_reason'])
             = (to_jsonb(OLD) - ARRAY['row_version', 'status', 'rejection_reason'])
           AND EXISTS (
               SELECT 1 FROM business.approval_requests r
               JOIN business.quote_versions v
                 ON v.quote_id = r.quote_id AND v.version = r.quote_version
               JOIN business.users u
                 ON u.user_id = r.resolved_by AND u.active AND u.role = r.required_role
               JOIN business.role_permissions p
                 ON p.role = u.role AND p.permission = 'reject_quote' AND p.allowed
               JOIN business.approval_history h
                 ON h.approval_request_id = r.approval_request_id
                AND h.quote_id = r.quote_id AND h.quote_version = r.quote_version
                AND h.action = 'rejected' AND h.actor_id = r.resolved_by
                AND h.action_at = transaction_timestamp()
               WHERE r.quote_id = NEW.quote_id AND r.quote_version = NEW.current_version
                 AND r.status = 'rejected' AND r.resolved_at = transaction_timestamp()
                 AND r.resolved_by <> r.requested_by AND r.resolved_by <> OLD.created_by
                 AND r.resolved_by <> v.created_by) THEN
            RETURN NEW;
        END IF;
        -- Release (decision role): an entitled releaser other than the approver, the
        -- approved request and the 'released' history row of this transaction, and a still
        -- current, valid, unexpired approved version.
        IF decision AND bump AND OLD.status = 'approved' AND NEW.status = 'released'
           AND OLD.released_at IS NULL AND NEW.released_by IS NOT NULL
           AND NEW.released_at = transaction_timestamp()
           AND NEW.released_by <> OLD.approver_id
           AND (to_jsonb(NEW) - ARRAY['row_version', 'status', 'released_by', 'released_at'])
             = (to_jsonb(OLD) - ARRAY['row_version', 'status', 'released_by', 'released_at'])
           AND EXISTS (
               SELECT 1 FROM business.approval_history h
               JOIN business.approval_requests r
                 ON r.approval_request_id = h.approval_request_id
                AND r.quote_id = h.quote_id AND r.quote_version = h.quote_version
               JOIN business.quote_versions v
                 ON v.quote_id = h.quote_id AND v.version = h.quote_version
               JOIN business.users u ON u.user_id = h.actor_id AND u.active
               JOIN business.role_permissions p
                 ON p.role = u.role AND p.permission = 'release_quote' AND p.allowed
               WHERE h.quote_id = NEW.quote_id AND h.quote_version = OLD.approved_version
                 AND h.action = 'released' AND h.actor_id = NEW.released_by
                 AND h.action_at = transaction_timestamp()
                 AND r.status = 'approved' AND r.resolved_by = OLD.approver_id
                 AND v.version = OLD.current_version AND v.is_current AND v.approval_valid
                 AND v.snapshot_completeness = 'complete' AND v.expiry_date >= today) THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION USING
        ERRCODE = 'integrity_constraint_violation',
        MESSAGE = format('%s on business.quotes is refused: no authorized lifecycle transition',
                         TG_OP),
        CONSTRAINT = TG_NAME;
END;
""",
    "k5_quote_versions_guard": """
DECLARE
    decision boolean := COALESCE(
        pg_has_role(current_user, to_regrole('rfq_quote_decision')::oid, 'MEMBER'), false);
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
        -- K5: the current version becomes the approved one, once, by the decision role.
        IF decision AND OLD.is_current AND NEW.is_current
           AND NOT OLD.approval_valid AND NEW.approval_valid
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
    "k5_approval_requests_guard": """
DECLARE
    decision boolean := COALESCE(
        pg_has_role(current_user, to_regrole('rfq_quote_decision')::oid, 'MEMBER'), false);
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- A new request is pending, dated now, by a user granted request_approval.
        IF pg_has_role(current_user,
                       (SELECT c.relowner FROM pg_class c WHERE c.oid = TG_RELID), 'MEMBER')
           OR (NEW.status = 'pending' AND NEW.resolved_by IS NULL AND NEW.resolved_at IS NULL
               AND NEW.requested_at = transaction_timestamp()
               AND EXISTS (
                   SELECT 1 FROM business.users u
                   JOIN business.role_permissions p
                     ON p.role = u.role AND p.permission = 'request_approval' AND p.allowed
                   WHERE u.user_id = NEW.requested_by AND u.active)) THEN
            RETURN NEW;
        END IF;
    END IF;
    -- Resolution, once, by the decision role, for an entitled resolver other than the requester.
    IF TG_OP = 'UPDATE' AND decision AND OLD.status = 'pending'
       AND NEW.status IN ('approved', 'rejected')
       AND NEW.resolved_by IS NOT NULL AND NEW.resolved_at = transaction_timestamp()
       AND NEW.resolved_by <> OLD.requested_by
       AND (to_jsonb(NEW) - ARRAY['status', 'resolved_by', 'resolved_at'])
         = (to_jsonb(OLD) - ARRAY['status', 'resolved_by', 'resolved_at'])
       AND EXISTS (
           SELECT 1 FROM business.users u
           JOIN business.role_permissions p ON p.role = u.role AND p.allowed
            AND p.permission = CASE NEW.status WHEN 'approved' THEN 'approve_quote'
                                               ELSE 'reject_quote' END
           WHERE u.user_id = NEW.resolved_by AND u.active AND u.role = OLD.required_role) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING
        ERRCODE = 'integrity_constraint_violation',
        MESSAGE = format('%s on business.approval_requests is refused: no authorized resolution',
                         TG_OP),
        CONSTRAINT = TG_NAME;
END;
""",
    "k5_approval_history_guard": """
DECLARE
    decision boolean := COALESCE(
        pg_has_role(current_user, to_regrole('rfq_quote_decision')::oid, 'MEMBER'), false);
BEGIN
    -- Seeded historical rows: the table owner, as K2 (it can disable triggers anyway).
    IF pg_has_role(current_user,
                   (SELECT c.relowner FROM pg_class c WHERE c.oid = TG_RELID), 'MEMBER') THEN
        RETURN NEW;
    END IF;
    IF NEW.action_at = transaction_timestamp() AND NEW.approval_request_id IS NOT NULL THEN
        -- Submitted: the requester's own pending request of this transaction.
        IF NEW.action = 'submitted' AND EXISTS (
               SELECT 1 FROM business.approval_requests r
               WHERE r.approval_request_id = NEW.approval_request_id
                 AND r.quote_id = NEW.quote_id AND r.quote_version = NEW.quote_version
                 AND r.status = 'pending' AND r.requested_by = NEW.actor_id
                 AND r.requested_at = transaction_timestamp()) THEN
            RETURN NEW;
        END IF;
        -- Approved / rejected: the decision role, the request resolved so by this actor now.
        IF decision AND NEW.action IN ('approved', 'rejected') AND EXISTS (
               SELECT 1 FROM business.approval_requests r
               WHERE r.approval_request_id = NEW.approval_request_id
                 AND r.quote_id = NEW.quote_id AND r.quote_version = NEW.quote_version
                 AND r.status = NEW.action AND r.resolved_by = NEW.actor_id
                 AND r.resolved_at = transaction_timestamp()) THEN
            RETURN NEW;
        END IF;
        -- Released: the decision role, an entitled releaser other than the approver, for
        -- the approved request of the quotation's approved version.
        IF decision AND NEW.action = 'released' AND EXISTS (
               SELECT 1 FROM business.quotes q
               JOIN business.approval_requests r
                 ON r.approval_request_id = NEW.approval_request_id
                AND r.quote_id = q.quote_id AND r.quote_version = q.approved_version
               JOIN business.users u ON u.user_id = NEW.actor_id AND u.active
               JOIN business.role_permissions p
                 ON p.role = u.role AND p.permission = 'release_quote' AND p.allowed
               WHERE q.quote_id = NEW.quote_id AND q.status = 'approved'
                 AND q.approved_version = NEW.quote_version AND q.released_at IS NULL
                 AND q.approver_id <> NEW.actor_id
                 AND r.status = 'approved' AND r.resolved_by = q.approver_id) THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION USING
        ERRCODE = 'integrity_constraint_violation',
        MESSAGE = format('%s on business.approval_history is refused: no bound decision', TG_OP),
        CONSTRAINT = TG_NAME;
END;
""",
    "k5_approval_history_bound": """
DECLARE
    bound boolean;
BEGIN
    -- Deferred to COMMIT: the quotation must end the transaction in the state this row records.
    IF pg_has_role(current_user,
                   (SELECT c.relowner FROM pg_class c WHERE c.oid = TG_RELID), 'MEMBER') THEN
        RETURN NULL;  -- seeded historical rows (the owner), as K2
    END IF;
    bound := CASE NEW.action
        WHEN 'submitted' THEN EXISTS (
            SELECT 1 FROM business.quotes q
            WHERE q.quote_id = NEW.quote_id AND q.current_version = NEW.quote_version
              AND q.status = 'pending_approval' AND q.submitted_by = NEW.actor_id)
        WHEN 'approved' THEN EXISTS (
            SELECT 1 FROM business.quotes q
            JOIN business.quote_versions v
              ON v.quote_id = q.quote_id AND v.version = q.approved_version
            WHERE q.quote_id = NEW.quote_id AND q.status = 'approved'
              AND q.approved_version = NEW.quote_version AND q.approver_id = NEW.actor_id
              AND q.approval_timestamp = NEW.action_at AND v.approval_valid)
        WHEN 'rejected' THEN EXISTS (
            SELECT 1 FROM business.quotes q
            WHERE q.quote_id = NEW.quote_id AND q.current_version = NEW.quote_version
              AND q.status = 'rejected' AND q.rejection_reason IS NOT NULL)
        WHEN 'released' THEN EXISTS (
            SELECT 1 FROM business.quotes q
            WHERE q.quote_id = NEW.quote_id AND q.status = 'released'
              AND q.approved_version = NEW.quote_version AND q.released_by = NEW.actor_id
              AND q.released_at = NEW.action_at)
        ELSE false
    END;
    IF bound THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION USING
        ERRCODE = 'integrity_constraint_violation',
        MESSAGE = 'approval_history row without its quotation transition is refused at commit',
        CONSTRAINT = TG_NAME;
END;
""",
    "k5_approval_request_bound": """
DECLARE
    bound boolean;
BEGIN
    -- Deferred to COMMIT: a resolution needs its history row and the quotation's decision.
    bound := CASE NEW.status
        WHEN 'approved' THEN EXISTS (
            SELECT 1 FROM business.approval_history h
            WHERE h.approval_request_id = NEW.approval_request_id
              AND h.quote_id = NEW.quote_id AND h.quote_version = NEW.quote_version
              AND h.action = 'approved' AND h.actor_id = NEW.resolved_by
              AND h.action_at = NEW.resolved_at)
          AND EXISTS (
            SELECT 1 FROM business.quotes q
            WHERE q.quote_id = NEW.quote_id AND q.status = 'approved'
              AND q.approved_version = NEW.quote_version AND q.approver_id = NEW.resolved_by)
        WHEN 'rejected' THEN EXISTS (
            SELECT 1 FROM business.approval_history h
            WHERE h.approval_request_id = NEW.approval_request_id
              AND h.quote_id = NEW.quote_id AND h.quote_version = NEW.quote_version
              AND h.action = 'rejected' AND h.actor_id = NEW.resolved_by
              AND h.action_at = NEW.resolved_at)
          AND EXISTS (
            SELECT 1 FROM business.quotes q
            WHERE q.quote_id = NEW.quote_id AND q.current_version = NEW.quote_version
              AND q.status = 'rejected')
        ELSE false
    END;
    IF bound THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION USING
        ERRCODE = 'integrity_constraint_violation',
        MESSAGE = 'approval_requests resolution without its decision is refused at commit',
        CONSTRAINT = TG_NAME;
END;
""",
}

ALL_EVENTS = ("INSERT", "UPDATE", "DELETE")
# (trigger, business table, function, events, kind): BEFORE ... FOR EACH ROW ("before"), or
# an AFTER ... FOR EACH ROW constraint trigger, DEFERRABLE INITIALLY DEFERRED ("deferred").
# fmt: off
TRIGGERS = (
    ("k5_quotes_guard", "quotes", "k5_quotes_guard", ALL_EVENTS, "before"),
    ("k5_quote_versions_guard", "quote_versions", "k5_quote_versions_guard", ALL_EVENTS, "before"),
    ("k5_approval_requests_guard", "approval_requests", "k5_approval_requests_guard", ALL_EVENTS, "before"),
    ("k5_approval_history_guard", "approval_history", "k5_approval_history_guard", ("INSERT",), "before"),
    ("k5_approval_history_bound", "approval_history", "k5_approval_history_bound", ("INSERT",), "deferred"),
    ("k5_approval_request_bound", "approval_requests", "k5_approval_request_bound", ("UPDATE",), "deferred"),
)
# fmt: on
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def _checked(*names: str) -> None:
    # Static module constants only; asserted anyway before they reach DDL text.
    if not all(_IDENTIFIER.fullmatch(name) for name in names):
        raise CommandError("Unexpected identifier in a K5 definition.")


def _create(
    trigger: str, table: str, function: str, events: Sequence[str], kind: str = "before"
) -> None:
    _checked(trigger, table, function)
    if kind == "deferred":
        op.execute(
            f"CREATE CONSTRAINT TRIGGER {trigger} AFTER {' OR '.join(events)} "
            f"ON business.{table} DEFERRABLE INITIALLY DEFERRED "
            f"FOR EACH ROW EXECUTE FUNCTION business.{function}()"
        )
    else:
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
            f"SECURITY INVOKER SET search_path = {SEARCH_PATH} AS $k5${body}$k5$"
        )
        op.execute(f"REVOKE ALL ON FUNCTION business.{name}() FROM PUBLIC")
        op.execute(f"COMMENT ON FUNCTION business.{name}() IS 'rfq-copilot:{revision}'")
    for trigger, table, function, events, kind in TRIGGERS:
        _create(trigger, table, function, events, kind)


def downgrade() -> None:
    settings = op.get_bind().info.get("rfq_migration_settings")
    allowed = settings is not None and settings.allow_test_downgrade
    if not allowed or settings.database.target != "test":
        raise CommandError(
            "K5 downgrade removes the decision guards: only the verified test target with "
            "allow_test_downgrade=true may run it. Repair other databases forward."
        )
    for trigger, table, *_ in reversed(TRIGGERS):
        _checked(trigger, table)
        op.execute(f"DROP TRIGGER {trigger} ON business.{table}")
    for name in reversed(FUNCTIONS):
        _checked(name)
        op.execute(f"DROP FUNCTION business.{name}()")
    for trigger, table, function, events in DROPPED_TRIGGERS:
        _create(trigger, table, function, events)  # K4's functions were never dropped
