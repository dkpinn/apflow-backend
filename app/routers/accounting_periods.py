from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import UserAuth, ensure_org_admin, ensure_org_read
from app.services.accounting_periods import list_accounting_periods, save_accounting_period

router = APIRouter(prefix="/api/accounting-periods", tags=["accounting-periods"])


class SaveAccountingPeriodRequest(BaseModel):
    organisation_id: str
    period_start: str
    period_end: str
    status: Literal["open", "closed", "locked"] = "open"
    lock_date: str | None = None
    checklist: dict[str, Any] = Field(default_factory=dict)
    notes: str = Field(default="", max_length=2000)


@router.get("")
def accounting_periods(auth: UserAuth, organisation_id: str):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    return {
        "success": True,
        "accounting_periods": list_accounting_periods(db, organisation_id=organisation_id),
    }


@router.put("")
def save_period(payload: SaveAccountingPeriodRequest, auth: UserAuth):
    user_id, db = auth
    ensure_org_admin(user_id, payload.organisation_id)
    try:
        return {
            "success": True,
            "period": save_accounting_period(
                db,
                organisation_id=payload.organisation_id,
                user_id=user_id,
                period_start=payload.period_start,
                period_end=payload.period_end,
                status=payload.status,
                lock_date=payload.lock_date,
                checklist=payload.checklist,
                notes=payload.notes,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
