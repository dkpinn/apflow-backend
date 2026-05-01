from uuid import uuid4
from decimal import Decimal
from app.models.schemas import RunReconciliationRequest, RunReconciliationResponse
from app.db.supabase_client import get_supabase_client


def money(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def dec_to_float(value):
    if value is None:
        return None
    return float(value)


def queue_reconciliation_job(request: RunReconciliationRequest) -> RunReconciliationResponse:
    supabase = get_supabase_client()

    reconciliation_id = str(uuid4())
    job_id = uuid4()

    # Create reconciliation header
    supabase.table("reconciliations").insert({
        "id": reconciliation_id,
        "organisation_id": str(request.organisation_id),
        "supplier_id": str(request.supplier_id),
        "statement_raw_id": str(request.statement_raw_id),
        "reconciliation_status": "completed",
        "notes": "Exact-match reconciliation run from FastAPI backend.",
    }).execute()

    # Fetch statement lines
    statement_lines = (
        supabase.table("statement_lines")
        .select("*")
        .eq("statement_raw_id", str(request.statement_raw_id))
        .execute()
        .data
    )

    # Fetch supplier invoices
    invoices = (
        supabase.table("invoices_extracted")
        .select("*")
        .eq("supplier_id", str(request.supplier_id))
        .execute()
        .data
    )

    invoices_by_number = {
        inv.get("invoice_number"): inv
        for inv in invoices
        if inv.get("invoice_number")
    }

    reconciliation_lines_to_insert = []
    line_results = []

    for line in statement_lines:
        invoice_number = line.get("invoice_number")
        debit_amount = money(line.get("debit_amount"))

        matched_invoice = invoices_by_number.get(invoice_number)

        if matched_invoice:
            expected_amount = money(matched_invoice.get("total_amount"))
            matched_amount = debit_amount
            variance = matched_amount - expected_amount

            if variance == Decimal("0.00"):
                match_status = "matched"
                exception_type = None
                notes = "Exact match on invoice number and amount."
            else:
                match_status = "exception"
                exception_type = "amount_mismatch"
                notes = "Invoice number matched, but amount differs."

            matched_invoice_id = matched_invoice.get("id")
            matched_invoice_number = matched_invoice.get("invoice_number")

        else:
            expected_amount = None
            matched_amount = debit_amount
            variance = None
            match_status = "unmatched"
            exception_type = "review_required"
            notes = "No matching invoice found for this statement line."
            matched_invoice_id = None
            matched_invoice_number = None

        reconciliation_lines_to_insert.append({
            "id": str(uuid4()),
            "organisation_id": str(request.organisation_id),
            "reconciliation_id": reconciliation_id,
            "statement_line_id": line.get("id"),
            "invoice_extracted_id": matched_invoice_id,
            "match_status": match_status,
            "exception_type": exception_type,
            "expected_amount": str(expected_amount) if expected_amount is not None else None,
            "matched_amount": str(matched_amount) if matched_amount is not None else None,
            "variance_amount": str(variance) if variance is not None else None,
            "notes": notes,
        })

        line_results.append({
            "line_id": line.get("id"),
            "match_status": match_status,
            "expected_amount": dec_to_float(expected_amount),
            "matched_amount": dec_to_float(matched_amount),
            "variance_amount": dec_to_float(variance),
            "matched_invoice_id": matched_invoice_id,
            "matched_invoice_number": matched_invoice_number,
            "notes": notes,
        })

    if reconciliation_lines_to_insert:
        supabase.table("reconciliation_lines").insert(reconciliation_lines_to_insert).execute()

        # Also update statement_lines.match_status for the UI
        for result in line_results:
            supabase.table("statement_lines").update({
                "match_status": result["match_status"]
            }).eq("id", str(result["line_id"])).execute()

    total_lines = len(line_results)
    matched = sum(1 for line in line_results if line["match_status"] == "matched")
    unmatched = sum(1 for line in line_results if line["match_status"] == "unmatched")
    exceptions = sum(1 for line in line_results if line["match_status"] == "exception")

    return RunReconciliationResponse(
        job_id=job_id,
        reconciliation_id=reconciliation_id,
        status="completed",
        summary={
            "total_lines": total_lines,
            "matched": matched,
            "unmatched": unmatched,
            "exceptions": exceptions,
        },
        lines=line_results,
    )