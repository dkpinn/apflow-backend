"""bank_journals.py
GL journal routes for bank transactions: preview, draft, post, list lines, unpost.

Helpers and models that are shared with the rest of bank.py (accounts, uploads,
lines) remain in bank.py and are imported here.  bank.py does NOT import this
module, so there is no circular-import risk.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.accounting_locks import assert_accounting_period_unlocked
from app.services.bank_statement_service import (
    dec_to_float,
    money,
    new_uuid,
)
from app.services.organisation_module_settings import (
    required_tracking_dimensions,
    validate_bank_allocation_tracking,
)
from app.services.protected_accounts import assert_manual_posting_account_allowed
from app.services.bank.extraction_gate import assert_bank_line_upload_extracted
from app.services.bank.journals import reverse_posted_journal
from app.services.bank.auto_post import auto_post_matched_lines
from app.services.bank.accounts import line_for_reconciliation_display
from app.services.bank.supplier_settlement import reverse_supplier_invoice_bank_match

# Shared helpers and models live in bank.py; import them here.
from app.routers.bank import (
    DraftJournalRequest,
    PostJournalRequest,
    _auth,
    _one,
    build_journal_rows_for_line,
    journal_preview_lines,
    log_bank_event,
    now_iso,
)

router = APIRouter(prefix="/api/bank", tags=["bank"])


@router.post("/lines/{line_id}/journal-preview")
def preview_bank_journal(line_id: str, payload: DraftJournalRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_read(user_id, organisation_id)
    line = _one(
        db.table("bank_statement_lines").select("*").eq("id", line_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank statement line not found",
    )
    try:
        assert_bank_line_upload_extracted(
            db,
            organisation_id=organisation_id,
            line=line,
            action="Preview bank allocation",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        assert_manual_posting_account_allowed(
            db,
            organisation_id=organisation_id,
            account_id=str(payload.gl_account_id),
            action="Allocate bank transaction",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = build_journal_rows_for_line(
        db,
        organisation_id=organisation_id,
        line=line,
        gl_account_id=str(payload.gl_account_id),
        tracking=payload.tracking,
        vat_rate=payload.vat_rate,
        vat_account_id=str(payload.vat_account_id) if payload.vat_account_id else None,
        description_override=payload.description_override,
    )
    return {
        "success": True,
        "lines": journal_preview_lines(db, organisation_id, rows),
        "total_debit": dec_to_float(sum(money(row["debit_amount"]) for row in rows)),
        "total_credit": dec_to_float(sum(money(row["credit_amount"]) for row in rows)),
    }


@router.post("/lines/{line_id}/draft-journal")
def draft_bank_journal(line_id: str, payload: DraftJournalRequest, auth: UserAuth):
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
            action="Draft bank journal",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if line.get("posting_status") == "posted":
        raise HTTPException(status_code=400, detail="Posted bank transactions must be unposted before redrafting")
    try:
        assert_manual_posting_account_allowed(
            db,
            organisation_id=organisation_id,
            account_id=str(payload.gl_account_id),
            action="Allocate bank transaction",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if line.get("posting_status") == "draft" and line.get("gl_journal_id"):
        journal = _one(
            db.table("gl_journals").select("*").eq("id", line["gl_journal_id"]).eq("organisation_id", organisation_id).limit(1).execute(),
            "Draft journal not found",
        )
        existing_lines = (
            db.table("gl_journal_lines")
            .select("*")
            .eq("gl_journal_id", journal["id"])
            .order("sort_order")
            .execute()
            .data
            or []
        )
        return {"success": True, "journal": journal, "lines": journal_preview_lines(db, organisation_id, existing_lines)}
    journal_id = new_uuid()
    description = (
        (payload.description_override or "").strip()
        or (line.get("allocation_narration") or "").strip()
        or line.get("description")
        or "Bank transaction"
    )
    journal_lines = build_journal_rows_for_line(
        db,
        organisation_id=organisation_id,
        line=line,
        gl_account_id=str(payload.gl_account_id),
        tracking=payload.tracking,
        vat_rate=payload.vat_rate,
        vat_account_id=str(payload.vat_account_id) if payload.vat_account_id else None,
        description_override=payload.description_override,
    )
    total_debit = sum(money(row["debit_amount"]) for row in journal_lines)
    total_credit = sum(money(row["credit_amount"]) for row in journal_lines)
    journal = {
        "id": journal_id,
        "organisation_id": organisation_id,
        "source_type": "bank_transaction",
        "source_id": line_id,
        "journal_date": line.get("line_date"),
        "description": description,
        "status": "draft",
        "total_debit": dec_to_float(total_debit),
        "total_credit": dec_to_float(total_credit),
        "created_by": user_id,
    }
    db.table("gl_journals").insert(journal).execute()
    db.table("gl_journal_lines").insert([{**row, "gl_journal_id": journal_id} for row in journal_lines]).execute()
    db.table("bank_statement_lines").update({"posting_status": "draft", "gl_journal_id": journal_id}).eq("id", line_id).execute()
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_journal_drafted",
        actor_user_id=user_id,
        bank_account_id=line["bank_account_id"],
        bank_statement_upload_id=line["bank_statement_upload_id"],
        bank_statement_line_id=line_id,
        gl_journal_id=journal_id,
    )
    return {"success": True, "journal": journal, "lines": journal_preview_lines(db, organisation_id, journal_lines)}


@router.post("/journals/{journal_id}/post")
def post_bank_journal(journal_id: str, payload: PostJournalRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    journal = _one(
        db.table("gl_journals").select("*").eq("id", journal_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Journal not found",
    )
    if journal.get("status") != "draft":
        raise HTTPException(status_code=400, detail="Only draft journals can be posted")
    try:
        assert_accounting_period_unlocked(
            db,
            organisation_id=organisation_id,
            transaction_date=journal.get("journal_date"),
            action="Post bank journal",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    journal_lines = (
        db.table("gl_journal_lines")
        .select("account_id, tracking, sort_order")
        .eq("gl_journal_id", journal_id)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    if journal.get("source_type") == "bank_transaction" and journal.get("source_id"):
        source_line = _one(
            db.table("bank_statement_lines")
            .select("bank_account_id, bank_statement_upload_id")
            .eq("id", journal["source_id"])
            .eq("organisation_id", organisation_id)
            .limit(1)
            .execute(),
            "Bank statement line not found",
        )
        try:
            assert_bank_line_upload_extracted(
                db,
                organisation_id=organisation_id,
                line=source_line,
                action="Post bank journal",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        bank_account = _one(
            db.table("bank_accounts")
            .select("gl_account_id")
            .eq("id", source_line["bank_account_id"])
            .eq("organisation_id", organisation_id)
            .limit(1)
            .execute(),
            "Bank account not found",
        )
        allocation_line = next(
            (
                line
                for line in journal_lines
                if str(line.get("account_id")) != str(bank_account.get("gl_account_id"))
            ),
            None,
        )
        try:
            validate_bank_allocation_tracking(
                tracking=(allocation_line or {}).get("tracking") or {},
                required_dimensions=required_tracking_dimensions(
                    db,
                    organisation_id=organisation_id,
                    module_key="bank_cash",
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.table("gl_journals").update({"status": "posted", "posted_by": user_id, "posted_at": now_iso()}).eq("id", journal_id).execute()
    if journal.get("source_type") == "bank_transaction" and journal.get("source_id"):
        db.table("bank_statement_lines").update({"posting_status": "posted"}).eq("id", journal["source_id"]).execute()
    log_bank_event(db, organisation_id=organisation_id, event_type="bank_journal_posted", actor_user_id=user_id, gl_journal_id=journal_id)
    return {"success": True}


@router.get("/journals/{journal_id}/lines")
def list_bank_journal_lines(journal_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    _one(
        db.table("gl_journals").select("id").eq("id", journal_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Journal not found",
    )
    rows = (
        db.table("gl_journal_lines")
        .select("*")
        .eq("gl_journal_id", journal_id)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    return {"success": True, "lines": journal_preview_lines(db, organisation_id, rows)}


def _pause_account_auto_post(db, organisation_id: str, line_id: str, *, reason: str) -> None:
    """Put rule auto-posting on hold for the bank account owning ``line_id``."""
    line = (
        db.table("bank_statement_lines")
        .select("bank_account_id")
        .eq("id", line_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    account_id = line[0].get("bank_account_id") if line else None
    if not account_id:
        return
    db.table("bank_accounts").update(
        {
            "auto_post_paused": True,
            "auto_post_paused_at": now_iso(),
            "auto_post_paused_reason": reason,
        }
    ).eq("id", account_id).eq("organisation_id", organisation_id).execute()


@router.post("/journals/{journal_id}/unpost")
def unpost_bank_journal(journal_id: str, payload: PostJournalRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    journal = _one(
        db.table("gl_journals").select("*").eq("id", journal_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Journal not found",
    )
    if journal.get("status") != "posted":
        raise HTTPException(status_code=400, detail="Only posted journals can be unposted")
    try:
        assert_accounting_period_unlocked(
            db,
            organisation_id=organisation_id,
            transaction_date=journal.get("journal_date"),
            action="Reverse bank journal",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    source_line_id = journal.get("source_id") if journal.get("source_type") == "bank_transaction" else None
    if not source_line_id:
        raise HTTPException(status_code=400, detail="Only bank transaction journals can be unposted here")

    settlement_payments = (
        db.table("payments")
        .select("id")
        .eq("organisation_id", organisation_id)
        .eq("bank_statement_line_id", source_line_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if settlement_payments:
        try:
            reversed_settlement = reverse_supplier_invoice_bank_match(
                db,
                organisation_id=organisation_id,
                journal_id=journal_id,
                actor_user_id=user_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to reverse supplier payment: {exc}") from exc
        reversal_id = str(reversed_settlement["reversal_id"])
        reversal_journal = _one(
            db.table("gl_journals").select("*").eq("id", reversal_id).eq("organisation_id", organisation_id).limit(1).execute(),
            "Reversal journal not found",
        )
        reversal_rows = (
            db.table("gl_journal_lines")
            .select("*")
            .eq("gl_journal_id", reversal_id)
            .order("sort_order")
            .execute()
            .data
            or []
        )
        reversal = {
            "reversal_id": reversal_id,
            "reversal_journal": reversal_journal,
            "reversal_rows": reversal_rows,
        }
    else:
        reversal = reverse_posted_journal(
            db,
            organisation_id=organisation_id,
            journal=journal,
            actor_user_id=user_id,
        )
    if reversal is None:
        raise HTTPException(
            status_code=400,
            detail="Journal could not be reversed (already reversed or has no lines)",
        )
    reversal_id = reversal["reversal_id"]
    reversal_journal = reversal["reversal_journal"]
    reversal_rows = reversal["reversal_rows"]

    # Put rule auto-posting on hold for this account until the user resumes.
    _pause_account_auto_post(db, organisation_id, source_line_id, reason="manual_unpost")

    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_journal_unposted",
        actor_user_id=user_id,
        bank_statement_line_id=source_line_id,
        gl_journal_id=journal_id,
        reversal_journal_id=reversal_id,
    )
    return {
        "success": True,
        "journal": journal,
        "reversal_journal": reversal_journal,
        "lines": journal_preview_lines(db, organisation_id, reversal_rows),
    }


@router.get("/accounts/{account_id}/posted-lines")
def list_posted_bank_lines(account_id: str, organisation_id: str, auth: UserAuth, limit: int = 30):
    """Return recently posted bank statement lines for the account (newest first).

    Used by the frontend "Recently posted" section to surface an Undo button.
    """
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    rows = (
        db.table("bank_statement_lines")
        .select("id, line_date, description, counterparty, reference, signed_amount, gl_journal_id")
        .eq("organisation_id", organisation_id)
        .eq("bank_account_id", account_id)
        .eq("posting_status", "posted")
        .order("line_date", desc=True)
        .limit(max(1, min(limit, 100)))
        .execute()
        .data
        or []
    )
    return {"lines": [line_for_reconciliation_display(row) for row in rows]}


@router.post("/accounts/{account_id}/resume-auto-post")
def resume_account_auto_post(account_id: str, payload: PostJournalRequest, auth: UserAuth):
    """Clear the auto-post hold on an account and re-run rules over its pending lines."""
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    _one(
        db.table("bank_accounts").select("id").eq("id", account_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank account not found",
    )
    db.table("bank_accounts").update(
        {
            "auto_post_paused": False,
            "auto_post_paused_at": None,
            "auto_post_paused_reason": None,
        }
    ).eq("id", account_id).eq("organisation_id", organisation_id).execute()

    pending = (
        db.table("bank_statement_lines")
        .select("id")
        .eq("organisation_id", organisation_id)
        .eq("bank_account_id", account_id)
        .eq("posting_status", "unposted")
        .eq("duplicate_status", "clear")
        .execute()
        .data
        or []
    )
    line_ids = [str(row["id"]) for row in pending if row.get("id")]
    summary = auto_post_matched_lines(
        db,
        organisation_id=organisation_id,
        bank_account_id=account_id,
        line_ids=line_ids,
    )
    return {"resumed": True, "posted_count": summary.get("posted_count", 0)}
