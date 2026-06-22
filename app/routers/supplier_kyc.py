"""supplier_kyc.py
KYC request and document routes for suppliers.

Shared models, constants, and helper functions (e.g. _kyc_request,
_sync_supplier_kyc_status) are imported from suppliers.py.  suppliers.py does
NOT import this module, so there is no circular-import risk.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.invoice_data_builders import utc_now_iso

from app.routers.suppliers import (
    KYC_DOCUMENT_TYPES,
    KYC_REQUEST_STATUSES,
    KYC_TRIGGER_TYPES,
    SupplierKycDocumentCreate,
    SupplierKycRequestCreate,
    SupplierKycRequestUpdate,
    _kyc_document,
    _kyc_request,
    _org_for_supplier,
    _sync_supplier_kyc_status,
    _validate_choice,
    supabase,
)

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])


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
