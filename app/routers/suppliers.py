from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
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
# Cross-entity org resolution (used for auth validation in branch endpoints)
# ---------------------------------------------------------------------------

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


class SupplierBranchCreateRequest(BaseModel):
    organisation_id: str
    supplier_id: str
    branch_name: str
    branch_code: Optional[str] = None
    vat_number: Optional[str] = None
    tax_number: Optional[str] = None
    company_registration_number: Optional[str] = None
    phone: Optional[str] = None
    default_email: Optional[str] = None
    website: Optional[str] = None
    delivery_address: Optional[str] = None
    postal_address: Optional[str] = None
    bank_account_name: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_branch_code: Optional[str] = None
    bank_swift_code: Optional[str] = None
    invoice_extracted_id: Optional[str] = None
    link_invoice: bool = True


class SupplierBranchFromInvoiceRequest(BaseModel):
    invoice_extracted_id: str
    supplier_id: str
    branch_name: Optional[str] = None
    link_invoice: bool = True


class SupplierBranchLinkRequest(BaseModel):
    invoice_extracted_id: str
    supplier_branch_id: str
    supplier_id: Optional[str] = None
    organisation_id: Optional[str] = None


class SupplierBranchUnlinkRequest(BaseModel):
    invoice_extracted_id: str
    supplier_id: Optional[str] = None


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


@router.get("/{supplier_id}/branches")
def list_supplier_branches(
    supplier_id: str,
    organisation_id: str = Query(...),
    active_only: bool = True,
    auth: UserAuth = ...,
):
    user_id, _db = auth
    # Verify the supplier actually belongs to the requested org (prevents org hopping)
    supplier_org = _org_for_supplier(supplier_id)
    if supplier_org and supplier_org != organisation_id:
        raise HTTPException(status_code=404, detail="Supplier not found")
    ensure_org_read(user_id, organisation_id)

    query = (
        supabase
        .table("supplier_branches")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("supplier_id", supplier_id)
        .order("branch_name", desc=False)
    )
    if active_only:
        query = query.eq("active", True)
    res = query.execute()
    return {"success": True, "branches": res.data or []}


def _normalise_branch_payload(payload: dict) -> dict:
    allowed = {
        "organisation_id",
        "supplier_id",
        "branch_name",
        "branch_code",
        "vat_number",
        "tax_number",
        "company_registration_number",
        "phone",
        "default_email",
        "website",
        "delivery_address",
        "postal_address",
        "bank_account_name",
        "bank_name",
        "bank_account_number",
        "bank_branch_code",
        "bank_swift_code",
        "source_invoice_extracted_id",
        "active",
    }
    return {
        key: value
        for key, value in payload.items()
        if key in allowed and value not in ("", None)
    }


def _supplier_for_branch_name(supplier_id: str | None) -> dict | None:
    if not supplier_id:
        return None
    try:
        res = (
            supabase
            .table("suppliers")
            .select("id, supplier_name, name, trading_name")
            .eq("id", supplier_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception:
        return None


def _tidy_branch_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9 &().,-]+", " ", str(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -,.")
    if not cleaned:
        return None
    return cleaned.title()


def _infer_branch_name_from_invoice(invoice: dict, supplier: dict | None) -> str:
    explicit_name = _tidy_branch_name(
        invoice.get("supplier_name_extracted")
        or invoice.get("issuer_name_extracted")
    )
    supplier_name = _tidy_branch_name(
        (supplier or {}).get("supplier_name")
        or (supplier or {}).get("name")
        or (supplier or {}).get("trading_name")
    )

    if explicit_name and supplier_name:
        supplier_words = {
            word
            for word in re.findall(r"[A-Za-z0-9]+", supplier_name.lower())
            if len(word) > 1
        }
        invoice_words = re.findall(r"[A-Za-z0-9]+", explicit_name)
        branch_words = [
            word for word in invoice_words
            if word.lower() not in supplier_words
        ]
        branch_name = _tidy_branch_name(" ".join(branch_words))
        if branch_name:
            return branch_name

    address = invoice.get("supplier_del_address_extracted") or ""
    for pattern in [r"\b(Pinetown)\b", r"\b(Durban)\b", r"\b(Johannesburg)\b", r"\b(Cape\s+Town)\b"]:
        match = re.search(pattern, address, re.IGNORECASE)
        if match:
            return _tidy_branch_name(match.group(1)) or "Branch"

    return explicit_name or "Branch"


def _link_branch_to_invoice(*, invoice_extracted_id: str, supplier_branch_id: str, supplier_id: str | None = None) -> dict:
    branch_res = (
        supabase
        .table("supplier_branches")
        .select("*")
        .eq("id", supplier_branch_id)
        .limit(1)
        .execute()
    )
    branch = branch_res.data[0] if branch_res.data else None
    if not branch:
        raise HTTPException(status_code=404, detail="Supplier branch not found")
    patch = {
        "supplier_branch_id": supplier_branch_id,
        "supplier_id": supplier_id or branch.get("supplier_id"),
    }
    invoice_res = (
        supabase
        .table("invoices_extracted")
        .update(patch)
        .eq("id", invoice_extracted_id)
        .execute()
    )
    linked = invoice_res.data[0] if invoice_res.data else patch
    log_invoice_event(
        supabase,
        organisation_id=branch.get("organisation_id"),
        invoice_extracted_id=invoice_extracted_id,
        event_type="supplier_branch_linked",
        stage="supplier_master",
        actor_type="api",
        new_value={
            "supplier_id": patch.get("supplier_id"),
            "supplier_branch_id": supplier_branch_id,
            "branch_name": branch.get("branch_name"),
        },
        notes="Supplier branch linked to extracted invoice.",
    )
    return linked


@router.post("/branches")
def create_supplier_branch(payload: SupplierBranchCreateRequest, auth: UserAuth):
    user_id, _db = auth
    # Verify the supplier belongs to the declared org
    supplier_org = _org_for_supplier(payload.supplier_id)
    if supplier_org and supplier_org != payload.organisation_id:
        raise HTTPException(status_code=400, detail="Supplier does not belong to the specified organisation")
    ensure_org_write(user_id, payload.organisation_id)

    insert_payload = _normalise_branch_payload({
        **payload.model_dump(exclude={"invoice_extracted_id", "link_invoice"}),
        "source_invoice_extracted_id": payload.invoice_extracted_id,
        "active": True,
    })
    if not insert_payload.get("organisation_id"):
        raise HTTPException(status_code=400, detail="Missing organisation_id")
    if not insert_payload.get("supplier_id"):
        raise HTTPException(status_code=400, detail="Missing supplier_id")
    if not insert_payload.get("branch_name"):
        raise HTTPException(status_code=400, detail="Missing branch_name")

    res = supabase.table("supplier_branches").insert(insert_payload).execute()
    branch = res.data[0] if res.data else None
    if not branch:
        raise HTTPException(status_code=400, detail="Supplier branch create failed")

    linked = None
    if payload.link_invoice and payload.invoice_extracted_id:
        linked = _link_branch_to_invoice(
            invoice_extracted_id=payload.invoice_extracted_id,
            supplier_branch_id=branch["id"],
            supplier_id=branch.get("supplier_id"),
        )
    return {"success": True, "branch": branch, "linked": linked}


@router.post("/branches/from-invoice")
def create_supplier_branch_from_invoice(payload: SupplierBranchFromInvoiceRequest, auth: UserAuth):
    user_id, _db = auth
    # Derive org from invoice (authoritative source of truth)
    invoice_org = _org_for_invoice_extracted(payload.invoice_extracted_id)
    if not invoice_org:
        raise HTTPException(status_code=404, detail="Invoice not found")
    # Verify supplier belongs to the same org
    supplier_org = _org_for_supplier(payload.supplier_id)
    if supplier_org and supplier_org != invoice_org:
        raise HTTPException(status_code=400, detail="Supplier and invoice belong to different organisations")
    ensure_org_write(user_id, invoice_org)

    invoice = get_extracted_invoice(payload.invoice_extracted_id)
    if invoice.get("supplier_id") and invoice.get("supplier_id") != payload.supplier_id:
        raise HTTPException(status_code=400, detail="Invoice is linked to a different supplier")
    supplier = _supplier_for_branch_name(payload.supplier_id)
    branch_name = (
        payload.branch_name
        or _infer_branch_name_from_invoice(invoice, supplier)
    )
    insert_payload = _normalise_branch_payload({
        "organisation_id": invoice.get("organisation_id"),
        "supplier_id": payload.supplier_id,
        "branch_name": branch_name,
        "branch_code": invoice.get("cus_code_extracted"),
        "vat_number": invoice.get("vat_number_extracted"),
        "tax_number": invoice.get("vat_number_extracted"),
        "company_registration_number": invoice.get("company_registration_number_extracted"),
        "phone": invoice.get("supplier_telephone_extracted"),
        "default_email": invoice.get("supplier_email_extracted"),
        "website": invoice.get("supplier_website_extracted"),
        "delivery_address": invoice.get("supplier_del_address_extracted"),
        "postal_address": invoice.get("supplier_pos_address_extracted"),
        "bank_account_name": invoice.get("bank_account_name_extracted"),
        "bank_name": invoice.get("bank_name_extracted"),
        "bank_account_number": invoice.get("bank_account_number_extracted"),
        "bank_branch_code": invoice.get("bank_branch_code_extracted"),
        "bank_swift_code": invoice.get("bank_swift_code_extracted"),
        "source_invoice_extracted_id": invoice.get("id"),
        "active": True,
    })
    if not insert_payload.get("organisation_id"):
        raise HTTPException(status_code=400, detail="Missing organisation_id")
    if not insert_payload.get("branch_name"):
        raise HTTPException(status_code=400, detail="No branch name could be inferred")

    res = supabase.table("supplier_branches").insert(insert_payload).execute()
    branch = res.data[0] if res.data else None
    if not branch:
        raise HTTPException(status_code=400, detail="Supplier branch create failed")

    linked = None
    if payload.link_invoice:
        linked = _link_branch_to_invoice(
            invoice_extracted_id=payload.invoice_extracted_id,
            supplier_branch_id=branch["id"],
            supplier_id=payload.supplier_id,
        )

    log_invoice_event(
        supabase,
        organisation_id=invoice.get("organisation_id"),
        invoice_raw_id=invoice.get("invoice_raw_id"),
        invoice_extracted_id=invoice.get("id"),
        event_type="supplier_branch_created_from_invoice",
        stage="supplier_master",
        actor_type="api",
        new_value={"supplier_branch_id": branch.get("id"), "branch_name": branch.get("branch_name")},
        notes="Supplier branch record created from extracted invoice fields.",
    )
    return {"success": True, "branch": branch, "linked": linked}


@router.post("/branches/link")
def link_supplier_branch(payload: SupplierBranchLinkRequest, auth: UserAuth):
    user_id, _db = auth
    # Resolve org from invoice (authoritative)
    invoice_org = _org_for_invoice_extracted(payload.invoice_extracted_id)
    if not invoice_org:
        raise HTTPException(status_code=404, detail="Invoice not found")
    # Verify the branch belongs to the same org
    branch_org = _org_for_branch(payload.supplier_branch_id)
    if branch_org and branch_org != invoice_org:
        raise HTTPException(status_code=400, detail="Branch and invoice belong to different organisations")
    ensure_org_write(user_id, invoice_org)

    linked = _link_branch_to_invoice(
        invoice_extracted_id=payload.invoice_extracted_id,
        supplier_branch_id=payload.supplier_branch_id,
        supplier_id=payload.supplier_id,
    )
    return {"success": True, "linked": linked}


@router.post("/branches/unlink")
def unlink_supplier_branch(payload: SupplierBranchUnlinkRequest, auth: UserAuth):
    user_id, _db = auth
    invoice_org = _org_for_invoice_extracted(payload.invoice_extracted_id)
    if not invoice_org:
        raise HTTPException(status_code=404, detail="Invoice not found")
    ensure_org_write(user_id, invoice_org)

    invoice = get_extracted_invoice(payload.invoice_extracted_id)
    if payload.supplier_id and invoice.get("supplier_id") and invoice.get("supplier_id") != payload.supplier_id:
        raise HTTPException(status_code=400, detail="Invoice is linked to a different supplier")

    patch = {"supplier_branch_id": None}
    res = (
        supabase
        .table("invoices_extracted")
        .update(patch)
        .eq("id", payload.invoice_extracted_id)
        .execute()
    )
    linked = res.data[0] if res.data else patch
    log_invoice_event(
        supabase,
        organisation_id=invoice.get("organisation_id"),
        invoice_raw_id=invoice.get("invoice_raw_id"),
        invoice_extracted_id=invoice.get("id"),
        event_type="supplier_branch_unlinked",
        stage="supplier_master",
        actor_type="api",
        old_value={"supplier_branch_id": invoice.get("supplier_branch_id")},
        new_value={"supplier_branch_id": None},
        notes="Supplier branch unlinked from extracted invoice.",
    )
    return {"success": True, "linked": linked}


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
