from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
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


def list_supplier_payment_run_drafts(db, organisation_id: str) -> list[dict]:
    return _fetch_rows(
        db.table("supplier_payment_runs")
        .select(
            "id, status, pay_on_date, due_within_days, currency, invoice_count, "
            "selected_total, notes, created_by, approved_by, approved_at, created_at, updated_at"
        )
        .eq("organisation_id", organisation_id)
        .order("created_at", desc=True)
        .limit(100)
    )


def get_supplier_payment_run_draft(db, draft_id: str, organisation_id: str) -> dict:
    rows = _fetch_rows(
        db.table("supplier_payment_runs")
        .select("*")
        .eq("id", draft_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
    )
    if not rows:
        raise ValueError("Payment run draft not found")
    items = _fetch_rows(
        db.table("supplier_payment_run_items")
        .select("*")
        .eq("payment_run_id", draft_id)
        .order("priority")
    )
    remittances = _fetch_rows(
        db.table("remittances")
        .select("*")
        .eq("payment_run_id", draft_id)
        .order("supplier_name")
    )
    return {**rows[0], "items": items, "remittances": remittances}


def approve_supplier_payment_run_draft(
    db, draft_id: str, organisation_id: str, approved_by: str
) -> dict:
    rows = _fetch_rows(
        db.table("supplier_payment_runs")
        .select("id, status")
        .eq("id", draft_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
    )
    if not rows:
        raise ValueError("Payment run draft not found")
    if rows[0]["status"] != "draft":
        raise ValueError(
            f"Only draft payment runs can be approved (current status: {rows[0]['status']})"
        )
    now = datetime.now(timezone.utc).isoformat()
    result = (
        db.table("supplier_payment_runs")
        .update({"status": "approved", "approved_by": approved_by, "approved_at": now})
        .eq("id", draft_id)
        .execute()
    )
    return (result.data or [{}])[0]


def cancel_supplier_payment_run_draft(db, draft_id: str, organisation_id: str) -> dict:
    rows = _fetch_rows(
        db.table("supplier_payment_runs")
        .select("id, status")
        .eq("id", draft_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
    )
    if not rows:
        raise ValueError("Payment run draft not found")
    status = rows[0]["status"]
    if status in ("cancelled", "exported"):
        raise ValueError(f"Cannot cancel a payment run with status '{status}'")
    result = (
        db.table("supplier_payment_runs")
        .update({"status": "cancelled"})
        .eq("id", draft_id)
        .execute()
    )
    return (result.data or [{}])[0]


def export_supplier_payment_run_draft_csv(
    db, draft_id: str, organisation_id: str
) -> tuple[str, bytes]:
    draft = get_supplier_payment_run_draft(db, draft_id, organisation_id)
    if draft.get("status") == "cancelled":
        raise ValueError("Cannot export a cancelled payment run")
    items = draft.get("items", [])
    if not items:
        raise ValueError("Payment run has no items to export")

    supplier_ids = _dedupe([str(item["supplier_id"]) for item in items if item.get("supplier_id")])
    suppliers_by_id = _fetch_suppliers(db, organisation_id, supplier_ids) if supplier_ids else {}

    pay_date = str(draft.get("pay_on_date") or "")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Supplier Name",
        "Bank Name",
        "Branch Code",
        "Account Number",
        "Account Name",
        "Amount",
        "Currency",
        "Invoice Reference",
        "Pay Date",
    ])
    for item in items:
        sid = str(item.get("supplier_id") or "")
        sup = suppliers_by_id.get(sid)
        writer.writerow([
            item.get("supplier_name") or "",
            (sup or {}).get("bank_name") or "",
            (sup or {}).get("bank_branch_code") or "",
            (sup or {}).get("bank_account_number") or "",
            (sup or {}).get("bank_account_name") or "",
            item.get("outstanding_amount") or 0,
            item.get("currency") or "ZAR",
            item.get("invoice_number") or "",
            pay_date,
        ])

    filename = f"payment_run_{draft_id[:8]}_{pay_date}.csv"
    return filename, buf.getvalue().encode("utf-8")


def generate_supplier_payment_run_remittances(
    db,
    draft_id: str,
    organisation_id: str,
    generated_by: str,
) -> dict:
    draft = get_supplier_payment_run_draft(db, draft_id, organisation_id)
    status = str(draft.get("status") or "")
    if status not in ("approved", "exported"):
        raise ValueError("Remittances can only be generated for approved payment runs")

    items = list(draft.get("items") or [])
    if not items:
        raise ValueError("Payment run has no invoice lines for remittance generation")

    existing = list(draft.get("remittances") or [])
    if existing:
        return {
            "remittances": existing,
            "created_count": 0,
            "reused_count": len(existing),
        }

    missing_supplier = [item for item in items if not item.get("supplier_id")]
    if missing_supplier:
        raise ValueError("All remittance lines must have a linked supplier")

    supplier_ids = _dedupe([str(item["supplier_id"]) for item in items if item.get("supplier_id")])
    suppliers_by_id = _fetch_suppliers(db, organisation_id, supplier_ids) if supplier_ids else {}
    by_supplier: dict[str, list[dict]] = {}
    for item in items:
        by_supplier.setdefault(str(item["supplier_id"]), []).append(item)

    remittance_payloads = []
    remittance_date = str(draft.get("pay_on_date") or date.today().isoformat())
    for supplier_id, supplier_items in by_supplier.items():
        supplier = suppliers_by_id.get(supplier_id) or {}
        currency = str(supplier_items[0].get("currency") or draft.get("currency") or "ZAR")
        total = sum((money(item.get("outstanding_amount")) for item in supplier_items), ZERO)
        references = [
            {
                "invoice_extracted_id": item.get("invoice_extracted_id"),
                "invoice_number": item.get("invoice_number") or "",
                "invoice_date": item.get("invoice_date"),
                "due_date": item.get("due_date"),
                "amount": item.get("outstanding_amount") or 0,
                "currency": item.get("currency") or currency,
            }
            for item in supplier_items
        ]
        remittance_payloads.append({
            "id": str(uuid4()),
            "organisation_id": organisation_id,
            "supplier_id": supplier_id,
            "payment_run_id": draft_id,
            "remittance_date": remittance_date,
            "remittance_status": "draft",
            "total_amount": amount_out(total),
            "currency": currency,
            "supplier_name": supplier.get("supplier_name") or supplier_items[0].get("supplier_name") or "Unknown supplier",
            "supplier_email": _supplier_email(supplier),
            "invoice_count": len(supplier_items),
            "invoice_references": references,
            "generated_by": generated_by,
        })

    result = db.table("remittances").insert(remittance_payloads).execute()
    created = result.data or remittance_payloads
    return {
        "remittances": created,
        "created_count": len(created),
        "reused_count": 0,
    }


def _payment_reference(draft_id: str, supplier_id: str) -> str:
    return f"PAYRUN-{draft_id[:8]}-{supplier_id[:8]}"


def mark_supplier_payment_run_paid(
    db,
    draft_id: str,
    organisation_id: str,
    paid_by: str,
    payment_date: str | None = None,
) -> dict:
    draft = get_supplier_payment_run_draft(db, draft_id, organisation_id)
    status = str(draft.get("status") or "")
    if status not in ("approved", "exported"):
        raise ValueError("Only approved or exported payment runs can be marked paid")

    paid_on = _validate_date(payment_date or str(draft.get("pay_on_date") or date.today().isoformat()), "payment_date")
    items = list(draft.get("items") or [])
    if not items:
        raise ValueError("Payment run has no invoice lines to mark paid")

    missing_supplier = [item for item in items if not item.get("supplier_id")]
    if missing_supplier:
        raise ValueError("All payment run lines must have a linked supplier")

    by_supplier: dict[str, list[dict]] = {}
    for item in items:
        by_supplier.setdefault(str(item["supplier_id"]), []).append(item)

    created_payments: list[dict] = []
    reused_payments: list[dict] = []
    reconciliation_rows: list[dict] = []
    remittances_by_supplier = {
        str(row.get("supplier_id")): row
        for row in list(draft.get("remittances") or [])
        if row.get("supplier_id")
    }

    for supplier_id, supplier_items in by_supplier.items():
        reference = _payment_reference(draft_id, supplier_id)
        total = sum((money(item.get("outstanding_amount")) for item in supplier_items), ZERO)
        payment_amount = amount_out(total)
        existing = _fetch_rows(
            db.table("payments")
            .select("*")
            .eq("organisation_id", organisation_id)
            .eq("supplier_id", supplier_id)
            .eq("payment_reference", reference)
            .limit(1)
        )
        if existing:
            payment = existing[0]
            reused_payments.append(payment)
            payment_id = str(payment.get("id"))
        else:
            payment_payload = {
                "id": str(uuid4()),
                "organisation_id": organisation_id,
                "supplier_id": supplier_id,
                "payment_date": paid_on.isoformat(),
                "payment_reference": reference,
                "amount": payment_amount,
                "currency": str(supplier_items[0].get("currency") or draft.get("currency") or "ZAR"),
                "payment_source": "supplier_payment_run",
                "match_status": "matched",
            }
            payment_result = db.table("payments").insert(payment_payload).execute()
            payment = (payment_result.data or [payment_payload])[0]
            created_payments.append(payment)
            payment_id = str(payment.get("id") or payment_payload["id"])

        existing_lines = _fetch_rows(
            db.table("reconciliation_lines")
            .select("id")
            .eq("organisation_id", organisation_id)
            .eq("payment_id", payment_id)
            .limit(1)
        )
        if not existing_lines:
            reconciliation_id = str(uuid4())
            db.table("reconciliations").insert({
                "id": reconciliation_id,
                "organisation_id": organisation_id,
                "supplier_id": supplier_id,
                "reconciliation_date": paid_on.isoformat(),
                "reconciliation_status": "completed",
                "total_statement_amount": payment_amount,
                "total_matched_amount": payment_amount,
                "total_unmatched_amount": 0,
                "notes": f"Payment run {draft_id} marked paid by {paid_by}.",
                "created_by": paid_by,
            }).execute()

            for item in supplier_items:
                amount = money(item.get("outstanding_amount"))
                reconciliation_rows.append({
                    "id": str(uuid4()),
                    "organisation_id": organisation_id,
                    "reconciliation_id": reconciliation_id,
                    "invoice_extracted_id": item.get("invoice_extracted_id"),
                    "payment_id": payment_id,
                    "match_status": "matched",
                    "expected_amount": amount_out(amount),
                    "matched_amount": amount_out(amount),
                    "variance_amount": 0,
                    "notes": f"Matched from supplier payment run {draft_id}.",
                })

        remittance = remittances_by_supplier.get(supplier_id)
        if remittance and not remittance.get("payment_id"):
            db.table("remittances").update({"payment_id": payment_id}).eq("id", remittance["id"]).execute()

    if reconciliation_rows:
        db.table("reconciliation_lines").insert(reconciliation_rows).execute()

    run_update = db.table("supplier_payment_runs").update({"status": "exported"}).eq("id", draft_id).execute()
    updated_run = (run_update.data or [draft])[0]

    return {
        "draft": updated_run,
        "payments": created_payments + reused_payments,
        "created_payment_count": len(created_payments),
        "reused_payment_count": len(reused_payments),
        "reconciliation_line_count": len(reconciliation_rows),
    }
