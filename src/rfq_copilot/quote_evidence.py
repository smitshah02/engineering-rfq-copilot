"""K3 (Day 54B): deterministic version evidence, calculation records and quotation validity.

    pinned rules + trusted evaluation + stored RFQ lines + database time
        -> calculation record (insert or verified reuse) + version evidence + expiry_date

Three separate things:
- ``calculation_id`` (``services.pricing``): the unchanged content-derived fingerprint of
  (parts, pricing version). Not unique across RFQs or customers.
- ``content_sha256`` (``services.pricing.calculation_record_content``): the reusable
  pricing-content fingerprint, over every price-affecting input and result.
- ``calculation_record_id``: the identity of one persisted record, unique per (RFQ, RFQ
  source digest, content); revisions of one quotation reuse it, another RFQ or customer
  never can (``fk_quote_versions_calculation_record_rfq``).

Version-specific facts (RFQ source digest, evaluation fingerprint, rule-pin digest, horizon,
requested delivery date, snapshot pins, expiry) live on the version, written once with it
and immutable afterwards (K2). Every digest is consistency evidence, never authenticated
origin: the RFQ source and the evaluation context are still application-supplied until
54C, and ``EVALUATION_CONTEXT_NOT_AUTHENTICATED`` stays.

Validity: the pinned workflow rule's ``quote_validity`` (workflow-v1.2: 30 calendar dates
inclusive, the version's UTC creation date being day one), never a constant here. Time is
always PostgreSQL's ``transaction_timestamp()`` (``database_now``), never the app clock.

Nothing here commits or logs; the caller owns the session and its savepoint.
"""

import secrets
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from rfq_copilot.canonical import fingerprint
from rfq_copilot.entities import Part
from rfq_copilot.models import PricingCalculation
from rfq_copilot.services.pricing import (
    PriceCalculation,
    calculation_record_content,
    canonical_part,
)

COMPLETE = "complete"
RFQ_SOURCE_VERSION = "rfq-source-v1"
RULE_PINS_VERSION = "rule-pins-v1"
RECORD_PREFIX = "CALCREC-"
VALIDITY_FIRST_DATE = "version_created_at_utc_date"
# Revision 20261116_01: the reuse key and the two deferred line-completeness triggers.
REUSE_KEY = "uq_pricing_calculations_reuse_key"
LINE_CONSTRAINTS = "business.k3_quote_version_lines_complete, business.k3_quote_line_items_complete"  # fmt: skip
EVIDENCE_COLUMNS = (
    "rfq_id",
    "calculation_record_id",
    "rfq_source_sha256",
    "evaluation_fingerprint",
    "rules_sha256",
    "horizon_start",
    "requested_delivery_date",
)


async def database_now(session: AsyncSession) -> datetime:
    """PostgreSQL ``transaction_timestamp()``: one creation time for every row this
    transaction writes (version, calculation record, quote; audit and receipt rows take
    the same value from their ``CURRENT_TIMESTAMP`` defaults)."""
    now = await session.scalar(text("SELECT transaction_timestamp()"))
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("The database returned no timezone-aware transaction time.")
    return now


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required.")
    return value


def validity_dates(workflow) -> int | None:
    """``calendar_dates_inclusive`` of the pinned workflow rule; None when the rule states no
    quotation validity (workflow-v1.0/v1.1: their versions stay partial). A malformed
    setting is a ``ValueError``: never guessed, never a default."""
    setting = workflow.document.get("quote_validity")
    if setting is None:
        return None
    dates = setting.get("calendar_dates_inclusive") if hasattr(setting, "get") else None
    if type(dates) is not int or dates < 1 or setting.get("first_date") != VALIDITY_FIRST_DATE:
        raise ValueError(f"{workflow.rule_version}.quote_validity is malformed.")
    return dates


def server_horizon(now: datetime) -> date:
    """54C: the server-owned evaluation horizon, the Monday of the UTC week of ``now``
    (the capacity service requires a Monday). A version whose recorded ``horizon_start``
    equals this for its own ``created_at`` has a server-dated context."""
    today = _aware(now).astimezone(UTC).date()
    return today - timedelta(days=today.weekday())


def expiry_date(created_at: datetime, dates: int) -> date:
    """The inclusive last valid date: the UTC creation date is day one of ``dates``."""
    if type(dates) is not int or dates < 1:
        raise ValueError("The validity must be a positive whole number of calendar dates.")
    return _aware(created_at).astimezone(UTC).date() + timedelta(days=dates - 1)


def expires_at(last_valid: date) -> datetime:
    """The first instant the version is expired: 00:00 UTC the day after its last valid date."""
    return datetime.combine(last_valid + timedelta(days=1), time(0), tzinfo=UTC)


def is_expired(last_valid: date, now: datetime) -> bool:
    """True from 00:00:00 UTC on the day after ``last_valid``."""
    return _aware(now) >= expires_at(last_valid)


def rfq_source_sha256(rfq_id: str, customer_id: str, runtime_lines: Sequence) -> str:
    """Digest of the stored RFQ source a version was evaluated from: RFQ, customer, and each
    runtime line's number, requirement revision and canonical specification."""
    lines = sorted(runtime_lines, key=lambda line: line.line_number)
    return fingerprint(
        {
            "source_version": RFQ_SOURCE_VERSION,
            "rfq_id": rfq_id,
            "customer_id": customer_id,
            "lines": [
                {
                    "line_number": line.line_number,
                    "requirement_revision": line.requirement_revision,
                    "part": canonical_part(Part.from_line(line)),
                }
                for line in lines
            ],
        }
    )


def rules_sha256(rules: dict, workflow) -> str:
    """Digest of the four server-pinned, manifest-verified rules (version, kind and canonical
    fingerprint), in the order manufacturing, pricing, approval, workflow."""
    pins = [*rules.values(), workflow]
    return fingerprint(
        {
            "digest_version": RULE_PINS_VERSION,
            "rules": [[r.rule_version, r.rule_kind, r.fingerprint] for r in pins],
        }
    )


def record_row(
    *, rfq_id, actor, now, rfq_source, parts, pricing: PriceCalculation, pricing_rules, snapshots
) -> dict:
    """The calculation record a version under these facts needs (a fresh identity)."""
    lines, content, manufacturing = calculation_record_content(pricing, pricing_rules, parts)
    return {
        "calculation_record_id": RECORD_PREFIX + secrets.token_hex(16).upper(),
        "calculation_id": pricing.calculation_id,
        "rfq_id": rfq_id,
        "created_by": actor,
        "created_at": _aware(now),
        "requirement_sha256": rfq_source,
        "parts": [canonical_part(part) for part in parts],
        "lines": lines,
        "quote_total_usd": pricing.quote_total_usd,
        "pricing_rule_version": pricing.pricing_rule_version,
        "manufacturing_rule_version": manufacturing,
        "source_snapshot_versions": snapshots,
        "content_sha256": content,
    }


# Columns a reused record must hold exactly; identity, actor, time and first-seen snapshot
# pins are the first recording's own facts.
RECORD_CONTENT = (
    "calculation_id",
    "rfq_id",
    "requirement_sha256",
    "parts",
    "lines",
    "quote_total_usd",
    "pricing_rule_version",
    "manufacturing_rule_version",
    "content_sha256",
)


async def calculation_record(session: AsyncSession, row: dict) -> PricingCalculation | None:
    """Insert the record, or reuse the stored one with the same (RFQ, source, content),
    re-read and compared column by column. None when a stored record with that key holds
    anything else (tampered: the caller refuses). INSERT ... ON CONFLICT DO NOTHING needs
    only the INSERT grant; a concurrent creator's row is waited for by the unique index."""
    await session.execute(
        insert(PricingCalculation).values(**row).on_conflict_do_nothing(constraint=REUSE_KEY)
    )
    stored = await session.scalar(
        select(PricingCalculation)
        .where(
            PricingCalculation.rfq_id == row["rfq_id"],
            PricingCalculation.requirement_sha256 == row["requirement_sha256"],
            PricingCalculation.content_sha256 == row["content_sha256"],
        )
        .options(raiseload("*"))
        .execution_options(populate_existing=True)
    )
    if stored is None or any(getattr(stored, name) != row[name] for name in RECORD_CONTENT):
        return None
    return stored


def version_evidence(
    *, rfq_id, record, rfq_source, evaluation, rules, workflow, inputs, now, dates
) -> dict:
    """The K3 columns of a complete version, plus its expiry and calculation id."""
    return {
        "rfq_id": rfq_id,
        "calculation_record_id": record.calculation_record_id,
        "rfq_source_sha256": rfq_source,
        "evaluation_fingerprint": evaluation.evaluation_fingerprint,
        "rules_sha256": rules_sha256(rules, workflow),
        "horizon_start": inputs["horizon_start"],
        "requested_delivery_date": inputs["requested_delivery_date"],
        "expiry_date": expiry_date(now, dates),
        "calculation_id": record.calculation_id,
    }
