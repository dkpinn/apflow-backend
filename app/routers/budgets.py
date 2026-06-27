from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.dependencies import UserAuth, ensure_org_write
from app.services.budgets import delete_budget_entry, get_budget_grid, upsert_budget_entry

router = APIRouter(prefix="/api/budgets", tags=["budgets"])


class UpsertEntryBody(BaseModel):
    organisation_id: str
    account_id: str
    period_start: str
    amount: float = Field(..., ge=0)


@router.get("")
def budget_grid(
    auth: UserAuth,
    organisation_id: str = Query(...),
    year_start: str = Query(...),
    year_end: str = Query(...),
):
    user_id, db = auth
    ensure_org_write(user_id, organisation_id)
    try:
        return {"success": True, "grid": get_budget_grid(db, organisation_id, year_start, year_end)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/entries")
def upsert_entry(auth: UserAuth, body: UpsertEntryBody):
    user_id, db = auth
    ensure_org_write(user_id, body.organisation_id)
    try:
        result = upsert_budget_entry(
            db,
            organisation_id=body.organisation_id,
            user_id=user_id,
            account_id=body.account_id,
            period_start=body.period_start,
            amount=body.amount,
        )
        return {"success": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/entries/{entry_id}")
def delete_entry(
    auth: UserAuth,
    entry_id: str,
    organisation_id: str = Query(...),
):
    user_id, db = auth
    ensure_org_write(user_id, organisation_id)
    delete_budget_entry(db, organisation_id=organisation_id, entry_id=entry_id)
    return {"success": True}
