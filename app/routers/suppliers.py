from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db.supabase_client import get_supabase_client
from app.services.audit_log import log_invoice_event
from app.services.invoice_data_builders import utc_now_iso
from app.services.supplier_service import (
    KNOWN_SUPPLIER_COLUMNS,
    _build_supplier_payload_from_extracted,
    _compact,
    _extracted_supplier_profile,
    _filter_supplier_payload,
    _link_supplier_to_invoice,
    _raise_if_duplicate,
    _supplier_columns,
    _supplier_processing_overrides,
    get_extracted_invoice,
    get_extracted_invoice_by_raw,
)

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])
supabase = get_supabase_client()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

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
    parse_line_items: Optional[bool] = None
    line_items_include_vat: Optional[bool] = None
    track_inventory: Optional[bool] = None
    use_uom_from_description: Optional[bool] = None
    default_expense_account: Optional[str] = None
    default_vat_rate: Optional[float] = None
    invoice_extracted_id: Optional[str] = None
    invoice_raw_id: Optional[str] = None
    link_invoice: bool = True


class SupplierFromInvoiceRequest(BaseModel):
    invoice_extracted_id: str
    organisation_id: Optional[str] = None
    supplier_name: Optional[str] = None
    parse_line_items: Optional[bool] = None
    line_items_include_vat: Optional[bool] = None
    track_inventory: Optional[bool] = None
    use_uom_from_description: Optional[bool] = None
    default_expense_account: Optional[str] = None
    default_vat_rate: Optional[float] = None
    link_invoice: bool = True


class SupplierLinkRequest(BaseModel):
    supplier_id: str
    invoice_extracted_id: Optional[str] = None
    invoice_raw_id: Optional[str] = None
    organisation_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

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


@router.get("/match-suggest")
def suggest_supplier_match(invoice_extracted_id: str):
    """Return best fuzzy name match suggestion for an unlinked invoice."""
    inv_res = (
        supabase.table("invoices_extracted")
        .select("organisation_id, supplier_name_extracted, supplier_id")
        .eq("id", invoice_extracted_id)
        .limit(1)
        .execute()
    )
    if not inv_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    inv = inv_res.data[0]
    if inv.get("supplier_id"):
        return {"suggestion": None}

    from app.services.supplier_matcher import find_name_match_suggestion
    suggestion = find_name_match_suggestion(
        supabase,
        org_id=inv["organisation_id"],
        supplier_name_extracted=inv.get("supplier_name_extracted"),
    )
    return {"suggestion": suggestion}


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

    _raise_if_duplicate(
        org_id=insert_payload["organisation_id"],
        supplier_name=insert_payload.get("supplier_name"),
        vat_number=insert_payload.get("vat_number"),
        company_registration_number=insert_payload.get("company_registration_number"),
        account_number=insert_payload.get("account_number"),
        bank_account_number=insert_payload.get("bank_account_number"),
    )

    res = supabase.table("suppliers").insert(insert_payload).execute()
    supplier = res.data[0] if res.data else None
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
        {
            **_build_supplier_payload_from_extracted(invoice, supplier_name_override=payload.supplier_name),
            **_supplier_processing_overrides(payload),
        }
    )

    if not insert_payload.get("supplier_name"):
        raise HTTPException(status_code=400, detail="No supplier name was extracted")

    _raise_if_duplicate(
        org_id=insert_payload["organisation_id"],
        supplier_name=insert_payload.get("supplier_name"),
        vat_number=insert_payload.get("vat_number"),
        company_registration_number=insert_payload.get("company_registration_number"),
        account_number=insert_payload.get("account_number"),
        bank_account_number=insert_payload.get("bank_account_number"),
    )

    res = supabase.table("suppliers").insert(insert_payload).execute()
    supplier = res.data[0] if res.data else None
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
