from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
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


class SupplierMatchProfileRequest(BaseModel):
    organisation_id: str
    supplier_name: Optional[str] = None
    vat_number: Optional[str] = None
    company_registration_number: Optional[str] = None
    account_number: Optional[str] = None
    bank_account_number: Optional[str] = None
    phone: Optional[str] = None
    default_email: Optional[str] = None
    accounting_email: Optional[str] = None


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


class SupplierAllocationRuleSplitRequest(BaseModel):
    expense_account: Optional[str] = None
    tracking: dict[str, Any] = {}
    percent: float = 100
    note: Optional[str] = None
    sort_order: int = 0


class SupplierAllocationRuleRequest(BaseModel):
    organisation_id: str
    supplier_id: str
    name: str
    active: bool = True
    priority: int = 100
    document_scope: str = "all"
    match_type: str = "all_lines"
    match_field: str = "description"
    pattern: Optional[str] = None
    notes: Optional[str] = None
    source_invoice_extracted_id: Optional[str] = None
    splits: list[SupplierAllocationRuleSplitRequest] = []


class SupplierAllocationRuleUpdateRequest(BaseModel):
    name: Optional[str] = None
    active: Optional[bool] = None
    priority: Optional[int] = None
    document_scope: Optional[str] = None
    match_type: Optional[str] = None
    match_field: Optional[str] = None
    pattern: Optional[str] = None
    notes: Optional[str] = None
    splits: Optional[list[SupplierAllocationRuleSplitRequest]] = None


class SupplierAllocationRulesFromInvoiceRequest(BaseModel):
    invoice_extracted_id: str
    supplier_id: str
    line_item_ids: Optional[list[str]] = None
    document_scope: str = "all"
    priority: int = 100


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


class SupplierKycRequestCreate(BaseModel):
    organisation_id: str
    trigger_type: str = "new_supplier"
    status: str = "draft"
    notes: Optional[str] = None
    submitted_at: Optional[str] = None


class SupplierKycRequestUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    reviewer_notes: Optional[str] = None


class SupplierKycDocumentCreate(BaseModel):
    organisation_id: Optional[str] = None
    document_type: str
    document_label: Optional[str] = None
    storage_path: str
    file_name: str
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    notes: Optional[str] = None


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
            "supplier_telephone_extracted, supplier_cell_extracted, supplier_email_extracted, supplier_acc_email_extracted"
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


@router.get("/{supplier_id}/kyc-requests")
def list_supplier_kyc_requests(
    supplier_id: str,
    organisation_id: str = Query(...),
    auth: UserAuth = ...,
):
    user_id, _db = auth
    supplier_org = _org_for_supplier(supplier_id)
    if supplier_org and supplier_org != organisation_id:
        raise HTTPException(status_code=404, detail="Supplier not found")
    ensure_org_read(user_id, organisation_id)

    requests = (
        supabase
        .table("supplier_kyc_requests")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("supplier_id", supplier_id)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    request_ids = [row.get("id") for row in requests if row.get("id")]
    documents: list[dict] = []
    if request_ids:
        documents = (
            supabase
            .table("supplier_kyc_documents")
            .select("*")
            .eq("organisation_id", organisation_id)
            .in_("kyc_request_id", request_ids)
            .order("created_at", desc=False)
            .execute()
            .data
            or []
        )
    docs_by_request: dict[str, list[dict]] = {}
    for document in documents:
        docs_by_request.setdefault(document.get("kyc_request_id"), []).append(document)
    return {
        "success": True,
        "requests": [
            {**request, "documents": docs_by_request.get(request.get("id"), [])}
            for request in requests
        ],
    }


@router.post("/{supplier_id}/kyc-requests")
def create_supplier_kyc_request(
    supplier_id: str,
    payload: SupplierKycRequestCreate,
    auth: UserAuth,
):
    user_id, _db = auth
    supplier_org = _org_for_supplier(supplier_id)
    if supplier_org and supplier_org != payload.organisation_id:
        raise HTTPException(status_code=400, detail="Supplier does not belong to the specified organisation")
    ensure_org_write(user_id, payload.organisation_id)
    _validate_choice(payload.trigger_type, KYC_TRIGGER_TYPES, "KYC trigger_type")
    _validate_choice(payload.status, KYC_REQUEST_STATUSES, "KYC status")

    now = utc_now_iso()
    insert_payload = {
        "organisation_id": payload.organisation_id,
        "supplier_id": supplier_id,
        "trigger_type": payload.trigger_type,
        "status": payload.status,
        "notes": payload.notes,
        "requested_by": user_id,
        "submitted_at": payload.submitted_at or (now if payload.status == "submitted" else None),
        "reviewed_by": user_id if payload.status in {"approved", "rejected"} else None,
        "reviewed_at": now if payload.status in {"approved", "rejected"} else None,
        "created_at": now,
        "updated_at": now,
    }
    res = supabase.table("supplier_kyc_requests").insert(insert_payload).execute()
    request = res.data[0] if res.data else None
    if not request:
        raise HTTPException(status_code=400, detail="Supplier KYC request create failed")

    supplier_patch = _sync_supplier_kyc_status(
        supplier_id=supplier_id,
        status=payload.status,
        user_id=user_id,
    )
    return {"success": True, "request": request, "supplier": supplier_patch}


@router.patch("/kyc-requests/{request_id}")
def update_supplier_kyc_request(
    request_id: str,
    payload: SupplierKycRequestUpdate,
    auth: UserAuth,
):
    user_id, _db = auth
    request = _kyc_request(request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Supplier KYC request not found")
    organisation_id = request.get("organisation_id")
    ensure_org_write(user_id, organisation_id)

    updates = payload.model_dump(exclude_unset=True)
    status = updates.get("status")
    if status is not None:
        _validate_choice(status, KYC_REQUEST_STATUSES, "KYC status")
        if status == "submitted" and not request.get("submitted_at"):
            updates["submitted_at"] = utc_now_iso()
        if status in {"approved", "rejected"}:
            updates["reviewed_by"] = user_id
            updates["reviewed_at"] = utc_now_iso()
    if not updates:
        raise HTTPException(status_code=400, detail="No KYC request fields were provided to update")
    updates["updated_at"] = utc_now_iso()

    res = (
        supabase
        .table("supplier_kyc_requests")
        .update(updates)
        .eq("id", request_id)
        .execute()
    )
    updated = res.data[0] if res.data else {**request, **updates}
    supplier_patch = None
    if status:
        supplier_patch = _sync_supplier_kyc_status(
            supplier_id=request.get("supplier_id"),
            status=status,
            user_id=user_id,
        )
    return {"success": True, "request": updated, "supplier": supplier_patch}


@router.get("/kyc-requests/{request_id}/documents")
def list_supplier_kyc_documents(request_id: str, auth: UserAuth):
    user_id, _db = auth
    request = _kyc_request(request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Supplier KYC request not found")
    ensure_org_read(user_id, request.get("organisation_id"))
    documents = (
        supabase
        .table("supplier_kyc_documents")
        .select("*")
        .eq("kyc_request_id", request_id)
        .order("created_at", desc=False)
        .execute()
        .data
        or []
    )
    return {"success": True, "documents": documents}


@router.post("/kyc-requests/{request_id}/documents")
def create_supplier_kyc_document(
    request_id: str,
    payload: SupplierKycDocumentCreate,
    auth: UserAuth,
):
    user_id, _db = auth
    request = _kyc_request(request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Supplier KYC request not found")
    organisation_id = request.get("organisation_id")
    if payload.organisation_id and payload.organisation_id != organisation_id:
        raise HTTPException(status_code=400, detail="KYC document does not belong to the specified organisation")
    ensure_org_write(user_id, organisation_id)
    _validate_choice(payload.document_type, KYC_DOCUMENT_TYPES, "KYC document_type")
    if not payload.storage_path.strip():
        raise HTTPException(status_code=400, detail="Missing KYC document storage_path")
    if not payload.file_name.strip():
        raise HTTPException(status_code=400, detail="Missing KYC document file_name")

    insert_payload = {
        **payload.model_dump(exclude={"organisation_id"}),
        "kyc_request_id": request_id,
        "organisation_id": organisation_id,
        "uploaded_by": user_id,
    }
    res = supabase.table("supplier_kyc_documents").insert(insert_payload).execute()
    document = res.data[0] if res.data else None
    if not document:
        raise HTTPException(status_code=400, detail="Supplier KYC document create failed")
    return {"success": True, "document": document}


@router.delete("/kyc-documents/{document_id}")
def delete_supplier_kyc_document(document_id: str, auth: UserAuth):
    user_id, _db = auth
    document = _kyc_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Supplier KYC document not found")
    if document.get("uploaded_by") != user_id:
        ensure_org_write(user_id, document.get("organisation_id"))
    res = (
        supabase
        .table("supplier_kyc_documents")
        .delete()
        .eq("id", document_id)
        .execute()
    )
    return {"success": True, "document": (res.data[0] if res.data else document)}


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


def _normalise_allocation_rule_payload(payload: dict) -> dict:
    allowed = {
        "organisation_id",
        "supplier_id",
        "name",
        "active",
        "priority",
        "document_scope",
        "match_type",
        "match_field",
        "pattern",
        "notes",
        "source_invoice_extracted_id",
    }
    return {
        key: value
        for key, value in payload.items()
        if key in allowed and value not in ("", None)
    }


def _normalise_allocation_split_payloads(
    *,
    rule_id: str,
    organisation_id: str,
    splits: list[SupplierAllocationRuleSplitRequest],
) -> list[dict]:
    payload: list[dict] = []
    for index, split in enumerate(splits or []):
        row = split.model_dump()
        percent = row.get("percent")
        if percent is None or percent <= 0:
            continue
        payload.append({
            "rule_id": rule_id,
            "organisation_id": organisation_id,
            "expense_account": row.get("expense_account"),
            "tracking": {
                str(key): value
                for key, value in (row.get("tracking") or {}).items()
                if value not in (None, "")
            },
            "percent": percent,
            "note": row.get("note"),
            "sort_order": row.get("sort_order") if row.get("sort_order") is not None else index,
        })
    return payload


def _rule_with_splits(rule: dict) -> dict:
    if rule.get("id") and not rule.get("supplier_id"):
        try:
            res = (
                supabase
                .table("supplier_line_item_allocation_rules")
                .select("*")
                .eq("id", rule["id"])
                .limit(1)
                .execute()
            )
            if res.data:
                rule = res.data[0]
        except Exception:
            pass
    rules = fetch_supplier_allocation_rules(supabase, rule.get("supplier_id"), active_only=False)
    return next((row for row in rules if row.get("id") == rule.get("id")), {**rule, "splits": []})


def _line_rule_pattern(description: str | None, code: str | None) -> tuple[str, str]:
    clean_description = re.sub(r"\s+", " ", str(description or "")).strip()
    clean_code = re.sub(r"\s+", " ", str(code or "")).strip()
    if clean_description:
        return "description", clean_description[:180]
    return "code", clean_code[:180]


def _splits_from_line_item(line_item: dict, allocations: list[dict]) -> list[SupplierAllocationRuleSplitRequest]:
    if allocations:
        line_total = None
        try:
            line_total = float(line_item.get("line_total") or line_item.get("amount") or 0)
        except Exception:
            line_total = None
        return [
            SupplierAllocationRuleSplitRequest(
                expense_account=allocation.get("expense_account") or line_item.get("expense_account"),
                tracking=allocation.get("tracking") or line_item.get("tracking") or {},
                percent=float(
                    allocation.get("percent")
                    or (
                        round((float(allocation.get("amount") or 0) / line_total) * 100, 4)
                        if line_total
                        else 100
                    )
                ),
                note=allocation.get("note"),
                sort_order=index,
            )
            for index, allocation in enumerate(allocations)
        ]

    if not line_item.get("expense_account") and not line_item.get("tracking"):
        return []
    return [
        SupplierAllocationRuleSplitRequest(
            expense_account=line_item.get("expense_account"),
            tracking=line_item.get("tracking") or {},
            percent=100,
            sort_order=0,
        )
    ]


@router.get("/{supplier_id}/allocation-rules")
def list_supplier_allocation_rules(
    supplier_id: str,
    organisation_id: str = Query(...),
    auth: UserAuth = ...,
):
    user_id, _db = auth
    supplier_org = _org_for_supplier(supplier_id)
    if supplier_org and supplier_org != organisation_id:
        raise HTTPException(status_code=404, detail="Supplier not found")
    ensure_org_read(user_id, organisation_id)
    rules = fetch_supplier_allocation_rules(supabase, supplier_id, active_only=False)
    return {"success": True, "rules": rules}


@router.post("/{supplier_id}/allocation-rules")
def create_supplier_allocation_rule(
    supplier_id: str,
    payload: SupplierAllocationRuleRequest,
    auth: UserAuth,
):
    user_id, _db = auth
    supplier_org = _org_for_supplier(supplier_id)
    if supplier_org and supplier_org != payload.organisation_id:
        raise HTTPException(status_code=400, detail="Supplier does not belong to the specified organisation")
    if supplier_id != payload.supplier_id:
        raise HTTPException(status_code=400, detail="Supplier id mismatch")
    ensure_org_write(user_id, payload.organisation_id)

    insert_payload = _normalise_allocation_rule_payload(payload.model_dump(exclude={"splits"}))
    if not insert_payload.get("name"):
        raise HTTPException(status_code=400, detail="Missing rule name")
    res = supabase.table("supplier_line_item_allocation_rules").insert(insert_payload).execute()
    rule = res.data[0] if res.data else None
    if not rule:
        raise HTTPException(status_code=400, detail="Allocation rule create failed")

    split_payload = _normalise_allocation_split_payloads(
        rule_id=rule["id"],
        organisation_id=payload.organisation_id,
        splits=payload.splits,
    )
    if split_payload:
        supabase.table("supplier_line_item_allocation_rule_splits").insert(split_payload).execute()

    return {"success": True, "rule": _rule_with_splits(rule)}


@router.patch("/allocation-rules/{rule_id}")
def update_supplier_allocation_rule(
    rule_id: str,
    payload: SupplierAllocationRuleUpdateRequest,
    auth: UserAuth,
):
    user_id, _db = auth
    organisation_id = _org_for_allocation_rule(rule_id)
    if not organisation_id:
        raise HTTPException(status_code=404, detail="Allocation rule not found")
    ensure_org_write(user_id, organisation_id)

    updates = _normalise_allocation_rule_payload(payload.model_dump(exclude={"splits"}, exclude_unset=True))
    rule = None
    if updates:
        res = (
            supabase
            .table("supplier_line_item_allocation_rules")
            .update(updates)
            .eq("id", rule_id)
            .execute()
        )
        rule = res.data[0] if res.data else {"id": rule_id, "organisation_id": organisation_id}
    else:
        res = (
            supabase
            .table("supplier_line_item_allocation_rules")
            .select("*")
            .eq("id", rule_id)
            .limit(1)
            .execute()
        )
        rule = res.data[0] if res.data else None

    if payload.splits is not None:
        supabase.table("supplier_line_item_allocation_rule_splits").delete().eq("rule_id", rule_id).execute()
        split_payload = _normalise_allocation_split_payloads(
            rule_id=rule_id,
            organisation_id=organisation_id,
            splits=payload.splits,
        )
        if split_payload:
            supabase.table("supplier_line_item_allocation_rule_splits").insert(split_payload).execute()

    if not rule:
        raise HTTPException(status_code=404, detail="Allocation rule not found")
    return {"success": True, "rule": _rule_with_splits(rule)}


@router.delete("/allocation-rules/{rule_id}")
def delete_supplier_allocation_rule(rule_id: str, auth: UserAuth):
    user_id, _db = auth
    organisation_id = _org_for_allocation_rule(rule_id)
    if not organisation_id:
        raise HTTPException(status_code=404, detail="Allocation rule not found")
    ensure_org_write(user_id, organisation_id)
    supabase.table("supplier_line_item_allocation_rules").delete().eq("id", rule_id).execute()
    return {"success": True}


@router.post("/{supplier_id}/allocation-rules/from-invoice")
def create_supplier_allocation_rules_from_invoice(
    supplier_id: str,
    payload: SupplierAllocationRulesFromInvoiceRequest,
    auth: UserAuth,
):
    user_id, _db = auth
    invoice = get_extracted_invoice(payload.invoice_extracted_id)
    organisation_id = invoice.get("organisation_id")
    if not organisation_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.get("supplier_id") and invoice.get("supplier_id") != supplier_id:
        raise HTTPException(status_code=400, detail="Invoice is linked to a different supplier")
    if supplier_id != payload.supplier_id:
        raise HTTPException(status_code=400, detail="Supplier id mismatch")
    supplier_org = _org_for_supplier(supplier_id)
    if supplier_org and supplier_org != organisation_id:
        raise HTTPException(status_code=400, detail="Supplier and invoice belong to different organisations")
    ensure_org_write(user_id, organisation_id)

    line_query = (
        supabase
        .table("invoice_line_items")
        .select("*")
        .eq("invoice_extracted_id", payload.invoice_extracted_id)
        .order("created_at", desc=False)
        .order("id", desc=False)
    )
    if payload.line_item_ids:
        line_query = line_query.in_("id", payload.line_item_ids)
    line_items = line_query.execute().data or []
    if not line_items:
        raise HTTPException(status_code=400, detail="No invoice line items found to save as rules")

    line_ids = [line.get("id") for line in line_items if line.get("id")]
    allocations_by_line: dict[str, list[dict]] = {}
    if line_ids:
        allocation_rows = (
            supabase
            .table("invoice_line_item_allocations")
            .select("*")
            .in_("invoice_line_item_id", line_ids)
            .order("sort_order", desc=False)
            .execute()
            .data
            or []
        )
        for allocation in allocation_rows:
            line_id = allocation.get("invoice_line_item_id")
            if line_id:
                allocations_by_line.setdefault(line_id, []).append(allocation)

    created_rules: list[dict] = []
    for index, line_item in enumerate(line_items):
        splits = _splits_from_line_item(line_item, allocations_by_line.get(line_item.get("id"), []))
        if not splits:
            continue
        match_field, pattern = _line_rule_pattern(line_item.get("description"), line_item.get("code"))
        if not pattern:
            continue
        rule_payload = {
            "organisation_id": organisation_id,
            "supplier_id": supplier_id,
            "name": f"{line_item.get('description') or line_item.get('code') or 'Line item'} allocation",
            "active": True,
            "priority": payload.priority + index,
            "document_scope": payload.document_scope,
            "match_type": "contains",
            "match_field": match_field,
            "pattern": pattern,
            "source_invoice_extracted_id": payload.invoice_extracted_id,
        }
        res = supabase.table("supplier_line_item_allocation_rules").insert(rule_payload).execute()
        rule = res.data[0] if res.data else None
        if not rule:
            continue
        split_payload = _normalise_allocation_split_payloads(
            rule_id=rule["id"],
            organisation_id=organisation_id,
            splits=splits,
        )
        if split_payload:
            supabase.table("supplier_line_item_allocation_rule_splits").insert(split_payload).execute()
        created_rules.append(_rule_with_splits(rule))

    if not created_rules:
        raise HTTPException(status_code=400, detail="No coded line items or allocations were available to save as rules")

    log_invoice_event(
        supabase,
        organisation_id=organisation_id,
        invoice_raw_id=invoice.get("invoice_raw_id"),
        invoice_extracted_id=invoice.get("id"),
        event_type="supplier_allocation_rules_created",
        stage="supplier_processing_rules",
        actor_type="api",
        new_value={"supplier_id": supplier_id, "rules_created": len(created_rules)},
        notes="Supplier allocation rules were created from reviewed invoice line items.",
    )
    return {"success": True, "rules": created_rules}


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
def create_supplier(payload: SupplierCreateRequest, auth: UserAuth):
    user_id, _db = auth
    ensure_org_write(user_id, payload.organisation_id)

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
def create_supplier_from_invoice(payload: SupplierFromInvoiceRequest, auth: UserAuth):
    user_id, _db = auth
    invoice = get_extracted_invoice(payload.invoice_extracted_id)
    if payload.organisation_id and payload.organisation_id != invoice.get("organisation_id"):
        raise HTTPException(status_code=400, detail="Invoice does not belong to organisation_id")
    organisation_id = invoice.get("organisation_id")
    if not organisation_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    ensure_org_write(user_id, organisation_id)

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
