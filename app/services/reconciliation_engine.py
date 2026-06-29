from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from app.models.schemas import RunReconciliationRequest, RunReconciliationResponse
from app.services.money import money


ZERO = Decimal("0.00")
DEFAULT_TOLERANCE = Decimal("0.01")


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _f(value: Optional[Decimal]) -> Optional[float]:
    return float(value) if value is not None else None


def _line_amount(line: dict) -> Decimal:
    debit = money(line.get("debit_amount")) or ZERO
    credit = money(line.get("credit_amount")) or ZERO
    return debit if debit > ZERO else credit


def _invoice_amount(inv: dict) -> Decimal:
    return money(inv.get("total_amount")) or ZERO


def _payment_amount(payment: dict) -> Decimal:
    return money(payment.get("amount")) or ZERO


def _normalise_number(value: Any) -> str:
    return str(value or "").strip().upper()


def _line_match_text(line: dict) -> str:
    return " ".join(
        _normalise_number(line.get(field))
        for field in ("invoice_number", "reference", "description")
        if line.get(field)
    )


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _payment_linked_invoice_ids(db, organisation_id: str, payment_ids: list[str]) -> dict[str, set[str]]:
    if not payment_ids:
        return {}
    try:
        rows = (
            db.table("reconciliation_lines")
            .select("payment_id, invoice_extracted_id")
            .eq("organisation_id", organisation_id)
            .in_("payment_id", payment_ids)
            .execute()
            .data or []
        )
    except Exception:
        return {}

    linked: dict[str, set[str]] = {}
    for row in rows:
        payment_id = str(row.get("payment_id") or "")
        invoice_id = str(row.get("invoice_extracted_id") or "")
        if payment_id and invoice_id:
            linked.setdefault(payment_id, set()).add(invoice_id)
    return linked


def _reference_payment_candidates(line: dict, payments: list[dict], used_payment_ids: set[str]) -> list[dict]:
    line_text = _line_match_text(line)
    if not line_text:
        return []
    candidates = []
    for payment in payments:
        payment_id = str(payment.get("id") or "")
        reference = _normalise_number(payment.get("payment_reference"))
        if payment_id in used_payment_ids or not reference:
            continue
        if reference in line_text:
            candidates.append(payment)
    return candidates


def _amount_date_payment_candidates(
    line: dict,
    payments: list[dict],
    used_payment_ids: set[str],
    amount: Decimal,
    tolerance: Decimal,
    date_tolerance_days: int,
) -> list[dict]:
    line_date = _parse_date(line.get("line_date"))
    candidates = []
    for payment in payments:
        payment_id = str(payment.get("id") or "")
        if payment_id in used_payment_ids:
            continue
        if abs(_payment_amount(payment) - amount) > tolerance:
            continue
        payment_date = _parse_date(payment.get("payment_date"))
        if line_date and payment_date and abs((line_date - payment_date).days) > date_tolerance_days:
            continue
        candidates.append(payment)
    return candidates


def run_reconciliation(db, request: RunReconciliationRequest) -> RunReconciliationResponse:
    reconciliation_id = str(uuid4())
    job_id = uuid4()
    tolerance = _dec(getattr(request.options, "amount_tolerance", None)) or DEFAULT_TOLERANCE
    date_tolerance_days = int(getattr(request.options, "date_tolerance_days", 5) or 0)

    statement_lines = (
        db.table("statement_lines")
        .select("*")
        .eq("statement_raw_id", str(request.statement_raw_id))
        .execute()
        .data or []
    )

    invoices = (
        db.table("invoices_extracted")
        .select("*")
        .eq("supplier_id", str(request.supplier_id))
        .execute()
        .data or []
    )

    payments = (
        db.table("payments")
        .select("*")
        .eq("organisation_id", str(request.organisation_id))
        .eq("supplier_id", str(request.supplier_id))
        .execute()
        .data or []
    )
    payment_run_payments = [
        payment
        for payment in payments
        if str(payment.get("payment_source") or "") == "supplier_payment_run"
        and _payment_amount(payment) > ZERO
    ]
    invoices_by_payment_id = _payment_linked_invoice_ids(
        db,
        str(request.organisation_id),
        [str(payment.get("id")) for payment in payment_run_payments if payment.get("id")],
    )

    by_number: dict[str, dict] = {}
    by_amount: dict[str, list[dict]] = {}
    for inv in invoices:
        num = _normalise_number(inv.get("invoice_number"))
        if num:
            by_number[num] = inv
        amt_key = str(_invoice_amount(inv).quantize(Decimal("0.01")))
        by_amount.setdefault(amt_key, []).append(inv)

    matched_invoice_ids: set[str] = set()
    matched_payment_ids: set[str] = set()
    recon_rows: list[dict] = []
    line_results: list[dict] = []
    line_status_updates: list[tuple[str, str]] = []

    for line in statement_lines:
        line_id = line.get("id")
        line_num = _normalise_number(line.get("invoice_number") or line.get("reference"))
        line_amt = _line_amount(line)

        match_status: str
        exception_type: Optional[str]
        notes: str
        matched_inv: Optional[dict] = None
        matched_payment: Optional[dict] = None
        expected: Optional[Decimal] = None
        variance: Optional[Decimal] = None

        payment_candidates = _reference_payment_candidates(line, payment_run_payments, matched_payment_ids)
        if len(payment_candidates) == 1:
            matched_payment = payment_candidates[0]
            expected = _payment_amount(matched_payment)
            variance = line_amt - expected
            if abs(variance) <= tolerance:
                match_status = "matched"
                exception_type = None
                notes = f"Matched supplier payment run payment {matched_payment.get('payment_reference')}."
                variance = ZERO
            else:
                match_status = "exception"
                exception_type = "amount_mismatch"
                notes = (
                    f"Payment reference matched but amount differs by "
                    f"{abs(variance):.2f} (statement: {line_amt:.2f}, "
                    f"payment: {expected:.2f})."
                )

        elif len(payment_candidates) > 1:
            match_status = "exception"
            exception_type = "ambiguous_payment_reference"
            notes = f"Statement reference matches {len(payment_candidates)} payment-run payments - manual review required."

        elif line_num and line_num in by_number:
            matched_inv = by_number[line_num]
            expected = _invoice_amount(matched_inv)
            variance = line_amt - expected
            if abs(variance) <= tolerance:
                match_status = "matched"
                exception_type = None
                notes = "Matched on invoice number and amount."
                variance = ZERO
            else:
                match_status = "exception"
                exception_type = "amount_mismatch"
                notes = (
                    f"Invoice number matched but amount differs by "
                    f"{abs(variance):.2f} (statement: {line_amt:.2f}, "
                    f"invoice: {expected:.2f})."
                )

        elif line_amt > ZERO:
            payment_candidates = _amount_date_payment_candidates(
                line,
                payment_run_payments,
                matched_payment_ids,
                line_amt,
                tolerance,
                date_tolerance_days,
            )
            if len(payment_candidates) == 1:
                matched_payment = payment_candidates[0]
                expected = _payment_amount(matched_payment)
                variance = line_amt - expected
                if abs(variance) <= tolerance:
                    variance = ZERO
                match_status = "matched"
                exception_type = None
                notes = (
                    f"Matched supplier payment run payment {matched_payment.get('payment_reference')} "
                    f"on amount/date."
                )
            elif len(payment_candidates) > 1:
                match_status = "exception"
                exception_type = "ambiguous_payment_amount"
                notes = f"Amount {line_amt:.2f} matches {len(payment_candidates)} payment-run payments - manual review required."
            else:
                amt_key = str(line_amt.quantize(Decimal("0.01")))
                candidates = [
                    inv for inv in by_amount.get(amt_key, [])
                    if inv.get("id") not in matched_invoice_ids
                ]
                if not candidates:
                    candidates = [
                        inv for inv in invoices
                        if inv.get("id") not in matched_invoice_ids
                        and abs(_invoice_amount(inv) - line_amt) <= tolerance
                    ]
                if len(candidates) == 1:
                    matched_inv = candidates[0]
                    expected = _invoice_amount(matched_inv)
                    variance = line_amt - expected
                    if abs(variance) <= tolerance:
                        variance = ZERO
                    match_status = "matched"
                    exception_type = None
                    inv_num = matched_inv.get("invoice_number") or matched_inv.get("id", "")[:8]
                    notes = f"Matched on amount ({line_amt:.2f}) - no invoice number on statement line. Matched to {inv_num}."
                elif len(candidates) > 1:
                    match_status = "exception"
                    exception_type = "ambiguous_amount"
                    notes = f"Amount {line_amt:.2f} matches {len(candidates)} invoices - manual review required."
                    matched_inv = None
                else:
                    match_status = "unmatched"
                    exception_type = "no_match"
                    closest = min(invoices, key=lambda i: abs(_invoice_amount(i) - line_amt), default=None)
                    if closest:
                        diff = abs(_invoice_amount(closest) - line_amt)
                        notes = (
                            f"No matching invoice found. Closest: "
                            f"{closest.get('invoice_number') or 'unknown'} "
                            f"({_invoice_amount(closest):.2f}, diff {diff:.2f})."
                        )
                    else:
                        notes = "No invoices or payment-run payments on record for this supplier."
                    matched_inv = None

        else:
            match_status = "unmatched"
            exception_type = "credit_or_zero"
            notes = "Line has no debit amount - may be a payment or credit."
            matched_inv = None

        if matched_inv:
            matched_invoice_ids.add(str(matched_inv.get("id", "")))
        if matched_payment:
            matched_payment_id_for_tracking = str(matched_payment.get("id") or "")
            if matched_payment_id_for_tracking:
                matched_payment_ids.add(matched_payment_id_for_tracking)
                matched_invoice_ids.update(invoices_by_payment_id.get(matched_payment_id_for_tracking, set()))

        matched_invoice_id = str(matched_inv["id"]) if matched_inv else None
        matched_invoice_number = matched_inv.get("invoice_number") if matched_inv else None
        matched_payment_id = str(matched_payment["id"]) if matched_payment else None
        matched_payment_reference = matched_payment.get("payment_reference") if matched_payment else None

        recon_rows.append({
            "id": str(uuid4()),
            "organisation_id": str(request.organisation_id),
            "reconciliation_id": reconciliation_id,
            "statement_line_id": line_id,
            "invoice_extracted_id": matched_invoice_id,
            "payment_id": matched_payment_id,
            "match_status": match_status,
            "exception_type": exception_type,
            "expected_amount": str(expected) if expected is not None else None,
            "matched_amount": str(line_amt),
            "variance_amount": str(variance) if variance is not None else None,
            "notes": notes,
        })

        line_results.append({
            "line_id": line_id,
            "match_status": match_status,
            "expected_amount": _f(expected),
            "matched_amount": _f(line_amt),
            "variance_amount": _f(variance),
            "matched_invoice_id": matched_invoice_id,
            "matched_invoice_number": matched_invoice_number,
            "matched_payment_id": matched_payment_id,
            "matched_payment_reference": matched_payment_reference,
            "notes": notes,
        })

        line_status_updates.append((line_id, match_status))

    missing_invoices = [
        {
            "invoice_id": inv.get("id"),
            "invoice_number": inv.get("invoice_number"),
            "invoice_date": inv.get("invoice_date"),
            "total_amount": _f(_invoice_amount(inv)),
            "supplier_name": inv.get("supplier_name"),
        }
        for inv in invoices
        if str(inv.get("id", "")) not in matched_invoice_ids
        and _invoice_amount(inv) > ZERO
    ]

    try:
        db.table("reconciliations").insert({
            "id": reconciliation_id,
            "organisation_id": str(request.organisation_id),
            "supplier_id": str(request.supplier_id),
            "statement_raw_id": str(request.statement_raw_id),
            "reconciliation_status": "completed",
            "notes": f"Reconciliation run: {len(line_results)} lines, {len(missing_invoices)} missing invoices.",
        }).execute()
    except Exception:
        pass

    if recon_rows:
        try:
            db.table("reconciliation_lines").insert(recon_rows).execute()
        except Exception:
            pass

    for line_id, status in line_status_updates:
        if line_id:
            try:
                db.table("statement_lines").update({"match_status": status}).eq("id", line_id).execute()
            except Exception:
                pass

    matched = sum(1 for r in line_results if r["match_status"] == "matched")
    unmatched = sum(1 for r in line_results if r["match_status"] == "unmatched")
    exceptions = sum(1 for r in line_results if r["match_status"] == "exception")

    return RunReconciliationResponse(
        job_id=job_id,
        reconciliation_id=reconciliation_id,
        status="completed",
        summary={
            "total_lines": len(line_results),
            "matched": matched,
            "unmatched": unmatched,
            "exceptions": exceptions,
            "missing_invoice_count": len(missing_invoices),
        },
        lines=line_results,
        missing_invoices=missing_invoices,
    )
