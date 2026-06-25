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


def _validate_as_at(as_at_date: str) -> date:
    parsed = _parse_date(as_at_date)
    if not parsed:
        raise ValueError("As-at date must use YYYY-MM-DD format")
    return parsed


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


def _fetch_open_invoices(db, organisation_id: str) -> list[dict]:
    return _fetch_rows(
        db.table("sales_invoices")
        .select(
            "id, organisation_id, customer_id, invoice_number, issue_date, due_date, "
            "currency, total_amount, amount_paid, amount_credited, amount_outstanding, "
            "payment_status, status, document_type"
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
                .select("id, organisation_id, legal_name, trading_name, customer_code")
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


def generate_aged_receivables(
    db,
    *,
    organisation_id: str,
    as_at_date: str,
) -> dict:
    as_at = _validate_as_at(as_at_date)
    warnings: list[dict] = []

    all_invoices = _fetch_open_invoices(db, organisation_id)
    future_invoices = 0
    undated_invoices = 0
    zero_balance = 0
    invoices: list[dict] = []

    for invoice in all_invoices:
        outstanding = money(invoice.get("amount_outstanding"))
        if outstanding <= ZERO:
            zero_balance += 1
            continue

        issue_date = _parse_date(invoice.get("issue_date"))
        due_date = _parse_date(invoice.get("due_date"))
        basis_date = issue_date or due_date
        if basis_date and basis_date > as_at:
            future_invoices += 1
            continue
        if not basis_date:
            undated_invoices += 1
        invoices.append(invoice)

    if future_invoices:
        warnings.append({
            "code": "future_invoices_excluded",
            "message": f"{future_invoices} issued invoice(s) dated after the as-at date were excluded.",
        })
    if undated_invoices:
        warnings.append({
            "code": "undated_invoices_included",
            "message": f"{undated_invoices} issued invoice(s) had no issue or due date and were included as current.",
        })
    if zero_balance:
        warnings.append({
            "code": "zero_balance_excluded",
            "message": f"{zero_balance} issued invoice(s) had no outstanding balance and were excluded.",
        })

    customer_ids = sorted({str(row.get("customer_id")) for row in invoices if row.get("customer_id")})
    customers_by_id = _fetch_customers(db, organisation_id, customer_ids)

    customer_groups: dict[str, dict[str, Any]] = {}
    summary_buckets = _blank_buckets()
    total_outstanding = ZERO
    open_invoice_count = 0

    for invoice in invoices:
        invoice_id = str(invoice.get("id"))
        customer_id = str(invoice.get("customer_id") or "unknown")
        customer = customers_by_id.get(customer_id)
        outstanding = money(invoice.get("amount_outstanding"))
        total = money(invoice.get("total_amount"))
        paid = money(invoice.get("amount_paid"))
        credited = money(invoice.get("amount_credited"))
        issue_date = _parse_date(invoice.get("issue_date"))
        due_date = _parse_date(invoice.get("due_date"))
        age_date = due_date or issue_date or as_at
        days_overdue = max((as_at - age_date).days, 0)
        bucket_key = _bucket(days_overdue)

        open_invoice_count += 1
        total_outstanding += outstanding
        summary_buckets[bucket_key] += outstanding

        if customer_id not in customer_groups:
            customer_groups[customer_id] = {
                "customer_id": None if customer_id == "unknown" else customer_id,
                "customer_name": _customer_name(customer_id, customer),
                "customer_code": (customer or {}).get("customer_code"),
                "total_outstanding": ZERO,
                "buckets": _blank_buckets(),
                "invoices": [],
            }

        group = customer_groups[customer_id]
        group["total_outstanding"] += outstanding
        group["buckets"][bucket_key] += outstanding
        group["invoices"].append({
            "invoice_id": invoice_id,
            "invoice_number": invoice.get("invoice_number") or "",
            "issue_date": issue_date.isoformat() if issue_date else None,
            "due_date": due_date.isoformat() if due_date else None,
            "currency": invoice.get("currency") or "ZAR",
            "payment_status": invoice.get("payment_status") or "unpaid",
            "total_amount": amount_out(total),
            "paid_amount": amount_out(paid),
            "credited_amount": amount_out(credited),
            "outstanding_amount": amount_out(outstanding),
            "days_overdue": days_overdue,
            "bucket": bucket_key,
        })

    customers = []
    for group in customer_groups.values():
        customers.append({
            "customer_id": group["customer_id"],
            "customer_name": group["customer_name"],
            "customer_code": group["customer_code"],
            "total_outstanding": amount_out(group["total_outstanding"]),
            "buckets": _buckets_out(group["buckets"]),
            "invoices": sorted(
                group["invoices"],
                key=lambda row: (
                    row["due_date"] or row["issue_date"] or "",
                    row["invoice_number"] or "",
                ),
            ),
        })
    customers.sort(key=lambda row: (-row["total_outstanding"], row["customer_name"]))

    return {
        "organisation_id": organisation_id,
        "as_at_date": as_at.isoformat(),
        "customers": customers,
        "summary": {
            "customer_count": len(customers),
            "open_invoice_count": open_invoice_count,
            "total_outstanding": amount_out(total_outstanding),
            "buckets": _buckets_out(summary_buckets),
        },
        "warnings": warnings,
        "disclaimer": (
            "Calculated from issued customer invoices with outstanding balances "
            "as at the selected date. Draft, approval-pending, voided, paid, "
            "credit-note, and future-dated invoices are excluded."
        ),
    }
