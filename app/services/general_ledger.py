from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from app.services.money import money


ZERO = Decimal("0.00")
MONEY = Decimal("0.01")
CHUNK_SIZE = 200
DEBIT_NORMAL_TYPES = {"asset", "expense", "other"}
CREDIT_NORMAL_TYPES = {"income", "liability", "equity"}

SOURCE_TYPE_LABELS = {
    "bank_transaction": "Bank",
    "invoice": "Invoice",
}


def amount_out(value: Decimal) -> float:
    return float(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def _fetch_rows(query) -> list[dict]:
    result = query.execute()
    return list(result.data or [])


def _validate_dates(date_from: str, date_to: str) -> tuple[date, date]:
    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except ValueError as exc:
        raise ValueError("Dates must use YYYY-MM-DD format") from exc
    if start > end:
        raise ValueError("From date must be on or before To date")
    return start, end


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _source_label(source_type: str | None) -> str:
    if not source_type:
        return "Manual"
    return SOURCE_TYPE_LABELS.get(source_type, source_type.replace("_", " ").title())


def _normal_delta(account_type: str, debit: Decimal, credit: Decimal) -> Decimal:
    if account_type in CREDIT_NORMAL_TYPES:
        return credit - debit
    return debit - credit


def _fetch_accounts(db, organisation_id: str, account_id: Optional[str]) -> list[dict]:
    query = (
        db.table("accounts")
        .select("id, code, name, type, group_name, active")
        .eq("organisation_id", organisation_id)
    )
    if account_id:
        query = query.eq("id", account_id)
    return _fetch_rows(query.order("code", desc=False, nullsfirst=True))


def _fetch_posted_journals_until(db, organisation_id: str, date_to: str) -> list[dict]:
    return _fetch_rows(
        db.table("gl_journals")
        .select("id, journal_date, description, source_type, source_id, created_at")
        .eq("organisation_id", organisation_id)
        .eq("status", "posted")
        .lte("journal_date", date_to)
        .order("journal_date")
        .order("created_at")
    )


def _fetch_journal_lines(db, organisation_id: str, journal_ids: list[str], account_id: Optional[str]) -> list[dict]:
    if not journal_ids:
        return []
    rows: list[dict] = []
    for chunk in _chunked(journal_ids, CHUNK_SIZE):
        query = (
            db.table("gl_journal_lines")
            .select("id, gl_journal_id, account_id, description, debit_amount, credit_amount, sort_order")
            .eq("organisation_id", organisation_id)
            .in_("gl_journal_id", chunk)
        )
        if account_id:
            query = query.eq("account_id", account_id)
        rows.extend(_fetch_rows(query.order("sort_order")))
    return rows


def generate_general_ledger(
    db,
    *,
    organisation_id: str,
    date_from: str,
    date_to: str,
    account_id: Optional[str] = None,
) -> dict:
    start, end = _validate_dates(date_from, date_to)

    accounts = _fetch_accounts(db, organisation_id, account_id)
    if account_id and not accounts:
        raise ValueError("Account not found for this organisation")
    accounts_by_id = {str(row.get("id")): row for row in accounts if row.get("id")}

    journals = _fetch_posted_journals_until(db, organisation_id, end.isoformat())
    journal_map = {str(row.get("id")): row for row in journals if row.get("id")}
    journal_ids = list(journal_map.keys())
    lines = _fetch_journal_lines(db, organisation_id, journal_ids, account_id)

    lines_by_account: dict[str, list[dict]] = {}
    missing_account_lines = 0
    for line in lines:
        line_account_id = str(line.get("account_id") or "")
        if line_account_id not in accounts_by_id:
            missing_account_lines += 1
            continue
        lines_by_account.setdefault(line_account_id, []).append(line)

    warnings: list[dict] = []
    if missing_account_lines:
        warnings.append({
            "code": "missing_account",
            "message": f"{missing_account_lines} journal line(s) reference an account that could not be loaded and were excluded.",
        })

    account_sections: list[dict[str, Any]] = []
    summary_debit = ZERO
    summary_credit = ZERO

    for account in accounts:
        acct_id = str(account.get("id"))
        account_type = str(account.get("type") or "other").lower()
        account_lines = lines_by_account.get(acct_id, [])
        opening_balance = ZERO
        period_debit = ZERO
        period_credit = ZERO
        period_rows: list[dict] = []

        sorted_lines = sorted(
            account_lines,
            key=lambda line: (
                str((journal_map.get(str(line.get("gl_journal_id"))) or {}).get("journal_date") or ""),
                str((journal_map.get(str(line.get("gl_journal_id"))) or {}).get("created_at") or ""),
                line.get("sort_order") or 0,
                str(line.get("id") or ""),
            ),
        )

        for line in sorted_lines:
            journal = journal_map.get(str(line.get("gl_journal_id"))) or {}
            journal_date_raw = journal.get("journal_date")
            if not journal_date_raw:
                continue
            try:
                journal_date = date.fromisoformat(str(journal_date_raw))
            except ValueError:
                continue

            debit = money(line.get("debit_amount"))
            credit = money(line.get("credit_amount"))
            delta = _normal_delta(account_type, debit, credit)

            if journal_date < start:
                opening_balance += delta
                continue
            if journal_date > end:
                continue

            opening_balance += delta
            period_debit += debit
            period_credit += credit
            period_rows.append({
                "line_id": line.get("id"),
                "journal_id": journal.get("id"),
                "journal_date": journal_date.isoformat(),
                "source_type": journal.get("source_type") or "",
                "source_label": _source_label(journal.get("source_type")),
                "journal_description": journal.get("description") or "",
                "line_description": line.get("description") or "",
                "debit_amount": amount_out(debit),
                "credit_amount": amount_out(credit),
                "running_balance": amount_out(opening_balance),
            })

        closing_balance = opening_balance
        real_opening_balance = closing_balance - _normal_delta(account_type, period_debit, period_credit)

        if not period_rows and not real_opening_balance and not closing_balance:
            continue

        summary_debit += period_debit
        summary_credit += period_credit
        account_sections.append({
            "account_id": acct_id,
            "code": account.get("code"),
            "name": account.get("name") or "",
            "type": account_type,
            "group_name": account.get("group_name"),
            "opening_balance": amount_out(real_opening_balance),
            "period_debit": amount_out(period_debit),
            "period_credit": amount_out(period_credit),
            "closing_balance": amount_out(closing_balance),
            "lines": period_rows,
        })

    return {
        "organisation_id": organisation_id,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "account_id": account_id,
        "accounts": account_sections,
        "summary": {
            "account_count": len(account_sections),
            "line_count": sum(len(section["lines"]) for section in account_sections),
            "total_debits": amount_out(summary_debit),
            "total_credits": amount_out(summary_credit),
        },
        "warnings": warnings,
        "disclaimer": "Calculated from posted general-ledger journal entries only.",
    }
