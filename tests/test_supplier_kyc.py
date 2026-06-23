"""Tests for app/routers/supplier_kyc.py

supplier_kyc.py imports `supabase` from suppliers.py (via `from app.routers.suppliers
import supabase`). Monkeypatching `supplier_kyc.supabase` replaces the name in
this module's namespace. Helper functions (_org_for_supplier, _kyc_request, etc.)
are also imported into supplier_kyc's namespace and can be replaced directly.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.supplier_kyc as kyc
from tests.conftest import MemoryDB, StubDB


# ── Fixtures / shared setup ──────────────────────────────────────────────────

AUTH = ("user-1", None)


def _kyc_request_row(request_id="req-1", supplier_id="sup-1", org="org-1", status="pending"):
    return {
        "id": request_id,
        "organisation_id": org,
        "supplier_id": supplier_id,
        "trigger_type": "onboarding",
        "status": status,
    }


def _kyc_doc_row(doc_id="doc-1", request_id="req-1", org="org-1"):
    return {
        "id": doc_id,
        "organisation_id": org,
        "kyc_request_id": request_id,
        "document_type": "id_document",
        "storage_path": "storage/kyc/doc-1.pdf",
        "file_name": "passport.pdf",
        "uploaded_by": "user-1",
    }


# ── list_supplier_kyc_requests ───────────────────────────────────────────────

def test_list_supplier_kyc_requests_returns_grouped(monkeypatch):
    db = StubDB({
        "supplier_kyc_requests": [_kyc_request_row()],
        "supplier_kyc_documents": [_kyc_doc_row()],
    })
    monkeypatch.setattr(kyc, "supabase", db)
    monkeypatch.setattr(kyc, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(kyc, "ensure_org_read", lambda *_: None)

    result = kyc.list_supplier_kyc_requests("sup-1", "org-1", AUTH)

    assert result["success"] is True
    assert len(result["requests"]) == 1
    grouped = result["requests"][0]
    assert grouped["id"] == "req-1"
    assert len(grouped["documents"]) == 1


def test_list_supplier_kyc_requests_empty(monkeypatch):
    db = StubDB({"supplier_kyc_requests": [], "supplier_kyc_documents": []})
    monkeypatch.setattr(kyc, "supabase", db)
    monkeypatch.setattr(kyc, "_org_for_supplier", lambda _: None)
    monkeypatch.setattr(kyc, "ensure_org_read", lambda *_: None)

    result = kyc.list_supplier_kyc_requests("sup-99", "org-1", AUTH)

    assert result["requests"] == []


def test_list_supplier_kyc_requests_404_org_mismatch(monkeypatch):
    monkeypatch.setattr(kyc, "supabase", StubDB({}))
    monkeypatch.setattr(kyc, "_org_for_supplier", lambda _: "other-org")

    with pytest.raises(HTTPException) as exc_info:
        kyc.list_supplier_kyc_requests("sup-1", "org-1", AUTH)

    assert exc_info.value.status_code == 404


# ── create_supplier_kyc_request ──────────────────────────────────────────────

def test_create_supplier_kyc_request_inserts_and_returns(monkeypatch):
    db = MemoryDB({"supplier_kyc_requests": []})
    monkeypatch.setattr(kyc, "supabase", db)
    monkeypatch.setattr(kyc, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(kyc, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(kyc, "_validate_choice", lambda *_: None)
    monkeypatch.setattr(kyc, "_sync_supplier_kyc_status", lambda **_kw: {"kyc_status": "pending"})

    from app.routers.suppliers import SupplierKycRequestCreate

    payload = SupplierKycRequestCreate(
        organisation_id="org-1",
        trigger_type="onboarding",
        status="pending",
    )
    result = kyc.create_supplier_kyc_request("sup-1", payload, AUTH)

    assert result["success"] is True
    assert result["request"]["organisation_id"] == "org-1"
    assert len(db.tables["supplier_kyc_requests"]) == 1


def test_create_supplier_kyc_request_400_org_mismatch(monkeypatch):
    monkeypatch.setattr(kyc, "supabase", MemoryDB({}))
    monkeypatch.setattr(kyc, "_org_for_supplier", lambda _: "other-org")

    from app.routers.suppliers import SupplierKycRequestCreate

    payload = SupplierKycRequestCreate(organisation_id="org-1", trigger_type="onboarding", status="pending")
    with pytest.raises(HTTPException) as exc_info:
        kyc.create_supplier_kyc_request("sup-1", payload, AUTH)

    assert exc_info.value.status_code == 400


# ── update_supplier_kyc_request ──────────────────────────────────────────────

def test_update_supplier_kyc_request_updates_status(monkeypatch):
    db = MemoryDB({"supplier_kyc_requests": [_kyc_request_row()]})
    monkeypatch.setattr(kyc, "supabase", db)
    monkeypatch.setattr(kyc, "_kyc_request", lambda _: _kyc_request_row())
    monkeypatch.setattr(kyc, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(kyc, "_validate_choice", lambda *_: None)
    monkeypatch.setattr(kyc, "_sync_supplier_kyc_status", lambda **_kw: {"kyc_status": "approved"})

    from app.routers.suppliers import SupplierKycRequestUpdate

    payload = SupplierKycRequestUpdate(status="approved")
    result = kyc.update_supplier_kyc_request("req-1", payload, AUTH)

    assert result["success"] is True


def test_update_supplier_kyc_request_404_when_missing(monkeypatch):
    monkeypatch.setattr(kyc, "supabase", MemoryDB({}))
    monkeypatch.setattr(kyc, "_kyc_request", lambda _: None)

    from app.routers.suppliers import SupplierKycRequestUpdate

    payload = SupplierKycRequestUpdate(status="approved")
    with pytest.raises(HTTPException) as exc_info:
        kyc.update_supplier_kyc_request("nonexistent", payload, AUTH)

    assert exc_info.value.status_code == 404


def test_update_supplier_kyc_request_400_when_no_fields(monkeypatch):
    monkeypatch.setattr(kyc, "supabase", MemoryDB({"supplier_kyc_requests": [_kyc_request_row()]}))
    monkeypatch.setattr(kyc, "_kyc_request", lambda _: _kyc_request_row())
    monkeypatch.setattr(kyc, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(kyc, "_validate_choice", lambda *_: None)

    from app.routers.suppliers import SupplierKycRequestUpdate

    payload = SupplierKycRequestUpdate()  # no fields set
    with pytest.raises(HTTPException) as exc_info:
        kyc.update_supplier_kyc_request("req-1", payload, AUTH)

    assert exc_info.value.status_code == 400


# ── list_supplier_kyc_documents ──────────────────────────────────────────────

def test_list_supplier_kyc_documents_returns_docs(monkeypatch):
    db = StubDB({"supplier_kyc_documents": [_kyc_doc_row(), _kyc_doc_row("doc-2")]})
    monkeypatch.setattr(kyc, "supabase", db)
    monkeypatch.setattr(kyc, "_kyc_request", lambda _: _kyc_request_row())
    monkeypatch.setattr(kyc, "ensure_org_read", lambda *_: None)

    result = kyc.list_supplier_kyc_documents("req-1", AUTH)

    assert result["success"] is True
    assert len(result["documents"]) == 2


def test_list_supplier_kyc_documents_404_when_request_missing(monkeypatch):
    monkeypatch.setattr(kyc, "supabase", StubDB({}))
    monkeypatch.setattr(kyc, "_kyc_request", lambda _: None)

    with pytest.raises(HTTPException) as exc_info:
        kyc.list_supplier_kyc_documents("nonexistent", AUTH)

    assert exc_info.value.status_code == 404


# ── create_supplier_kyc_document ─────────────────────────────────────────────

def test_create_supplier_kyc_document_inserts_doc(monkeypatch):
    db = MemoryDB({"supplier_kyc_documents": []})
    monkeypatch.setattr(kyc, "supabase", db)
    monkeypatch.setattr(kyc, "_kyc_request", lambda _: _kyc_request_row())
    monkeypatch.setattr(kyc, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(kyc, "_validate_choice", lambda *_: None)

    from app.routers.suppliers import SupplierKycDocumentCreate

    payload = SupplierKycDocumentCreate(
        organisation_id="org-1",
        document_type="id_document",
        storage_path="storage/kyc/doc.pdf",
        file_name="passport.pdf",
    )
    result = kyc.create_supplier_kyc_document("req-1", payload, AUTH)

    assert result["success"] is True
    assert result["document"]["kyc_request_id"] == "req-1"


# ── delete_supplier_kyc_document ─────────────────────────────────────────────

def test_delete_supplier_kyc_document_by_uploader(monkeypatch):
    """Uploader can delete their own document without org write permission."""
    db = MemoryDB({"supplier_kyc_documents": [_kyc_doc_row()]})
    monkeypatch.setattr(kyc, "supabase", db)
    monkeypatch.setattr(kyc, "_kyc_document", lambda _: _kyc_doc_row())

    result = kyc.delete_supplier_kyc_document("doc-1", ("user-1", None))

    assert result["success"] is True


def test_delete_supplier_kyc_document_404_when_missing(monkeypatch):
    monkeypatch.setattr(kyc, "supabase", MemoryDB({}))
    monkeypatch.setattr(kyc, "_kyc_document", lambda _: None)

    with pytest.raises(HTTPException) as exc_info:
        kyc.delete_supplier_kyc_document("nonexistent", AUTH)

    assert exc_info.value.status_code == 404
