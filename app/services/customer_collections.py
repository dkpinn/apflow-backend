from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.services.money import money


ZERO = Decimal("0.00")
MONEY = Decimal("0.01")
CHUNK_SIZE = 200


def amount_out(value: Decimal) -> float:
    return float(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def _fetch_rows(query) -> list[dict]:
    result = query.execute()
    return list(result.data or [])


def _chunked(items: list, size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _parse_date(raw: Any) -> date | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _validate_date(raw: str, field_name: str) -> date:
    parsed = _parse_date(raw)
    if not parsed:
        raise ValueError(f"{field_name} must use YYYY-MM-DD format")
    return parsed


def _fetch_open_invoices(db, organisation_id: str) -> list[dict]:
    return _fetch_rows(
        db.table("sales_invoices")
        .select(
            "id, organisation_id, customer_id, invoice_number, issue_date, due_date, "
            "currency, total_amount, amount_paid, amount_credited, amount_outstanding, "
            "payment_status, status, document_type, customer_reference"
        )
        .eq("organisation_id", organisation_id)
        .eq("document_type", "invoice")
        .eq("status", "issued")
        .order("due_date")
    )


def _fetch_customers(db, organisation_id: str, customer_ids: list[str]) -> dict[str, dict]:
    if not customer_ids:
        return {}
    rows: list[dict] = []
    for chunk in _chunked(customer_ids, CHUNK_SIZE):
        rows.extend(
            _fetch_rows(
                db.table("customers")
                .select(
                    "id, organisation_id, legal_name, trading_name, customer_code, "
                    "email, billing_email, accounts_email, phone"
                )
                .eq("organisation_id", organisation_id)
                .in_("id", chunk)
            )
        )
    return {str(row.get("id")): row for row in rows if row.get("id")}


def _customer_name(customer_id: str, customer: dict | None) -> str:
    if not customer:
        return "Unknown customer" if customer_id == "unknown" else f"Customer {customer_id[:8]}"
    return (
        customer.get("legal_name")
        or customer.get("trading_name")
        or customer.get("customer_code")
        or "Unnamed customer"
    )


def _customer_email(customer: dict | None) -> str | None:
    if not customer:
        return None
    return customer.get("accounts_email") or customer.get("billing_email") or customer.get("email")


def _bucket(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "days_1_30"
    if days_overdue <= 60:
        return "days_31_60"
    if days_overdue <= 90:
        return "days_61_90"
    return "days_90_plus"


def _blank_buckets() -> dict[str, Decimal]:
    return {
        "current": ZERO,
        "days_1_30": ZERO,
        "days_31_60": ZERO,
        "days_61_90": ZERO,
        "days_90_plus": ZERO,
    }


def _buckets_out(buckets: dict[str, Decimal]) -> dict[str, float]:
    return {key: amount_out(value) for key, value in buckets.items()}


def _priority(days_overdue: int, due_in_days: int | None, outstanding: Decimal) -> str:
    if days_overdue >= 60:
        return "urgent"
    if days_overdue > 0:
        return "overdue"
    if due_in_days is not None and due_in_days <= 7:
        return "due_soon"
    if outstanding >= Decimal("10000.00"):
        return "watch"
    return "current"


def _next_action(priority: str) -> str:
    return {
        "urgent": "Call customer and send final reminder",
        "overdue": "Send overdue reminder",
        "due_soon": "Send friendly due-soon reminder",
        "watch": "Monitor high-value open invoice",
        "current": "No immediate action",
    }.get(priority, "Review")


def build_customer_collections(
    db,
    *,
    organisation_id: str,
    as_at_date: str,
    due_soon_days: int = 7,
) -> dict:
    as_at = _validate_date(as_at_date, "as_at_date")
    if due_soon_days < 0 or due_soon_days > 90:
        raise ValueError("due_soon_days must be between 0 and 90")

    warnings: list[dict] = []
    all_invoices = _fetch_open_invoices(db, organisation_id)
    invoices: list[dict] = []
    excluded_zero_balance = 0
    excluded_future_issue = 0

    for invoice in all_invoices:
        outstanding = money(invoice.get("amount_outstanding"))
        if outstanding <= ZERO:
            excluded_zero_balance += 1
            continue

        issue_date = _parse_date(invoice.get("issue_date"))
        if issue_date and issue_date > as_at:
            excluded_future_issue += 1
            continue
        invoices.append(invoice)

    if excluded_zero_balance:
        warnings.append({
            "code": "zero_balance_excluded",
            "message": f"{excluded_zero_balance} issued invoice(s) had no outstanding balance and were excluded.",
        })
    if excluded_future_issue:
        warnings.append({
            "code": "future_invoices_excluded",
            "message": f"{excluded_future_issue} invoice(s) issued after the as-at date were excluded.",
        })

    customer_ids = sorted({str(row.get("customer_id")) for row in invoices if row.get("customer_id")})
    customers_by_id = _fetch_customers(db, organisation_id, customer_ids)

    summary_buckets = _blank_buckets()
    total_outstanding = ZERO
    overdue_outstanding = ZERO
    due_soon_outstanding = ZERO
    queue: list[dict[str, Any]] = []
    customers: dict[str, dict[str, Any]] = {}

    for invoice in invoices:
        customer_id = str(invoice.get("customer_id") or "unknown")
        customer = customers_by_id.get(customer_id)
        due_date = _parse_date(invoice.get("due_date"))
        issue_date = _parse_date(invoice.get("issue_date"))
        outstanding = money(invoice.get("amount_outstanding"))
        total = money(invoice.get("total_amount"))
        paid = money(invoice.get("amount_paid"))
        credited = money(invoice.get("amount_credited"))
        days_overdue = max((as_at - (due_date or issue_date or as_at)).days, 0)
        due_in_days = (due_date - as_at).days if due_date and due_date >= as_at else None
        bucket = _bucket(days_overdue)
        priority = _priority(days_overdue, due_in_days, outstanding)
        needs_follow_up = priority in {"urgent", "overdue", "due_soon", "watch"}

        total_outstanding += outstanding
        summary_buckets[bucket] += outstanding
        if days_overdue > 0:
            overdue_outstanding += outstanding
        if due_in_days is not None and due_in_days <= due_soon_days:
            due_soon_outstanding += outstanding

        if customer_id not in customers:
            customers[customer_id] = {
                "customer_id": None if customer_id == "unknown" else customer_id,
                "customer_name": _customer_name(customer_id, customer),
                "customer_code": (customer or {}).get("customer_code"),
                "email": _customer_email(customer),
                "phone": (customer or {}).get("phone"),
                "total_outstanding": ZERO,
                "overdue_outstanding": ZERO,
                "open_invoice_count": 0,
                "follow_up_count": 0,
                "oldest_days_overdue": 0,
                "buckets": _blank_buckets(),
            }

        group = customers[customer_id]
        group["total_outstanding"] += outstanding
        group["open_invoice_count"] += 1
        group["buckets"][bucket] += outstanding
        if days_overdue > 0:
            group["overdue_outstanding"] += outstanding
            group["oldest_days_overdue"] = max(group["oldest_days_overdue"], days_overdue)
        if needs_follow_up:
            group["follow_up_count"] += 1

        queue.append({
            "invoice_id": str(invoice.get("id")),
            "invoice_number": invoice.get("invoice_number") or "",
            "customer_id": None if customer_id == "unknown" else customer_id,
            "customer_name": group["customer_name"],
            "customer_code": group["customer_code"],
            "customer_email": group["email"],
            "issue_date": issue_date.isoformat() if issue_date else None,
            "due_date": due_date.isoformat() if due_date else None,
            "currency": invoice.get("currency") or "ZAR",
            "payment_status": invoice.get("payment_status") or "unpaid",
            "customer_reference": invoice.get("customer_reference"),
            "total_amount": amount_out(total),
            "paid_amount": amount_out(paid),
            "credited_amount": amount_out(credited),
            "outstanding_amount": amount_out(outstanding),
            "days_overdue": days_overdue,
            "due_in_days": due_in_days,
            "bucket": bucket,
            "priority": priority,
            "needs_follow_up": needs_follow_up,
            "next_action": _next_action(priority),
        })

    priority_rank = {"urgent": 0, "overdue": 1, "due_soon": 2, "watch": 3, "current": 4}
    queue.sort(
        key=lambda row: (
            priority_rank.get(str(row["priority"]), 9),
            -int(row["days_overdue"]),
            -float(row["outstanding_amount"]),
            row["due_date"] or "",
        )
    )

    customer_rows = []
    for group in customers.values():
        customer_rows.append({
            "customer_id": group["customer_id"],
            "customer_name": group["customer_name"],
            "customer_code": group["customer_code"],
            "email": group["email"],
            "phone": group["phone"],
            "total_outstanding": amount_out(group["total_outstanding"]),
            "overdue_outstanding": amount_out(group["overdue_outstanding"]),
            "open_invoice_count": group["open_invoice_count"],
            "follow_up_count": group["follow_up_count"],
            "oldest_days_overdue": group["oldest_days_overdue"],
            "buckets": _buckets_out(group["buckets"]),
        })
    customer_rows.sort(
        key=lambda row: (
            -float(row["overdue_outstanding"]),
            -float(row["total_outstanding"]),
            row["customer_name"],
        )
    )

    follow_up_queue = [row for row in queue if row["needs_follow_up"]]

    return {
        "organisation_id": organisation_id,
        "as_at_date": as_at.isoformat(),
        "due_soon_days": due_soon_days,
        "summary": {
            "customer_count": len(customer_rows),
            "open_invoice_count": len(queue),
            "follow_up_count": len(follow_up_queue),
            "overdue_invoice_count": sum(1 for row in queue if row["days_overdue"] > 0),
            "due_soon_invoice_count": sum(
                1 for row in queue if row["due_in_days"] is not None and row["due_in_days"] <= due_soon_days
            ),
            "total_outstanding": amount_out(total_outstanding),
            "overdue_outstanding": amount_out(overdue_outstanding),
            "due_soon_outstanding": amount_out(due_soon_outstanding),
            "buckets": _buckets_out(summary_buckets),
        },
        "customers": customer_rows,
        "queue": queue,
        "follow_up_queue": follow_up_queue,
        "warnings": warnings,
        "disclaimer": (
            "Calculated from issued customer invoices with outstanding balances. "
            "This view prepares the collections queue only; it does not send reminders or alter invoices."
        ),
    }
