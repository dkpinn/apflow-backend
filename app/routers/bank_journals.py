"""bank_journals.py
GL journal routes for bank transactions: preview, draft, post, list lines, unpost.

Helpers and models that are shared with the rest of bank.py (accounts, uploads,
lines) remain in bank.py and are imported here.  bank.py does NOT import this
module, so there is no circular-import risk.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.bank_statement_service import (
    dec_to_float,
    money,
    new_uuid,
    reversal_lines_for_journal,
)
from app.services.organisation_module_settings import (
    required_tracking_dimensions,
    validate_bank_allocation_tracking,
)

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
    rows = build_journal_rows_for_line(
        db,
        organisation_id=organisation_id,
        line=line,
        gl_account_id=str(payload.gl_account_id),
        tracking=payload.tracking,
        vat_rate=payload.vat_rate,
        vat_account_id=str(payload.vat_account_id) if payload.vat_account_id else None,
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
    if line.get("posting_status") == "posted":
        raise HTTPException(status_code=400, detail="Posted bank transactions must be unposted before redrafting")
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
    description = line.get("description") or "Bank transaction"
    journal_lines = build_journal_rows_for_line(
        db,
        organisation_id=organisation_id,
        line=line,
        gl_account_id=str(payload.gl_account_id),
        tracking=payload.tracking,
        vat_rate=payload.vat_rate,
        vat_account_id=str(payload.vat_account_id) if payload.vat_account_id else None,
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
            .select("bank_account_id")
            .eq("id", journal["source_id"])
            .eq("organisation_id", organisation_id)
            .limit(1)
            .execute(),
            "Bank statement line not found",
        )
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
    source_line_id = journal.get("source_id") if journal.get("source_type") == "bank_transaction" else None
    if not source_line_id:
        raise HTTPException(status_code=400, detail="Only bank transaction journals can be unposted here")
    existing_reversal = (
        db.table("gl_journals")
        .select("id")
        .eq("organisation_id", organisation_id)
        .eq("reversal_of_journal_id", journal_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing_reversal:
        raise HTTPException(status_code=400, detail="This journal has already been reversed")

    original_lines = (
        db.table("gl_journal_lines")
        .select("*")
        .eq("gl_journal_id", journal_id)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    if not original_lines:
        raise HTTPException(status_code=400, detail="Journal has no lines to reverse")

    reversal_id = new_uuid()
    description = f"Reversal: {journal.get('description') or 'Bank journal'}"
    reversal_rows = reversal_lines_for_journal(original_lines, description=description)
    total_debit = sum(money(row["debit_amount"]) for row in reversal_rows)
    total_credit = sum(money(row["credit_amount"]) for row in reversal_rows)
    reversal_journal = {
        "id": reversal_id,
        "organisation_id": organisation_id,
        "source_type": "bank_transaction_reversal",
        "source_id": source_line_id,
        "reversal_of_journal_id": journal_id,
        "journal_date": journal.get("journal_date"),
        "description": description,
        "status": "posted",
        "total_debit": dec_to_float(total_debit),
        "total_credit": dec_to_float(total_credit),
        "created_by": user_id,
        "posted_by": user_id,
        "posted_at": now_iso(),
    }
    db.table("gl_journals").insert(reversal_journal).execute()
    db.table("gl_journal_lines").insert([{**row, "gl_journal_id": reversal_id} for row in reversal_rows]).execute()
    db.table("gl_journals").update({"status": "reversed", "reversed_by": user_id, "reversed_at": now_iso()}).eq("id", journal_id).execute()
    db.table("bank_statement_lines").update({
        "posting_status": "unposted",
        "allocation_status": "unallocated",
        "match_status": "unmatched",
        "review_status": "pending",
        "accepted_suggestion_id": None,
        "accepted_rule_id": None,
        "gl_journal_id": None,
        "reviewed_by": None,
        "reviewed_at": None,
    }).eq("id", source_line_id).eq("organisation_id", organisation_id).execute()
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
    return {"lines": rows}
