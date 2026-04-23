from fastapi import APIRouter
from app.models.schemas import (
    RunReconciliationRequest,
    RunReconciliationResponse,
)
from app.services.reconciliation_engine import queue_reconciliation_job

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])


@router.post("/run", response_model=RunReconciliationResponse)
def run_reconciliation(
    payload: RunReconciliationRequest,
) -> RunReconciliationResponse:
    return queue_reconciliation_job(payload)