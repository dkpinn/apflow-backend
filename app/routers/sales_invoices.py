from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from app.dependencies import (
    UserAuth,
    ensure_org_admin,
    ensure_org_read,
    ensure_org_write,
    org_role_for_user,
)
from app.services.sales_invoice_documents import (
    CUSTOMER_DOCUMENT_BUCKET,
    persist_sales_invoice_pdf,
    render_sales_invoice_pdf,
    send_sales_invoice_email,
)
from app.services.sales_invoices import (
    build_rebill_lines,
    calculate_sales_invoice,
    default_due_date,
    issue_sales_invoice,
    validate_customer_line_tracking,
)


router = APIRouter(prefix="/api/sales-invoices", tags=["sales-invoices"])


class SalesInvoiceLineInput(BaseModel):
    id: Optional[str] = None
    description: str = Field(min_length=1, max_length=2000)
    item_code: Optional[str] = Field(default=None, max_length=200)
    quantity: float = Field(default=1, gt=0)
    unit_price: float = Field(default=0, ge=0)
    prices_include_vat: bool = False
    discount_percent: float = Field(default=0, ge=0, le=100)
    discount_amount: float = Field(default=0, ge=0)
    vat_treatment: Literal["standard", "zero_rated", "exempt"] = "standard"
    vat_rate: float = Field(default=15, ge=0, le=100)
    revenue_account_id: str
    tracking: dict[str, str] = Field(default_factory=dict)
    source_invoice_extracted_id: Optional[str] = None
    source_invoice_line_id: Optional[str] = None
    source_unit_cost: Optional[float] = Field(default=None, ge=0)
    markup_percent: Optional[float] = Field(default=None, ge=-100)
    sort_order: int = Field(default=0, ge=0, le=32767)


class SalesInvoiceInput(BaseModel):
    organisation_id: str
    customer_id: str
    issue_date: Optional[date] = None
    due_date: Optional[date] = None
    currency: str = Field(default="ZAR", min_length=3, max_length=3)
    customer_reference: Optional[str] = Field(default=None, max_length=300)
    purchase_order_number: Optional[str] = Field(default=None, max_length=300)
    notes: Optional[str] = Field(default=None, max_length=10000)
    lines: list[SalesInvoiceLineInput] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class RebillRequest(BaseModel):
    organisation_id: str
    source_invoice_line_ids: list[str] = Field(min_length=1)
    revenue_account_id: str
    markup_percent: float = Field(default=0, ge=-100)


class IssueRequest(BaseModel):
    organisation_id: str


class SendRequest(BaseModel):
    organisation_id: str
    recipient_email: Optional[str] = Field(default=None, max_length=320)


class CreditNoteRequest(BaseModel):
    organisation_id: str
    reason: str = Field(min_length=1, max_length=2000)
    lines: list[SalesInvoiceLineInput] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _one(result, detail: str) -> dict[str, Any]:
    if not result.data:
        raise HTTPException(status_code=404, detail=detail)
    return result.data[0] if isinstance(result.data, list) else result.data


def _customer(db, organisation_id: str, customer_id: str) -> dict[str, Any]:
    return _one(
        db.table("customers")
        .select("*")
        .eq("id", customer_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Customer not found",
    )


def _organisation(db, organisation_id: str) -> dict[str, Any]:
    return _one(
        db.table("organisations")
        .select("id, currency, base_currency, default_payment_terms, invoice_approval_required")
        .eq("id", organisation_id)
        .limit(1)
        .execute(),
        "Organisation not found",
    )


def _invoice(db, organisation_id: str, invoice_id: str) -> dict[str, Any]:
    return _one(
        db.table("sales_invoices")
        .select("*")
        .eq("id", invoice_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Sales invoice not found",
    )


def _lines(db, organisation_id: str, invoice_id: str) -> list[dict[str, Any]]:
    return (
        db.table("sales_invoice_lines")
        .select("*")
        .eq("sales_invoice_id", invoice_id)
        .eq("organisation_id", organisation_id)
        .order("sort_order")
        .order("id")
        .execute()
        .data
        or []
    )


def _validate_revenue_accounts(
    db,
    *,
    organisation_id: str,
    lines: list[dict[str, Any]],
) -> None:
    account_ids = list(
        dict.fromkeys(str(line.get("revenue_account_id")) for line in lines if line.get("revenue_account_id"))
    )
    accounts = (
        db.table("accounts")
        .select("id, type, active, organisation_id")
        .eq("organisation_id", organisation_id)
        .in_("id", account_ids)
        .execute()
        .data
        or []
    ) if account_ids else []
    valid = {
        str(account["id"])
        for account in accounts
        if account.get("type") == "income" and account.get("active") is not False
    }
    if len(valid) != len(account_ids):
        raise ValueError("Every sales line needs an active income account")


def _calculated_lines(payload_lines: list[SalesInvoiceLineInput]) -> dict[str, Any]:
    if not payload_lines:
        return {
            "lines": [],
            "subtotal": 0.0,
            "discount_total": 0.0,
            "tax_total": 0.0,
            "total_amount": 0.0,
        }
    return calculate_sales_invoice(
        [line.model_dump(exclude={"id"}) for line in payload_lines]
    )


def _replace_lines(
    db,
    *,
    organisation_id: str,
    invoice_id: str,
    calculated: dict[str, Any],
) -> None:
    db.table("sales_invoice_lines").delete().eq("sales_invoice_id", invoice_id).eq(
        "organisation_id", organisation_id
    ).execute()
    rows = [
        {
            key: value
            for key, value in {
                **line,
                "organisation_id": organisation_id,
                "sales_invoice_id": invoice_id,
            }.items()
            if key not in {"source_cost_total", "margin_amount", "id"}
        }
        for line in calculated["lines"]
    ]
    if rows:
        db.table("sales_invoice_lines").insert(rows).execute()


def _sync_totals(db, organisation_id: str, invoice_id: str) -> dict[str, Any]:
    lines = _lines(db, organisation_id, invoice_id)
    calculated = calculate_sales_invoice(lines)
    _validate_revenue_accounts(db, organisation_id=organisation_id, lines=calculated["lines"])
    validate_customer_line_tracking(
        db, organisation_id=organisation_id, lines=calculated["lines"]
    )
    db.table("sales_invoices").update(
        {
            "subtotal": calculated["subtotal"],
            "discount_total": calculated["discount_total"],
            "tax_total": calculated["tax_total"],
            "total_amount": calculated["total_amount"],
        }
    ).eq("id", invoice_id).eq("organisation_id", organisation_id).execute()
    return calculated


def _detail(db, organisation_id: str, invoice_id: str) -> dict[str, Any]:
    invoice = _invoice(db, organisation_id, invoice_id)
    invoice["lines"] = _lines(db, organisation_id, invoice_id)
    invoice["customer"] = _customer(db, organisation_id, str(invoice["customer_id"]))
    invoice["delivery_events"] = (
        db.table("sales_invoice_delivery_events")
        .select("*")
        .eq("sales_invoice_id", invoice_id)
        .eq("organisation_id", organisation_id)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    invoice["audit_events"] = (
        db.table("sales_invoice_audit_events")
        .select("*")
        .eq("sales_invoice_id", invoice_id)
        .eq("organisation_id", organisation_id)
        .order("created_at")
        .execute()
        .data
        or []
    )
    invoice["receipt_allocations"] = (
        db.table("customer_receipt_allocations")
        .select("*, customer_receipts(*)")
        .eq("sales_invoice_id", invoice_id)
        .eq("organisation_id", organisation_id)
        .execute()
        .data
        or []
    )
    invoice["credit_notes"] = (
        db.table("sales_invoices")
        .select("id, invoice_number, issue_date, total_amount, status, credit_reason")
        .eq("original_invoice_id", invoice_id)
        .eq("organisation_id", organisation_id)
        .order("created_at")
        .execute()
        .data
        or []
    )
    if invoice.get("original_invoice_id"):
        original = _invoice(db, organisation_id, str(invoice["original_invoice_id"]))
        invoice["original_invoice_number"] = original.get("invoice_number")
    return invoice


@router.get("")
def list_sales_invoices(
    organisation_id: str,
    auth: UserAuth,
    status: Optional[str] = None,
    payment_status: Optional[str] = None,
    customer_id: Optional[str] = None,
    search: Optional[str] = Query(default=None, max_length=200),
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    query = (
        db.table("sales_invoices")
        .select("*, customers(legal_name, trading_name, customer_code)")
        .eq("organisation_id", organisation_id)
        .order("created_at", desc=True)
        .limit(500)
    )
    if status:
        query = query.eq("status", status)
    if payment_status:
        query = query.eq("payment_status", payment_status)
    if customer_id:
        query = query.eq("customer_id", customer_id)
    rows = query.execute().data or []
    needle = (search or "").strip().lower()
    if needle:
        rows = [
            row
            for row in rows
            if needle
            in " ".join(
                [
                    str(row.get("invoice_number") or "").lower(),
                    str(row.get("customer_reference") or "").lower(),
                    str(row.get("purchase_order_number") or "").lower(),
                    str((row.get("customers") or {}).get("legal_name") or "").lower(),
                ]
            )
        ]
    today = date.today().isoformat()
    for row in rows:
        if (
            row.get("status") == "issued"
            and row.get("payment_status") == "unpaid"
            and row.get("due_date")
            and str(row["due_date"]) < today
        ):
            row["payment_status"] = "overdue"
    return rows


@router.post("", status_code=201)
def create_sales_invoice(payload: SalesInvoiceInput, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    customer = _customer(db, payload.organisation_id, payload.customer_id)
    organisation = _organisation(db, payload.organisation_id)
    calculated = _calculated_lines(payload.lines)
    _validate_revenue_accounts(
        db, organisation_id=payload.organisation_id, lines=calculated["lines"]
    )
    issue_on = payload.issue_date or date.today()
    due_on = payload.due_date or default_due_date(
        issue_date=issue_on,
        customer_terms_days=customer.get("payment_terms_days"),
        organisation_terms_days=organisation.get("default_payment_terms"),
    )
    try:
        result = db.table("sales_invoices").insert(
            {
                "organisation_id": payload.organisation_id,
                "customer_id": payload.customer_id,
                "issue_date": issue_on.isoformat(),
                "due_date": due_on.isoformat(),
                "currency": payload.currency,
                "customer_reference": payload.customer_reference,
                "purchase_order_number": payload.purchase_order_number,
                "notes": payload.notes,
                "subtotal": calculated["subtotal"],
                "discount_total": calculated["discount_total"],
                "tax_total": calculated["tax_total"],
                "total_amount": calculated["total_amount"],
                "created_by": str(user_id),
                "updated_by": str(user_id),
            }
        ).execute()
        invoice_id = str(result.data[0]["id"])
        _replace_lines(
            db,
            organisation_id=payload.organisation_id,
            invoice_id=invoice_id,
            calculated=calculated,
        )
        db.table("sales_invoice_audit_events").insert(
            {
                "organisation_id": payload.organisation_id,
                "sales_invoice_id": invoice_id,
                "event_type": "created",
                "actor_user_id": str(user_id),
            }
        ).execute()
        return _detail(db, payload.organisation_id, invoice_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{invoice_id}")
def get_sales_invoice(invoice_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    return _detail(db, organisation_id, invoice_id)


@router.put("/{invoice_id}")
def update_sales_invoice(
    invoice_id: str,
    payload: SalesInvoiceInput,
    auth: UserAuth,
):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    current = _invoice(db, payload.organisation_id, invoice_id)
    if current.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Only draft sales invoices can be edited")
    calculated = _calculated_lines(payload.lines)
    _validate_revenue_accounts(
        db, organisation_id=payload.organisation_id, lines=calculated["lines"]
    )
    db.table("sales_invoices").update(
        {
            "customer_id": payload.customer_id,
            "issue_date": payload.issue_date.isoformat() if payload.issue_date else None,
            "due_date": payload.due_date.isoformat() if payload.due_date else None,
            "currency": payload.currency,
            "customer_reference": payload.customer_reference,
            "purchase_order_number": payload.purchase_order_number,
            "notes": payload.notes,
            "subtotal": calculated["subtotal"],
            "discount_total": calculated["discount_total"],
            "tax_total": calculated["tax_total"],
            "total_amount": calculated["total_amount"],
            "updated_by": str(user_id),
        }
    ).eq("id", invoice_id).eq("organisation_id", payload.organisation_id).execute()
    _replace_lines(
        db,
        organisation_id=payload.organisation_id,
        invoice_id=invoice_id,
        calculated=calculated,
    )
    return _detail(db, payload.organisation_id, invoice_id)


@router.post("/{invoice_id}/rebill-lines")
def add_rebill_lines(invoice_id: str, payload: RebillRequest, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    invoice = _invoice(db, payload.organisation_id, invoice_id)
    if invoice.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Only drafts can receive rebilled lines")
    source_rows = (
        db.table("invoice_line_items")
        .select("*, invoices_extracted!inner(organisation_id)")
        .in_("id", payload.source_invoice_line_ids)
        .eq("invoices_extracted.organisation_id", payload.organisation_id)
        .execute()
        .data
        or []
    )
    if len(source_rows) != len(set(payload.source_invoice_line_ids)):
        raise HTTPException(status_code=400, detail="One or more supplier invoice lines were not found")
    for row in source_rows:
        row["invoice_extracted_id"] = row.get("invoice_extracted_id")
    rebill = build_rebill_lines(
        source_rows,
        default_revenue_account_id=payload.revenue_account_id,
        markup_percent=payload.markup_percent,
    )
    current_count = len(_lines(db, payload.organisation_id, invoice_id))
    inserts = [
        {
            key: value
            for key, value in {
                **line,
                "organisation_id": payload.organisation_id,
                "sales_invoice_id": invoice_id,
                "sort_order": current_count + index,
            }.items()
            if key not in {"source_cost_total", "margin_amount"}
        }
        for index, line in enumerate(rebill)
    ]
    db.table("sales_invoice_lines").insert(inserts).execute()
    calculated = _sync_totals(db, payload.organisation_id, invoice_id)
    return {"lines": rebill, **{key: value for key, value in calculated.items() if key != "lines"}}


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
