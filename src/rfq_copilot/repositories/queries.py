"""Bound, scoped async reads. Authentication/authorization remains a service duty."""

import json
import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

from sqlalchemy import Text, cast, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, raiseload, selectinload

from rfq_copilot.canonical import fingerprint
from rfq_copilot.entities import Part
from rfq_copilot.models import (
    RFQ,
    ApprovalRequest,
    BusinessRuleVersion,
    Customer,
    DataSnapshot,
    Material,
    MaterialFinishCompatibility,
    MaterialInventory,
    MaterialSupportedOperation,
    MaterialThicknessOption,
    Operation,
    OperationRouting,
    ProductionCapacity,
    Quote,
    QuoteVersion,
    Workcentre,
)
from rfq_copilot.repositories.results import (
    CustomerDetails,
    ManufacturingCapabilities,
    MaterialAvailability,
    QuotationDetails,
    QuoteVersionDetails,
    RFQDetails,
    RuleVersionDetails,
    SnapshotDetails,
    WorkcentreCapacity,
)
from rfq_copilot.repositories.rule_manifest import TRUSTED_RULES
from rfq_copilot.seeding.transform import decimal_value

AvailabilityFilter = Literal["all", "available", "unavailable"]
PART_TYPES = frozenset(
    {"flat_panel", "mounting_plate", "l_bracket", "u_bracket", "enclosure", "fabricated_assembly"}
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


class RuleIntegrityError(ValueError):
    """A stored rule version does not match what the caller pinned or what it claims."""


class SnapshotMismatchError(ValueError):
    """No single snapshot could be selected, or data disagrees with the selected one."""


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 64 or "\x00" in value:
        raise ValueError("Identifier must be nonblank text of at most 64 characters.")


def _thickness(value: Decimal | None) -> None:
    if value is not None:
        if not isinstance(value, Decimal):
            raise ValueError("Thickness must be Decimal millimetres.")
        decimal_value(value, "thickness", 12, 3)
        if value <= 0:
            raise ValueError("Thickness must be positive.")


def _week(value: date | None) -> None:
    if value is not None and (type(value) is not date or value.weekday() != 0):
        raise ValueError("Week filters must be Monday calendar dates.")


async def get_customer(session: AsyncSession, customer_id: str) -> CustomerDetails | None:
    """Fetch only the named customer and its contacts; caller authorizes the ID."""
    _identifier(customer_id)
    statement = (
        select(Customer)
        .where(Customer.customer_id == customer_id)
        .options(
            selectinload(Customer.contacts),
            raiseload("*"),
        )
    )
    with session.no_autoflush:
        customer = await session.scalar(statement)
    if customer is None:
        return None
    return CustomerDetails(
        customer, tuple(sorted(customer.contacts, key=lambda c: (c.contact_name, c.contact_id)))
    )


def assigned_to(user_id: str):
    """SQL condition: the RFQ in scope has an active assignment for ``user_id``.

    Used only when assignments are enforced (revision 20261111_02). It narrows the
    customer-scoped query; it never replaces the customer condition.
    """
    from rfq_copilot.models import RfqAssignment

    assignment = RfqAssignment.__table__

    _identifier(user_id)
    return exists().where(
        assignment.c.rfq_id == RFQ.rfq_id,
        assignment.c.user_id == user_id,
        assignment.c.revoked_at.is_(None),
    )


async def get_rfq(
    session: AsyncSession, rfq_id: str, *, customer_id: str, assigned_user: str | None = None
) -> RFQDetails | None:
    """Historical part_ids are opaque references, separate from runtime RFQ lines."""
    _identifier(rfq_id)
    _identifier(customer_id)
    statement = (
        select(RFQ)
        .where(RFQ.rfq_id == rfq_id, RFQ.customer_id == customer_id)
        .options(
            joinedload(RFQ.customer),
            selectinload(RFQ.line_items),
            raiseload("*"),
        )
    )
    if assigned_user is not None:
        statement = statement.where(assigned_to(assigned_user))
    with session.no_autoflush:
        rfq = await session.scalar(statement)
    if rfq is None:
        return None
    historical = tuple(rfq.part_ids) if rfq.source_type == "historical_internal" else ()
    return RFQDetails(
        rfq,
        rfq.customer,
        historical,
        tuple(sorted(rfq.line_items, key=lambda line: (line.line_number, str(line.rfq_line_id)))),
    )


async def list_rfqs(
    session: AsyncSession,
    customer_id: str,
    *,
    limit: int,
    offset: int = 0,
    assigned_user: str | None = None,
) -> tuple[RFQ, ...]:
    """One customer's RFQ headers, newest first (ties by rfq_id); no lines or relations."""
    _identifier(customer_id)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000.")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer.")
    statement = (
        select(RFQ)
        .where(RFQ.customer_id == customer_id)
        .order_by(RFQ.submitted_at.desc(), RFQ.rfq_id)
        .limit(limit)
        .offset(offset)
        .options(raiseload("*"))
    )
    if assigned_user is not None:
        statement = statement.where(assigned_to(assigned_user))
    with session.no_autoflush:
        return tuple((await session.scalars(statement)).all())


async def get_rfq_parts(
    session: AsyncSession, rfq_id: str, *, customer_id: str
) -> tuple[Part, ...] | None:
    """Runtime RFQ lines as Parts in line order; None if the RFQ is absent or out of scope."""
    details = await get_rfq(session, rfq_id, customer_id=customer_id)
    if details is None:
        return None
    return tuple(Part.from_line(line) for line in details.runtime_lines)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


async def get_rule_version(
    session: AsyncSession,
    rule_kind: str,
    rule_version: str,
    *,
    expected_sha256: str | None = None,
) -> RuleVersionDetails | None:
    """Read one seeded rule document; verify kind, version identity and the optional sha pin."""
    _identifier(rule_kind)
    if not isinstance(rule_version, str) or not rule_version.strip() or len(rule_version) > 128:
        raise ValueError("Rule version must be nonblank text of at most 128 characters.")
    if expected_sha256 is not None and not (
        isinstance(expected_sha256, str) and _SHA256.fullmatch(expected_sha256)
    ):
        raise ValueError("Expected sha256 must be 64 lowercase hex characters.")
    statement = select(
        BusinessRuleVersion.rule_kind,
        BusinessRuleVersion.sha256,
        cast(BusinessRuleVersion.document, Text),
    ).where(BusinessRuleVersion.rule_version == rule_version)
    with session.no_autoflush:
        row = (await session.execute(statement)).one_or_none()
    if row is None:
        return None
    kind, sha256, text = row
    if kind != rule_kind:
        raise RuleIntegrityError(f"Rule {rule_version} is {kind}, not {rule_kind}.")
    if not _SHA256.fullmatch(sha256):
        raise RuleIntegrityError(f"Rule {rule_version} has a malformed sha256.")
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise RuleIntegrityError(f"Rule {rule_version} does not match the pinned sha256.")
    # The jsonb text keeps PostgreSQL's exact numerics; never let them pass through float.
    document = json.loads(text, parse_float=Decimal)
    # Seeding names a rule by its declared version, or by its file hash when it has none.
    if rule_version != f"{kind}@sha256:{sha256}" and document.get("version") != rule_version:
        raise RuleIntegrityError(
            f"Rule {rule_version} document declares version {document.get('version')}."
        )
    # Trust anchor: the version-controlled manifest, not the row's own claims. A changed
    # document under an unchanged stored sha256 fails here.
    trusted = TRUSTED_RULES.get(rule_version)
    if trusted is None:
        raise RuleIntegrityError(f"Rule {rule_version} is not a registered, approved version.")
    computed = fingerprint(document)
    if trusted != (kind, sha256, computed):
        raise RuleIntegrityError(f"Rule {rule_version} content differs from its approved version.")
    return RuleVersionDetails(
        rule_version=rule_version,
        rule_kind=kind,
        sha256=sha256,
        fingerprint=computed,
        document=_freeze(document),
    )


async def get_snapshot(
    session: AsyncSession, snapshot_kind: str, snapshot_id: str | None = None
) -> SnapshotDetails:
    """The pinned snapshot of this kind, or the only registered one when nothing is pinned.

    Never guesses: no snapshot, several unpinned snapshots, an unknown pin or a pin of the
    other kind all raise SnapshotMismatchError. One SELECT.
    """
    if snapshot_kind not in ("inventory", "capacity"):
        raise ValueError("Snapshot kind must be inventory or capacity.")
    if snapshot_id is not None:
        _identifier(snapshot_id)
    last_week = (
        select(func.max(ProductionCapacity.week_start))
        .where(ProductionCapacity.snapshot_id == DataSnapshot.snapshot_id)
        .scalar_subquery()
    )
    statement = select(
        DataSnapshot.snapshot_id,
        DataSnapshot.snapshot_kind,
        DataSnapshot.dataset_version,
        DataSnapshot.as_of_date,
        last_week,
    )
    if snapshot_id is None:
        statement = statement.where(DataSnapshot.snapshot_kind == snapshot_kind)
    else:
        statement = statement.where(DataSnapshot.snapshot_id == snapshot_id)
    with session.no_autoflush:
        rows = (await session.execute(statement.order_by(DataSnapshot.snapshot_id).limit(2))).all()
    if not rows:
        what = f"snapshot {snapshot_id}" if snapshot_id else f"{snapshot_kind} snapshot"
        raise SnapshotMismatchError(f"No registered {what}.")
    if len(rows) > 1:
        raise SnapshotMismatchError(
            f"Several {snapshot_kind} snapshots are registered; pin one explicitly."
        )
    found_id, kind, version, as_of, week = rows[0]
    if kind != snapshot_kind:
        raise SnapshotMismatchError(f"Snapshot {found_id} is {kind}, not {snapshot_kind}.")
    return SnapshotDetails(found_id, kind, version, as_of, week if kind == "capacity" else None)


async def get_material_availability(
    session: AsyncSession,
    material_id: str,
    *,
    thickness_mm: Decimal | None = None,
    availability: AvailabilityFilter = "all",
    snapshot_id: str | None = None,
) -> MaterialAvailability | None:
    """Read generated free kg; positive free stock does not promise feasibility.

    snapshot_id restricts the rows to one inventory snapshot. Calculations always pass it;
    without it, rows of every stored snapshot are returned (a catalog view, not a total).
    """
    _identifier(material_id)
    if snapshot_id is not None:
        _identifier(snapshot_id)
    if availability not in {"all", "available", "unavailable"}:
        raise ValueError("Availability filter must be all, available or unavailable.")
    _thickness(thickness_mm)
    with session.no_autoflush:
        material = await session.scalar(
            select(Material).where(Material.material_id == material_id).options(raiseload("*"))
        )
        if material is None:
            return None
        statement = (
            select(MaterialInventory)
            .where(MaterialInventory.material_id == material_id)
            .options(raiseload("*"))
        )
        if thickness_mm is not None:
            statement = statement.where(MaterialInventory.thickness_mm == thickness_mm)
        if snapshot_id is not None:
            statement = statement.where(MaterialInventory.snapshot_id == snapshot_id)
        if availability == "available":
            statement = statement.where(MaterialInventory.available_kg > 0)
        elif availability == "unavailable":
            statement = statement.where(MaterialInventory.available_kg == 0)
        inventory = tuple(
            (
                await session.scalars(
                    statement.order_by(
                        MaterialInventory.thickness_mm,
                        MaterialInventory.warehouse,
                        MaterialInventory.inventory_id,
                    )
                )
            ).all()
        )
    return MaterialAvailability(
        material, inventory, sum((r.available_kg for r in inventory), Decimal(0))
    )


async def find_materials(
    session: AsyncSession,
    *,
    grade: str | None = None,
    category: str | None = None,
    thickness_mm: Decimal | None = None,
    finish_id: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[str, ...]:
    """Material IDs matching every given catalog filter; grade matches case-insensitively.

    Ordered by ``material_id``. ``limit``/``offset`` page in SQL (TD-011); without
    ``limit`` every match is returned, as before.
    """
    for value in (grade, category, finish_id):
        if value is not None:
            _identifier(value)
    _thickness(thickness_mm)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer.")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer.")
    statement = select(Material.material_id)
    if grade is not None:
        statement = statement.where(func.lower(Material.grade) == grade.strip().lower())
    if category is not None:
        statement = statement.where(Material.category == category)
    if thickness_mm is not None:
        statement = statement.where(
            exists().where(
                MaterialThicknessOption.material_id == Material.material_id,
                MaterialThicknessOption.thickness_mm == thickness_mm,
            )
        )
    if finish_id is not None:
        statement = statement.where(
            exists().where(
                MaterialFinishCompatibility.material_id == Material.material_id,
                MaterialFinishCompatibility.finish_id == finish_id,
            )
        )
    statement = statement.order_by(Material.material_id).offset(offset)
    if limit is not None:
        statement = statement.limit(limit)
    with session.no_autoflush:
        return tuple((await session.scalars(statement)).all())


async def list_materials(
    session: AsyncSession, material_ids: Sequence[str]
) -> tuple[Material, ...]:
    """Catalog rows (including cost and pricing version) for the given IDs, ordered by ID."""
    identifiers = sorted(set(material_ids))
    for identifier in identifiers:
        _identifier(identifier)
    if not identifiers:
        return ()
    statement = (
        select(Material)
        .where(Material.material_id.in_(identifiers))
        .order_by(Material.material_id)
        .options(raiseload("*"))
    )
    with session.no_autoflush:
        return tuple((await session.scalars(statement)).all())


async def list_operations(
    session: AsyncSession, operation_ids: Sequence[str]
) -> tuple[Operation, ...]:
    """Operation catalog rows (including is_quoted) for the given IDs, ordered by ID."""
    identifiers = sorted(set(operation_ids))
    for identifier in identifiers:
        _identifier(identifier)
    if not identifiers:
        return ()
    statement = (
        select(Operation)
        .where(Operation.operation_id.in_(identifiers))
        .order_by(Operation.operation_id)
        .options(raiseload("*"))
    )
    with session.no_autoflush:
        return tuple((await session.scalars(statement)).all())


async def list_part_routing(session: AsyncSession, part_type: str) -> tuple[OperationRouting, ...]:
    """Stored routing steps for one part type in sequence order; () for an unknown type."""
    _identifier(part_type)
    statement = (
        select(OperationRouting)
        .where(OperationRouting.part_type == part_type)
        .order_by(OperationRouting.sequence, OperationRouting.routing_id)
        .options(raiseload("*"))
    )
    with session.no_autoflush:
        return tuple((await session.scalars(statement)).all())


async def get_manufacturing_capabilities(
    session: AsyncSession,
    material_id: str,
    *,
    part_type: str | None = None,
) -> ManufacturingCapabilities | None:
    """Retrieve stored capabilities/compatibility/routing; perform no feasibility decision."""
    _identifier(material_id)
    if part_type is not None and part_type not in PART_TYPES:
        raise ValueError("Unsupported part-type filter.")
    statement = (
        select(Material)
        .where(Material.material_id == material_id)
        .options(
            selectinload(Material.thickness_options),
            selectinload(Material.finish_compatibilities).joinedload(
                MaterialFinishCompatibility.finish
            ),
            selectinload(Material.supported_operations).joinedload(
                MaterialSupportedOperation.operation
            ),
            raiseload("*"),
        )
    )
    with session.no_autoflush:
        material = await session.scalar(statement)
        if material is None:
            return None
        operations = tuple(
            sorted(
                (pair.operation for pair in material.supported_operations),
                key=lambda op: op.operation_id,
            )
        )
        identifiers = [op.operation_id for op in operations]
        workcentres = tuple(
            (
                await session.scalars(
                    select(Workcentre)
                    .where(Workcentre.operation_id.in_(identifiers))
                    .order_by(Workcentre.operation_id, Workcentre.workcentre_id)
                    .options(raiseload("*"))
                )
            ).all()
        )
        route = (
            select(OperationRouting)
            .where(OperationRouting.operation_id.in_(identifiers))
            .options(raiseload("*"))
        )
        if part_type is not None:
            route = route.where(OperationRouting.part_type == part_type)
        routing = tuple(
            (
                await session.scalars(
                    route.order_by(
                        OperationRouting.part_type,
                        OperationRouting.sequence,
                        OperationRouting.routing_id,
                    )
                )
            ).all()
        )
    return ManufacturingCapabilities(
        material,
        tuple(sorted(r.thickness_mm for r in material.thickness_options)),
        tuple(
            sorted(
                (pair.finish for pair in material.finish_compatibilities), key=lambda f: f.finish_id
            )
        ),
        operations,
        workcentres,
        routing,
    )


async def get_workcentre_capacity(
    session: AsyncSession,
    workcentre_id: str,
    *,
    week_start: date | None = None,
    week_end: date | None = None,
    snapshot_id: str | None = None,
) -> WorkcentreCapacity | None:
    """Read inclusive Monday-week bounds in hours, without scheduling or reserving.

    snapshot_id restricts the weeks to one capacity snapshot; calculations always pass it.
    """
    _identifier(workcentre_id)
    if snapshot_id is not None:
        _identifier(snapshot_id)
    _week(week_start)
    _week(week_end)
    if week_start is not None and week_end is not None and week_start > week_end:
        raise ValueError("Week start must not follow week end.")
    with session.no_autoflush:
        workcentre = await session.scalar(
            select(Workcentre)
            .where(Workcentre.workcentre_id == workcentre_id)
            .options(joinedload(Workcentre.operation), raiseload("*"))
        )
        if workcentre is None:
            return None
        statement = (
            select(ProductionCapacity)
            .where(ProductionCapacity.workcentre_id == workcentre_id)
            .options(raiseload("*"))
        )
        if week_start is not None:
            statement = statement.where(ProductionCapacity.week_start >= week_start)
        if week_end is not None:
            statement = statement.where(ProductionCapacity.week_start <= week_end)
        if snapshot_id is not None:
            statement = statement.where(ProductionCapacity.snapshot_id == snapshot_id)
        weeks = tuple(
            (
                await session.scalars(
                    statement.order_by(
                        ProductionCapacity.week_start, ProductionCapacity.capacity_id
                    )
                )
            ).all()
        )
    return WorkcentreCapacity(
        workcentre, weeks, sum((w.remaining_hours for w in weeks), Decimal(0))
    )


async def list_quotation_history(
    session: AsyncSession,
    customer_id: str,
    *,
    rfq_id: str | None = None,
) -> tuple[QuotationDetails, ...]:
    """One result per quotation; preserve all version metadata and supplied lines."""
    _identifier(customer_id)
    if rfq_id is not None:
        _identifier(rfq_id)
    statement = (
        select(Quote)
        .join(Quote.rfq)
        .where(RFQ.customer_id == customer_id)
        .options(
            joinedload(Quote.rfq),
            selectinload(Quote.versions).selectinload(QuoteVersion.line_items),
            raiseload("*"),
        )
    )
    if rfq_id is not None:
        statement = statement.where(Quote.rfq_id == rfq_id)
    with session.no_autoflush:
        quotes = (
            await session.scalars(statement.order_by(Quote.created_at.desc(), Quote.quote_id))
        ).all()
    return _quotation_details(quotes)


async def get_released_quotation(
    session: AsyncSession, quote_id: str, *, customer_id: str, assigned_user: str | None = None
) -> QuotationDetails | None:
    """One quote by id, only if it belongs to ``customer_id`` and a human released it.

    Ownership and release are filtered in SQL (TD-010): one quote row plus its versions
    and lines (two SELECT IN loads), independent of the customer's history size.
    """
    _identifier(customer_id)
    _identifier(quote_id)
    statement = (
        select(Quote)
        .join(Quote.rfq)
        .where(
            Quote.quote_id == quote_id,
            RFQ.customer_id == customer_id,
            Quote.status == "released",
            Quote.released_at.is_not(None),
        )
        .options(
            joinedload(Quote.rfq),
            selectinload(Quote.versions).selectinload(QuoteVersion.line_items),
            raiseload("*"),
        )
    )
    if assigned_user is not None:
        statement = statement.where(assigned_to(assigned_user))
    with session.no_autoflush:
        quote = await session.scalar(statement)
    return None if quote is None else _quotation_details((quote,))[0]


def _quotation_details(quotes) -> tuple[QuotationDetails, ...]:
    return tuple(
        QuotationDetails(
            quote,
            tuple(
                QuoteVersionDetails(
                    v,
                    tuple(
                        sorted(
                            v.line_items, key=lambda line: (line.line_number, line.quote_line_id)
                        )
                    ),
                )
                for v in sorted(
                    quote.versions, key=lambda version: (version.version, version.quote_version_id)
                )
            ),
        )
        for quote in quotes
    )


async def list_pending_approvals(
    session: AsyncSession,
    *,
    customer_id: str,
    required_role: str = "sales_manager",
) -> tuple[ApprovalRequest, ...]:
    """Read real pending requests only. A role filter is not authenticated authority."""
    _identifier(customer_id)
    if required_role != "sales_manager":
        raise ValueError("The approved request schema requires sales_manager.")
    statement = (
        select(ApprovalRequest)
        .join(ApprovalRequest.version_record)
        .join(QuoteVersion.quote)
        .join(Quote.rfq)
        .where(
            RFQ.customer_id == customer_id,
            ApprovalRequest.status == "pending",
            ApprovalRequest.required_role == required_role,
        )
        .order_by(ApprovalRequest.requested_at, ApprovalRequest.approval_request_id)
        .options(raiseload("*"))
    )
    with session.no_autoflush:
        return tuple((await session.scalars(statement)).all())
