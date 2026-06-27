from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.recurring_transactions import (
    approve_draft,
    create_template,
    delete_template,
    get_template,
    list_drafts,
    list_templates,
    skip_draft,
    update_template,
)

router = APIRouter(prefix="/api/recurring-transactions", tags=["recurring-transactions"])


# ─── Templates ────────────────────────────────────────────────────────────────

@router.get("")
def list_templates_route(auth: UserAuth, organisation_id: str):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    return {"success": True, "templates": list_templates(db, organisation_id)}


@router.post("")
def create_template_route(auth: UserAuth, organisation_id: str, payload: dict):
    user_id, db = auth
    ensure_org_write(str(user_id), organisation_id)
    try:
        return {"success": True, "template": create_template(db, organisation_id, str(user_id), payload)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/drafts")
def list_drafts_route(
    auth: UserAuth,
    organisation_id: str,
    status: str = Query(default="pending"),
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    return {"success": True, "drafts": list_drafts(db, organisation_id, status)}


@router.post("/drafts/{draft_id}/approve")
def approve_draft_route(draft_id: str, auth: UserAuth, organisation_id: str):
    user_id, db = auth
    ensure_org_write(str(user_id), organisation_id)
    try:
        return {"success": True, **approve_draft(db, organisation_id, draft_id, str(user_id))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/drafts/{draft_id}/skip")
def skip_draft_route(draft_id: str, auth: UserAuth, organisation_id: str):
    user_id, db = auth
    ensure_org_write(str(user_id), organisation_id)
    try:
        return {"success": True, **skip_draft(db, organisation_id, draft_id, str(user_id))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{template_id}")
def get_template_route(template_id: str, auth: UserAuth, organisation_id: str):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        return {"success": True, "template": get_template(db, organisation_id, template_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/{template_id}")
def update_template_route(template_id: str, auth: UserAuth, organisation_id: str, payload: dict):
    user_id, db = auth
    ensure_org_write(str(user_id), organisation_id)
    try:
        return {"success": True, "template": update_template(db, organisation_id, template_id, payload)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{template_id}")
def delete_template_route(template_id: str, auth: UserAuth, organisation_id: str):
    user_id, db = auth
    ensure_org_write(str(user_id), organisation_id)
    try:
        delete_template(db, organisation_id, template_id)
        return {"success": True}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
