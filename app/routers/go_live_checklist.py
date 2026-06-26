from __future__ import annotations

from fastapi import APIRouter

from app.dependencies import UserAuth, ensure_org_read
from app.services.go_live_checklist import generate_go_live_checklist

router = APIRouter(prefix="/api/go-live-checklist", tags=["go-live-checklist"])


@router.get("")
def go_live_checklist(auth: UserAuth, organisation_id: str):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    return {
        "success": True,
        "checklist": generate_go_live_checklist(db, organisation_id=organisation_id),
    }
