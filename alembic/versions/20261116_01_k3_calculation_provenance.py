"""k3_calculation_provenance (K3, Day 54B; written, not applied)

Revision ID: 20261116_01
Revises: 20261115_01
Create Date: 2026-11-16

Durable, version-specific calculation provenance and governed quotation validity. The
approved Day 54B design (docs/dev_log/day_54.md, revision 2, decisions R1-R8).

- ``pricing_calculations``: one persisted record per (RFQ, RFQ source, calculation content).
  Identity is ``calculation_record_id``; ``calculation_id`` keeps its formula and values and
  is no longer unique (the same parts priced for another RFQ or customer get another record).
  ``content_sha256`` is the reusable pricing-content fingerprint. ``expires_at`` is dropped:
  calculations do not expire, quotation versions do (``quote_versions.expiry_date``).
- ``quote_versions``: seven nullable evidence columns, NULL for every existing row (no
  backfill). A ``complete`` version needs all of them (rule_07); any other version needs
  none (rule_09). ``(quote_id, rfq_id)`` and ``(calculation_record_id, rfq_id)`` are foreign
  keys, so a version can reference only a calculation record of its own quotation's RFQ.
- ``k3_quote_versions_evidence`` (BEFORE INSERT): ``created_at`` must be the database
  ``transaction_timestamp()`` (complete versions: every role; other versions: every role
  except the table owner, who imports history when seeding). A complete version must
  carry the expiry its workflow rule states (``quote_validity``; a rule without it can
  never produce one) and match its calculation record (RFQ, calculation id, RFQ source,
  total and rule versions).
- ``k3_quote_version_lines_complete`` (on quote_versions) and ``k3_quote_line_items_complete``
  (on quote_line_items), AFTER INSERT, DEFERRABLE INITIALLY DEFERRED constraint triggers
  sharing ``k3_complete_version_lines``: a complete version's lines equal its record's
  lines in count, order, part identity, specification, quantity and selling total, and sum
  to its total. The line trigger re-checks the whole version, so a line added later in
  the creating transaction (which K2 still allows) cannot extend a complete version. The
  services set both IMMEDIATE inside their savepoint after the lines are flushed.
- ``k3_pricing_calculations_seal`` (BEFORE INSERT): ``created_at`` is the database
  transaction time for every role (seeding never writes calculations).
- ``k3_business_rule_versions_immutable``: UPDATE and DELETE refused, with K2's
  ``k2_refuse_change`` (registration only inserts).

Every refusal is SQLSTATE 23000 naming the trigger. Functions are SECURITY INVOKER with a
fixed ``search_path`` and no PUBLIC EXECUTE; every trigger is ENABLE ALWAYS. K2's revision,
functions and pinned SHA-256 are untouched; ``FUNCTIONS`` and ``TRIGGERS`` here are K3's own
allowlist, pinned separately in ``schema_contract.GUARD_FILE_SHA256``. A K3 TRIGGERS entry
is ``(trigger, table, function, events, kind)``, kind ``before`` (BEFORE ... FOR EACH ROW)
or ``deferred`` (AFTER ... constraint trigger, DEFERRABLE INITIALLY DEFERRED).

A digest is consistency evidence, not authenticated origin: nothing here authenticates the
RFQ source or the evaluation context (54C). Downgrade runs only on the verified test target
with explicit opt-in, and only while no calculation record or complete version exists.
"""

import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic.util import CommandError
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261116_01"
down_revision: Union[str, Sequence[str], None] = "20261115_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEARCH_PATH = "pg_catalog, pg_temp"

# name -> PL/pgSQL body, stored verbatim as pg_proc.prosrc.
FUNCTIONS = {
    "k3_quote_versions_evidence": """
DECLARE
    dates integer;
    record_row business.pricing_calculations%ROWTYPE;
BEGIN
    IF NEW.snapshot_completeness <> 'complete' THEN
        -- Partial and metadata-only versions: database time, except when the table owner
        -- imports history (seeding). They carry no K3 evidence (rule_09).
        IF NEW.created_at = transaction_timestamp()
           OR pg_has_role(current_user,
                          (SELECT c.relowner FROM pg_class c WHERE c.oid = TG_RELID),
                          'MEMBER') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION USING
            ERRCODE = 'integrity_constraint_violation',
            MESSAGE = 'INSERT on business.quote_versions is refused: created_at must be the database transaction time',
            CONSTRAINT = TG_NAME;
    END IF;
    IF NEW.created_at IS DISTINCT FROM transaction_timestamp() THEN
        RAISE EXCEPTION USING
            ERRCODE = 'integrity_constraint_violation',
            MESSAGE = 'INSERT on business.quote_versions is refused: a complete version is created at the database transaction time',
            CONSTRAINT = TG_NAME;
    END IF;
    SELECT (r.document -> 'quote_validity' ->> 'calendar_dates_inclusive')::integer INTO dates
      FROM business.business_rule_versions r
     WHERE r.rule_version = NEW.workflow_rule_version
       AND r.rule_kind = 'workflow'
       AND r.document -> 'quote_validity' ->> 'first_date' = 'version_created_at_utc_date';
    IF dates IS NULL OR dates < 1
       OR NEW.expiry_date IS DISTINCT FROM (NEW.created_at AT TIME ZONE 'UTC')::date + (dates - 1) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'integrity_constraint_violation',
            MESSAGE = 'INSERT on business.quote_versions is refused: no validity, or not the validity its workflow rule states',
            CONSTRAINT = TG_NAME;
    END IF;
    SELECT * INTO record_row FROM business.pricing_calculations p
     WHERE p.calculation_record_id = NEW.calculation_record_id;
    IF NOT FOUND
       OR record_row.rfq_id IS DISTINCT FROM NEW.rfq_id
       OR record_row.calculation_id IS DISTINCT FROM NEW.calculation_id
       OR record_row.requirement_sha256 IS DISTINCT FROM NEW.rfq_source_sha256
       OR record_row.quote_total_usd IS DISTINCT FROM NEW.total_usd
       OR record_row.pricing_rule_version IS DISTINCT FROM NEW.pricing_rule_version
       OR record_row.manufacturing_rule_version IS DISTINCT FROM NEW.manufacturing_rule_version THEN
        RAISE EXCEPTION USING
            ERRCODE = 'integrity_constraint_violation',
            MESSAGE = 'INSERT on business.quote_versions is refused: the calculation record does not match the version',
            CONSTRAINT = TG_NAME;
    END IF;
    RETURN NEW;
END;
""",
    "k3_complete_version_lines": """
DECLARE
    v_quote text;
    v_number integer;
    v_total numeric;
    v_record text;
    expected jsonb;
BEGIN
    -- Fired for a new version and for every new line: the version as a whole is checked,
    -- so a line added later in the creating transaction is checked again.
    IF TG_TABLE_NAME = 'quote_versions' THEN
        IF NEW.snapshot_completeness <> 'complete' THEN
            RETURN NULL;
        END IF;
        v_quote := NEW.quote_id;
        v_number := NEW.version;
        v_total := NEW.total_usd;
        v_record := NEW.calculation_record_id;
    ELSE
        SELECT q.quote_id, q.version, q.total_usd, q.calculation_record_id
          INTO v_quote, v_number, v_total, v_record
          FROM business.quote_versions q
         WHERE q.quote_id = NEW.quote_id AND q.version = NEW.quote_version
           AND q.snapshot_completeness = 'complete';
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
    END IF;
    SELECT p.lines INTO expected FROM business.pricing_calculations p
     WHERE p.calculation_record_id = v_record;
    IF expected IS NOT NULL
       AND (SELECT count(*) FROM business.quote_line_items l
             WHERE l.quote_id = v_quote AND l.quote_version = v_number)
           = jsonb_array_length(expected)
       AND (SELECT sum(l.line_total_usd) FROM business.quote_line_items l
             WHERE l.quote_id = v_quote AND l.quote_version = v_number)
           = v_total
       AND NOT EXISTS (
           SELECT 1
             FROM (SELECT l.part_id, l.quantity, l.line_total_usd, l.part_json,
                          (row_number() OVER (ORDER BY l.line_number) - 1)::integer AS position
                     FROM business.quote_line_items l
                    WHERE l.quote_id = v_quote AND l.quote_version = v_number) s
            WHERE s.part_id IS DISTINCT FROM (expected -> s.position ->> 'part_id')
               OR s.quantity IS DISTINCT FROM (expected -> s.position ->> 'quantity')::integer
               OR s.line_total_usd IS DISTINCT FROM (expected -> s.position ->> 'line_total_usd')::numeric
               OR s.part_json IS DISTINCT FROM (expected -> s.position -> 'part')) THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION USING
        ERRCODE = 'integrity_constraint_violation',
        MESSAGE = format('INSERT on business.%s is refused: a complete version needs exactly its calculation lines',
                         TG_TABLE_NAME),
        CONSTRAINT = TG_NAME;
END;
""",
    "k3_pricing_calculations_seal": """
BEGIN
    IF NEW.created_at = transaction_timestamp() THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING
        ERRCODE = 'integrity_constraint_violation',
        MESSAGE = 'INSERT on business.pricing_calculations is refused: created_at must be the database transaction time',
        CONSTRAINT = TG_NAME;
END;
""",
}

# (trigger, business table, function, events, kind). k2_refuse_change belongs to K2.
TRIGGERS = (
    (
        "k3_quote_versions_evidence",
        "quote_versions",
        "k3_quote_versions_evidence",
        ("INSERT",),
        "before",
    ),
    (
        "k3_quote_version_lines_complete",
        "quote_versions",
        "k3_complete_version_lines",
        ("INSERT",),
        "deferred",
    ),
    (
        "k3_quote_line_items_complete",
        "quote_line_items",
        "k3_complete_version_lines",
        ("INSERT",),
        "deferred",
    ),
    (
        "k3_pricing_calculations_seal",
        "pricing_calculations",
        "k3_pricing_calculations_seal",
        ("INSERT",),
        "before",
    ),
    (
        "k3_business_rule_versions_immutable",
        "business_rule_versions",
        "k2_refuse_change",
        ("UPDATE", "DELETE"),
        "before",
    ),
)

# Schema statements, ';'-separated and executed one by one (no ';' inside any statement).
UPGRADE_DDL = """
ALTER TABLE business.quote_versions
    DROP CONSTRAINT fk_quote_versions_calculation_id_pricing_calculations;
ALTER TABLE business.pricing_calculations DROP CONSTRAINT pk_pricing_calculations;
ALTER TABLE business.pricing_calculations DROP CONSTRAINT ck_pricing_calculations_rule_01;
ALTER TABLE business.pricing_calculations DROP COLUMN expires_at;
ALTER TABLE business.pricing_calculations ADD COLUMN calculation_record_id text NOT NULL;
ALTER TABLE business.pricing_calculations ADD COLUMN content_sha256 text NOT NULL;
ALTER TABLE business.pricing_calculations
    ADD CONSTRAINT pk_pricing_calculations PRIMARY KEY (calculation_record_id);
ALTER TABLE business.pricing_calculations
    ADD CONSTRAINT ck_pricing_calculations_rule_01 CHECK (quote_total_usd >= 0);
ALTER TABLE business.pricing_calculations
    ADD CONSTRAINT ck_pricing_calculations_calculation_record_id_identity
    CHECK (length(btrim(calculation_record_id)) > 0 AND length(calculation_record_id) <= 64);
ALTER TABLE business.pricing_calculations
    ADD CONSTRAINT ck_pricing_calculations_content_sha256_hex
    CHECK (content_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE business.pricing_calculations
    ADD CONSTRAINT uq_pricing_calculations_reuse_key
    UNIQUE (rfq_id, requirement_sha256, content_sha256);
ALTER TABLE business.pricing_calculations
    ADD CONSTRAINT uq_pricing_calculations_calculation_record_id_rfq_id
    UNIQUE (calculation_record_id, rfq_id);
ALTER TABLE business.quotes ADD CONSTRAINT uq_quotes_quote_id_rfq_id UNIQUE (quote_id, rfq_id);
ALTER TABLE business.quote_versions ADD COLUMN rfq_id text;
ALTER TABLE business.quote_versions ADD COLUMN calculation_record_id text;
ALTER TABLE business.quote_versions ADD COLUMN rfq_source_sha256 text;
ALTER TABLE business.quote_versions ADD COLUMN evaluation_fingerprint text;
ALTER TABLE business.quote_versions ADD COLUMN rules_sha256 text;
ALTER TABLE business.quote_versions ADD COLUMN horizon_start date;
ALTER TABLE business.quote_versions ADD COLUMN requested_delivery_date date;
ALTER TABLE business.quote_versions
    ADD CONSTRAINT fk_quote_versions_quote_id_rfq_id_quotes
    FOREIGN KEY (quote_id, rfq_id) REFERENCES business.quotes (quote_id, rfq_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;
ALTER TABLE business.quote_versions
    ADD CONSTRAINT fk_quote_versions_calculation_record_rfq
    FOREIGN KEY (calculation_record_id, rfq_id)
    REFERENCES business.pricing_calculations (calculation_record_id, rfq_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;
ALTER TABLE business.quote_versions DROP CONSTRAINT ck_quote_versions_rule_07;
ALTER TABLE business.quote_versions ADD CONSTRAINT ck_quote_versions_rule_07 CHECK (
    snapshot_completeness <> 'complete' OR (manufacturing_rule_version IS NOT NULL
    AND approval_rule_version IS NOT NULL AND workflow_rule_version IS NOT NULL
    AND source_snapshot_versions IS NOT NULL AND calculation_id IS NOT NULL
    AND expiry_date IS NOT NULL AND estimated_lead_business_days IS NOT NULL
    AND rfq_id IS NOT NULL AND calculation_record_id IS NOT NULL
    AND rfq_source_sha256 IS NOT NULL AND evaluation_fingerprint IS NOT NULL
    AND rules_sha256 IS NOT NULL AND horizon_start IS NOT NULL));
ALTER TABLE business.quote_versions ADD CONSTRAINT ck_quote_versions_rule_09 CHECK (
    snapshot_completeness = 'complete' OR (rfq_id IS NULL AND calculation_record_id IS NULL
    AND rfq_source_sha256 IS NULL AND evaluation_fingerprint IS NULL AND rules_sha256 IS NULL
    AND horizon_start IS NULL AND requested_delivery_date IS NULL));
ALTER TABLE business.quote_versions ADD CONSTRAINT ck_quote_versions_rule_10 CHECK (
    snapshot_completeness <> 'complete'
    OR expiry_date >= (created_at AT TIME ZONE 'UTC')::date);
ALTER TABLE business.quote_versions ADD CONSTRAINT ck_quote_versions_rfq_id_identity
    CHECK (length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64);
ALTER TABLE business.quote_versions
    ADD CONSTRAINT ck_quote_versions_calculation_record_id_identity
    CHECK (length(btrim(calculation_record_id)) > 0 AND length(calculation_record_id) <= 64);
ALTER TABLE business.quote_versions ADD CONSTRAINT ck_quote_versions_rfq_source_sha256_hex
    CHECK (rfq_source_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE business.quote_versions ADD CONSTRAINT ck_quote_versions_evaluation_fingerprint_hex
    CHECK (evaluation_fingerprint ~ '^[0-9a-f]{64}$');
ALTER TABLE business.quote_versions ADD CONSTRAINT ck_quote_versions_rules_sha256_hex
    CHECK (rules_sha256 ~ '^[0-9a-f]{64}$')
"""

DOWNGRADE_DDL = """
ALTER TABLE business.quote_versions DROP CONSTRAINT ck_quote_versions_rules_sha256_hex;
ALTER TABLE business.quote_versions DROP CONSTRAINT ck_quote_versions_evaluation_fingerprint_hex;
ALTER TABLE business.quote_versions DROP CONSTRAINT ck_quote_versions_rfq_source_sha256_hex;
ALTER TABLE business.quote_versions
    DROP CONSTRAINT ck_quote_versions_calculation_record_id_identity;
ALTER TABLE business.quote_versions DROP CONSTRAINT ck_quote_versions_rfq_id_identity;
ALTER TABLE business.quote_versions DROP CONSTRAINT ck_quote_versions_rule_10;
ALTER TABLE business.quote_versions DROP CONSTRAINT ck_quote_versions_rule_09;
ALTER TABLE business.quote_versions DROP CONSTRAINT ck_quote_versions_rule_07;
ALTER TABLE business.quote_versions ADD CONSTRAINT ck_quote_versions_rule_07 CHECK (
    snapshot_completeness <> 'complete' OR (manufacturing_rule_version IS NOT NULL
    AND approval_rule_version IS NOT NULL AND workflow_rule_version IS NOT NULL
    AND source_snapshot_versions IS NOT NULL AND calculation_id IS NOT NULL
    AND expiry_date IS NOT NULL AND estimated_lead_business_days IS NOT NULL));
ALTER TABLE business.quote_versions DROP CONSTRAINT fk_quote_versions_calculation_record_rfq;
ALTER TABLE business.quote_versions DROP CONSTRAINT fk_quote_versions_quote_id_rfq_id_quotes;
ALTER TABLE business.quote_versions DROP COLUMN requested_delivery_date;
ALTER TABLE business.quote_versions DROP COLUMN horizon_start;
ALTER TABLE business.quote_versions DROP COLUMN rules_sha256;
ALTER TABLE business.quote_versions DROP COLUMN evaluation_fingerprint;
ALTER TABLE business.quote_versions DROP COLUMN rfq_source_sha256;
ALTER TABLE business.quote_versions DROP COLUMN calculation_record_id;
ALTER TABLE business.quote_versions DROP COLUMN rfq_id;
ALTER TABLE business.quotes DROP CONSTRAINT uq_quotes_quote_id_rfq_id;
"""

# Run after the K3 table is dropped and K2's pricing_calculations is rebuilt.
DOWNGRADE_RESTORE_DDL = """
CREATE TRIGGER k2_pricing_calculations_immutable BEFORE UPDATE OR DELETE
    ON business.pricing_calculations FOR EACH ROW EXECUTE FUNCTION business.k2_refuse_change();
ALTER TABLE business.pricing_calculations
    ENABLE ALWAYS TRIGGER k2_pricing_calculations_immutable;
ALTER TABLE business.quote_versions
    ADD CONSTRAINT fk_quote_versions_calculation_id_pricing_calculations
    FOREIGN KEY (calculation_id) REFERENCES business.pricing_calculations (calculation_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
"""
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def _checked(*names: str) -> None:
    # Static module constants only; asserted anyway before they reach DDL text.
    if not all(_IDENTIFIER.fullmatch(name) for name in names):
        raise CommandError("Unexpected identifier in a K3 definition.")


def _create_pricing_calculations_before_k3() -> None:
    """pricing_calculations exactly as revision 20261108_01 created it: same column order,
    types, defaults and (``op.f``) constraint names, so the initial revision's frozen
    downgrade shape matches again. ALTER ... ADD COLUMN would append ``expires_at`` last."""
    op.create_table(
        "pricing_calculations",
        sa.Column("calculation_id", sa.Text(), nullable=False),
        sa.Column("rfq_id", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requirement_sha256", sa.Text(), nullable=False),
        sa.Column(
            "parts", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "lines", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column("quote_total_usd", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("pricing_rule_version", sa.Text(), nullable=False),
        sa.Column("manufacturing_rule_version", sa.Text(), nullable=False),
        sa.Column(
            "source_snapshot_versions",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(lines) = 'array'", name=op.f("ck_pricing_calculations_lines_shape")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(parts) = 'array'", name=op.f("ck_pricing_calculations_parts_shape")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_snapshot_versions) = 'object'",
            name=op.f("ck_pricing_calculations_source_snapshot_versions_shape"),
        ),
        sa.CheckConstraint(
            "quote_total_usd <> 'NaN'::numeric",
            name=op.f("ck_pricing_calculations_quote_total_usd_finite"),
        ),
        sa.CheckConstraint(
            "requirement_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_pricing_calculations_requirement_sha256_hex"),
        ),
        sa.CheckConstraint(
            "length(btrim(calculation_id)) > 0 AND length(calculation_id) <= 64",
            name=op.f("ck_pricing_calculations_calculation_id_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(created_by)) > 0 AND length(created_by) <= 64",
            name=op.f("ck_pricing_calculations_created_by_identity"),
        ),
        sa.CheckConstraint(
            "length(btrim(manufacturing_rule_version)) > 0",
            name=op.f("ck_pricing_calculations_manufacturing_rule_version_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(pricing_rule_version)) > 0",
            name=op.f("ck_pricing_calculations_pricing_rule_version_nonblank"),
        ),
        sa.CheckConstraint(
            "length(btrim(rfq_id)) > 0 AND length(rfq_id) <= 64",
            name=op.f("ck_pricing_calculations_rfq_id_identity"),
        ),
        sa.CheckConstraint(
            "quote_total_usd >= 0 AND expires_at > created_at",
            name=op.f("ck_pricing_calculations_rule_01"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["business.users.user_id"],
            name=op.f("fk_pricing_calculations_created_by_users"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["manufacturing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            name=op.f("fk_pricing_calculations_manufacturing_rule_version_business_rule_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["pricing_rule_version"],
            ["business.business_rule_versions.rule_version"],
            name=op.f("fk_pricing_calculations_pricing_rule_version_business_rule_versions"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.ForeignKeyConstraint(
            ["rfq_id"],
            ["business.rfqs.rfq_id"],
            name=op.f("fk_pricing_calculations_rfq_id_rfqs"),
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            match="SIMPLE",
        ),
        sa.PrimaryKeyConstraint("calculation_id", name=op.f("pk_pricing_calculations")),
        schema="business",
    )


def _statements(block: str) -> list[str]:
    return [statement.strip() for statement in block.split(";") if statement.strip()]


def _refuse_if(sql: str, message: str) -> None:
    if op.get_bind().exec_driver_sql(sql).scalar():
        raise CommandError(message)


def upgrade() -> None:
    _refuse_if(
        "SELECT EXISTS (SELECT 1 FROM business.pricing_calculations)",
        "K3 needs an empty pricing_calculations table: existing rows are never re-keyed.",
    )
    _refuse_if(
        "SELECT EXISTS (SELECT 1 FROM business.quote_versions "
        "WHERE calculation_id IS NOT NULL OR snapshot_completeness = 'complete')",
        "K3 found versions that already claim a calculation or completeness: nothing is "
        "backfilled or certified; repair forward first.",
    )
    for statement in _statements(UPGRADE_DDL):
        op.execute(statement)
    for name, body in FUNCTIONS.items():
        _checked(name)
        op.execute(
            f"CREATE FUNCTION business.{name}() RETURNS trigger LANGUAGE plpgsql "
            f"SECURITY INVOKER SET search_path = {SEARCH_PATH} AS $k3${body}$k3$"
        )
        op.execute(f"REVOKE ALL ON FUNCTION business.{name}() FROM PUBLIC")
        op.execute(f"COMMENT ON FUNCTION business.{name}() IS 'rfq-copilot:{revision}'")
    for trigger, table, function, events, kind in TRIGGERS:
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


def downgrade() -> None:
    settings = op.get_bind().info.get("rfq_migration_settings")
    allowed = settings is not None and settings.allow_test_downgrade
    if not allowed or settings.database.target != "test":
        raise CommandError(
            "K3 downgrade removes calculation provenance: only the verified test target with "
            "allow_test_downgrade=true may run it. Repair other databases forward."
        )
    _refuse_if(
        "SELECT EXISTS (SELECT 1 FROM business.pricing_calculations) OR EXISTS "
        "(SELECT 1 FROM business.quote_versions WHERE snapshot_completeness = 'complete')",
        "K3 downgrade would have to invent calculation expiries or drop evidence: refused.",
    )
    for trigger, table, _, _, _ in reversed(TRIGGERS):
        _checked(trigger, table)
        op.execute(f"DROP TRIGGER {trigger} ON business.{table}")
    for name in reversed(FUNCTIONS):
        _checked(name)
        op.execute(f"DROP FUNCTION business.{name}()")
    for statement in _statements(DOWNGRADE_DDL):
        op.execute(statement)
    # The table is empty (refused above otherwise) and nothing references it any more:
    # rebuild it in its original physical form, then restore K2's guard and the old FK.
    op.drop_table("pricing_calculations", schema="business")
    _create_pricing_calculations_before_k3()
    for statement in _statements(DOWNGRADE_RESTORE_DDL):
        op.execute(statement)
