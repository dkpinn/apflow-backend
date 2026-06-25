from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from app.services.income_statement import generate_income_statement
from app.services.money import money
from app.services.trial_balance import (
    _financial_year_bounds,
    _parse_date,
    resolve_financial_year_end,
)


ZERO = Decimal("0.00")
MONEY = Decimal("0.01")
BALANCE_SHEET_TYPES = {"asset", "liability", "equity"}
CREDIT_NORMAL_TYPES = {"liability", "equity"}


def amount_out(value: Decimal) -> float:
    return float(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def _fetch_rows(query) -> list[dict]:
    result = query.execute()
    return list(result.data or [])


def _fetch_accounts(db, organisation_id: str) -> list[dict]:
    return _fetch_rows(
        db.table("accounts")
        .select("id, code, name, type, group_name, active")
        .eq("organisation_id", organisation_id)
        .order("type")
        .order("code", desc=False, nullsfirst=True)
    )


def _fetch_posted_journals_until(db, organisation_id: str, as_at_date: str) -> list[dict]:
    return _fetch_rows(
        db.table("gl_journals")
        .select("id, journal_date")
        .eq("organisation_id", organisation_id)
        .eq("status", "posted")
        .lte("journal_date", as_at_date)
    )


def _fetch_journal_lines(db, organisation_id: str, journal_ids: list[str]) -> list[dict]:
    if not journal_ids:
        return []
    return _fetch_rows(
        db.table("gl_journal_lines")
        .select("id, gl_journal_id, account_id, debit_amount, credit_amount")
        .eq("organisation_id", organisation_id)
        .in_("gl_journal_id", journal_ids)
    )


def _normal_balance(account_type: str, debit_total: Decimal, credit_total: Decimal) -> Decimal:
    if account_type in CREDIT_NORMAL_TYPES:
        return credit_total - debit_total
    return debit_total - credit_total


def _line_for_account(account: dict, amount: Decimal) -> dict:
    return {
        "account_id": account.get("id"),
        "code": account.get("code"),
        "name": account.get("name") or "",
        "group_name": account.get("group_name"),
        "amount": amount_out(amount),
    }


def _sort_lines(lines: list[dict]) -> list[dict]:
    return sorted(lines, key=lambda row: (row.get("group_name") or "", row.get("code") or "", row.get("name") or ""))


def generate_balance_sheet(
    db,
    *,
    organisation_id: str,
    as_at_date: str,
    financial_year_end: Optional[str] = None,
) -> dict:
    as_at = _parse_date(as_at_date)
    resolved_year_end = resolve_financial_year_end(db, organisation_id=organisation_id, override=financial_year_end)
    fy_start, fy_end = _financial_year_bounds(resolved_year_end, as_at)

    warnings: list[dict] = []
    accounts = _fetch_accounts(db, organisation_id)
    accounts_by_id = {str(row.get("id")): row for row in accounts if row.get("id")}

    journals = _fetch_posted_journals_until(db, organisation_id, as_at.isoformat())
    journal_ids = [str(row.get("id")) for row in journals if row.get("id")]
    journal_lines = _fetch_journal_lines(db, organisation_id, journal_ids)

    totals_by_account: dict[str, tuple[Decimal, Decimal]] = {}
    missing_account_lines = 0
    for line in journal_lines:
        account_id = str(line.get("account_id") or "")
        if account_id not in accounts_by_id:
            missing_account_lines += 1
            continue
        debit_total, credit_total = totals_by_account.get(account_id, (ZERO, ZERO))
        totals_by_account[account_id] = (
            debit_total + money(line.get("debit_amount")),
            credit_total + money(line.get("credit_amount")),
        )

    if missing_account_lines:
        warnings.append({
            "code": "missing_account",
            "message": f"{missing_account_lines} journal line(s) reference an account that could not be loaded and were excluded.",
        })

    sections = {"assets": [], "liabilities": [], "equity": []}
    totals = {"assets": ZERO, "liabilities": ZERO, "equity": ZERO}

    for account_id, account in accounts_by_id.items():
        account_type = str(account.get("type") or "").lower()
        if account_type not in BALANCE_SHEET_TYPES:
            continue
        debit_total, credit_total = totals_by_account.get(account_id, (ZERO, ZERO))
        balance = _normal_balance(account_type, debit_total, credit_total)
        if not balance:
            continue

        if account_type == "asset":
            sections["assets"].append(_line_for_account(account, balance))
            totals["assets"] += balance
        elif account_type == "liability":
            sections["liabilities"].append(_line_for_account(account, balance))
            totals["liabilities"] += balance
        else:
            sections["equity"].append(_line_for_account(account, balance))
            totals["equity"] += balance

    income_statement = generate_income_statement(
        db,
        organisation_id=organisation_id,
        date_from=fy_start.isoformat(),
        date_to=as_at.isoformat(),
    )
    current_year_profit = money((income_statement.get("subtotals") or {}).get("net_income"))
    if current_year_profit:
        sections["equity"].append({
            "account_id": None,
            "code": None,
            "name": "Current year profit / (loss)",
            "group_name": "Current earnings",
            "amount": amount_out(current_year_profit),
        })
        totals["equity"] += current_year_profit

    for warning in income_statement.get("warnings") or []:
        warnings.append({
            "code": f"income_statement_{warning.get('code', 'warning')}",
            "message": warning.get("message", "Income Statement warning while calculating current-year profit."),
        })

    liabilities_plus_equity = totals["liabilities"] + totals["equity"]
    variance = totals["assets"] - liabilities_plus_equity
    in_balance = variance.quantize(MONEY, rounding=ROUND_HALF_UP) == ZERO
    if not in_balance:
        warnings.append({
            "code": "balance_sheet_out_of_balance",
            "message": "Assets do not equal liabilities plus equity for the selected date.",
            "variance": amount_out(variance),
        })

    return {
        "organisation_id": organisation_id,
        "as_at_date": as_at.isoformat(),
        "financial_year_end": resolved_year_end,
        "financial_year": {
            "start": fy_start.isoformat(),
            "end": fy_end.isoformat(),
        },
        "sections": {
            "assets": _sort_lines(sections["assets"]),
            "liabilities": _sort_lines(sections["liabilities"]),
            "equity": _sort_lines(sections["equity"]),
        },
        "summary": {
            "total_assets": amount_out(totals["assets"]),
            "total_liabilities": amount_out(totals["liabilities"]),
            "total_equity": amount_out(totals["equity"]),
            "liabilities_plus_equity": amount_out(liabilities_plus_equity),
            "variance": amount_out(variance),
            "in_balance": in_balance,
        },
        "warnings": warnings,
        "disclaimer": (
            "Calculated from posted general-ledger journal entries as at the selected date. "
            "Current-year profit or loss is included in equity from the Income Statement for the active financial year."
        ),
    }
