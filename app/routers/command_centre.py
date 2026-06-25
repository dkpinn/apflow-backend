from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.dependencies import UserAuth, ensure_org_read
from app.services.command_centre import generate_command_centre

router = APIRouter(prefix="/api/command-centre", tags=["command-centre"])


@router.get("")
def command_centre(
    auth: UserAuth,
    organisation_id: str,
    as_at_date: str | None = Query(default=None, description="Optional snapshot date in YYYY-MM-DD format."),
):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    try:
        return {
            "success": True,
            "command_centre": generate_command_centre(
                db,
                organisation_id=organisation_id,
                as_at_date=as_at_date,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
