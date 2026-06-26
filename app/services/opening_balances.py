from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.services.accounting_locks import assert_accounting_period_unlocked, parse_accounting_date
from app.services.bank_statement_service import new_uuid
from app.services.money import money


MONEY = Decimal("0.01")
ZERO = Decimal("0.00")


def _amount(value: Any) -> Decimal:
    return money(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def _out(value: Decimal) -> float:
    return float(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def _fetch_accounts(db, organisation_id: str) -> dict[str, dict[str, Any]]:
    rows = (
        db.table("accounts")
        .select("id, code, name, type, active")
        .eq("organisation_id", organisation_id)
        .execute()
        .data
        or []
    )
    return {str(row.get("id")): row for row in rows if row.get("id")}


def _resolve_account(
    accounts_by_id: dict[str, dict[str, Any]],
    *,
    account_id: str | None,
    account_code: str | None,
) -> dict[str, Any]:
    if account_id and str(account_id) in accounts_by_id:
        return accounts_by_id[str(account_id)]
    if account_code:
        code = str(account_code).strip()
        matches = [row for row in accounts_by_id.values() if str(row.get("code") or "").strip() == code]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Account code {code} matches more than one account")
    label = account_id or account_code or "blank account"
    raise ValueError(f"Account {label} was not found in this organisation")


def _line_amounts(line: dict[str, Any]) -> tuple[Decimal, Decimal]:
    debit = _amount(line.get("debit") or line.get("debit_amount") or 0)
    credit = _amount(line.get("credit") or line.get("credit_amount") or 0)
    if debit < ZERO or credit < ZERO:
        raise ValueError("Opening balance debits and credits cannot be negative")
    if debit and credit:
        raise ValueError("Each opening balance line may have either a debit or a credit, not both")
    return debit, credit


def preview_opening_balance(
    db,
    *,
    organisation_id: str,
    as_at_date: str,
    lines: list[dict[str, Any]],
) -> dict[str, Any]:
    parsed_date = parse_accounting_date(as_at_date, field="as_at_date")
    if not lines:
        raise ValueError("Add at least one opening balance line")

    accounts_by_id = _fetch_accounts(db, organisation_id)
    normalised_lines: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    total_debit = ZERO
    total_credit = ZERO

    for index, line in enumerate(lines):
        account = _resolve_account(
            accounts_by_id,
            account_id=line.get("account_id"),
            account_code=line.get("account_code") or line.get("code"),
        )
        if account.get("active") is False:
            warnings.append({
                "code": "inactive_account",
                "message": f"{account.get('code') or account.get('name')} is inactive but included in the import.",
            })
        debit, credit = _line_amounts(line)
        if not debit and not credit:
            warnings.append({
                "code": "zero_line",
                "message": f"Line {index + 1} has a zero balance and will be ignored.",
            })
            continue
        total_debit += debit
        total_credit += credit
        normalised_lines.append({
            "account_id": account["id"],
            "code": account.get("code"),
            "name": account.get("name"),
            "type": account.get("type"),
            "description": line.get("description") or f"Opening balance - {account.get('code') or account.get('name')}",
            "debit_amount": _out(debit),
            "credit_amount": _out(credit),
            "tracking": line.get("tracking") if isinstance(line.get("tracking"), dict) else {},
            "sort_order": len(normalised_lines),
        })

    difference = total_debit - total_credit
    in_balance = difference == ZERO
    if not in_balance:
        warnings.append({
            "code": "out_of_balance",
            "message": "Opening balance import is out of balance.",
            "difference": _out(difference),
        })

    return {
        "organisation_id": organisation_id,
        "as_at_date": parsed_date.isoformat(),
        "lines": normalised_lines,
        "summary": {
            "line_count": len(normalised_lines),
            "total_debit": _out(total_debit),
            "total_credit": _out(total_credit),
            "difference": _out(difference),
            "in_balance": in_balance,
        },
        "warnings": warnings,
    }


def _existing_opening_balance_journal(db, *, organisation_id: str, as_at_date: str) -> dict[str, Any] | None:
    rows = (
        db.table("gl_journals")
        .select("id, status, journal_date, description")
        .eq("organisation_id", organisation_id)
        .eq("source_type", "opening_balance")
        .eq("journal_date", as_at_date)
        .execute()
        .data
        or []
    )
    return next((row for row in rows if row.get("status") != "reversed"), None)


def post_opening_balance(
    db,
    *,
    organisation_id: str,
    as_at_date: str,
    lines: list[dict[str, Any]],
    user_id: str,
    description: str | None = None,
) -> dict[str, Any]:
    preview = preview_opening_balance(
        db,
        organisation_id=organisation_id,
        as_at_date=as_at_date,
        lines=lines,
    )
    summary = preview["summary"]
    if not summary["in_balance"]:
        raise ValueError("Opening balance import must balance before posting")
    if summary["line_count"] == 0:
        raise ValueError("Opening balance import has no non-zero lines to post")

    assert_accounting_period_unlocked(
        db,
        organisation_id=organisation_id,
        transaction_date=preview["as_at_date"],
        action="Post opening balance",
    )
    existing = _existing_opening_balance_journal(
        db,
        organisation_id=organisation_id,
        as_at_date=preview["as_at_date"],
    )
    if existing:
        raise ValueError(f"An opening balance journal already exists for {preview['as_at_date']}")

    journal_id = new_uuid()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    journal = {
        "id": journal_id,
        "organisation_id": organisation_id,
        "source_type": "opening_balance",
        "source_id": None,
        "journal_date": preview["as_at_date"],
        "description": (description or "").strip() or f"Opening balances as at {preview['as_at_date']}",
        "status": "posted",
        "total_debit": summary["total_debit"],
        "total_credit": summary["total_credit"],
        "created_by": user_id,
        "posted_by": user_id,
        "posted_at": now,
    }
    db.table("gl_journals").insert(journal).execute()
    db.table("gl_journal_lines").insert([
        {
            "organisation_id": organisation_id,
            "gl_journal_id": journal_id,
            "account_id": row["account_id"],
            "description": row["description"],
            "debit_amount": row["debit_amount"],
            "credit_amount": row["credit_amount"],
            "tracking": row["tracking"],
            "sort_order": row["sort_order"],
        }
        for row in preview["lines"]
    ]).execute()
    return {"success": True, "journal": journal, "preview": preview}
