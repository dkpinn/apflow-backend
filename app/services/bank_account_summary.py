from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Optional


ZERO = Decimal("0")


def _money(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _date_key(value: Any) -> str:
    return str(value or "")


def select_latest_statement(
    uploads: Iterable[dict[str, Any]],
    lines: Iterable[dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    lines_by_upload: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        upload_id = str(line.get("bank_statement_upload_id") or "")
        if upload_id:
            lines_by_upload.setdefault(upload_id, []).append(line)

    candidates: list[tuple[tuple[str, str, str, str], dict[str, Any], Optional[str]]] = []
    for upload in uploads:
        if upload.get("extraction_status") != "extracted":
            continue
        upload_id = str(upload.get("id") or "")
        latest_line_date = max(
            (_date_key(line.get("line_date")) for line in lines_by_upload.get(upload_id, [])),
            default="",
        )
        statement_end = _date_key(upload.get("statement_period_to"))
        effective_end = statement_end or latest_line_date
        candidates.append(
            (
                (
                    effective_end,
                    latest_line_date,
                    _date_key(upload.get("uploaded_at")),
                    upload_id,
                ),
                upload,
                latest_line_date or None,
            )
        )

    if not candidates:
        return None, None
    _, upload, latest_line_date = max(candidates, key=lambda item: item[0])
    return upload, latest_line_date


def calculate_statement_balances(
    latest_upload: Optional[dict[str, Any]],
    lines: Iterable[dict[str, Any]],
) -> tuple[Optional[float], Optional[float]]:
    if not latest_upload:
        return None, None

    closing = latest_upload.get("closing_balance")
    bank_balance = float(_money(closing)) if closing is not None else None
    opening = latest_upload.get("opening_balance")
    if opening is None:
        return bank_balance, None

    upload_id = str(latest_upload.get("id") or "")
    movement = sum(
        (
            _money(line.get("signed_amount"))
            for line in lines
            if str(line.get("bank_statement_upload_id") or "") == upload_id
        ),
        ZERO,
    )
    return bank_balance, float((_money(opening) + movement).quantize(Decimal("0.01")))


def posted_gl_balance(
    db,
    *,
    organisation_id: str,
    gl_account_id: Optional[str],
) -> Optional[float]:
    if not gl_account_id:
        return None

    gl_lines = (
        db.table("gl_journal_lines")
        .select("gl_journal_id, debit_amount, credit_amount")
        .eq("organisation_id", organisation_id)
        .eq("account_id", gl_account_id)
        .execute()
        .data
        or []
    )
    journal_ids = list(
        dict.fromkeys(
            str(line.get("gl_journal_id"))
            for line in gl_lines
            if line.get("gl_journal_id")
        )
    )
    if not journal_ids:
        return 0.0

    posted_ids = {
        str(journal.get("id"))
        for journal in (
            db.table("gl_journals")
            .select("id")
            .eq("organisation_id", organisation_id)
            .eq("status", "posted")
            .in_("id", journal_ids)
            .execute()
            .data
            or []
        )
    }
    balance = sum(
        (
            _money(line.get("debit_amount")) - _money(line.get("credit_amount"))
            for line in gl_lines
            if str(line.get("gl_journal_id")) in posted_ids
        ),
        ZERO,
    )
    return float(balance.quantize(Decimal("0.01")))


def build_bank_balance_summary(
    db,
    *,
    organisation_id: str,
    account: dict[str, Any],
    lines: list[dict[str, Any]],
    uploads: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        result = db.rpc(
            "get_bank_account_balance_summary",
            {
                "p_org_id": organisation_id,
                "p_bank_account_id": str(account["id"]),
            },
        ).execute()
        rpc_data = getattr(result, "data", result)
        if isinstance(rpc_data, list):
            rpc_data = rpc_data[0] if rpc_data else None
        if isinstance(rpc_data, dict) and "tb_balance_status" in rpc_data:
            return rpc_data
    except Exception:
        pass

    latest_upload, latest_transaction_date = select_latest_statement(uploads, lines)
    bank_balance, imported_balance = calculate_statement_balances(latest_upload, lines)
    gl_account_id = str(account.get("gl_account_id")) if account.get("gl_account_id") else None
    tb_balance = posted_gl_balance(
        db,
        organisation_id=organisation_id,
        gl_account_id=gl_account_id,
    )
    return {
        "bank_statement_balance": bank_balance,
        "calculated_imported_balance": imported_balance,
        "current_tb_balance": tb_balance,
        "latest_statement_upload_id": (
            str(latest_upload.get("id")) if latest_upload and latest_upload.get("id") else None
        ),
        "statement_period_to": (
            str(latest_upload.get("statement_period_to"))
            if latest_upload and latest_upload.get("statement_period_to")
            else None
        ),
        "latest_transaction_date": latest_transaction_date,
        "bank_balance_status": "available" if bank_balance is not None else "unavailable",
        "imported_balance_status": "available" if imported_balance is not None else "unavailable",
        "tb_balance_status": "available" if gl_account_id else "gl_account_not_linked",
    }
