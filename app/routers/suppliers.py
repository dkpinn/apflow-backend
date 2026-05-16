from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db.supabase_client import get_supabase_client
from app.services.audit_log import log_invoice_event

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])
supabase = get_supabase_client()


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
}


class SupplierCreateRequest(BaseModel):
    organisation_id: str
    supplier_name: str
    supplier_code: Optional[str] = None
    account_number: Optional[str] = None
    tax_number: Optional[str] = None
    registration_number: Optional[str] = None
    currency: Optional[str] = None
    default_email: Optional[str] = None
    phone: Optional[str] = None
    vat_number: Optional[str] = None
    company_registration_number: Optional[str] = None
    bank_account_name: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_branch_code: Optional[str] = None
    bank_swift_code: Optional[str] = None
    bank_country: Optional[str] = None
    delivery_address: Optional[str] = None
    postal_address: Optional[str] = None
    accounting_email: Optional[str] = None
    fax: Optional[str] = None
    cell: Optional[str] = None
    website: Optional[str] = None
    invoice_extracted_id: Optional[str] = None
    invoice_raw_id: Optional[str] = None
    link_invoice: bool = True


class SupplierFromInvoiceRequest(BaseModel):
    invoice_extracted_id: str
    organisation_id: Optional[str] = None
    supplier_name: Optional[str] = None
    link_invoice: bool = True


class SupplierLinkRequest(BaseModel):
    supplier_id: str
    invoice_extracted_id: Optional[str] = None
    invoice_raw_id: Optional[str] = None
    organisation_id: Optional[str] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first(data: Any) -> Optional[dict]:
    return data[0] if data else None


def _supplier_columns() -> set[str]:
    return KNOWN_SUPPLIER_COLUMNS


def _compact(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _filter_supplier_payload(payload: dict) -> dict:
    columns = _supplier_columns()
    return {key: value for key, value in _compact(payload).items() if key in columns}


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

    return linked


@router.get("")
def list_suppliers(
    organisation_id: str = Query(...),
    search: Optional[str] = None,
    limit: int = Query(default=25, ge=1, le=100),
):
    query = (
        supabase
        .table("suppliers")
        .select("*")
        .eq("organisation_id", organisation_id)
        .limit(limit)
    )

    if search:
        query = query.ilike("supplier_name", f"%{search}%")

    res = query.execute()
    return {"success": True, "suppliers": res.data or []}


@router.get("/from-invoice/{invoice_extracted_id}")
def get_supplier_profile_from_invoice(invoice_extracted_id: str):
    invoice = get_extracted_invoice(invoice_extracted_id)
    return {
        "success": True,
        "invoice_extracted_id": invoice_extracted_id,
        "invoice_raw_id": invoice.get("invoice_raw_id"),
        "organisation_id": invoice.get("organisation_id"),
        "extracted_profile": _extracted_supplier_profile(invoice),
        "savable_supplier": _filter_supplier_payload(_build_supplier_payload_from_extracted(invoice)),
        "unsupported_extracted_fields": {
            key: value
            for key, value in _compact(_extracted_supplier_profile(invoice)).items()
            if key not in _supplier_columns()
        },
    }


@router.post("")
@router.post("/new")
def create_supplier(payload: SupplierCreateRequest):
    insert_payload = _filter_supplier_payload({
        **payload.model_dump(exclude={"invoice_extracted_id", "invoice_raw_id", "link_invoice"}),
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "active": True,
    })

    if not insert_payload.get("organisation_id"):
        raise HTTPException(status_code=400, detail="Missing organisation_id")
    if not insert_payload.get("supplier_name"):
        raise HTTPException(status_code=400, detail="Missing supplier_name")

    res = supabase.table("suppliers").insert(insert_payload).execute()
    supplier = _first(res.data)
    if not supplier:
        raise HTTPException(status_code=400, detail="Supplier create failed")

    linked = None
    if payload.link_invoice and (payload.invoice_extracted_id or payload.invoice_raw_id):
        linked = _link_supplier_to_invoice(
            supplier_id=supplier["id"],
            invoice_extracted_id=payload.invoice_extracted_id,
            invoice_raw_id=payload.invoice_raw_id,
            organisation_id=payload.organisation_id,
        )

    return {"success": True, "supplier": supplier, "linked": linked}


@router.post("/from-invoice")
@router.post("/create-from-invoice")
def create_supplier_from_invoice(payload: SupplierFromInvoiceRequest):
    invoice = get_extracted_invoice(payload.invoice_extracted_id)
    if payload.organisation_id and payload.organisation_id != invoice.get("organisation_id"):
        raise HTTPException(status_code=400, detail="Invoice does not belong to organisation_id")

    insert_payload = _filter_supplier_payload(
        _build_supplier_payload_from_extracted(invoice, supplier_name_override=payload.supplier_name)
    )

    if not insert_payload.get("supplier_name"):
        raise HTTPException(status_code=400, detail="No supplier name was extracted")

    res = supabase.table("suppliers").insert(insert_payload).execute()
    supplier = _first(res.data)
    if not supplier:
        raise HTTPException(status_code=400, detail="Supplier create failed")

    linked = None
    if payload.link_invoice:
        linked = _link_supplier_to_invoice(
            supplier_id=supplier["id"],
            invoice_extracted_id=payload.invoice_extracted_id,
            invoice_raw_id=invoice.get("invoice_raw_id"),
            organisation_id=invoice.get("organisation_id"),
        )

    log_invoice_event(
        supabase,
        organisation_id=invoice["organisation_id"],
        invoice_raw_id=invoice.get("invoice_raw_id"),
        invoice_extracted_id=invoice.get("id"),
        event_type="supplier_created_from_invoice",
        stage="supplier_master",
        actor_type="api",
        new_value={
            "supplier_id": supplier.get("id"),
            "supplier_name": supplier.get("supplier_name"),
            "fields_saved": sorted(insert_payload.keys()),
            "extracted_profile": _extracted_supplier_profile(invoice),
        },
        notes="Supplier master record created from extracted invoice fields.",
    )

    return {
        "success": True,
        "supplier": supplier,
        "linked": linked,
        "extracted_profile": _extracted_supplier_profile(invoice),
    }


@router.post("/link")
@router.post("/link-invoice")
def link_supplier(payload: SupplierLinkRequest):
    if not payload.invoice_extracted_id and not payload.invoice_raw_id:
        raise HTTPException(status_code=400, detail="Provide invoice_extracted_id or invoice_raw_id")

    linked = _link_supplier_to_invoice(
        supplier_id=payload.supplier_id,
        invoice_extracted_id=payload.invoice_extracted_id,
        invoice_raw_id=payload.invoice_raw_id,
        organisation_id=payload.organisation_id,
    )
    return {"success": True, "linked": linked}
