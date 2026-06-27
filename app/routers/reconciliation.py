from fastapi import APIRouter, HTTPException

from app.dependencies import UserAuth, ensure_org_write
from app.models.schemas import RunReconciliationRequest, RunReconciliationResponse
from app.services.reconciliation_engine import run_reconciliation

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])


@router.post("/run", response_model=RunReconciliationResponse)
def run_reconciliation_route(
    payload: RunReconciliationRequest,
    auth: UserAuth,
) -> RunReconciliationResponse:
    user_id, db = auth
    ensure_org_write(str(user_id), str(payload.organisation_id))
    try:
        return run_reconciliation(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
