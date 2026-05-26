"""
Supplier service — business logic and data helpers extracted from app/routers/suppliers.py.

Routers should import from here rather than implementing these directly.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from app.db.supabase_client import get_supabase_client
from app.services.audit_log import log_invoice_event
from app.services.invoice_data_builders import utc_now_iso
from app.services.invoice_supplier_rules import reapply_supplier_rules_to_invoice
from app.services.supplier_matcher import attempt_supplier_auto_link, find_name_match_suggestion

try:
    supabase = get_supabase_client()
except Exception:
    supabase = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Column registry
# ---------------------------------------------------------------------------

KNOWN_SUPPLIER_COLUMNS = {
    "id",
    "organisation_id",
    "supplier_name",
    "supplier_code",
    "account_number",
    "tax_number",
    "registration_number",
    "currency",
    "default_email",
    "phone",
    "payment_terms",
    "active",
    "created_at",
    "updated_at",
    "vat_number",
    "company_registration_number",
    "payment_terms_text",
    "payment_terms_days",
    "early_settlement_discount_percent",
    "early_settlement_days",
    "bank_account_name",
    "bank_name",
    "bank_account_number",
    "bank_branch_code",
    "bank_swift_code",
    "bank_country",
    "bank_verified",
    "bank_details_last_updated_at",
    "bank_details_source",
    # Optional richer profile columns if later migrations add them.
    "delivery_address",
    "postal_address",
    "accounting_email",
    "fax",
    "cell",
    "website",
    "source_invoice_extracted_id",
    "parse_line_items",
    "line_items_include_vat",
    "track_inventory",
    "use_uom_from_description",
    "default_expense_account",
    "default_vat_rate",
}


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _first(data: Any) -> Optional[dict]:
    return data[0] if data else None


def _supplier_columns() -> set[str]:
    return KNOWN_SUPPLIER_COLUMNS


def _compact(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _filter_supplier_payload(payload: dict) -> dict:
    columns = _supplier_columns()
    return {key: value for key, value in _compact(payload).items() if key in columns}


def _supplier_processing_overrides(payload: BaseModel) -> dict:
    values = payload.model_dump()
    keys = {
        "parse_line_items",
        "line_items_include_vat",
        "track_inventory",
        "use_uom_from_description",
        "default_expense_account",
        "default_vat_rate",
    }
    return {key: values.get(key) for key in keys if values.get(key) is not None}


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def _raise_if_duplicate(
    *,
    org_id: str,
    supplier_name: Optional[str] = None,
    vat_number: Optional[str] = None,
    company_registration_number: Optional[str] = None,
    account_number: Optional[str] = None,
    bank_account_number: Optional[str] = None,
) -> None:
    existing_id = attempt_supplier_auto_link(
        supabase,
        org_id=org_id,
        vat_number_extracted=vat_number,
        company_registration_number_extracted=company_registration_number,
        cus_code_extracted=account_number,
        bank_account_number_extracted=bank_account_number,
    )
    if existing_id is None and supplier_name:
        suggestion = find_name_match_suggestion(supabase, org_id=org_id, supplier_name_extracted=supplier_name)
        if suggestion:
            existing_id = suggestion["id"]
            existing_name = suggestion["supplier_name"]
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Duplicate supplier",
                    "existing_supplier_id": existing_id,
                    "existing_supplier_name": existing_name,
                },
            )
    if existing_id:
        res = supabase.table("suppliers").select("id, supplier_name").eq("id", existing_id).limit(1).execute()
        existing = _first(res.data)
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Duplicate supplier",
                "existing_supplier_id": existing_id,
                "existing_supplier_name": existing.get("supplier_name") if existing else None,
            },
        )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_extracted_invoice(invoice_extracted_id: str) -> dict:
    res = (
        supabase
        .table("invoices_extracted")
        .select("*")
        .eq("id", invoice_extracted_id)
        .limit(1)
        .execute()
    )
    invoice = _first(res.data)
    if not invoice:
        raise HTTPException(status_code=404, detail="Extracted invoice not found")
    return invoice


def get_extracted_invoice_by_raw(invoice_raw_id: str) -> Optional[dict]:
    res = (
        supabase
        .table("invoices_extracted")
        .select("*")
        .eq("invoice_raw_id", invoice_raw_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return _first(res.data)


# ---------------------------------------------------------------------------
# Data transformers
# ---------------------------------------------------------------------------

def _build_supplier_payload_from_extracted(invoice: dict, *, supplier_name_override: Optional[str] = None) -> dict:
    now = utc_now_iso()
    default_email = (
        invoice.get("supplier_acc_email_extracted")
        or invoice.get("supplier_email_extracted")
    )
    phone = (
        invoice.get("supplier_telephone_extracted")
        or invoice.get("supplier_cell_extracted")
    )
    vat_number = invoice.get("vat_number_extracted")
    registration_number = invoice.get("company_registration_number_extracted")

    return {
        "organisation_id": invoice.get("organisation_id"),
        "supplier_name": supplier_name_override or invoice.get("supplier_name_extracted") or invoice.get("issuer_name_extracted"),
        "supplier_code": invoice.get("cus_code_extracted"),
        "account_number": invoice.get("cus_code_extracted"),
        "tax_number": vat_number,
        "vat_number": vat_number,
        "registration_number": registration_number,
        "company_registration_number": registration_number,
        "currency": invoice.get("currency") or "ZAR",
        "default_email": default_email,
        "phone": phone,
        "active": True,
        "bank_account_name": invoice.get("bank_account_name_extracted"),
        "bank_name": invoice.get("bank_name_extracted"),
        "bank_account_number": invoice.get("bank_account_number_extracted"),
        "bank_branch_code": invoice.get("bank_branch_code_extracted"),
        "bank_swift_code": invoice.get("bank_swift_code_extracted"),
        "bank_country": "ZA" if (invoice.get("currency") or "ZAR") == "ZAR" else None,
        "bank_verified": False,
        "bank_details_last_updated_at": now if invoice.get("bank_account_number_extracted") else None,
        "bank_details_source": "invoice_extraction" if invoice.get("bank_account_number_extracted") else None,
        "delivery_address": invoice.get("supplier_del_address_extracted"),
        "postal_address": invoice.get("supplier_pos_address_extracted"),
        "accounting_email": invoice.get("supplier_acc_email_extracted"),
        "fax": invoice.get("supplier_fax_extracted"),
        "cell": invoice.get("supplier_cell_extracted"),
        "website": invoice.get("supplier_website_extracted"),
        "source_invoice_extracted_id": invoice.get("id"),
        "created_at": now,
        "updated_at": now,
    }


def _extracted_supplier_profile(invoice: dict) -> dict:
    return {
        "supplier_name": invoice.get("supplier_name_extracted") or invoice.get("issuer_name_extracted"),
        "supplier_code": invoice.get("cus_code_extracted"),
        "account_number": invoice.get("cus_code_extracted"),
        "currency": invoice.get("currency"),
        "default_email": invoice.get("supplier_email_extracted"),
        "accounting_email": invoice.get("supplier_acc_email_extracted"),
        "phone": invoice.get("supplier_telephone_extracted"),
        "telephone": invoice.get("supplier_telephone_extracted"),
        "fax": invoice.get("supplier_fax_extracted"),
        "cell": invoice.get("supplier_cell_extracted"),
        "website": invoice.get("supplier_website_extracted"),
        "delivery_address": invoice.get("supplier_del_address_extracted"),
        "postal_address": invoice.get("supplier_pos_address_extracted"),
        "vat_number": invoice.get("vat_number_extracted"),
        "tax_number": invoice.get("vat_number_extracted"),
        "company_registration_number": invoice.get("company_registration_number_extracted"),
        "registration_number": invoice.get("company_registration_number_extracted"),
        "bank_account_name": invoice.get("bank_account_name_extracted"),
        "bank_name": invoice.get("bank_name_extracted"),
        "bank_account_number": invoice.get("bank_account_number_extracted"),
        "bank_branch_code": invoice.get("bank_branch_code_extracted"),
        "bank_swift_code": invoice.get("bank_swift_code_extracted"),
        "supplier_name_extracted": invoice.get("supplier_name_extracted"),
        "supplier_email_extracted": invoice.get("supplier_email_extracted"),
        "supplier_acc_email_extracted": invoice.get("supplier_acc_email_extracted"),
        "supplier_telephone_extracted": invoice.get("supplier_telephone_extracted"),
        "supplier_fax_extracted": invoice.get("supplier_fax_extracted"),
        "supplier_cell_extracted": invoice.get("supplier_cell_extracted"),
        "supplier_website_extracted": invoice.get("supplier_website_extracted"),
        "supplier_del_address_extracted": invoice.get("supplier_del_address_extracted"),
        "supplier_pos_address_extracted": invoice.get("supplier_pos_address_extracted"),
        "vat_number_extracted": invoice.get("vat_number_extracted"),
        "company_registration_number_extracted": invoice.get("company_registration_number_extracted"),
        "cus_code_extracted": invoice.get("cus_code_extracted"),
    }


# ---------------------------------------------------------------------------
# Supplier–invoice linking
# ---------------------------------------------------------------------------

def _link_supplier_to_invoice(
    *,
    supplier_id: str,
    invoice_extracted_id: Optional[str] = None,
    invoice_raw_id: Optional[str] = None,
    organisation_id: Optional[str] = None,
) -> dict:
    linked: dict[str, Any] = {
        "supplier_id": supplier_id,
        "invoice_extracted_id": invoice_extracted_id,
        "invoice_raw_id": invoice_raw_id,
    }

    invoice: Optional[dict] = None
    if invoice_extracted_id:
        invoice = get_extracted_invoice(invoice_extracted_id)
        invoice_raw_id = invoice_raw_id or invoice.get("invoice_raw_id")
        organisation_id = organisation_id or invoice.get("organisation_id")
        supabase.table("invoices_extracted").update({
            "supplier_id": supplier_id,
            "updated_at": utc_now_iso(),
        }).eq("id", invoice_extracted_id).execute()
        linked["invoice_raw_id"] = invoice_raw_id
    elif invoice_raw_id:
        invoice = get_extracted_invoice_by_raw(invoice_raw_id)
        if invoice:
            invoice_extracted_id = invoice.get("id")
            organisation_id = organisation_id or invoice.get("organisation_id")
            linked["invoice_extracted_id"] = invoice_extracted_id
            supabase.table("invoices_extracted").update({
                "supplier_id": supplier_id,
                "updated_at": utc_now_iso(),
            }).eq("id", invoice_extracted_id).execute()

    if invoice_raw_id:
        try:
            supabase.table("invoices_raw").update({
                "supplier_id": supplier_id,
                "updated_at": utc_now_iso(),
            }).eq("id", invoice_raw_id).execute()
        except Exception as exc:
            print("INVOICES_RAW SUPPLIER LINK FAILED:", str(exc))

    if organisation_id:
        log_invoice_event(
            supabase,
            organisation_id=organisation_id,
            invoice_raw_id=invoice_raw_id,
            invoice_extracted_id=invoice_extracted_id,
            event_type="supplier_linked",
            stage="supplier_master",
            actor_type="api",
            new_value={"supplier_id": supplier_id},
            notes="Supplier master record linked to extracted invoice.",
        )

    if invoice:
        try:
            linked["rules_applied"] = reapply_supplier_rules_to_invoice(
                supabase,
                invoice={**invoice, "supplier_id": supplier_id},
                supplier_id=supplier_id,
                actor_type="api",
                event_reason="Supplier rules applied after supplier link/create.",
            )
        except Exception as exc:
            linked["rules_apply_error"] = str(exc)

    return linked
