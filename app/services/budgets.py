from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

MONEY = Decimal("0.01")
ZERO = Decimal("0.00")
BUDGET_ACCOUNT_TYPES = {"income", "expense"}


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except Exception:
        return ZERO


def _f(value: Decimal) -> float:
    return float(value)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def get_budget_grid(db, organisation_id: str, year_start: str, year_end: str) -> dict:
    try:
        ys = date.fromisoformat(year_start)
        ye = date.fromisoformat(year_end)
    except ValueError:
        raise ValueError("year_start and year_end must be YYYY-MM-DD")

    # Build ordered list of (year, month) pairs covering the period
    months: list[tuple[int, int]] = []
    y, m = ys.year, ys.month
    while date(y, m, 1) <= ye:
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1

    # Fetch income + expense accounts for the org
    accounts = (
        db.table("accounts")
        .select("id, code, name, type, group_name, active")
        .eq("organisation_id", organisation_id)
        .in_("type", list(BUDGET_ACCOUNT_TYPES))
        .eq("active", True)
        .order("type")
        .order("code", nullsfirst=True)
        .execute()
        .data or []
    )

    account_ids = [str(a["id"]) for a in accounts if a.get("id")]

    # Fetch existing budget entries for the period
    raw_entries: list[dict] = []
    if account_ids:
        raw_entries = (
            db.table("account_budgets")
            .select("id, account_id, period_start, amount")
            .eq("organisation_id", organisation_id)
            .in_("account_id", account_ids)
            .gte("period_start", year_start)
            .lte("period_start", year_end)
            .execute()
            .data or []
        )

    # Index: (account_id, period_start_iso) → entry
    entry_map: dict[tuple[str, str], dict] = {}
    for e in raw_entries:
        key = (str(e.get("account_id")), str(e.get("period_start", ""))[:10])
        entry_map[key] = e

    accounts_out = []
    for account in accounts:
        aid = str(account.get("id"))
        monthly = []
        row_total = ZERO
        for yr, mo in months:
            ps = date(yr, mo, 1).isoformat()
            existing = entry_map.get((aid, ps))
            amt = _d(existing.get("amount")) if existing else ZERO
            monthly.append({
                "year": yr,
                "month": mo,
                "period_start": ps,
                "amount": _f(amt),
                "entry_id": str(existing["id"]) if existing and existing.get("id") else None,
            })
            row_total += amt
        accounts_out.append({
            "id": aid,
            "code": account.get("code"),
            "name": account.get("name"),
            "type": account.get("type"),
            "group_name": account.get("group_name"),
            "monthly": monthly,
            "total": _f(row_total),
        })

    # Column totals per month
    col_totals = []
    for i, (yr, mo) in enumerate(months):
        col_sum = sum(_d(a["monthly"][i]["amount"]) for a in accounts_out)
        col_totals.append({
            "year": yr,
            "month": mo,
            "period_start": date(yr, mo, 1).isoformat(),
            "total": _f(col_sum),
        })

    return {
        "year_start": year_start,
        "year_end": year_end,
        "months": [{"year": yr, "month": mo} for yr, mo in months],
        "accounts": accounts_out,
        "column_totals": col_totals,
        "grand_total": _f(sum(_d(a["total"]) for a in accounts_out)),
    }


def upsert_budget_entry(
    db,
    *,
    organisation_id: str,
    user_id: str,
    account_id: str,
    period_start: str,
    amount: float,
) -> dict:
    try:
        ps = date.fromisoformat(period_start)
    except ValueError:
        raise ValueError("period_start must be YYYY-MM-DD")

    pe = _month_end(ps.year, ps.month)
    amt = float(_d(amount))

    existing = (
        db.table("account_budgets")
        .select("id")
        .eq("organisation_id", organisation_id)
        .eq("account_id", account_id)
        .eq("period_start", ps.isoformat())
        .limit(1)
        .execute()
        .data or []
    )

    if existing:
        entry_id = existing[0]["id"]
        db.table("account_budgets").update({"amount": amt}).eq("id", entry_id).execute()
        return {"entry_id": str(entry_id), "action": "updated", "amount": amt}

    result = (
        db.table("account_budgets")
        .insert({
            "organisation_id": organisation_id,
            "account_id": account_id,
            "period_start": ps.isoformat(),
            "period_end": pe.isoformat(),
            "amount": amt,
            "created_by": user_id,
        })
        .execute()
    )
    new_id = ((result.data or [{}])[0]).get("id")
    return {"entry_id": str(new_id) if new_id else None, "action": "created", "amount": amt}


def delete_budget_entry(db, *, organisation_id: str, entry_id: str) -> None:
    db.table("account_budgets").delete().eq("id", entry_id).eq("organisation_id", organisation_id).execute()
