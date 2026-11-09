"""Bound, scoped async reads. Authentication/authorization remains a service duty."""

import hashlib
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

from rfq_copilot.entities import Part
from rfq_copilot.models import (
    RFQ,
    ApprovalRequest,
    BusinessRuleVersion,
    Customer,
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
    WorkcentreCapacity,
)
from rfq_copilot.seeding.transform import decimal_value

AvailabilityFilter = Literal["all", "available", "unavailable"]
PART_TYPES = frozenset(
    {"flat_panel", "mounting_plate", "l_bracket", "u_bracket", "enclosure", "fabricated_assembly"}
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


class RuleIntegrityError(ValueError):
    """A stored rule version does not match what the caller pinned or what it claims."""


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


async def get_rfq(session: AsyncSession, rfq_id: str, *, customer_id: str) -> RFQDetails | None:
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
    return RuleVersionDetails(
        rule_version=rule_version,
        rule_kind=kind,
        sha256=sha256,
        fingerprint=hashlib.sha256(text.encode()).hexdigest(),
        document=_freeze(document),
    )


async def get_material_availability(
    session: AsyncSession,
    material_id: str,
    *,
    thickness_mm: Decimal | None = None,
    availability: AvailabilityFilter = "all",
) -> MaterialAvailability | None:
    """Read current generated free kg; positive free stock does not promise feasibility."""
    _identifier(material_id)
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
) -> tuple[str, ...]:
    """Material IDs matching every given catalog filter; grade matches case-insensitively."""
    for value in (grade, category, finish_id):
        if value is not None:
            _identifier(value)
    _thickness(thickness_mm)
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
    with session.no_autoflush:
        return tuple((await session.scalars(statement.order_by(Material.material_id))).all())


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
) -> WorkcentreCapacity | None:
    """Read inclusive Monday-week bounds in hours, without scheduling or reserving."""
    _identifier(workcentre_id)
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
