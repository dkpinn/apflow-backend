from pydantic import BaseModel, Field
from uuid import UUID
from typing import Optional


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