from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.schemas.bank import BankAccountCreate, BankBalanceSummary
from app.services.bank_account_summary import build_bank_balance_summary
from app.services.bank_statement_service import money
from app.services.protected_accounts import assert_manual_posting_account_allowed


def _one(res, message: str):
    data = getattr(res, "data", None) or []
    if not data:
        raise HTTPException(status_code=404, detail=message)
    return data[0]


def list_accounts(db, *, organisation_id: str) -> list[dict[str, Any]]:
    res = (
        db.table("bank_accounts")
        .select("*")
        .eq("organisation_id", organisation_id)
        .order("name")
        .execute()
    )
    return res.data or []


def is_unreconciled_bank_line(line: dict[str, Any]) -> bool:
    posting_status = str(line.get("posting_status") or "unposted").lower()
    allocation_status = str(line.get("allocation_status") or "unallocated").lower()
    review_status = str(line.get("review_status") or "pending").lower()
    if review_status in {"ignored", "deferred"}:
        return False
    return (
        posting_status != "posted"
        or allocation_status not in {"allocated", "split"}
        or review_status != "reviewed"
    )


def bank_line_sort_key(line: dict[str, Any]) -> tuple[str, int, str]:
    row_index = line.get("source_row_index")
    try:
        row_order = int(row_index) if row_index is not None else 999999
    except (TypeError, ValueError):
        row_order = 999999
    return (str(line.get("line_date") or ""), row_order, str(line.get("id") or ""))


def get_unreconciled_lines_payload(
    db,
    *,
    organisation_id: str,
    account_id: str,
) -> dict[str, Any]:
    account = _one(
        db.table("bank_accounts")
        .select("*")
        .eq("id", account_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank account not found",
    )
    rows = (
        db.table("bank_statement_lines")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("bank_account_id", account_id)
        .limit(5000)
        .execute()
        .data
        or []
    )
    lines = sorted((row for row in rows if is_unreconciled_bank_line(row)), key=bank_line_sort_key)

    try:
        upload_rows = (
            db.table("bank_statement_uploads")
            .select("*")
            .eq("organisation_id", organisation_id)
            .eq("bank_account_id", account_id)
            .execute()
            .data
            or []
        )
    except Exception:
        upload_rows = []
    uploads_by_id = {str(row.get("id")): row for row in upload_rows if row.get("id")}

    top_suggestion: dict[str, dict[str, Any]] = {}
    if lines:
        line_id_strs = [str(line["id"]) for line in lines[:500]]
        sug_rows = (
            db.table("bank_transaction_suggestions")
            .select("bank_statement_line_id, confidence_score, suggested_account_id, suggested_tax_treatment, matched_invoice_number")
            .eq("organisation_id", organisation_id)
            .in_("bank_statement_line_id", line_id_strs)
            .eq("status", "open")
            .order("confidence_score", desc=True)
            .limit(5000)
            .execute()
            .data
            or []
        )
        for suggestion in sug_rows:
            line_id = str(suggestion.get("bank_statement_line_id") or "")
            if line_id and line_id not in top_suggestion:
                top_suggestion[line_id] = suggestion

    enriched = []
    for line in lines:
        upload = uploads_by_id.get(str(line.get("bank_statement_upload_id"))) or {}
        suggestion = top_suggestion.get(str(line.get("id") or ""), {})
        confidence = float(
            suggestion.get("confidence_score")
            or (0.75 if line.get("match_status") == "suggested" else 0.5)
        )
        enriched.append({
            **line,
            "upload_original_filename": upload.get("original_filename"),
            "upload_uploaded_at": upload.get("uploaded_at"),
            "recon_confidence": confidence,
            "recon_suggested_account_id": suggestion.get("suggested_account_id"),
            "recon_suggested_tax": suggestion.get("suggested_tax_treatment"),
            "recon_matched_invoice_ref": suggestion.get("matched_invoice_number") or line.get("matched_invoice_number"),
        })

    balances = BankBalanceSummary.model_validate(
        build_bank_balance_summary(
            db,
            organisation_id=organisation_id,
            account=account,
            lines=rows,
            uploads=upload_rows,
        )
    )
    return {
        "account": account,
        "lines": enriched,
        "balances": balances.model_dump(),
    }


def create_bank_account_record(
    db,
    *,
    payload: BankAccountCreate,
    organisation_id: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    requested_opening = money(payload.opening_balance)
    if requested_opening:
        raise HTTPException(
            status_code=400,
            detail="Bank/Cash opening balances must be captured in Chart of Accounts.",
        )

    gl_account_id: str | None = str(payload.gl_account_id) if payload.gl_account_id else None
    if gl_account_id:
        try:
            assert_manual_posting_account_allowed(
                db,
                organisation_id=organisation_id,
                account_id=gl_account_id,
                action="Link bank account",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not gl_account_id:
        code_rows = (
            db.table("accounts")
            .select("code")
            .eq("organisation_id", organisation_id)
            .like("code", "6200%")
            .execute()
            .data
            or []
        )
        used: set[int] = set()
        for row in code_rows:
            try:
                used.add(int(row["code"]))
            except (TypeError, ValueError):
                pass
        next_code = 6200001
        while next_code in used:
            next_code += 1
        if next_code > 6200999:
            raise HTTPException(status_code=400, detail="No bank GL codes available in range 6200001–6200999")
        gl_res = (
            db.table("accounts")
            .insert({
                "organisation_id": organisation_id,
                "code": str(next_code),
                "name": payload.name,
                "type": "asset",
                "group_name": "Bank",
                "vat_treatment": "full",
                "is_system": False,
                "active": True,
            })
            .execute()
        )
        if not gl_res.data:
            raise HTTPException(status_code=500, detail="Failed to create GL account for bank account")
        gl_account_id = str(gl_res.data[0]["id"])

    row = {
        "organisation_id": organisation_id,
        "name": payload.name,
        "institution_name": payload.institution_name,
        "account_type": payload.account_type,
        "currency": payload.currency,
        "account_number_mask": payload.account_number_mask,
        "account_number_hash": payload.account_number_hash,
        "gl_account_id": gl_account_id,
        "opening_balance": 0.0,
        "current_reconciled_balance": 0.0,
        "active": True,
    }
    bank_account = _one(db.table("bank_accounts").insert(row).execute(), "Bank account create failed")
    return bank_account


def create_bank_supplier_if_missing(
    db,
    *,
    organisation_id: str,
    payload: BankAccountCreate,
) -> None:
    try:
        db.table("suppliers").insert({
            "organisation_id": organisation_id,
            "supplier_name": payload.institution_name or payload.name,
            "bank_name": payload.institution_name,
            "active": True,
            "line_items_include_vat": True,
        }).execute()
    except Exception:
        pass
