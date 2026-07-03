from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from uuid import UUID

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.bank.extraction_gate import assert_bank_line_upload_extracted

router = APIRouter(prefix="/api/receipt-inbox", tags=["receipt-inbox"])


class LinkBankLineRequest(BaseModel):
    bank_line_id: UUID


@router.get("")
def list_receipts(
    auth: UserAuth,
    organisation_id: str,
    status: str = Query(default="all"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)

    q = (
        db.table("invoices_extracted")
        .select(
            "id, invoice_raw_id, supplier_name, invoice_number, invoice_date, "
            "total_amount, document_type, parse_status, created_at"
        )
        .eq("organisation_id", organisation_id)
        .eq("document_type", "receipt")
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )

    rows = q.execute().data or []

    if status == "matched":
        matched_ids = {
            r["receipt_document_id"]
            for r in (
                db.table("bank_statement_lines")
                .select("receipt_document_id")
                .eq("organisation_id", organisation_id)
                .not_.is_("receipt_document_id", "null")
                .execute()
                .data or []
            )
        }
        rows = [r for r in rows if r["id"] in matched_ids]
    elif status == "unmatched":
        matched_ids = {
            r["receipt_document_id"]
            for r in (
                db.table("bank_statement_lines")
                .select("receipt_document_id")
                .eq("organisation_id", organisation_id)
                .not_.is_("receipt_document_id", "null")
                .execute()
                .data or []
            )
        }
        rows = [r for r in rows if r["id"] not in matched_ids]

    # Annotate each receipt with its linked bank line (if any)
    if rows:
        receipt_ids = [r["id"] for r in rows]
        linked = (
            db.table("bank_statement_lines")
            .select("id, receipt_document_id, line_date, description, signed_amount")
            .in_("receipt_document_id", receipt_ids)
            .execute()
            .data or []
        )
        linked_by_receipt = {r["receipt_document_id"]: r for r in linked}
        for row in rows:
            row["linked_bank_line"] = linked_by_receipt.get(row["id"])

    return {"success": True, "receipts": rows, "total": len(rows)}


@router.post("/{receipt_id}/link")
def link_bank_line(
    receipt_id: str,
    payload: LinkBankLineRequest,
    auth: UserAuth,
    organisation_id: str,
):
    user_id, db = auth
    ensure_org_write(str(user_id), organisation_id)

    receipt = (
        db.table("invoices_extracted")
        .select("id, organisation_id")
        .eq("id", receipt_id)
        .eq("organisation_id", organisation_id)
        .maybe_single()
        .execute()
        .data
    )
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")

    line = (
        db.table("bank_statement_lines")
        .select("id, organisation_id, bank_statement_upload_id")
        .eq("id", str(payload.bank_line_id))
        .eq("organisation_id", organisation_id)
        .maybe_single()
        .execute()
        .data
    )
    if not line:
        raise HTTPException(status_code=404, detail="Bank line not found")
    try:
        assert_bank_line_upload_extracted(
            db,
            organisation_id=organisation_id,
            line=line,
            action="Link receipt to bank transaction",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.table("bank_statement_lines").update(
        {"receipt_document_id": receipt_id}
    ).eq("id", str(payload.bank_line_id)).execute()

    return {"success": True, "receipt_id": receipt_id, "bank_line_id": str(payload.bank_line_id)}


@router.delete("/{receipt_id}/link")
def unlink_bank_line(
    receipt_id: str,
    auth: UserAuth,
    organisation_id: str,
):
    user_id, db = auth
    ensure_org_write(str(user_id), organisation_id)

    db.table("bank_statement_lines").update(
        {"receipt_document_id": None}
    ).eq("receipt_document_id", receipt_id).eq("organisation_id", organisation_id).execute()

    return {"success": True, "receipt_id": receipt_id}
