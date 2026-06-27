from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


ZERO = Decimal("0.00")
MONEY = Decimal("0.01")
CHUNK_SIZE = 200


def _amount(value: Any) -> Decimal:
    if value is None:
        return ZERO
    try:
        return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except Exception:
        return ZERO


def _out(value: Decimal) -> float:
    return float(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def _fetch_rows(query) -> list[dict]:
    result = query.execute()
    return list(result.data or [])


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


def _customer_display_name(customer: dict) -> str:
    return (
        customer.get("trading_name")
        or customer.get("legal_name")
        or customer.get("customer_code")
        or "Unknown customer"
    )


def _customer_email(customer: dict) -> str | None:
    return customer.get("default_email")


def _org_display_name(org: dict) -> str:
    return org.get("trading_name") or org.get("name") or org.get("legal_name") or ""


def _org_email(org: dict) -> str | None:
    return org.get("accounts_email") or org.get("primary_email") or org.get("email")


def _org_address(org: dict) -> str:
    parts = [
        org.get("physical_address_line_1"),
        org.get("physical_address_line_2"),
        org.get("physical_city"),
        org.get("physical_province"),
        org.get("physical_postal_code"),
    ]
    return ", ".join(p for p in parts if p)


def _bucket_key(days_overdue: int) -> str:
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
    return {k: ZERO for k in ("current", "days_1_30", "days_31_60", "days_61_90", "days_90_plus")}


def _buckets_out(buckets: dict[str, Decimal]) -> dict[str, float]:
    return {k: _out(v) for k, v in buckets.items()}


# ── List ────────────────────────────────────────────────────────────────────────

def list_customer_statement_summaries(
    db,
    *,
    organisation_id: str,
    as_at_date: str,
) -> dict:
    as_at = _validate_date(as_at_date, "as_at_date")

    customers = _fetch_rows(
        db.table("customers")
        .select(
            "id, customer_code, legal_name, trading_name, default_email, phone, active"
        )
        .eq("organisation_id", organisation_id)
        .eq("active", True)
        .order("legal_name")
    )

    invoices = _fetch_rows(
        db.table("sales_invoices")
        .select(
            "id, customer_id, issue_date, due_date, currency, "
            "total_amount, amount_outstanding, status, document_type"
        )
        .eq("organisation_id", organisation_id)
        .eq("status", "issued")
        .lte("issue_date", as_at.isoformat())
    )

    by_customer: dict[str, dict[str, Any]] = {}
    for inv in invoices:
        cid = str(inv.get("customer_id") or "")
        if not cid:
            continue
        outstanding = _amount(inv.get("amount_outstanding"))
        if outstanding <= ZERO:
            continue
        issue_date = _parse_date(inv.get("issue_date"))
        due_date = _parse_date(inv.get("due_date"))
        basis = due_date or issue_date or as_at
        days_overdue = max((as_at - basis).days, 0)

        if cid not in by_customer:
            by_customer[cid] = {
                "total_outstanding": ZERO,
                "invoice_count": 0,
                "last_invoice_date": None,
                "oldest_days_overdue": 0,
                "buckets": _blank_buckets(),
                "currency": inv.get("currency") or "ZAR",
            }
        grp = by_customer[cid]
        grp["total_outstanding"] += outstanding
        grp["invoice_count"] += 1
        if issue_date:
            if grp["last_invoice_date"] is None or issue_date > grp["last_invoice_date"]:
                grp["last_invoice_date"] = issue_date
        grp["oldest_days_overdue"] = max(grp["oldest_days_overdue"], days_overdue)
        grp["buckets"][_bucket_key(days_overdue)] += outstanding

    rows = []
    for c in customers:
        cid = str(c.get("id") or "")
        grp = by_customer.get(cid)
        rows.append({
            "customer_id": cid,
            "customer_name": _customer_display_name(c),
            "customer_code": c.get("customer_code"),
            "email": _customer_email(c),
            "phone": c.get("phone"),
            "total_outstanding": _out(grp["total_outstanding"]) if grp else 0.0,
            "invoice_count": grp["invoice_count"] if grp else 0,
            "last_invoice_date": (
                grp["last_invoice_date"].isoformat() if grp and grp["last_invoice_date"] else None
            ),
            "oldest_days_overdue": grp["oldest_days_overdue"] if grp else 0,
            "buckets": _buckets_out(grp["buckets"]) if grp else _buckets_out(_blank_buckets()),
            "currency": grp["currency"] if grp else "ZAR",
            "has_balance": bool(grp),
        })

    rows.sort(key=lambda r: (-r["total_outstanding"], r["customer_name"]))

    return {
        "organisation_id": organisation_id,
        "as_at_date": as_at.isoformat(),
        "customers": rows,
        "summary": {
            "customer_count": len(rows),
            "customers_with_balance": sum(1 for r in rows if r["has_balance"]),
            "total_outstanding": _out(sum((_amount(r["total_outstanding"]) for r in rows), ZERO)),
        },
    }


# ── Detail ───────────────────────────────────────────────────────────────────────

def build_customer_statement(
    db,
    *,
    organisation_id: str,
    customer_id: str,
    as_at_date: str,
    from_date: str | None = None,
) -> dict:
    as_at = _validate_date(as_at_date, "as_at_date")

    if from_date:
        from_dt = _validate_date(from_date, "from_date")
    else:
        try:
            from_dt = date(as_at.year - 1, as_at.month, as_at.day)
        except ValueError:
            from_dt = date(as_at.year - 1, as_at.month, 28)

    if from_dt > as_at:
        raise ValueError("from_date must not be after as_at_date")

    customers = _fetch_rows(
        db.table("customers")
        .select("*")
        .eq("id", customer_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
    )
    if not customers:
        raise ValueError("Customer not found")
    customer = customers[0]

    orgs = _fetch_rows(
        db.table("organisations")
        .select(
            "id, name, legal_name, trading_name, vat_number, registration_number, "
            "primary_email, accounts_email, phone, "
            "physical_address_line_1, physical_address_line_2, "
            "physical_city, physical_province, physical_postal_code"
        )
        .eq("id", organisation_id)
        .limit(1)
    )
    org = orgs[0] if orgs else {}

    all_invoices = _fetch_rows(
        db.table("sales_invoices")
        .select(
            "id, invoice_number, document_type, issue_date, due_date, currency, "
            "total_amount, amount_paid, amount_credited, amount_outstanding, "
            "status, customer_reference"
        )
        .eq("organisation_id", organisation_id)
        .eq("customer_id", customer_id)
        .eq("status", "issued")
        .lte("issue_date", as_at.isoformat())
        .order("issue_date")
    )

    all_receipts = _fetch_rows(
        db.table("customer_receipts")
        .select("id, receipt_date, amount, currency, reference, status")
        .eq("organisation_id", organisation_id)
        .eq("customer_id", customer_id)
        .eq("status", "posted")
        .lte("receipt_date", as_at.isoformat())
        .order("receipt_date")
    )

    # Opening balance: all transactions strictly before from_dt
    opening = ZERO
    for inv in all_invoices:
        d = _parse_date(inv.get("issue_date"))
        if d and d < from_dt:
            total = _amount(inv.get("total_amount"))
            if inv.get("document_type") == "credit_note":
                opening -= total
            else:
                opening += total
    for rec in all_receipts:
        d = _parse_date(rec.get("receipt_date"))
        if d and d < from_dt:
            opening -= _amount(rec.get("amount"))

    # Period transaction lines
    transactions: list[dict[str, Any]] = []
    for inv in all_invoices:
        d = _parse_date(inv.get("issue_date"))
        if not d or d < from_dt:
            continue
        is_credit = inv.get("document_type") == "credit_note"
        total = _amount(inv.get("total_amount"))
        ref = inv.get("invoice_number") or ""
        transactions.append({
            "date": d.isoformat(),
            "_sort": (d.isoformat(), "0", ref),
            "type": "credit_note" if is_credit else "invoice",
            "reference": ref,
            "description": f"{'Credit Note' if is_credit else 'Invoice'} {ref}".strip(),
            "debit": 0.0 if is_credit else _out(total),
            "credit": _out(total) if is_credit else 0.0,
            "currency": inv.get("currency") or "ZAR",
            "balance": 0.0,
        })
    for rec in all_receipts:
        d = _parse_date(rec.get("receipt_date"))
        if not d or d < from_dt:
            continue
        amt = _amount(rec.get("amount"))
        ref = rec.get("reference") or ""
        transactions.append({
            "date": d.isoformat(),
            "_sort": (d.isoformat(), "1", ref),
            "type": "receipt",
            "reference": ref,
            "description": f"Receipt{(' - ' + ref) if ref else ''}",
            "debit": 0.0,
            "credit": _out(amt),
            "currency": rec.get("currency") or "ZAR",
            "balance": 0.0,
        })

    transactions.sort(key=lambda t: t["_sort"])
    running = opening
    for txn in transactions:
        running += _amount(txn["debit"]) - _amount(txn["credit"])
        txn["balance"] = _out(running)
        del txn["_sort"]

    # Aging from current invoice outstanding amounts
    aging_buckets = _blank_buckets()
    for inv in all_invoices:
        outstanding = _amount(inv.get("amount_outstanding"))
        if outstanding <= ZERO:
            continue
        due_date = _parse_date(inv.get("due_date"))
        issue_date = _parse_date(inv.get("issue_date"))
        basis = due_date or issue_date or as_at
        days_overdue = max((as_at - basis).days, 0)
        aging_buckets[_bucket_key(days_overdue)] += outstanding

    currency = (
        customer.get("currency")
        or (all_invoices[0].get("currency") if all_invoices else None)
        or "ZAR"
    )

    return {
        "organisation_id": organisation_id,
        "customer_id": customer_id,
        "as_at_date": as_at.isoformat(),
        "from_date": from_dt.isoformat(),
        "currency": currency,
        "customer": {
            "customer_name": _customer_display_name(customer),
            "customer_code": customer.get("customer_code"),
            "legal_name": customer.get("legal_name"),
            "trading_name": customer.get("trading_name"),
            "vat_number": customer.get("vat_number"),
            "billing_address": customer.get("billing_address"),
            "email": _customer_email(customer),
            "phone": customer.get("phone"),
        },
        "organisation": {
            "name": _org_display_name(org),
            "vat_number": org.get("vat_number"),
            "registration_number": org.get("registration_number"),
            "email": _org_email(org),
            "phone": org.get("phone"),
            "address": _org_address(org),
        },
        "opening_balance": _out(opening),
        "closing_balance": _out(running),
        "transactions": transactions,
        "summary": {
            "total_invoiced": _out(
                sum((_amount(t["debit"]) for t in transactions if t["type"] == "invoice"), ZERO)
            ),
            "total_credited": _out(
                sum((_amount(t["credit"]) for t in transactions if t["type"] == "credit_note"), ZERO)
            ),
            "total_receipts": _out(
                sum((_amount(t["credit"]) for t in transactions if t["type"] == "receipt"), ZERO)
            ),
            "transaction_count": len(transactions),
        },
        "aging": {
            "as_at_date": as_at.isoformat(),
            "total_outstanding": _out(sum(aging_buckets.values())),
            "buckets": _buckets_out(aging_buckets),
        },
    }


def export_customer_statement_csv(
    db,
    *,
    organisation_id: str,
    customer_id: str,
    as_at_date: str,
    from_date: str | None = None,
) -> tuple[str, bytes]:
    stmt = build_customer_statement(
        db,
        organisation_id=organisation_id,
        customer_id=customer_id,
        as_at_date=as_at_date,
        from_date=from_date,
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Customer Statement"])
    writer.writerow(["Customer", stmt["customer"]["customer_name"]])
    writer.writerow(["Period", f"{stmt['from_date']} to {stmt['as_at_date']}"])
    writer.writerow([])
    writer.writerow(["Date", "Type", "Reference", "Description", "Debit", "Credit", "Balance"])
    writer.writerow(["", "Opening balance", "", "", "", "", stmt["opening_balance"]])
    for txn in stmt["transactions"]:
        writer.writerow([
            txn["date"],
            txn["type"].replace("_", " ").title(),
            txn["reference"],
            txn["description"],
            txn["debit"] or "",
            txn["credit"] or "",
            txn["balance"],
        ])
    writer.writerow([])
    writer.writerow(["", "Closing balance", "", "", "", "", stmt["closing_balance"]])
    writer.writerow([])
    writer.writerow(["Aging as at", stmt["aging"]["as_at_date"]])
    writer.writerow(["Current", stmt["aging"]["buckets"]["current"]])
    writer.writerow(["1-30 days", stmt["aging"]["buckets"]["days_1_30"]])
    writer.writerow(["31-60 days", stmt["aging"]["buckets"]["days_31_60"]])
    writer.writerow(["61-90 days", stmt["aging"]["buckets"]["days_61_90"]])
    writer.writerow(["90+ days", stmt["aging"]["buckets"]["days_90_plus"]])

    name_slug = (stmt["customer"]["customer_code"] or customer_id[:8]).replace(" ", "_")
    filename = f"statement_{name_slug}_{as_at_date}.csv"
    return filename, buf.getvalue().encode("utf-8")
