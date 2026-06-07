from __future__ import annotations

from typing import Any

from app.services.invoice_gl_posting import (
    has_duplicate_invoice_reference,
    persist_prepared_invoice_posting,
    prepare_invoice_gl_posting,
)


def _not_eligible(reason: str) -> dict:
    return {"status": "not_eligible", "reason": reason}


def attempt_invoice_stp(
    supabase,
    *,
    invoice_id: str,
    org_id: str,
    readiness_result: dict | None,
) -> dict[str, Any]:
    if not readiness_result or not readiness_result.get("ready"):
        return _not_eligible("invoice_not_ready")

    try:
        invoice_result = (
            supabase.table("invoices_extracted")
            .select("id, organisation_id, supplier_id, invoice_number, posting_status")
            .eq("id", invoice_id)
            .eq("organisation_id", org_id)
            .limit(1)
            .execute()
        )
        if not invoice_result.data:
            return _not_eligible("invoice_not_found")
        invoice = invoice_result.data[0]
        if invoice.get("posting_status") == "posted":
            return _not_eligible("already_posted")
        if has_duplicate_invoice_reference(
            supabase,
            invoice_id=invoice_id,
            organisation_id=org_id,
            supplier_id=invoice.get("supplier_id"),
            invoice_number=invoice.get("invoice_number"),
        ):
            return _not_eligible("duplicate_invoice")

        supplier_id = invoice.get("supplier_id")
        if not supplier_id:
            return _not_eligible("supplier_not_linked")

        supplier_result = (
            supabase.table("suppliers")
            .select("id, organisation_id, active, stp_enabled, stp_max_amount")
            .eq("id", supplier_id)
            .eq("organisation_id", org_id)
            .limit(1)
            .execute()
        )
        if not supplier_result.data:
            return _not_eligible("supplier_not_trusted")
        supplier = supplier_result.data[0]
        if supplier.get("active") is False:
            return _not_eligible("supplier_inactive")
        if not supplier.get("stp_enabled"):
            return _not_eligible("stp_disabled")

        prepared = prepare_invoice_gl_posting(
            supabase,
            invoice_id=invoice_id,
            org_id=org_id,
        )
        maximum = supplier.get("stp_max_amount")
        if maximum is not None and prepared["gross_total"] > float(maximum):
            return _not_eligible("amount_exceeds_supplier_limit")

        posted = persist_prepared_invoice_posting(
            supabase,
            prepared=prepared,
            user_id=None,
        )
        return {
            "status": "posted",
            "supplier_id": str(supplier_id),
            **posted,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason": str(exc),
        }
