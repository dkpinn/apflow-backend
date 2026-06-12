from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.sales_invoices import post_customer_receipt


router = APIRouter(prefix="/api/customer-receipts", tags=["customer-receipts"])


class ReceiptAllocationInput(BaseModel):
    sales_invoice_id: str
    amount: float = Field(gt=0)


class CustomerReceiptInput(BaseModel):
    organisation_id: str
    customer_id: str
    bank_account_id: str
    receipt_date: date
    amount: float = Field(gt=0)
    currency: str = Field(default="ZAR", min_length=3, max_length=3)
    reference: Optional[str] = Field(default=None, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=5000)
    allocations: list[ReceiptAllocationInput] = Field(default_factory=list)
    bank_statement_line_id: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=200)


@router.get("")
def list_customer_receipts(
    organisation_id: str,
    auth: UserAuth,
    customer_id: Optional[str] = None,
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    query = (
        db.table("customer_receipts")
        .select("*, customers(legal_name), customer_receipt_allocations(*)")
        .eq("organisation_id", organisation_id)
        .order("receipt_date", desc=True)
        .limit(500)
    )
    if customer_id:
        query = query.eq("customer_id", customer_id)
    return query.execute().data or []


@router.post("", status_code=201)
def create_customer_receipt(payload: CustomerReceiptInput, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    try:
        return post_customer_receipt(
            db,
            organisation_id=payload.organisation_id,
            customer_id=payload.customer_id,
            bank_account_id=payload.bank_account_id,
            receipt_date=payload.receipt_date.isoformat(),
            amount=payload.amount,
            currency=payload.currency.upper(),
            reference=payload.reference,
            notes=payload.notes,
            allocations=[allocation.model_dump() for allocation in payload.allocations],
            actor_user_id=str(user_id),
            bank_statement_line_id=payload.bank_statement_line_id,
            idempotency_key=payload.idempotency_key,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

