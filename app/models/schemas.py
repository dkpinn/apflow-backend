from pydantic import BaseModel, Field
from uuid import UUID
from typing import Literal, Optional


class ReconciliationOptions(BaseModel):
    amount_tolerance: float = Field(default=1.00, ge=0)
    date_tolerance_days: int = Field(default=5, ge=0)
    enable_fuzzy: bool = True
    detect_discounts: bool = True


class RunReconciliationRequest(BaseModel):
    organisation_id: UUID
    supplier_id: UUID
    statement_raw_id: UUID
    options: ReconciliationOptions = ReconciliationOptions()


class ReconciliationSummary(BaseModel):
    total_lines: int
    matched: int
    unmatched: int
    exceptions: int
    missing_invoice_count: int = 0


class ReconciliationLineResult(BaseModel):
    line_id: UUID
    match_status: str
    expected_amount: Optional[float] = None
    matched_amount: Optional[float] = None
    variance_amount: Optional[float] = None
    matched_invoice_id: Optional[UUID] = None
    matched_invoice_number: Optional[str] = None
    notes: Optional[str] = None


class RunReconciliationResponse(BaseModel):
    job_id: UUID
    reconciliation_id: UUID
    status: str
    summary: ReconciliationSummary
    lines: list[ReconciliationLineResult]
    missing_invoices: list[dict] = []


class LineSkipRequest(BaseModel):
    organisation_id: UUID


class BankDraftAllocation(BaseModel):
    account_id: UUID
    gross_amount: float = Field(gt=0)
    tracking: dict[str, str] = Field(default_factory=dict)
    vat_treatment: Optional[Literal["full", "blocked", "exempt", "zero_rated"]] = None
    vat_rate: Optional[float] = Field(default=None, ge=0, le=100)


class BankBulkDraftItem(BaseModel):
    line_id: UUID
    allocations: list[BankDraftAllocation] = Field(min_length=1)


class BulkAllocateRequest(BaseModel):
    organisation_id: UUID
    items: list[BankBulkDraftItem] = Field(min_length=1)
