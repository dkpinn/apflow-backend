from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from app.dependencies import UserAuth, ensure_org_read
from app.services.customer_collections import build_customer_collections


router = APIRouter(prefix="/api/customer-collections", tags=["customer-collections"])


@router.get("")
def customer_collections_dashboard(
    auth: UserAuth,
    organisation_id: str,
    as_at_date: str = Query(default_factory=lambda: date.today().isoformat()),
    due_soon_days: int = Query(default=7, ge=0, le=90),
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        return {
            "success": True,
            "collections": build_customer_collections(
                db,
                organisation_id=organisation_id,
                as_at_date=as_at_date,
                due_soon_days=due_soon_days,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
