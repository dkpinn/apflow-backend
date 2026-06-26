from __future__ import annotations

from datetime import date
from typing import Any


LOCKED_PERIOD_STATUSES = {"closed", "locked"}


def parse_accounting_date(value: Any, *, field: str = "transaction_date") -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be in YYYY-MM-DD format") from exc


def effective_accounting_lock_date(db, *, organisation_id: str) -> date | None:
    rows = (
        db.table("organisation_accounting_periods")
        .select("organisation_id, status, lock_date")
        .eq("organisation_id", organisation_id)
        .execute()
        .data
        or []
    )
    lock_dates = [
        parse_accounting_date(row.get("lock_date"), field="lock_date")
        for row in rows
        if row.get("lock_date")
        and str(row.get("status") or "").lower() in LOCKED_PERIOD_STATUSES
    ]
    return max(lock_dates) if lock_dates else None


def assert_accounting_period_unlocked(
    db,
    *,
    organisation_id: str,
    transaction_date: Any,
    action: str = "Post transaction",
) -> None:
    lock_date = effective_accounting_lock_date(db, organisation_id=organisation_id)
    if not lock_date:
        return

    if transaction_date in (None, ""):
        raise ValueError(
            f"{action} is blocked because the transaction date is missing and "
            f"this organisation is locked through {lock_date.isoformat()}."
        )

    parsed_transaction_date = parse_accounting_date(
        transaction_date,
        field="transaction_date",
    )
    if parsed_transaction_date <= lock_date:
        raise ValueError(
            f"{action} is blocked because {parsed_transaction_date.isoformat()} "
            f"is on or before the accounting lock date {lock_date.isoformat()}."
        )
