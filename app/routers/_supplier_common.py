from __future__ import annotations

from typing import Optional

from app.db.supabase_client import get_supabase_client

supabase = get_supabase_client()


def _org_for_supplier(supplier_id: str) -> Optional[str]:
    """Return the organisation_id that owns this supplier, or None."""
    try:
        res = (
            supabase.table("suppliers")
            .select("organisation_id")
            .eq("id", supplier_id)
            .limit(1)
            .execute()
        )
        return res.data[0]["organisation_id"] if res.data else None
    except Exception:
        return None


def _org_for_branch(branch_id: str) -> Optional[str]:
    """Return the organisation_id that owns this supplier branch, or None."""
    try:
        res = (
            supabase.table("supplier_branches")
            .select("organisation_id")
            .eq("id", branch_id)
            .limit(1)
            .execute()
        )
        return res.data[0]["organisation_id"] if res.data else None
    except Exception:
        return None


def _org_for_invoice_extracted(invoice_extracted_id: str) -> Optional[str]:
    """Return the organisation_id of the extracted invoice, or None."""
    try:
        res = (
            supabase.table("invoices_extracted")
            .select("organisation_id")
            .eq("id", invoice_extracted_id)
            .limit(1)
            .execute()
        )
        return res.data[0]["organisation_id"] if res.data else None
    except Exception:
        return None


def _org_for_allocation_rule(rule_id: str) -> Optional[str]:
    try:
        res = (
            supabase.table("supplier_line_item_allocation_rules")
            .select("organisation_id")
            .eq("id", rule_id)
            .limit(1)
            .execute()
        )
        return res.data[0]["organisation_id"] if res.data else None
    except Exception:
        return None


def _kyc_request(request_id: str) -> Optional[dict]:
    try:
        res = (
            supabase.table("supplier_kyc_requests")
            .select("*")
            .eq("id", request_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception:
        return None


def _kyc_document(document_id: str) -> Optional[dict]:
    try:
        res = (
            supabase.table("supplier_kyc_documents")
            .select("*")
            .eq("id", document_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception:
        return None

