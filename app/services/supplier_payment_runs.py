from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import uuid4

from app.services.aged_payables import (
    RECEIPT_DOCUMENT_TYPES,
    _fetch_payments,
    _fetch_reconciliation_lines,
    _fetch_statement_lines,
    _payment_date,
    _supplier_name,
)
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


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = str(value or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _fetch_posted_invoices(db, organisation_id: str) -> list[dict]:
    return _fetch_rows(
        db.table("invoices_extracted")
        .select(
            "id, organisation_id, supplier_id, supplier_name_extracted, invoice_number, "
            "invoice_date, due_date, total_amount, currency, posting_status, document_type, "
            "document_direction"
        )
        .eq("organisation_id", organisation_id)
        .eq("posting_status", "posted")
        .order("due_date")
    )


def _fetch_suppliers(db, organisation_id: str, supplier_ids: list[str]) -> dict[str, dict]:
    if not supplier_ids:
        return {}
    rows: list[dict] = []
    for chunk in _chunked(supplier_ids, CHUNK_SIZE):
        rows.extend(
            _fetch_rows(
                db.table("suppliers")
                .select(
                    "id, organisation_id, supplier_name, trading_name, supplier_code, "
                    "default_email, accounting_email, bank_account_name, bank_name, "
                    "bank_account_number, bank_branch_code, bank_swift_code"
                )
                .eq("organisation_id", organisation_id)
                .in_("id", chunk)
            )
        )
    return {str(row.get("id")): row for row in rows if row.get("id")}


def _supplier_email(supplier: dict | None) -> str | None:
    if not supplier:
        return None
    return supplier.get("accounting_email") or supplier.get("default_email")


def _bank_ready(supplier: dict | None) -> bool:
    if not supplier:
        return False
    return bool(supplier.get("bank_account_number") and supplier.get("bank_name"))


def _priority(days_overdue: int, due_in_days: int | None, bank_ready: bool) -> str:
    if not bank_ready:
        return "blocked"
    if days_overdue >= 30:
        return "urgent"
    if days_overdue > 0:
        return "overdue"
    if due_in_days is not None and due_in_days <= 7:
        return "due_soon"
    return "scheduled"


def _next_action(priority: str) -> str:
    return {
        "blocked": "Add or verify supplier banking details",
        "urgent": "Include in next payment run",
        "overdue": "Schedule payment approval",
        "due_soon": "Prepare for upcoming payment run",
        "scheduled": "Monitor until due",
    }.get(priority, "Review")


def build_supplier_payment_run(
    db,
    *,
    organisation_id: str,
    pay_on_date: str,
    due_within_days: int = 7,
) -> dict:
    pay_on = _validate_date(pay_on_date, "pay_on_date")
    if due_within_days < 0 or due_within_days > 90:
        raise ValueError("due_within_days must be between 0 and 90")

    warnings: list[dict] = []
    all_invoices = _fetch_posted_invoices(db, organisation_id)
    invoices: list[dict] = []
    excluded_receipts = 0
    excluded_future = 0

    for invoice in all_invoices:
        document_type = str(invoice.get("document_type") or "").lower()
        if document_type in RECEIPT_DOCUMENT_TYPES:
            excluded_receipts += 1
            continue
        invoice_date = _parse_date(invoice.get("invoice_date"))
        if invoice_date and invoice_date > pay_on:
            excluded_future += 1
            continue
        invoices.append(invoice)

    if excluded_receipts:
        warnings.append({
            "code": "receipts_excluded",
            "message": f"{excluded_receipts} posted receipt/card receipt document(s) were excluded from payment runs.",
        })
    if excluded_future:
        warnings.append({
            "code": "future_invoices_excluded",
            "message": f"{excluded_future} posted invoice(s) dated after the pay-on date were excluded.",
        })

    invoice_ids = [str(row.get("id")) for row in invoices if row.get("id")]
    supplier_ids = sorted({str(row.get("supplier_id")) for row in invoices if row.get("supplier_id")})
    suppliers_by_id = _fetch_suppliers(db, organisation_id, supplier_ids)

    reconciliation_lines = _fetch_reconciliation_lines(db, organisation_id, invoice_ids)
    statement_line_ids = sorted({str(row.get("statement_line_id")) for row in reconciliation_lines if row.get("statement_line_id")})
    payment_ids = sorted({str(row.get("payment_id")) for row in reconciliation_lines if row.get("payment_id")})
    statements_by_id = _fetch_statement_lines(db, organisation_id, statement_line_ids)
    payments_by_id = _fetch_payments(db, organisation_id, payment_ids)

    payments_by_invoice: dict[str, Decimal] = {}
    undated_payments = 0
    future_payments = 0
    for line in reconciliation_lines:
        invoice_id = str(line.get("invoice_extracted_id") or "")
        paid_on = _payment_date(line, statements_by_id, payments_by_id)
        if paid_on is None:
            undated_payments += 1
            continue
        if paid_on > pay_on:
            future_payments += 1
            continue
        amount = money(line.get("matched_amount") if line.get("matched_amount") is not None else line.get("expected_amount"))
        payments_by_invoice[invoice_id] = payments_by_invoice.get(invoice_id, ZERO) + amount

    if undated_payments:
        warnings.append({
            "code": "undated_payments_excluded",
            "message": f"{undated_payments} matched payment line(s) had no payment/statement date and were excluded.",
        })
    if future_payments:
        warnings.append({
            "code": "future_payments_excluded",
            "message": f"{future_payments} matched payment line(s) after the pay-on date were excluded.",
        })

    queue: list[dict[str, Any]] = []
    suppliers: dict[str, dict[str, Any]] = {}
    total_payable = ZERO
    selected_total = ZERO
    blocked_total = ZERO
    overdue_total = ZERO

    for invoice in invoices:
        invoice_id = str(invoice.get("id"))
        total = money(invoice.get("total_amount"))
        paid = payments_by_invoice.get(invoice_id, ZERO)
        outstanding = total - paid
        if outstanding <= ZERO:
            continue

        due_date = _parse_date(invoice.get("due_date"))
        invoice_date = _parse_date(invoice.get("invoice_date"))
        basis_date = due_date or invoice_date or pay_on
        days_overdue = max((pay_on - basis_date).days, 0)
        due_in_days = (basis_date - pay_on).days if basis_date >= pay_on else None
        include_in_run = basis_date <= pay_on or (due_in_days is not None and due_in_days <= due_within_days)

        supplier_id = str(invoice.get("supplier_id") or "unknown")
        supplier = suppliers_by_id.get(supplier_id)
        bank_ready = _bank_ready(supplier)
        priority = _priority(days_overdue, due_in_days, bank_ready)

        total_payable += outstanding
        if include_in_run and bank_ready:
            selected_total += outstanding
        if not bank_ready:
            blocked_total += outstanding
        if days_overdue > 0:
            overdue_total += outstanding

        if supplier_id not in suppliers:
            suppliers[supplier_id] = {
                "supplier_id": None if supplier_id == "unknown" else supplier_id,
                "supplier_name": _supplier_name(invoice, supplier),
                "supplier_code": (supplier or {}).get("supplier_code"),
                "email": _supplier_email(supplier),
                "bank_ready": bank_ready,
                "bank_name": (supplier or {}).get("bank_name"),
                "bank_account_name": (supplier or {}).get("bank_account_name"),
                "bank_account_number": (supplier or {}).get("bank_account_number"),
                "payable_total": ZERO,
                "selected_total": ZERO,
                "blocked_total": ZERO,
                "invoice_count": 0,
                "selected_invoice_count": 0,
            }

        group = suppliers[supplier_id]
        group["payable_total"] += outstanding
        group["invoice_count"] += 1
        if include_in_run and bank_ready:
            group["selected_total"] += outstanding
            group["selected_invoice_count"] += 1
        if not bank_ready:
            group["blocked_total"] += outstanding

        queue.append({
            "invoice_id": invoice_id,
            "invoice_number": invoice.get("invoice_number") or "",
            "supplier_id": None if supplier_id == "unknown" else supplier_id,
            "supplier_name": group["supplier_name"],
            "supplier_code": group["supplier_code"],
            "supplier_email": group["email"],
            "invoice_date": invoice_date.isoformat() if invoice_date else None,
            "due_date": due_date.isoformat() if due_date else None,
            "currency": invoice.get("currency") or "ZAR",
            "total_amount": amount_out(total),
            "paid_amount": amount_out(paid),
            "outstanding_amount": amount_out(outstanding),
            "days_overdue": days_overdue,
            "due_in_days": due_in_days,
            "include_in_run": include_in_run,
            "bank_ready": bank_ready,
            "priority": priority,
            "next_action": _next_action(priority),
        })

    priority_rank = {"blocked": 0, "urgent": 1, "overdue": 2, "due_soon": 3, "scheduled": 4}
    queue.sort(
        key=lambda row: (
            priority_rank.get(str(row["priority"]), 9),
            not row["include_in_run"],
            -int(row["days_overdue"]),
            row["due_date"] or row["invoice_date"] or "",
        )
    )

    supplier_rows = []
    for group in suppliers.values():
        supplier_rows.append({
            "supplier_id": group["supplier_id"],
            "supplier_name": group["supplier_name"],
            "supplier_code": group["supplier_code"],
            "email": group["email"],
            "bank_ready": group["bank_ready"],
            "bank_name": group["bank_name"],
            "bank_account_name": group["bank_account_name"],
            "bank_account_number": group["bank_account_number"],
            "payable_total": amount_out(group["payable_total"]),
            "selected_total": amount_out(group["selected_total"]),
            "blocked_total": amount_out(group["blocked_total"]),
            "invoice_count": group["invoice_count"],
            "selected_invoice_count": group["selected_invoice_count"],
        })
    supplier_rows.sort(key=lambda row: (-float(row["selected_total"]), -float(row["payable_total"]), row["supplier_name"]))

    selected_queue = [row for row in queue if row["include_in_run"]]

    return {
        "organisation_id": organisation_id,
        "pay_on_date": pay_on.isoformat(),
        "due_within_days": due_within_days,
        "summary": {
            "supplier_count": len(supplier_rows),
            "open_invoice_count": len(queue),
            "selected_invoice_count": len(selected_queue),
            "blocked_invoice_count": sum(1 for row in queue if not row["bank_ready"]),
            "overdue_invoice_count": sum(1 for row in queue if row["days_overdue"] > 0),
            "total_payable": amount_out(total_payable),
            "selected_total": amount_out(selected_total),
            "blocked_total": amount_out(blocked_total),
            "overdue_total": amount_out(overdue_total),
        },
        "suppliers": supplier_rows,
        "queue": queue,
        "selected_queue": selected_queue,
        "warnings": warnings,
        "disclaimer": (
            "Calculated from posted supplier invoices less matched payments dated on or before the pay-on date. "
            "This prepares a payment run only; it does not approve, export, mark paid, or post anything."
        ),
    }


def create_supplier_payment_run_draft(
    db,
    *,
    organisation_id: str,
    pay_on_date: str,
    due_within_days: int,
    selected_invoice_ids: list[str],
    created_by: str,
    notes: str | None = None,
) -> dict:
    selected_ids = _dedupe(selected_invoice_ids)
    if not selected_ids:
        raise ValueError("Select at least one invoice for the draft payment run")

    preview = build_supplier_payment_run(
        db,
        organisation_id=organisation_id,
        pay_on_date=pay_on_date,
        due_within_days=due_within_days,
    )
    rows_by_id = {str(row["invoice_id"]): row for row in preview["queue"]}

    missing = [invoice_id for invoice_id in selected_ids if invoice_id not in rows_by_id]
    if missing:
        raise ValueError("Selected invoices are no longer available for this payment run")

    selected_rows = [rows_by_id[invoice_id] for invoice_id in selected_ids]
    blocked = [row for row in selected_rows if not row.get("bank_ready")]
    if blocked:
        raise ValueError("Selected invoices must have supplier banking details before saving a draft")

    currencies = {str(row.get("currency") or "ZAR") for row in selected_rows}
    if len(currencies) > 1:
        raise ValueError("A draft payment run can only contain one currency")

    currency = next(iter(currencies), "ZAR")
    selected_total = sum((money(row.get("outstanding_amount")) for row in selected_rows), ZERO)
    run_id = str(uuid4())
    run_payload = {
        "id": run_id,
        "organisation_id": organisation_id,
        "status": "draft",
        "pay_on_date": preview["pay_on_date"],
        "due_within_days": preview["due_within_days"],
        "currency": currency,
        "invoice_count": len(selected_rows),
        "selected_total": amount_out(selected_total),
        "notes": notes,
        "created_by": created_by,
    }
    run_result = db.table("supplier_payment_runs").insert(run_payload).execute()
    saved_run = (run_result.data or [run_payload])[0]
    run_id = str(saved_run.get("id") or run_id)

    item_payloads = []
    for row in selected_rows:
        item_payloads.append({
            "id": str(uuid4()),
            "organisation_id": organisation_id,
            "payment_run_id": run_id,
            "invoice_extracted_id": row["invoice_id"],
            "supplier_id": row.get("supplier_id"),
            "supplier_name": row.get("supplier_name") or "Unknown supplier",
            "invoice_number": row.get("invoice_number") or "",
            "invoice_date": row.get("invoice_date"),
            "due_date": row.get("due_date"),
            "currency": row.get("currency") or currency,
            "total_amount": row.get("total_amount") or 0,
            "paid_amount": row.get("paid_amount") or 0,
            "outstanding_amount": row.get("outstanding_amount") or 0,
            "priority": row.get("priority") or "scheduled",
            "bank_ready": bool(row.get("bank_ready")),
        })

    item_result = db.table("supplier_payment_run_items").insert(item_payloads).execute()
    saved_items = item_result.data or item_payloads

    return {
        "payment_run": {
            **saved_run,
            "items": saved_items,
            "summary": {
                "invoice_count": len(saved_items),
                "selected_total": amount_out(selected_total),
                "currency": currency,
            },
        }
    }
