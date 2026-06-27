from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.supplier_payment_runs import (
    approve_supplier_payment_run_draft,
    build_supplier_payment_run,
    cancel_supplier_payment_run_draft,
    create_supplier_payment_run_draft,
    export_supplier_payment_run_draft_csv,
    get_supplier_payment_run_draft,
    list_supplier_payment_run_drafts,
)


router = APIRouter(prefix="/api/supplier-payment-runs", tags=["supplier-payment-runs"])


class SupplierPaymentRunDraftRequest(BaseModel):
    organisation_id: str
    pay_on_date: str = Field(default_factory=lambda: date.today().isoformat())
    due_within_days: int = Field(default=7, ge=0, le=90)
    selected_invoice_ids: list[str] = Field(min_length=1)
    notes: str | None = None


class DraftActionRequest(BaseModel):
    organisation_id: str


@router.get("")
def supplier_payment_run_preview(
    auth: UserAuth,
    organisation_id: str,
    pay_on_date: str = Query(default_factory=lambda: date.today().isoformat()),
    due_within_days: int = Query(default=7, ge=0, le=90),
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        return {
            "success": True,
            "payment_run": build_supplier_payment_run(
                db,
                organisation_id=organisation_id,
                pay_on_date=pay_on_date,
                due_within_days=due_within_days,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/drafts")
def list_drafts(
    auth: UserAuth,
    organisation_id: str,
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    return {
        "success": True,
        "drafts": list_supplier_payment_run_drafts(db, organisation_id),
    }


@router.get("/drafts/{draft_id}")
def get_draft(
    auth: UserAuth,
    draft_id: str,
    organisation_id: str,
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        return {
            "success": True,
            "draft": get_supplier_payment_run_draft(db, draft_id, organisation_id),
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/drafts")
def create_supplier_payment_run_draft_route(
    payload: SupplierPaymentRunDraftRequest,
    auth: UserAuth,
):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    try:
        return {
            "success": True,
            **create_supplier_payment_run_draft(
                db,
                organisation_id=payload.organisation_id,
                pay_on_date=payload.pay_on_date,
                due_within_days=payload.due_within_days,
                selected_invoice_ids=payload.selected_invoice_ids,
                created_by=str(user_id),
                notes=payload.notes,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/drafts/{draft_id}/approve")
def approve_draft(
    payload: DraftActionRequest,
    auth: UserAuth,
    draft_id: str,
):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    try:
        return {
            "success": True,
            "draft": approve_supplier_payment_run_draft(
                db, draft_id, payload.organisation_id, str(user_id)
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/drafts/{draft_id}/cancel")
def cancel_draft(
    payload: DraftActionRequest,
    auth: UserAuth,
    draft_id: str,
):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    try:
        return {
            "success": True,
            "draft": cancel_supplier_payment_run_draft(
                db, draft_id, payload.organisation_id
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/drafts/{draft_id}/export")
def export_draft(
    auth: UserAuth,
    draft_id: str,
    organisation_id: str,
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        filename, csv_bytes = export_supplier_payment_run_draft_csv(
            db, draft_id, organisation_id
        )
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
