from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.review_queue import get_review_counts, list_review_items, set_review_status

router = APIRouter(prefix="/api/review-queue", tags=["review-queue"])


class ReviewActionBody(BaseModel):
    organisation_id: str
    note: Optional[str] = None


@router.get("")
def get_review_queue(
    auth: UserAuth,
    organisation_id: str = Query(...),
    status: str = Query(default="pending"),
):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    items = list_review_items(db, organisation_id, filter_status=status)
    counts = get_review_counts(db, organisation_id)
    return {"success": True, "items": items, "counts": counts}


@router.post("/{invoice_id}/approve")
def approve_invoice(invoice_id: str, body: ReviewActionBody, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(user_id, body.organisation_id)
    try:
        result = set_review_status(
            db,
            invoice_id=invoice_id,
            organisation_id=body.organisation_id,
            new_status="approved",
            reviewed_by=user_id,
            note=body.note,
        )
        return {"success": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{invoice_id}/flag")
def flag_invoice(invoice_id: str, body: ReviewActionBody, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(user_id, body.organisation_id)
    try:
        result = set_review_status(
            db,
            invoice_id=invoice_id,
            organisation_id=body.organisation_id,
            new_status="needs_info",
            reviewed_by=user_id,
            note=body.note,
        )
        return {"success": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{invoice_id}/ignore")
def ignore_invoice(invoice_id: str, body: ReviewActionBody, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(user_id, body.organisation_id)
    try:
        result = set_review_status(
            db,
            invoice_id=invoice_id,
            organisation_id=body.organisation_id,
            new_status="ignored",
            reviewed_by=user_id,
            note=body.note,
        )
        return {"success": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
