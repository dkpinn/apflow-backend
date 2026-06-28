from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException

from app.services.bank_statement_service import (
    dec_to_float,
    journal_lines_for_bank_transaction,
    money,
)
from app.services.organisation_module_settings import (
    required_tracking_dimensions,
    validate_bank_allocation_tracking,
)


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
        description=(description_override or "").strip() or line.get("description") or "Bank transaction",
        tracking=tracking,
    )
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
