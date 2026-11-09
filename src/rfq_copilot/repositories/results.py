"""Typed read results. ORM entities remain owned by the caller's session."""

from dataclasses import dataclass
from decimal import Decimal

from rfq_copilot.models import (
    RFQ,
    Contact,
    Customer,
    Finish,
    Material,
    MaterialInventory,
    Operation,
    OperationRouting,
    ProductionCapacity,
    Quote,
    QuoteLineItem,
    QuoteVersion,
    RFQLineItem,
    Workcentre,
)


@dataclass(frozen=True)
class CustomerDetails:
    customer: Customer
    contacts: tuple[Contact, ...]


@dataclass(frozen=True)
class RFQDetails:
    rfq: RFQ
    customer: Customer
    historical_part_ids: tuple[str, ...]
    runtime_lines: tuple[RFQLineItem, ...]


@dataclass(frozen=True)
class MaterialAvailability:
    material: Material
    inventory: tuple[MaterialInventory, ...]
    available_kg: Decimal


@dataclass(frozen=True)
class ManufacturingCapabilities:
    material: Material
    supported_thickness_mm: tuple[Decimal, ...]
    finishes: tuple[Finish, ...]
    operations: tuple[Operation, ...]
    workcentres: tuple[Workcentre, ...]
    routing: tuple[OperationRouting, ...]


@dataclass(frozen=True)
class WorkcentreCapacity:
    workcentre: Workcentre
    weeks: tuple[ProductionCapacity, ...]
    remaining_hours: Decimal


@dataclass(frozen=True)
class QuoteVersionDetails:
    version: QuoteVersion
    line_items: tuple[QuoteLineItem, ...]


@dataclass(frozen=True)
class QuotationDetails:
    quote: Quote
    versions: tuple[QuoteVersionDetails, ...]
