from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timezone
from typing import Any

VALID_STATUSES = {"open", "closed", "locked"}
DEFAULT_CHECKLIST = {
    "bank_reconciled": False,
    "supplier_review_complete": False,
    "customer_review_complete": False,
    "vat_reviewed": False,
    "reports_reviewed": False,
}


def _parse_date(value: Any, *, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be in YYYY-MM-DD format") from exc


def _month_end(day: date) -> date:
    return date(day.year, day.month, monthrange(day.year, day.month)[1])


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _fetch_rows(db, table: str, organisation_id: str, select: str = "*") -> list[dict[str, Any]]:
    return (
        db.table(table)
        .select(select)
        .eq("organisation_id", organisation_id)
        .limit(1000)
        .execute()
    ).data or []


def _normalise_checklist(value: Any) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    return {key: bool(source.get(key, default)) for key, default in DEFAULT_CHECKLIST.items()}


def _row_out(row: dict[str, Any], *, generated: bool = False) -> dict[str, Any]:
    checklist = _normalise_checklist(row.get("checklist"))
    completed = sum(1 for value in checklist.values() if value)
    return {
        "id": row.get("id"),
        "organisation_id": row.get("organisation_id"),
        "period_start": str(row.get("period_start")),
        "period_end": str(row.get("period_end")),
        "status": row.get("status") or "open",
        "lock_date": row.get("lock_date"),
        "checklist": checklist,
        "checklist_complete": completed == len(DEFAULT_CHECKLIST),
        "checklist_completed": completed,
        "checklist_total": len(DEFAULT_CHECKLIST),
        "notes": row.get("notes") or "",
        "closed_at": row.get("closed_at"),
        "locked_at": row.get("locked_at"),
        "reopened_at": row.get("reopened_at"),
        "generated": generated,
    }


def list_accounting_periods(
    db,
    *,
    organisation_id: str,
    months_back: int = 11,
    months_forward: int = 1,
    today: date | None = None,
) -> dict[str, Any]:
    reference = today or date.today()
    current_month = date(reference.year, reference.month, 1)
    start = _add_months(current_month, -max(months_back, 0))
    end = _add_months(current_month, max(months_forward, 0))

    saved = _fetch_rows(
        db,
        "organisation_accounting_periods",
        organisation_id,
        (
            "id, organisation_id, period_start, period_end, status, lock_date, checklist, notes, "
            "closed_at, locked_at, reopened_at"
        ),
    )
    saved_by_start = {str(row.get("period_start")): row for row in saved}

    periods: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        key = cursor.isoformat()
        if key in saved_by_start:
            periods.append(_row_out(saved_by_start[key], generated=False))
        else:
            periods.append(
                _row_out(
                    {
                        "organisation_id": organisation_id,
                        "period_start": key,
                        "period_end": _month_end(cursor).isoformat(),
                        "status": "open",
                        "checklist": DEFAULT_CHECKLIST,
                    },
                    generated=True,
                )
            )
        cursor = _add_months(cursor, 1)

    periods.sort(key=lambda row: row["period_start"], reverse=True)
    active_lock_dates = [
        _parse_date(row["lock_date"], field="lock_date")
        for row in periods
        if row.get("lock_date") and row.get("status") in {"closed", "locked"}
    ]
    effective_lock_date = max(active_lock_dates).isoformat() if active_lock_dates else None

    return {
        "organisation_id": organisation_id,
        "effective_lock_date": effective_lock_date,
        "periods": periods,
        "summary": {
            "open": sum(1 for row in periods if row["status"] == "open"),
            "closed": sum(1 for row in periods if row["status"] == "closed"),
            "locked": sum(1 for row in periods if row["status"] == "locked"),
            "generated": sum(1 for row in periods if row["generated"]),
        },
        "disclaimer": (
            "Lock dates are now tracked centrally. Posting-route enforcement should be enabled "
            "as the next hardening step across bank, invoice, inventory, lease, and journal writes."
        ),
    }


def save_accounting_period(
    db,
    *,
    organisation_id: str,
    user_id: str,
    period_start: str,
    period_end: str,
    status: str,
    lock_date: str | None = None,
    checklist: dict[str, Any] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    start = _parse_date(period_start, field="period_start")
    end = _parse_date(period_end, field="period_end")
    if start > end:
        raise ValueError("period_start must be on or before period_end")
    if status not in VALID_STATUSES:
        raise ValueError("status must be open, closed, or locked")
    parsed_lock_date = _parse_date(lock_date, field="lock_date") if lock_date else None
    if parsed_lock_date and parsed_lock_date > end:
        raise ValueError("lock_date cannot be after period_end")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row: dict[str, Any] = {
        "organisation_id": organisation_id,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "status": status,
        "lock_date": parsed_lock_date.isoformat() if parsed_lock_date else None,
        "checklist": _normalise_checklist(checklist),
        "notes": notes.strip(),
    }
    if status == "closed":
        row.update({"closed_by": user_id, "closed_at": now, "locked_by": None, "locked_at": None})
    elif status == "locked":
        row.update({"locked_by": user_id, "locked_at": now})
        if not row.get("closed_at"):
            row.update({"closed_by": user_id, "closed_at": now})
    else:
        row.update(
            {
                "reopened_by": user_id,
                "reopened_at": now,
                "closed_by": None,
                "closed_at": None,
                "locked_by": None,
                "locked_at": None,
            }
        )

    result = (
        db.table("organisation_accounting_periods")
        .upsert(row, on_conflict="organisation_id,period_start")
        .execute()
    )
    saved = result.data[0] if result.data else row
    return _row_out(saved, generated=False)
