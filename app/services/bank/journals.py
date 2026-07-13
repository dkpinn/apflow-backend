from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException

from app.services.bank_statement_service import (
    dec_to_float,
    journal_lines_for_bank_transaction,
    money,
    new_uuid,
    reversal_lines_for_journal,
)
from app.services.organisation_module_settings import (
    required_tracking_dimensions,
    validate_bank_allocation_tracking,
)
from app.services.organisation_vat import vat_applicability


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _one(res, message: str):
    data = getattr(res, "data", None) or []
    if not data:
        raise HTTPException(status_code=404, detail=message)
    return data[0]


def account_labels(db, organisation_id: str, account_ids: list[str]) -> dict[str, dict[str, Any]]:
    ids = [account_id for account_id in dict.fromkeys(account_ids) if account_id]
    if not ids:
        return {}
    try:
        rows = (
            db.table("accounts")
            .select("id, code, name")
            .eq("organisation_id", organisation_id)
            .in_("id", ids)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    return {str(row["id"]): row for row in rows if row.get("id")}


def journal_preview_lines(db, organisation_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = account_labels(db, organisation_id, [str(row.get("account_id") or "") for row in rows])
    preview: list[dict[str, Any]] = []
    for row in rows:
        account_id = str(row.get("account_id") or "")
        account = labels.get(account_id) or {}
        preview.append({
            **row,
            "account_code": account.get("code"),
            "account_name": account.get("name") or account_id,
        })
    return preview


def reverse_posted_journal(
    db,
    *,
    organisation_id: str,
    journal: dict[str, Any],
    actor_user_id: str,
) -> Optional[dict[str, Any]]:
    """Reverse a posted bank-transaction journal (contra entry) and reset its line.

    Creates a `bank_transaction_reversal` journal with debit/credit-swapped lines,
    marks the original journal `reversed`, and returns the source bank line to
    `unposted`. Returns the reversal detail, or ``None`` when the journal is not a
    reversible posted bank-transaction journal (already reversed, wrong source type,
    or no lines) so batch callers can skip it. Caller is responsible for period-lock
    and permission checks.
    """
    journal_id = str(journal.get("id"))
    if journal.get("status") != "posted":
        return None
    source_line_id = (
        journal.get("source_id") if journal.get("source_type") == "bank_transaction" else None
    )
    if not source_line_id:
        return None

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
        return None

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
        return None

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
        "created_by": actor_user_id,
        "posted_by": actor_user_id,
        "posted_at": _now_iso(),
    }
    db.table("gl_journals").insert(reversal_journal).execute()
    db.table("gl_journal_lines").insert(
        [{**row, "gl_journal_id": reversal_id} for row in reversal_rows]
    ).execute()
    db.table("gl_journals").update(
        {"status": "reversed", "reversed_by": actor_user_id, "reversed_at": _now_iso()}
    ).eq("id", journal_id).execute()
    db.table("bank_statement_lines").update(
        {
            "posting_status": "unposted",
            "allocation_status": "unallocated",
            "match_status": "unmatched",
            "review_status": "pending",
            "accepted_suggestion_id": None,
            "accepted_rule_id": None,
            "gl_journal_id": None,
            "reviewed_by": None,
            "reviewed_at": None,
        }
    ).eq("id", source_line_id).eq("organisation_id", organisation_id).execute()
    return {
        "reversal_id": reversal_id,
        "reversal_journal": reversal_journal,
        "reversal_rows": reversal_rows,
        "source_line_id": source_line_id,
    }


def build_journal_rows_for_line(
    db,
    *,
    organisation_id: str,
    line: dict[str, Any],
    gl_account_id: str,
    tracking: dict[str, Any],
    vat_rate: Optional[float] = None,
    vat_account_id: Optional[str] = None,
    description_override: Optional[str] = None,
) -> list[dict[str, Any]]:
    try:
        validate_bank_allocation_tracking(
            tracking=tracking,
            required_dimensions=required_tracking_dimensions(
                db,
                organisation_id=organisation_id,
                module_key="bank_cash",
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    account = _one(
        db.table("bank_accounts")
        .select("*")
        .eq("id", line["bank_account_id"])
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank account not found",
    )
    bank_gl = account.get("gl_account_id")
    if not bank_gl:
        raise HTTPException(status_code=400, detail="Bank account needs a linked GL account before posting")
    rows = journal_lines_for_bank_transaction(
        organisation_id=organisation_id,
        bank_account_gl_id=str(bank_gl),
        allocation_account_id=str(gl_account_id),
        amount=money(line.get("signed_amount")),
        description=(
            (description_override or "").strip()
            or (line.get("allocation_narration") or "").strip()
            or line.get("description")
            or "Bank transaction"
        ),
        tracking=tracking,
    )
    if vat_rate and vat_account_id:
        applicability = vat_applicability(
            db,
            organisation_id=organisation_id,
            transaction_date=line.get("line_date"),
            action="Post bank VAT",
        )
        if not applicability.applicable:
            vat_rate = None
            vat_account_id = None
    if vat_rate and vat_account_id and len(rows) >= 2:
        # rows[0] = allocation/expense side; rows[1] = bank GL side at full amount.
        alloc = rows[0]
        total_alloc = money(alloc.get("debit_amount") or alloc.get("credit_amount"))
        vat_amount = (total_alloc * Decimal(str(vat_rate)) / (100 + Decimal(str(vat_rate)))).quantize(Decimal("0.01"))
        net_amount = total_alloc - vat_amount
        if alloc.get("debit_amount"):
            rows[0] = {**alloc, "debit_amount": dec_to_float(net_amount), "credit_amount": 0}
            vat_line = {**alloc, "account_id": vat_account_id, "debit_amount": dec_to_float(vat_amount), "credit_amount": 0, "tracking": {}, "sort_order": 2}
        else:
            rows[0] = {**alloc, "credit_amount": dec_to_float(net_amount), "debit_amount": 0}
            vat_line = {**alloc, "account_id": vat_account_id, "credit_amount": dec_to_float(vat_amount), "debit_amount": 0, "tracking": {}, "sort_order": 2}
        rows.append(vat_line)
    return rows
