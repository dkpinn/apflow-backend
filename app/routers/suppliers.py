from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_org_admin, ensure_org_read, ensure_org_write
from app.routers._supplier_common import (
    _kyc_document,
    _kyc_request,
    _org_for_allocation_rule,
    _org_for_branch,
    _org_for_invoice_extracted,
    _org_for_supplier,
)
from app.schemas.suppliers import (
    SupplierAllocationRuleRequest,
    SupplierAllocationRuleSplitRequest,
    SupplierAllocationRuleUpdateRequest,
    SupplierAllocationRulesFromInvoiceRequest,
    SupplierAllocationSettingsRequest,
    SupplierAllocationSettingsResponse,
    SupplierBranchCreateRequest,
    SupplierBranchFromInvoiceRequest,
    SupplierBranchLinkRequest,
    SupplierBranchUnlinkRequest,
    SupplierCreateRequest,
    SupplierFromInvoiceRequest,
    SupplierKycDocumentCreate,
    SupplierKycRequestCreate,
    SupplierKycRequestUpdate,
    SupplierLinkRequest,
    SupplierMatchProfileRequest,
    SupplierStpSettingsRequest,
    SupplierStpSettingsResponse,
)
from app.services.audit_log import log_invoice_event
from app.services.invoice_data_builders import utc_now_iso
from app.services.invoice_supplier_rules import fetch_supplier_allocation_rules
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


KYC_TRIGGER_TYPES = {"new_supplier", "bank_change", "info_change", "periodic_review", "other"}
KYC_REQUEST_STATUSES = {"draft", "submitted", "approved", "rejected", "cancelled"}
KYC_DOCUMENT_TYPES = {
    "id_document",
    "company_registration",
    "bank_confirmation",
    "vat_certificate",
    "tax_clearance",
    "proof_of_address",
    "other",
}


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
        .select(
            "organisation_id, supplier_name_extracted, supplier_id, vat_number_extracted, "
            "company_registration_number_extracted, cus_code_extracted, bank_account_number_extracted, "
            "supplier_telephone_extracted, supplier_cell_extracted, supplier_email_extracted, "
            "supplier_acc_email_extracted, total_amount"
        )
        .eq("id", invoice_extracted_id)
        .limit(1)
        .execute()
    )
    if not inv_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    inv = inv_res.data[0]
    if inv.get("supplier_id"):
        return {"suggestion": None}

    from app.services.supplier_matcher import find_supplier_match_result
    suggestion = find_supplier_match_result(
        supabase,
        org_id=inv["organisation_id"],
        invoice_total=inv.get("total_amount"),
        supplier_name_extracted=inv.get("supplier_name_extracted"),
        vat_number_extracted=inv.get("vat_number_extracted"),
        company_registration_number_extracted=inv.get("company_registration_number_extracted"),
        cus_code_extracted=inv.get("cus_code_extracted"),
        bank_account_number_extracted=inv.get("bank_account_number_extracted"),
        supplier_telephone_extracted=inv.get("supplier_telephone_extracted") or inv.get("supplier_cell_extracted"),
        supplier_email_extracted=inv.get("supplier_email_extracted"),
        supplier_acc_email_extracted=inv.get("supplier_acc_email_extracted"),
    )
    return {"suggestion": suggestion}


@router.post("/match-profile")
def match_supplier_profile(payload: SupplierMatchProfileRequest):
    """Return the best supplier match for extracted supplier identity fields."""
    from app.services.supplier_matcher import find_supplier_match_result

    suggestion = find_supplier_match_result(
        supabase,
        org_id=payload.organisation_id,
        invoice_total=payload.invoice_total,
        supplier_name_extracted=payload.supplier_name,
        vat_number_extracted=payload.vat_number,
        company_registration_number_extracted=payload.company_registration_number,
        cus_code_extracted=payload.account_number,
        bank_account_number_extracted=payload.bank_account_number,
        supplier_telephone_extracted=payload.phone,
        supplier_email_extracted=payload.default_email,
        supplier_acc_email_extracted=payload.accounting_email,
    )
    return {"suggestion": suggestion}


@router.patch(
    "/{supplier_id}/stp-settings",
    response_model=SupplierStpSettingsResponse,
)
def update_supplier_stp_settings(
    supplier_id: str,
    payload: SupplierStpSettingsRequest,
    auth: UserAuth,
):
    user_id, db = auth
    ensure_org_admin(user_id, payload.organisation_id)

    supplier_result = (
        db.table("suppliers")
        .select("id, organisation_id")
        .eq("id", supplier_id)
        .eq("organisation_id", payload.organisation_id)
        .limit(1)
        .execute()
    )
    if not supplier_result.data:
        raise HTTPException(status_code=404, detail="Supplier not found")

    update_result = (
        db.table("suppliers")
        .update({
            "stp_enabled": payload.stp_enabled,
            "stp_max_amount": payload.stp_max_amount,
            "updated_at": utc_now_iso(),
        })
        .eq("id", supplier_id)
        .eq("organisation_id", payload.organisation_id)
        .execute()
    )
    saved = update_result.data[0] if update_result.data else {
        "id": supplier_id,
        "organisation_id": payload.organisation_id,
        "stp_enabled": payload.stp_enabled,
        "stp_max_amount": payload.stp_max_amount,
    }
    return {
        "supplier_id": str(saved.get("id") or supplier_id),
        "organisation_id": str(saved.get("organisation_id") or payload.organisation_id),
        "stp_enabled": bool(saved.get("stp_enabled")),
        "stp_max_amount": saved.get("stp_max_amount"),
    }


def _validated_default_tracking(
    db,
    *,
    organisation_id: str,
    tracking: dict[str, str],
) -> dict[str, str]:
    normalised = {
        str(dimension_id): str(value_id)
        for dimension_id, value_id in (tracking or {}).items()
        if dimension_id and value_id
    }
    if not normalised:
        return {}

    dimension_ids = list(normalised)
    dimensions = (
        db.table("tracking_dimensions")
        .select("id")
        .eq("organisation_id", organisation_id)
        .eq("active", True)
        .in_("id", dimension_ids)
        .execute()
        .data
        or []
    )
    valid_dimension_ids = {str(row.get("id")) for row in dimensions}
    if valid_dimension_ids != set(dimension_ids):
        raise HTTPException(
            status_code=422,
            detail="Every default tracking dimension must be active and belong to this organisation",
        )

    values = (
        db.table("tracking_values")
        .select("id, dimension_id")
        .eq("active", True)
        .in_("id", list(normalised.values()))
        .execute()
        .data
        or []
    )
    value_dimensions = {
        str(row.get("id")): str(row.get("dimension_id"))
        for row in values
    }
    if any(value_dimensions.get(value_id) != dimension_id for dimension_id, value_id in normalised.items()):
        raise HTTPException(
            status_code=422,
            detail="Every default tracking value must be active and belong to its selected dimension",
        )
    return normalised


@router.get(
    "/{supplier_id}/allocation-settings",
    response_model=SupplierAllocationSettingsResponse,
)
def get_supplier_allocation_settings(
    supplier_id: str,
    organisation_id: str = Query(...),
    auth: UserAuth = ...,
):
    user_id, db = auth
    ensure_org_read(user_id, organisation_id)
    result = (
        db.table("suppliers")
        .select("id, organisation_id, default_expense_account, default_tracking")
        .eq("id", supplier_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Supplier not found")
    supplier = result.data[0]
    return {
        "supplier_id": str(supplier["id"]),
        "organisation_id": str(supplier["organisation_id"]),
        "default_expense_account": supplier.get("default_expense_account"),
        "default_tracking": supplier.get("default_tracking") or {},
    }


@router.patch(
    "/{supplier_id}/allocation-settings",
    response_model=SupplierAllocationSettingsResponse,
)
def update_supplier_allocation_settings(
    supplier_id: str,
    payload: SupplierAllocationSettingsRequest,
    auth: UserAuth,
):
    user_id, db = auth
    ensure_org_write(user_id, payload.organisation_id)
    existing = (
        db.table("suppliers")
        .select("id, organisation_id, default_expense_account, default_tracking")
        .eq("id", supplier_id)
        .eq("organisation_id", payload.organisation_id)
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Supplier not found")

    current = existing.data[0]
    updates = {"updated_at": utc_now_iso()}
    if "default_expense_account" in payload.model_fields_set:
        updates["default_expense_account"] = payload.default_expense_account
    if "default_tracking" in payload.model_fields_set:
        updates["default_tracking"] = _validated_default_tracking(
            db,
            organisation_id=payload.organisation_id,
            tracking=payload.default_tracking or {},
        )
    if len(updates) == 1:
        raise HTTPException(status_code=400, detail="No allocation settings were provided to update")

    result = (
        db.table("suppliers")
        .update(updates)
        .eq("id", supplier_id)
        .eq("organisation_id", payload.organisation_id)
        .execute()
    )
    supplier = result.data[0] if result.data else {
        **current,
        **updates,
    }
    return {
        "supplier_id": str(supplier.get("id") or supplier_id),
        "organisation_id": str(supplier.get("organisation_id") or payload.organisation_id),
        "default_expense_account": supplier.get("default_expense_account"),
        "default_tracking": supplier.get("default_tracking") or {},
    }


def _validate_choice(value: str, allowed: set[str], label: str) -> None:
    if value not in allowed:
        raise HTTPException(status_code=422, detail=f"Invalid {label}")


def _supplier_kyc_patch_for_request_status(status: str, user_id: str) -> dict:
    if status == "approved":
        return {
            "kyc_status": "approved",
            "kyc_verified_at": utc_now_iso(),
            "kyc_verified_by": user_id,
            "updated_at": utc_now_iso(),
        }
    if status == "rejected":
        return {
            "kyc_status": "rejected",
            "kyc_verified_at": None,
            "kyc_verified_by": None,
            "updated_at": utc_now_iso(),
        }
    if status == "submitted":
        return {
            "kyc_status": "pending",
            "kyc_verified_at": None,
            "kyc_verified_by": None,
            "updated_at": utc_now_iso(),
        }
    if status == "cancelled":
        return {
            "kyc_status": "not_started",
            "kyc_verified_at": None,
            "kyc_verified_by": None,
            "updated_at": utc_now_iso(),
        }
    return {}


def _sync_supplier_kyc_status(*, supplier_id: str, status: str, user_id: str) -> Optional[dict]:
    patch = _supplier_kyc_patch_for_request_status(status, user_id)
    if not patch:
        return None
    res = (
        supabase
        .table("suppliers")
        .update(patch)
        .eq("id", supplier_id)
        .execute()
    )
    return res.data[0] if res.data else patch


@router.post("")
@router.post("/new")
def create_supplier(payload: SupplierCreateRequest, auth: UserAuth):
    user_id, _db = auth
    ensure_org_write(user_id, payload.organisation_id)

    default_tracking = _validated_default_tracking(
        supabase,
        organisation_id=payload.organisation_id,
        tracking=payload.default_tracking,
    )
    insert_payload = _filter_supplier_payload({
        **payload.model_dump(exclude={"invoice_extracted_id", "invoice_raw_id", "link_invoice"}),
        "default_tracking": default_tracking,
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
def create_supplier_from_invoice(payload: SupplierFromInvoiceRequest, auth: UserAuth):
    user_id, _db = auth
    invoice = get_extracted_invoice(payload.invoice_extracted_id)
    if payload.organisation_id and payload.organisation_id != invoice.get("organisation_id"):
        raise HTTPException(status_code=400, detail="Invoice does not belong to organisation_id")
    organisation_id = invoice.get("organisation_id")
    if not organisation_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    ensure_org_write(user_id, organisation_id)

    default_tracking = _validated_default_tracking(
        supabase,
        organisation_id=organisation_id,
        tracking=payload.default_tracking,
    )
    insert_payload = _filter_supplier_payload(
        {
            **_build_supplier_payload_from_extracted(invoice, supplier_name_override=payload.supplier_name),
            **_supplier_processing_overrides(payload),
            "default_tracking": default_tracking,
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
