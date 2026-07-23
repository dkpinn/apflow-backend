from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.schemas.bank import BankAccountCreate, BankBalanceSummary
from app.services.bank_account_summary import build_bank_balance_summary
from app.services.bank_statement_service import money
from app.services.bank.extraction_gate import (
    corrected_fixture_benchmark_blockers,
    upload_requires_corrected_fixture,
)
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


def _looks_like_serialized_csv_row(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return (
        text.startswith("{")
        and text.endswith("}")
        and ("'Date':" in text or '"Date":' in text)
        and ("'Reference':" in text or '"Reference":' in text)
    )


def line_for_reconciliation_display(line: dict[str, Any]) -> dict[str, Any]:
    """Return a bank line with an end-user-friendly description.

    Some CSV imports historically stored the entire serialized source row in
    ``description``.  Keep that raw value in the database for traceability, but
    never expose it as the transaction label when a parsed reference exists.
    """
    reference = line.get("reference")
    if _looks_like_serialized_csv_row(line.get("description")) and reference:
        return {**line, "description": str(reference)}
    return line


def supplier_invoice_match_eligibility(invoice: dict[str, Any] | None) -> tuple[bool, str | None]:
    """Return whether a supplier invoice may be settled from bank reconciliation."""
    if not invoice:
        return False, "matched_invoice_not_found"
    eligible = (
        str(invoice.get("review_status") or "").lower() == "approved"
        and str(invoice.get("approval_status") or "").lower() == "approved"
        and str(invoice.get("posting_status") or "").lower() == "posted"
    )
    return (True, None) if eligible else (False, "invoice_not_posted")


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
    def _is_blocked(upload: dict[str, Any]) -> bool:
        # Not yet reviewed/approved → blocked.
        if str(upload.get("extraction_status") or "").lower() != "extracted":
            return True
        # A failed/stale benchmark only blocks reconciliation when a corrected gold
        # fixture is actually required (strict mode). In the default optional/internal
        # mode a saved-but-failing internal benchmark must not keep approved lines out
        # of reconciliation — consistent with the approval gate.
        if not upload_requires_corrected_fixture(upload):
            return False
        return bool(
            corrected_fixture_benchmark_blockers(
                db,
                organisation_id=organisation_id,
                upload_id=str(upload.get("id")),
            )
        )

    blocked_upload_ids = {
        str(upload.get("id")) for upload in upload_rows if _is_blocked(upload)
    }
    candidate_lines = [row for row in rows if is_unreconciled_bank_line(row)]
    blocked_lines = [
        row
        for row in candidate_lines
        if str(row.get("bank_statement_upload_id") or "") in blocked_upload_ids
        or str(row.get("bank_statement_upload_id") or "") not in uploads_by_id
    ]
    lines = sorted(
        (
            row
            for row in candidate_lines
            if str(row.get("bank_statement_upload_id") or "") not in blocked_upload_ids
            and str(row.get("bank_statement_upload_id") or "") in uploads_by_id
        ),
        key=bank_line_sort_key,
    )

    top_suggestion: dict[str, dict[str, Any]] = {}
    if lines:
        line_id_strs = [str(line["id"]) for line in lines[:500]]
        sug_rows = (
            db.table("bank_transaction_suggestions")
            .select(
                "id, bank_statement_line_id, suggestion_type, confidence_score, "
                "suggested_account_id, suggested_tax_treatment, matched_invoice_id, "
                "matched_sales_invoice_id, matched_invoice_number, rationale, evidence"
            )
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
            if not line_id:
                continue
            current = top_suggestion.get(line_id)
            suggestion_matches_invoice = bool(suggestion.get("matched_invoice_number"))
            current_matches_invoice = bool((current or {}).get("matched_invoice_number"))
            if current is None or (suggestion_matches_invoice and not current_matches_invoice):
                top_suggestion[line_id] = suggestion

    supplier_invoice_ids = {
        str(suggestion.get("matched_invoice_id"))
        for suggestion in top_suggestion.values()
        if suggestion.get("matched_invoice_id")
    }
    supplier_invoices_by_id: dict[str, dict[str, Any]] = {}
    if supplier_invoice_ids:
        invoice_rows = (
            db.table("invoices_extracted")
            .select(
                "id, invoice_raw_id, invoice_number, invoice_date, due_date, total_amount, "
                "currency, supplier_id, supplier_name_extracted, review_status, "
                "approval_status, posting_status"
            )
            .eq("organisation_id", organisation_id)
            .in_("id", list(supplier_invoice_ids))
            .execute()
            .data
            or []
        )
        supplier_invoices_by_id = {str(row.get("id")): row for row in invoice_rows}

    enriched = []
    for line in lines:
        upload = uploads_by_id.get(str(line.get("bank_statement_upload_id"))) or {}
        suggestion = top_suggestion.get(str(line.get("id") or ""), {})
        matched_invoice = supplier_invoices_by_id.get(str(suggestion.get("matched_invoice_id") or ""), {})
        is_supplier_invoice_match = bool(suggestion.get("matched_invoice_id"))
        match_eligible, match_block_code = (
            supplier_invoice_match_eligibility(matched_invoice)
            if is_supplier_invoice_match
            else (None, None)
        )
        confidence = float(
            suggestion.get("confidence_score")
            or (0.75 if line.get("match_status") == "suggested" else 0.5)
        )
        enriched.append({
            **line_for_reconciliation_display(line),
            "upload_original_filename": upload.get("original_filename"),
            "upload_uploaded_at": upload.get("uploaded_at"),
            "recon_confidence": confidence,
            "recon_suggestion_id": suggestion.get("id"),
            "recon_suggestion_type": suggestion.get("suggestion_type"),
            "recon_suggested_account_id": suggestion.get("suggested_account_id"),
            "recon_suggested_tax": suggestion.get("suggested_tax_treatment"),
            "recon_matched_invoice_id": suggestion.get("matched_invoice_id") or suggestion.get("matched_sales_invoice_id"),
            "recon_matched_invoice_ref": suggestion.get("matched_invoice_number") or line.get("matched_invoice_number"),
            "recon_match_rationale": suggestion.get("rationale"),
            "recon_match_evidence": suggestion.get("evidence") or {},
            "recon_match_eligible": match_eligible,
            "recon_match_block_code": match_block_code,
            "recon_matched_supplier_name": matched_invoice.get("supplier_name_extracted"),
            "recon_matched_invoice_date": matched_invoice.get("invoice_date"),
            "recon_matched_invoice_due_date": matched_invoice.get("due_date"),
            "recon_matched_invoice_total": matched_invoice.get("total_amount"),
            "recon_matched_invoice_currency": matched_invoice.get("currency"),
            "recon_matched_invoice_raw_id": matched_invoice.get("invoice_raw_id"),
            "recon_matched_invoice_review_status": matched_invoice.get("review_status"),
            "recon_matched_invoice_approval_status": matched_invoice.get("approval_status"),
            "recon_matched_invoice_posting_status": matched_invoice.get("posting_status"),
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
        "blocked_extraction_line_count": len(blocked_lines),
        "blocked_extraction_upload_count": len(blocked_upload_ids),
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
