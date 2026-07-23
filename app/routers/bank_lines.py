"""bank_lines.py
Bank statement line operation routes: suggest, review, skip, delete, and
bulk-allocate.

Shared helpers, models, and private utilities are imported from bank.py.
bank.py does NOT import this module — no circular import risk.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.models.schemas import BulkAllocateRequest, LineSkipRequest
from app.services.bank_statement_service import (
    default_rule_criteria_from_line,
    money,
    normalize_rule_criteria,
    score_invoice_suggestions,
    score_rule_suggestions,
)
from app.services.accounting_locks import assert_accounting_period_unlocked
from app.services.bank.supplier_settlement import accept_supplier_invoice_bank_match
from app.services.bank.accounts import supplier_invoice_match_eligibility
from app.services.sales_invoices import post_customer_receipt
from app.services.protected_accounts import assert_manual_posting_account_allowed
from app.services.organisation_vat import vat_applicability
from app.services.bank.extraction_gate import assert_bank_line_upload_extracted, assert_bank_lines_uploads_extracted
from app.services.bank.ai_suggestions import score_ai_suggestions

from app.routers.bank import (
    BulkDeleteLinesRequest,
    ExtractUploadRequest,
    ReviewLineRequest,
    _auth,
    _bank_delete_error,
    _database_error_parts,
    _delete_bank_lines_rpc,
    _one,
    _rpc_data,
    journal_preview_lines,
    log_bank_event,
    now_iso,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bank", tags=["bank"])


@router.delete("/lines")
def delete_bank_lines(payload: BulkDeleteLinesRequest, auth: UserAuth):
    return bulk_delete_bank_lines(payload, auth)


@router.post("/lines/bulk-delete")
def bulk_delete_bank_lines(payload: BulkDeleteLinesRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    line_ids = [str(line_id) for line_id in payload.line_ids]
    try:
        result = _delete_bank_lines_rpc(
            db,
            organisation_id=organisation_id,
            line_ids=line_ids,
            actor_user_id=user_id,
        )
    except Exception as exc:
        raise _bank_delete_error(exc) from exc
    return {
        "success": True,
        "deleted_count": int(result.get("deleted_count") or 0),
    }


@router.post("/lines/{line_id}/suggest")
def suggest_bank_line(line_id: str, payload: ExtractUploadRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    line = _one(
        db.table("bank_statement_lines").select("*").eq("id", line_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank statement line not found",
    )
    try:
        assert_bank_line_upload_extracted(
            db,
            organisation_id=organisation_id,
            line=line,
            action="Suggest bank allocation",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rejected = (
        db.table("bank_transaction_suggestions")
        .select("suggestion_type, matched_invoice_id, matched_sales_invoice_id")
        .eq("organisation_id", organisation_id)
        .eq("bank_statement_line_id", line_id)
        .eq("status", "rejected")
        .execute()
        .data
        or []
    )
    rejected_keys = {
        (
            str(row.get("suggestion_type") or ""),
            str(row.get("matched_invoice_id") or row.get("matched_sales_invoice_id") or ""),
        )
        for row in rejected
    }
    suggestions = score_invoice_suggestions(db, organisation_id=organisation_id, line=line)
    suggestions = [
        suggestion for suggestion in suggestions
        if (
            str(suggestion.get("suggestion_type") or ""),
            str(suggestion.get("matched_invoice_id") or suggestion.get("matched_sales_invoice_id") or ""),
        ) not in rejected_keys
    ]
    suggestions += score_rule_suggestions(
        db,
        organisation_id=organisation_id,
        bank_account_id=line["bank_account_id"],
        line=line,
    )
    db.table("bank_transaction_suggestions").delete().eq("bank_statement_line_id", line_id).eq("status", "open").execute()
    inserts = [{**s, "organisation_id": organisation_id, "bank_statement_line_id": line_id} for s in suggestions]
    if inserts:
        db.table("bank_transaction_suggestions").insert(inserts).execute()
        db.table("bank_statement_lines").update({"match_status": "suggested"}).eq("id", line_id).execute()
    return {"success": True, "suggestions": inserts}


@router.get("/suggestions/{suggestion_id}/invoice-preview")
def get_invoice_match_preview(suggestion_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    suggestion = _one(
        db.table("bank_transaction_suggestions")
        .select("*")
        .eq("id", suggestion_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Invoice match suggestion not found",
    )
    invoice_id = suggestion.get("matched_invoice_id")
    if not invoice_id:
        raise HTTPException(status_code=400, detail="This suggestion is not a supplier invoice match")
    invoice = _one(
        db.table("invoices_extracted")
        .select("*")
        .eq("id", invoice_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Matched invoice not found",
    )
    line_items = (
        db.table("invoice_line_items")
        .select("id, description, quantity, unit_price, line_total, expense_account, tracking, vat_treatment")
        .eq("invoice_extracted_id", invoice_id)
        .eq("organisation_id", organisation_id)
        .order("created_at")
        .execute()
        .data
        or []
    )
    eligible, block_code = supplier_invoice_match_eligibility(invoice)
    return {
        "suggestion": suggestion,
        "invoice": invoice,
        "line_items": line_items,
        "match_eligibility": {
            "eligible": eligible,
            "block_code": block_code,
        },
    }


@router.post("/suggestions/{suggestion_id}/reject")
def reject_invoice_match(suggestion_id: str, payload: ExtractUploadRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    suggestion = _one(
        db.table("bank_transaction_suggestions")
        .select("*")
        .eq("id", suggestion_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Invoice match suggestion not found",
    )
    if not (suggestion.get("matched_invoice_id") or suggestion.get("matched_sales_invoice_id")):
        raise HTTPException(status_code=400, detail="Only invoice-match suggestions can be rejected")
    db.table("bank_transaction_suggestions").update({
        "status": "rejected",
        "updated_at": now_iso(),
    }).eq("id", suggestion_id).eq("organisation_id", organisation_id).execute()
    line = _one(
        db.table("bank_statement_lines")
        .select("id, bank_account_id, bank_statement_upload_id")
        .eq("id", suggestion["bank_statement_line_id"])
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank statement line not found",
    )
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_invoice_match_rejected",
        actor_user_id=user_id,
        bank_account_id=line.get("bank_account_id"),
        bank_statement_upload_id=line.get("bank_statement_upload_id"),
        bank_statement_line_id=line.get("id"),
        suggestion_id=suggestion_id,
    )
    return {"success": True, "status": "rejected"}


@router.post("/lines/{line_id}/suggest-ai")
def suggest_bank_line_ai(line_id: str, payload: ExtractUploadRequest, auth: UserAuth):
    """On-demand AI allocation suggestion for a single bank line.

    Kept separate from ``/suggest`` so the (paid) Gemini call is only made when a
    user explicitly asks. Emits a ``suggestion_type="ai"`` row into
    ``bank_transaction_suggestions`` so it surfaces through the normal
    unreconciled-lines enrichment. Degrades gracefully (empty ``suggestions``)
    when the AI is unavailable.
    """
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    line = _one(
        db.table("bank_statement_lines").select("*").eq("id", line_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank statement line not found",
    )
    try:
        assert_bank_line_upload_extracted(
            db,
            organisation_id=organisation_id,
            line=line,
            action="Suggest bank allocation",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    suggestions = score_ai_suggestions(db, organisation_id=organisation_id, line=line)
    # Replace only prior AI suggestions for this line so rule/invoice suggestions survive.
    db.table("bank_transaction_suggestions").delete().eq("bank_statement_line_id", line_id).eq(
        "status", "open"
    ).eq("suggestion_type", "ai").execute()
    inserts = [{**s, "organisation_id": organisation_id, "bank_statement_line_id": line_id} for s in suggestions]
    if inserts:
        db.table("bank_transaction_suggestions").insert(inserts).execute()
        db.table("bank_statement_lines").update({"match_status": "suggested"}).eq("id", line_id).execute()
    return {
        "success": True,
        "suggestions": inserts,
        "ai_available": bool(os.getenv("GOOGLE_API_KEY")),
    }


@router.post("/lines/{line_id}/review")
def review_bank_line(line_id: str, payload: ReviewLineRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    line = _one(
        db.table("bank_statement_lines").select("*").eq("id", line_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank statement line not found",
    )
    try:
        assert_bank_line_upload_extracted(
            db,
            organisation_id=organisation_id,
            line=line,
            action="Review bank transaction",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    suggestion = None
    if payload.suggestion_id:
        suggestion = _one(
            db.table("bank_transaction_suggestions").select("*").eq("id", str(payload.suggestion_id)).eq("organisation_id", organisation_id).limit(1).execute(),
            "Suggestion not found",
        )
    if suggestion and suggestion.get("matched_invoice_id"):
        try:
            assert_accounting_period_unlocked(
                db,
                organisation_id=organisation_id,
                transaction_date=line.get("line_date"),
                action="Reconcile supplier payment",
            )
            settlement = accept_supplier_invoice_bank_match(
                db,
                organisation_id=organisation_id,
                bank_statement_line_id=line_id,
                suggestion_id=str(suggestion["id"]),
                actor_user_id=user_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to reconcile supplier payment: {exc}") from exc
        log_bank_event(
            db,
            organisation_id=organisation_id,
            event_type="bank_supplier_invoice_payment_reconciled",
            actor_user_id=user_id,
            bank_account_id=line.get("bank_account_id"),
            bank_statement_upload_id=line.get("bank_statement_upload_id"),
            bank_statement_line_id=line_id,
            suggestion_id=str(suggestion["id"]),
            matched_invoice_id=str(suggestion["matched_invoice_id"]),
            gl_journal_id=settlement.get("journal_id"),
            payment_id=settlement.get("payment_id"),
        )
        return {"success": True, "supplier_payment": settlement}
    selected_account_id = str(payload.gl_account_id) if payload.gl_account_id else (suggestion or {}).get("suggested_account_id")
    if selected_account_id:
        try:
            assert_manual_posting_account_allowed(
                db,
                organisation_id=organisation_id,
                account_id=str(selected_account_id),
                action="Review bank transaction",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    receipt_result = None
    if suggestion and suggestion.get("matched_sales_invoice_id"):
        evidence = suggestion.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {}
        customer_id = evidence.get("customer_id")
        receipt_amount = money(line.get("signed_amount"))
        outstanding = money(evidence.get("invoice_outstanding"))
        if receipt_amount <= 0 or not customer_id:
            raise HTTPException(
                status_code=400,
                detail="Receivable suggestions require an incoming amount and customer",
            )
        allocation_amount = min(receipt_amount, outstanding)
        try:
            receipt_result = post_customer_receipt(
                db,
                organisation_id=organisation_id,
                customer_id=str(customer_id),
                bank_account_id=str(line["bank_account_id"]),
                receipt_date=str(line.get("line_date") or datetime.now(timezone.utc).date()),
                amount=float(receipt_amount),
                currency=str(line.get("currency") or "ZAR").upper(),
                reference=(
                    line.get("reference")
                    or line.get("description")
                    or suggestion.get("matched_invoice_number")
                ),
                notes="Posted from an accepted bank receipt suggestion",
                allocations=[
                    {
                        "sales_invoice_id": suggestion["matched_sales_invoice_id"],
                        "amount": float(allocation_amount),
                    }
                ],
                actor_user_id=user_id,
                bank_statement_line_id=line_id,
                idempotency_key=f"bank-statement-line:{line_id}",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to post matched customer receipt: {exc}",
            ) from exc
    if suggestion:
        db.table("bank_transaction_suggestions").update({"status": "accepted"}).eq("id", suggestion["id"]).execute()
    accepted_rule_id = None
    if payload.create_rule:
        criteria_mode = (payload.criteria_mode or "and").lower()
        if criteria_mode not in {"and", "or", "only"}:
            raise HTTPException(status_code=400, detail="Rule criteria mode must be and, or, or only")
        criteria = normalize_rule_criteria(payload.rule_criteria)
        if not criteria:
            criteria = [] if criteria_mode == "only" else default_rule_criteria_from_line(line)
        if not criteria:
            raise HTTPException(status_code=400, detail="Add at least one rule condition")
        rule_name = payload.rule_name or f"Rule from {line.get('description') or 'bank transaction'}"
        res = db.table("bank_transaction_rules").insert({
            "organisation_id": organisation_id,
            "bank_account_id": line["bank_account_id"],
            "name": rule_name,
            "amount_direction": "money_in" if money(line.get("signed_amount")) >= 0 else "money_out",
            "match_type": "contains",
            "criteria": criteria,
            "criteria_mode": criteria_mode,
            "description_pattern": None if criteria_mode == "only" else (line.get("description") or "")[:80],
            "reference_pattern": None if criteria_mode == "only" else line.get("reference"),
            "counterparty_pattern": None if criteria_mode == "only" else line.get("counterparty"),
            "gl_account_id": selected_account_id,
            "tracking": payload.tracking or (suggestion or {}).get("suggested_tracking") or {},
            "tax_treatment": payload.tax_treatment or (suggestion or {}).get("suggested_tax_treatment"),
            "source_bank_statement_line_id": line_id,
            "created_by": user_id,
        }).execute()
        accepted_rule_id = _one(res, "Rule create failed")["id"]
        log_bank_event(
            db,
            organisation_id=organisation_id,
            event_type="bank_rule_created",
            actor_user_id=user_id,
            bank_account_id=line["bank_account_id"],
            bank_statement_upload_id=line["bank_statement_upload_id"],
            bank_statement_line_id=line_id,
            created_rule_id=accepted_rule_id,
            criteria=criteria,
            criteria_mode=criteria_mode,
        )
    patch = {
        "accepted_suggestion_id": str(payload.suggestion_id) if payload.suggestion_id else None,
        "accepted_rule_id": accepted_rule_id,
        "supplier_id": str(payload.supplier_id) if payload.supplier_id else None,
        "customer_id": str(payload.customer_id) if payload.customer_id else None,
        "allocation_narration": (
            payload.narration.strip() if (payload.narration and payload.narration.strip()) else None
        ),
        "matched_sales_invoice_id": (
            suggestion.get("matched_sales_invoice_id") if suggestion else None
        ),
        "match_status": (
            "matched"
            if suggestion
            and (
                suggestion.get("matched_invoice_id")
                or suggestion.get("matched_sales_invoice_id")
            )
            else "suggested"
        ),
        "allocation_status": "allocated" if (payload.gl_account_id or suggestion) else "unallocated",
        "review_status": "reviewed",
        "reviewed_by": user_id,
        "reviewed_at": now_iso(),
    }
    try:
        db.table("bank_statement_lines").update(patch).eq("id", line_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save review: {exc}") from exc
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_line_reviewed",
        actor_user_id=user_id,
        bank_account_id=line["bank_account_id"],
        bank_statement_upload_id=line["bank_statement_upload_id"],
        bank_statement_line_id=line_id,
        suggestion_id=str(payload.suggestion_id) if payload.suggestion_id else None,
        created_rule_id=accepted_rule_id,
    )
    return {"success": True, "customer_receipt": receipt_result}


@router.post("/lines/{line_id}/skip")
def skip_bank_line(line_id: str, payload: LineSkipRequest, auth: UserAuth):
    """Defer a bank statement line — keeps it in the unreconciled queue."""
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    line = _one(
        db.table("bank_statement_lines")
        .select("id, bank_account_id, bank_statement_upload_id")
        .eq("id", line_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank statement line not found",
    )
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_line_deferred",
        actor_user_id=user_id,
        bank_account_id=line["bank_account_id"],
        bank_statement_upload_id=line.get("bank_statement_upload_id"),
        bank_statement_line_id=line_id,
    )
    return {"success": True}


@router.post("/lines/bulk-allocate")
def bulk_allocate_bank_lines(payload: BulkAllocateRequest, auth: UserAuth):
    """Atomically create balanced draft journals for selected bank lines."""
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    items = [item.model_dump(mode="json") for item in payload.items]
    line_ids = [str(item["line_id"]) for item in items]
    line_rows = (
        db.table("bank_statement_lines")
        .select("id, line_date, bank_statement_upload_id")
        .eq("organisation_id", organisation_id)
        .in_("id", line_ids)
        .execute()
        .data
        or []
    )
    try:
        assert_bank_lines_uploads_extracted(
            db,
            organisation_id=organisation_id,
            lines=line_rows,
            action="Bulk allocate bank transactions",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    line_dates = {str(row.get("id")): row.get("line_date") for row in line_rows if row.get("id")}
    for item in items:
        has_vat_allocation = any(
            allocation.get("vat_treatment") in {"full", "blocked", "zero_rated"}
            or float(allocation.get("vat_rate") or 0) > 0
            for allocation in item.get("allocations", [])
        )
        if not has_vat_allocation:
            continue
        applicability = vat_applicability(
            db,
            organisation_id=organisation_id,
            transaction_date=line_dates.get(str(item["line_id"])),
            action="Post bank VAT",
        )
        if applicability.applicable:
            continue
        item["allocations"] = [
            {
                **allocation,
                "vat_treatment": "exempt",
                "vat_rate": 0,
            }
            for allocation in item.get("allocations", [])
        ]
    try:
        result = db.rpc(
            "create_bank_draft_journals_atomic",
            {
                "p_org_id": organisation_id,
                "p_items": items,
                "p_actor_user_id": user_id,
            },
        ).execute()
    except Exception as exc:
        message, details = _database_error_parts(exc)
        raise HTTPException(
            status_code=400,
            detail={"message": message, "details": details},
        ) from exc
    data = _rpc_data(result)
    for item in data.get("items") or []:
        item["lines"] = journal_preview_lines(
            db,
            organisation_id,
            item.get("lines") or [],
        )
    return {"success": True, **data}
