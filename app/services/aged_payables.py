from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.services.money import money


ZERO = Decimal("0.00")
MONEY = Decimal("0.01")
CHUNK_SIZE = 200
RECEIPT_DOCUMENT_TYPES = {"receipt", "cashreceipt", "cash_receipt", "cardreceipt", "card_receipt"}


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


def _supplier_name(invoice: dict, supplier: dict | None) -> str:
    if supplier:
        return (
            supplier.get("supplier_name")
            or supplier.get("trading_name")
            or supplier.get("name")
            or "Unnamed supplier"
        )
    return invoice.get("supplier_name_extracted") or "Unknown supplier"


def _fetch_posted_invoices(db, organisation_id: str) -> list[dict]:
    return _fetch_rows(
        db.table("invoices_extracted")
        .select(
            "id, organisation_id, supplier_id, supplier_name_extracted, invoice_number, "
            "invoice_date, due_date, total_amount, currency, posting_status, document_type, document_direction"
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
                .select("id, organisation_id, supplier_name, trading_name")
                .eq("organisation_id", organisation_id)
                .in_("id", chunk)
            )
        )
    return {str(row.get("id")): row for row in rows if row.get("id")}


def _fetch_reconciliation_lines(db, organisation_id: str, invoice_ids: list[str]) -> list[dict]:
    if not invoice_ids:
        return []
    rows: list[dict] = []
    for chunk in _chunked(invoice_ids, CHUNK_SIZE):
        rows.extend(
            _fetch_rows(
                db.table("reconciliation_lines")
                .select(
                    "id, organisation_id, invoice_extracted_id, statement_line_id, payment_id, "
                    "match_status, matched_amount, expected_amount"
                )
                .eq("organisation_id", organisation_id)
                .in_("invoice_extracted_id", chunk)
                .eq("match_status", "matched")
            )
        )
    return rows


def _fetch_statement_lines(db, organisation_id: str, statement_line_ids: list[str]) -> dict[str, dict]:
    if not statement_line_ids:
        return {}
    rows: list[dict] = []
    for chunk in _chunked(statement_line_ids, CHUNK_SIZE):
        rows.extend(
            _fetch_rows(
                db.table("statement_lines")
                .select("id, organisation_id, line_date")
                .eq("organisation_id", organisation_id)
                .in_("id", chunk)
            )
        )
    return {str(row.get("id")): row for row in rows if row.get("id")}


def _fetch_payments(db, organisation_id: str, payment_ids: list[str]) -> dict[str, dict]:
    if not payment_ids:
        return {}
    rows: list[dict] = []
    for chunk in _chunked(payment_ids, CHUNK_SIZE):
        rows.extend(
            _fetch_rows(
                db.table("payments")
                .select("id, organisation_id, payment_date")
                .eq("organisation_id", organisation_id)
                .in_("id", chunk)
            )
        )
    return {str(row.get("id")): row for row in rows if row.get("id")}


def _payment_date(line: dict, statements_by_id: dict[str, dict], payments_by_id: dict[str, dict]) -> date | None:
    payment_id = str(line.get("payment_id") or "")
    if payment_id and payment_id in payments_by_id:
        parsed = _parse_date(payments_by_id[payment_id].get("payment_date"))
        if parsed:
            return parsed

    statement_line_id = str(line.get("statement_line_id") or "")
    if statement_line_id and statement_line_id in statements_by_id:
        return _parse_date(statements_by_id[statement_line_id].get("line_date"))
    return None


def generate_aged_payables(
    db,
    *,
    organisation_id: str,
    as_at_date: str,
) -> dict:
    as_at = _validate_as_at(as_at_date)
    warnings: list[dict] = []

    all_invoices = _fetch_posted_invoices(db, organisation_id)
    invoices: list[dict] = []
    excluded_receipts = 0
    excluded_future = 0
    undated_invoices = 0

    for invoice in all_invoices:
        document_type = str(invoice.get("document_type") or "").lower()
        if document_type in RECEIPT_DOCUMENT_TYPES:
            excluded_receipts += 1
            continue

        invoice_date = _parse_date(invoice.get("invoice_date"))
        due_date = _parse_date(invoice.get("due_date"))
        basis_date = invoice_date or due_date
        if basis_date and basis_date > as_at:
            excluded_future += 1
            continue
        if not basis_date:
            undated_invoices += 1
        invoices.append(invoice)

    if excluded_receipts:
        warnings.append({
            "code": "receipts_excluded",
            "message": f"{excluded_receipts} posted receipt/card receipt document(s) were excluded from payables aging.",
        })
    if excluded_future:
        warnings.append({
            "code": "future_invoices_excluded",
            "message": f"{excluded_future} posted invoice(s) dated after the as-at date were excluded.",
        })
    if undated_invoices:
        warnings.append({
            "code": "undated_invoices_included",
            "message": f"{undated_invoices} posted invoice(s) had no invoice or due date and were included as current.",
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
        if paid_on > as_at:
            future_payments += 1
            continue
        amount = money(line.get("matched_amount") if line.get("matched_amount") is not None else line.get("expected_amount"))
        payments_by_invoice[invoice_id] = payments_by_invoice.get(invoice_id, ZERO) + amount

    if undated_payments:
        warnings.append({
            "code": "undated_payments_excluded",
            "message": f"{undated_payments} matched payment line(s) had no payment/statement date and were excluded from the as-at balance.",
        })
    if future_payments:
        warnings.append({
            "code": "future_payments_excluded",
            "message": f"{future_payments} matched payment line(s) after the as-at date were excluded.",
        })

    supplier_groups: dict[str, dict[str, Any]] = {}
    summary_buckets = _blank_buckets()
    total_outstanding = ZERO
    open_invoice_count = 0

    for invoice in invoices:
        invoice_id = str(invoice.get("id"))
        total = money(invoice.get("total_amount"))
        paid = payments_by_invoice.get(invoice_id, ZERO)
        outstanding = total - paid
        if outstanding <= ZERO:
            continue

        open_invoice_count += 1
        supplier_id = str(invoice.get("supplier_id") or "unknown")
        supplier = suppliers_by_id.get(supplier_id)
        due_date = _parse_date(invoice.get("due_date"))
        invoice_date = _parse_date(invoice.get("invoice_date"))
        age_date = due_date or invoice_date or as_at
        days_overdue = max((as_at - age_date).days, 0)
        bucket_key = _bucket(days_overdue)

        if supplier_id not in supplier_groups:
            supplier_groups[supplier_id] = {
                "supplier_id": None if supplier_id == "unknown" else supplier_id,
                "supplier_name": _supplier_name(invoice, supplier),
                "total_outstanding": ZERO,
                "buckets": _blank_buckets(),
                "invoices": [],
            }

        group = supplier_groups[supplier_id]
        group["total_outstanding"] += outstanding
        group["buckets"][bucket_key] += outstanding
        total_outstanding += outstanding
        summary_buckets[bucket_key] += outstanding
        group["invoices"].append({
            "invoice_id": invoice_id,
            "invoice_number": invoice.get("invoice_number") or "",
            "invoice_date": invoice_date.isoformat() if invoice_date else None,
            "due_date": due_date.isoformat() if due_date else None,
            "currency": invoice.get("currency") or "ZAR",
            "total_amount": amount_out(total),
            "paid_amount": amount_out(paid),
            "outstanding_amount": amount_out(outstanding),
            "days_overdue": days_overdue,
            "bucket": bucket_key,
        })

    suppliers = []
    for group in supplier_groups.values():
        suppliers.append({
            "supplier_id": group["supplier_id"],
            "supplier_name": group["supplier_name"],
            "total_outstanding": amount_out(group["total_outstanding"]),
            "buckets": _buckets_out(group["buckets"]),
            "invoices": sorted(
                group["invoices"],
                key=lambda row: (
                    row["due_date"] or row["invoice_date"] or "",
                    row["invoice_number"] or "",
                ),
            ),
        })
    suppliers.sort(key=lambda row: (-row["total_outstanding"], row["supplier_name"]))

    return {
        "organisation_id": organisation_id,
        "as_at_date": as_at.isoformat(),
        "suppliers": suppliers,
        "summary": {
            "supplier_count": len(suppliers),
            "open_invoice_count": open_invoice_count,
            "total_outstanding": amount_out(total_outstanding),
            "buckets": _buckets_out(summary_buckets),
        },
        "warnings": warnings,
        "disclaimer": (
            "Calculated from posted supplier invoices and matched supplier statement/payment "
            "reconciliation lines dated on or before the selected as-at date."
        ),
    }
