from pydantic import BaseModel, Field
from uuid import UUID


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


class RunReconciliationResponse(BaseModel):
    job_id: UUID
    reconciliation_id: UUID
    status: str