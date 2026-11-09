"""Bound, scoped async reads. Authentication/authorization remains a service duty."""

from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, raiseload, selectinload

from rfq_copilot.models import (
    RFQ,
    ApprovalRequest,
    Customer,
    Material,
    MaterialFinishCompatibility,
    MaterialInventory,
    MaterialSupportedOperation,
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
    WorkcentreCapacity,
)
from rfq_copilot.seeding.transform import decimal_value

AvailabilityFilter = Literal["all", "available", "unavailable"]
PART_TYPES = frozenset(
    {"flat_panel", "mounting_plate", "l_bracket", "u_bracket", "enclosure", "fabricated_assembly"}
)


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 64 or "\x00" in value:
        raise ValueError("Identifier must be nonblank text of at most 64 characters.")


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
    if thickness_mm is not None:
        if not isinstance(thickness_mm, Decimal):
            raise ValueError("Thickness must be Decimal millimetres.")
        decimal_value(thickness_mm, "thickness", 12, 3)
        if thickness_mm <= 0:
            raise ValueError("Thickness must be positive.")
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
