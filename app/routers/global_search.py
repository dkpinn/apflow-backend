from __future__ import annotations

from fastapi import APIRouter, Query

from app.dependencies import UserAuth, ensure_org_read
from app.services.global_search import search

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("")
def global_search(
    auth: UserAuth,
    organisation_id: str = Query(...),
    q: str = Query(default="", min_length=0),
    limit: int = Query(default=6, ge=1, le=20),
):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    if len(q.strip()) < 2:
        return {
            "results": [],
            "query": q,
            "is_partial": False,
            "errors": [],
            "searched_sources": [],
            "failed_sources": [],
        }
    return search(db, organisation_id, q, limit=limit)
