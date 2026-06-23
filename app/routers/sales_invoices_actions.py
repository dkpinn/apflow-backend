from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.dependencies import (
    UserAuth,
    ensure_org_read,
    ensure_org_write,
    org_role_for_user,
)
from app.services.sales_invoice_documents import (
    persist_sales_invoice_pdf,
    render_sales_invoice_pdf,
    send_sales_invoice_email,
)
from app.services.sales_invoices import issue_sales_invoice
from app.routers.sales_invoices import (
    CreditNoteRequest,
    IssueRequest,
    SalesInvoiceLineInput,
    SendRequest,
    _calculated_lines,
    _detail,
    _invoice,
    _lines,
    _now,
    _organisation,
    _replace_lines,
    _sync_totals,
)


router = APIRouter(prefix="/api/sales-invoices", tags=["sales-invoices"])


@router.post("/{invoice_id}/submit")
def submit_sales_invoice(invoice_id: str, payload: IssueRequest, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    invoice = _invoice(db, payload.organisation_id, invoice_id)
    if invoice.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Only draft invoices can be submitted")
    calculated = _sync_totals(db, payload.organisation_id, invoice_id)
    organisation = _organisation(db, payload.organisation_id)
    if not organisation.get("invoice_approval_required", True):
        return {"success": True, "status": "draft", "ready_to_issue": True, **calculated}
    try:
        result = db.rpc(
            "create_sales_invoice_approval_request",
            {
                "p_org_id": payload.organisation_id,
                "p_sales_invoice_id": invoice_id,
                "p_amount": calculated["total_amount"],
                "p_requested_by": str(user_id),
            },
        ).execute()
        request_id = result.data[0] if isinstance(result.data, list) and result.data else result.data
        if not request_id:
            db.table("sales_invoices").update(
                {"status": "pending_approval", "updated_by": str(user_id)}
            ).eq("id", invoice_id).execute()
        return {
            "success": True,
            "status": "pending_approval",
            "approval_request_id": request_id,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{invoice_id}/approve")
def approve_sales_invoice(invoice_id: str, payload: IssueRequest, auth: UserAuth):
    user_id, db = auth
    ensure_org_read(str(user_id), payload.organisation_id)
    invoice = _invoice(db, payload.organisation_id, invoice_id)
    if invoice.get("status") != "pending_approval":
        raise HTTPException(status_code=409, detail="Invoice is not awaiting approval")
    role = org_role_for_user(str(user_id), payload.organisation_id)
    allowed = role in {"owner", "admin"}
    request_id = invoice.get("approval_request_id")
    if request_id and not allowed:
        pending = (
            db.table("approval_request_steps")
            .select("id, approver_user_id, approver_role")
            .eq("request_id", request_id)
            .eq("status", "pending")
            .execute()
            .data
            or []
        )
        allowed = any(
            str(step.get("approver_user_id") or "") == str(user_id)
            or (step.get("approver_role") and step.get("approver_role") == role)
            for step in pending
        )
    if not allowed:
        raise HTTPException(status_code=403, detail="You are not an approver for this invoice")
    if request_id:
        pending_steps = (
            db.table("approval_request_steps")
            .select("id, step_order")
            .eq("request_id", request_id)
            .eq("status", "pending")
            .order("step_order")
            .execute()
            .data
            or []
        )
        current_step = pending_steps[0] if pending_steps else None
        waiting_steps = (
            db.table("approval_request_steps")
            .select("id, step_order")
            .eq("request_id", request_id)
            .eq("status", "waiting")
            .order("step_order")
            .execute()
            .data
            or []
        )
        if waiting_steps:
            if current_step:
                db.table("approval_request_steps").update(
                    {
                        "status": "approved",
                        "actioned_by": str(user_id),
                        "actioned_at": _now(),
                    }
                ).eq("id", current_step["id"]).execute()
            next_step = waiting_steps[0]
            db.table("approval_request_steps").update(
                {"status": "pending", "included_at": _now()}
            ).eq("id", next_step["id"]).execute()
            db.table("approval_requests").update(
                {"current_step_order": next_step["step_order"]}
            ).eq("id", request_id).execute()
            return _detail(db, payload.organisation_id, invoice_id)
    db.table("sales_invoices").update(
        {
            "status": "approved",
            "approved_by": str(user_id),
            "approved_at": _now(),
            "updated_by": str(user_id),
        }
    ).eq("id", invoice_id).eq("organisation_id", payload.organisation_id).execute()
    if request_id:
        if current_step:
            db.table("approval_request_steps").update(
                {
                    "status": "approved",
                    "actioned_by": str(user_id),
                    "actioned_at": _now(),
                }
            ).eq("id", current_step["id"]).execute()
        db.table("approval_requests").update(
            {"status": "approved", "completed_at": _now()}
        ).eq("id", request_id).execute()
    return _detail(db, payload.organisation_id, invoice_id)


@router.post("/{invoice_id}/issue")
def issue_invoice(invoice_id: str, payload: IssueRequest, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    try:
        result = issue_sales_invoice(
            db,
            organisation_id=payload.organisation_id,
            sales_invoice_id=invoice_id,
            actor_user_id=str(user_id),
        )
        detail = _detail(db, payload.organisation_id, invoice_id)
        try:
            result["pdf_storage_path"] = persist_sales_invoice_pdf(
                db, detail, detail["lines"]
            )
        except Exception as exc:
            result["pdf_error"] = str(exc)
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{invoice_id}/pdf")
def download_invoice_pdf(invoice_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    detail = _detail(db, organisation_id, invoice_id)
    if detail.get("status") != "issued":
        raise HTTPException(status_code=409, detail="Only issued invoices have final PDFs")
    pdf = render_sales_invoice_pdf(detail, detail["lines"])
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{detail.get("invoice_number")}.pdf"'
        },
    )


@router.post("/{invoice_id}/send")
def send_invoice(invoice_id: str, payload: SendRequest, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    detail = _detail(db, payload.organisation_id, invoice_id)
    if detail.get("status") != "issued":
        raise HTTPException(status_code=409, detail="Only issued invoices can be sent")
    recipient = payload.recipient_email or detail["customer"].get("default_email")
    if not recipient:
        raise HTTPException(status_code=400, detail="Customer has no invoice email address")
    try:
        return send_sales_invoice_email(
            db,
            invoice=detail,
            pdf_bytes=render_sales_invoice_pdf(detail, detail["lines"]),
            recipient_email=recipient,
            actor_user_id=str(user_id),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/{invoice_id}/credit-notes", status_code=201)
def create_credit_note(
    invoice_id: str,
    payload: CreditNoteRequest,
    auth: UserAuth,
):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    original = _invoice(db, payload.organisation_id, invoice_id)
    if original.get("document_type") != "invoice" or original.get("status") != "issued":
        raise HTTPException(status_code=409, detail="Credit notes require an issued sales invoice")
    source_lines = payload.lines
    if not source_lines:
        source_lines = [
            SalesInvoiceLineInput(**{
                key: value
                for key, value in line.items()
                if key in SalesInvoiceLineInput.model_fields
            })
            for line in _lines(db, payload.organisation_id, invoice_id)
        ]
    calculated = _calculated_lines(source_lines)
    result = db.table("sales_invoices").insert(
        {
            "organisation_id": payload.organisation_id,
            "customer_id": original["customer_id"],
            "document_type": "credit_note",
            "original_invoice_id": invoice_id,
            "credit_reason": payload.reason.strip(),
            "issue_date": date.today().isoformat(),
            "due_date": date.today().isoformat(),
            "currency": original["currency"],
            "subtotal": calculated["subtotal"],
            "discount_total": calculated["discount_total"],
            "tax_total": calculated["tax_total"],
            "total_amount": calculated["total_amount"],
            "created_by": str(user_id),
            "updated_by": str(user_id),
        }
    ).execute()
    credit_id = str(result.data[0]["id"])
    _replace_lines(
        db,
        organisation_id=payload.organisation_id,
        invoice_id=credit_id,
        calculated=calculated,
    )
    return _detail(db, payload.organisation_id, credit_id)
