from __future__ import annotations

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


def _normalise_number(value: Any) -> str:
    return str(value or "").strip().upper()


def run_reconciliation(db, request: RunReconciliationRequest) -> RunReconciliationResponse:
    reconciliation_id = str(uuid4())
    job_id = uuid4()
    tolerance = _dec(getattr(request.options, "amount_tolerance", None)) or DEFAULT_TOLERANCE

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

    # Build lookup indexes
    by_number: dict[str, dict] = {}
    by_amount: dict[str, list[dict]] = {}
    for inv in invoices:
        num = _normalise_number(inv.get("invoice_number"))
        if num:
            by_number[num] = inv
        amt_key = str(_invoice_amount(inv).quantize(Decimal("0.01")))
        by_amount.setdefault(amt_key, []).append(inv)

    matched_invoice_ids: set[str] = set()
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
        expected: Optional[Decimal] = None
        variance: Optional[Decimal] = None

        if line_num and line_num in by_number:
            # Strategy 1: exact invoice number match
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
            # Strategy 2: amount-only match (when no invoice number or number not found)
            amt_key = str(line_amt.quantize(Decimal("0.01")))
            candidates = [
                inv for inv in by_amount.get(amt_key, [])
                if inv.get("id") not in matched_invoice_ids
            ]
            # Also check within tolerance range
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
                notes = f"Matched on amount ({line_amt:.2f}) — no invoice number on statement line. Matched to {inv_num}."
            elif len(candidates) > 1:
                match_status = "exception"
                exception_type = "ambiguous_amount"
                notes = f"Amount {line_amt:.2f} matches {len(candidates)} invoices — manual review required."
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
                    notes = "No invoices on record for this supplier."
                matched_inv = None

        else:
            # Credit line or zero amount — no match needed
            match_status = "unmatched"
            exception_type = "credit_or_zero"
            notes = "Line has no debit amount — may be a payment or credit."
            matched_inv = None

        if matched_inv:
            matched_invoice_ids.add(str(matched_inv.get("id", "")))

        matched_invoice_id = str(matched_inv["id"]) if matched_inv else None
        matched_invoice_number = matched_inv.get("invoice_number") if matched_inv else None

        recon_rows.append({
            "id": str(uuid4()),
            "organisation_id": str(request.organisation_id),
            "reconciliation_id": reconciliation_id,
            "statement_line_id": line_id,
            "invoice_extracted_id": matched_invoice_id,
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
            "notes": notes,
        })

        line_status_updates.append((line_id, match_status))

    # Missing invoices: in our AP but not on the supplier statement
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

    # Persist reconciliation header
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
        pass  # reconciliations table may not exist in all environments

    # Persist line results
    if recon_rows:
        try:
            db.table("reconciliation_lines").insert(recon_rows).execute()
        except Exception:
            pass

    # Batch-update statement_lines match_status
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
