from __future__ import annotations

from fastapi import APIRouter, Query

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.notifications import list_notifications, mark_all_read, mark_read

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
def get_notifications(auth: UserAuth, organisation_id: str = Query(...)):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    return {"success": True, **list_notifications(db, organisation_id)}


@router.post("/{notification_id}/read")
def read_notification(notification_id: str, auth: UserAuth, organisation_id: str = Query(...)):
    user_id, db = auth
    ensure_org_write(user_id, organisation_id)
    mark_read(db, organisation_id, notification_id)
    return {"success": True}


@router.post("/read-all")
def read_all_notifications(auth: UserAuth, organisation_id: str = Query(...)):
    user_id, db = auth
    ensure_org_write(user_id, organisation_id)
    mark_all_read(db, organisation_id)
    return {"success": True}
