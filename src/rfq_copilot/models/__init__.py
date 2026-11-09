"""Import this package to register all 33 approved tables with Base.metadata."""

from rfq_copilot.models.approvals import ApprovalHistory, ApprovalRequest
from rfq_copilot.models.base import Base
from rfq_copilot.models.customers import Contact, Customer
from rfq_copilot.models.identity import Permission, Role, RolePermission, User
from rfq_copilot.models.manufacturing import (
    Finish,
    FinishMaterialCategory,
    Material,
    MaterialFinishCompatibility,
    MaterialSupportedOperation,
    MaterialThicknessOption,
    Operation,
    OperationRouting,
    Supplier,
    Workcentre,
)
from rfq_copilot.models.quotations import PricingCalculation, Quote, QuoteLineItem, QuoteVersion
from rfq_copilot.models.reliability import AuditEvent, IdempotencyRecord, WorkflowRun
from rfq_copilot.models.rfqs import RFQ, RFQAttachment, RFQLineItem
from rfq_copilot.models.rules import BusinessRuleVersion, DataSnapshot
from rfq_copilot.models.supply import MaterialInventory, ProcurementRecord, ProductionCapacity

MODEL_CLASSES = (
    BusinessRuleVersion,
    DataSnapshot,
    Role,
    Permission,
    RolePermission,
    User,
    Customer,
    Contact,
    Material,
    Finish,
    Operation,
    Workcentre,
    Supplier,
    MaterialThicknessOption,
    MaterialFinishCompatibility,
    MaterialSupportedOperation,
    FinishMaterialCategory,
    OperationRouting,
    MaterialInventory,
    ProcurementRecord,
    ProductionCapacity,
    RFQ,
    RFQLineItem,
    RFQAttachment,
    PricingCalculation,
    Quote,
    QuoteVersion,
    QuoteLineItem,
    ApprovalRequest,
    ApprovalHistory,
    IdempotencyRecord,
    AuditEvent,
    WorkflowRun,
)

__all__ = [
    "Base",
    "MODEL_CLASSES",
    "BusinessRuleVersion",
    "DataSnapshot",
    "Role",
    "Permission",
    "RolePermission",
    "User",
    "Customer",
    "Contact",
    "Material",
    "Finish",
    "Operation",
    "Workcentre",
    "Supplier",
    "MaterialThicknessOption",
    "MaterialFinishCompatibility",
    "MaterialSupportedOperation",
    "FinishMaterialCategory",
    "OperationRouting",
    "MaterialInventory",
    "ProcurementRecord",
    "ProductionCapacity",
    "RFQ",
    "RFQLineItem",
    "RFQAttachment",
    "PricingCalculation",
    "Quote",
    "QuoteVersion",
    "QuoteLineItem",
    "ApprovalRequest",
    "ApprovalHistory",
    "IdempotencyRecord",
    "AuditEvent",
    "WorkflowRun",
]
