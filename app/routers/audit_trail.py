from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.dependencies import UserAuth, ensure_org_read
from app.services.audit_trail import list_audit_trail


router = APIRouter(prefix="/api/audit-trail", tags=["audit-trail"])


@router.get("")
def audit_trail(
    organisation_id: str,
    auth: UserAuth,
    source: Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    actor_user_id: Optional[str] = Query(default=None),
    entity_type: Optional[str] = Query(default=None),
    entity_id: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        return {
            "success": True,
            "audit_trail": list_audit_trail(
                db,
                organisation_id=organisation_id,
                source=source,
                event_type=event_type,
                actor_user_id=actor_user_id,
                entity_type=entity_type,
                entity_id=entity_id,
                date_from=date_from,
                date_to=date_to,
                search=search,
                limit=limit,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
