"""Tests for app/routers/supplier_branches.py

Like supplier_kyc.py, this module imports `supabase` from suppliers.py.
Monkeypatching `supplier_branches.supabase` replaces the name binding here.
Module-level helpers (_link_branch_to_invoice, _supplier_for_branch_name, etc.)
can be patched directly on the module.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.supplier_branches as sb
from tests.conftest import MemoryDB, StubDB


AUTH = ("user-1", None)


def _branch(branch_id="branch-1", supplier_id="sup-1", org="org-1"):
    return {
        "id": branch_id,
        "organisation_id": org,
        "supplier_id": supplier_id,
        "branch_name": "Durban Branch",
        "active": True,
    }


def _invoice(inv_id="inv-1", org="org-1", supplier_id="sup-1"):
    return {
        "id": inv_id,
        "organisation_id": org,
        "invoice_raw_id": "raw-1",
        "supplier_id": supplier_id,
        "supplier_name_extracted": "Acme Durban",
    }


# ── list_supplier_branches ───────────────────────────────────────────────────

def test_list_supplier_branches_returns_active(monkeypatch):
    db = StubDB({
        "supplier_branches": [
            _branch(),
            {**_branch("branch-2"), "active": False},
        ],
    })
    monkeypatch.setattr(sb, "supabase", db)
    monkeypatch.setattr(sb, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(sb, "ensure_org_read", lambda *_: None)

    result = sb.list_supplier_branches("sup-1", "org-1", active_only=True, auth=AUTH)

    assert result["success"] is True
    assert len(result["branches"]) == 1
    assert result["branches"][0]["id"] == "branch-1"


def test_list_supplier_branches_404_org_mismatch(monkeypatch):
    monkeypatch.setattr(sb, "supabase", StubDB({}))
    monkeypatch.setattr(sb, "_org_for_supplier", lambda _: "other-org")

    with pytest.raises(HTTPException) as exc_info:
        sb.list_supplier_branches("sup-1", "org-1", active_only=True, auth=AUTH)

    assert exc_info.value.status_code == 404


def test_list_supplier_branches_all_when_not_active_only(monkeypatch):
    db = StubDB({
        "supplier_branches": [
            _branch(),
            {**_branch("branch-2"), "active": False},
        ],
    })
    monkeypatch.setattr(sb, "supabase", db)
    monkeypatch.setattr(sb, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(sb, "ensure_org_read", lambda *_: None)

    result = sb.list_supplier_branches("sup-1", "org-1", active_only=False, auth=AUTH)

    assert len(result["branches"]) == 2


# ── create_supplier_branch ───────────────────────────────────────────────────

def test_create_supplier_branch_inserts(monkeypatch):
    db = MemoryDB({"supplier_branches": []})
    monkeypatch.setattr(sb, "supabase", db)
    monkeypatch.setattr(sb, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(sb, "ensure_org_write", lambda *_: None)

    from app.routers.suppliers import SupplierBranchCreateRequest

    payload = SupplierBranchCreateRequest(
        organisation_id="org-1",
        supplier_id="sup-1",
        branch_name="Durban Branch",
        link_invoice=False,
    )
    result = sb.create_supplier_branch(payload, AUTH)

    assert result["success"] is True
    assert result["branch"]["branch_name"] == "Durban Branch"
    assert len(db.tables["supplier_branches"]) == 1


def test_create_supplier_branch_links_invoice_when_requested(monkeypatch):
    branch_row = {**_branch(), "id": "new-branch"}
    db = MemoryDB({"supplier_branches": []})
    monkeypatch.setattr(sb, "supabase", db)
    monkeypatch.setattr(sb, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(sb, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(sb, "_link_branch_to_invoice", lambda **_kw: {"id": "inv-1", "supplier_branch_id": "new-branch"})

    from app.routers.suppliers import SupplierBranchCreateRequest

    payload = SupplierBranchCreateRequest(
        organisation_id="org-1",
        supplier_id="sup-1",
        branch_name="Durban Branch",
        invoice_extracted_id="inv-1",
        link_invoice=True,
    )
    result = sb.create_supplier_branch(payload, AUTH)

    assert result["success"] is True
    assert result["linked"] is not None


def test_create_supplier_branch_400_org_mismatch(monkeypatch):
    monkeypatch.setattr(sb, "supabase", MemoryDB({}))
    monkeypatch.setattr(sb, "_org_for_supplier", lambda _: "different-org")

    from app.routers.suppliers import SupplierBranchCreateRequest

    payload = SupplierBranchCreateRequest(organisation_id="org-1", supplier_id="sup-1", branch_name="Branch")
    with pytest.raises(HTTPException) as exc_info:
        sb.create_supplier_branch(payload, AUTH)

    assert exc_info.value.status_code == 400


# ── create_supplier_branch_from_invoice ─────────────────────────────────────

def test_create_supplier_branch_from_invoice_creates_branch(monkeypatch):
    db = MemoryDB({"supplier_branches": []})
    monkeypatch.setattr(sb, "supabase", db)
    monkeypatch.setattr(sb, "_org_for_invoice_extracted", lambda _: "org-1")
    monkeypatch.setattr(sb, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(sb, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(sb, "get_extracted_invoice", lambda _: _invoice())
    monkeypatch.setattr(sb, "_supplier_for_branch_name", lambda _: {"supplier_name": "Acme"})
    monkeypatch.setattr(sb, "log_invoice_event", lambda *_a, **_kw: None)

    from app.routers.suppliers import SupplierBranchFromInvoiceRequest

    payload = SupplierBranchFromInvoiceRequest(
        invoice_extracted_id="inv-1",
        supplier_id="sup-1",
        branch_name="Durban",
        link_invoice=False,
    )
    result = sb.create_supplier_branch_from_invoice(payload, AUTH)

    assert result["success"] is True
    assert result["branch"]["branch_name"] == "Durban"


def test_create_supplier_branch_from_invoice_404_when_not_found(monkeypatch):
    monkeypatch.setattr(sb, "supabase", StubDB({}))
    monkeypatch.setattr(sb, "_org_for_invoice_extracted", lambda _: None)

    from app.routers.suppliers import SupplierBranchFromInvoiceRequest

    payload = SupplierBranchFromInvoiceRequest(invoice_extracted_id="nonexistent", supplier_id="sup-1")
    with pytest.raises(HTTPException) as exc_info:
        sb.create_supplier_branch_from_invoice(payload, AUTH)

    assert exc_info.value.status_code == 404


# ── link_supplier_branch ─────────────────────────────────────────────────────

def test_link_supplier_branch_links_branch(monkeypatch):
    monkeypatch.setattr(sb, "supabase", StubDB({}))
    monkeypatch.setattr(sb, "_org_for_invoice_extracted", lambda _: "org-1")
    monkeypatch.setattr(sb, "_org_for_branch", lambda _: "org-1")
    monkeypatch.setattr(sb, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(sb, "_link_branch_to_invoice", lambda **_kw: {"id": "inv-1", "supplier_branch_id": "branch-1"})

    from app.routers.suppliers import SupplierBranchLinkRequest

    payload = SupplierBranchLinkRequest(invoice_extracted_id="inv-1", supplier_branch_id="branch-1")
    result = sb.link_supplier_branch(payload, AUTH)

    assert result["success"] is True
    assert result["linked"]["supplier_branch_id"] == "branch-1"


def test_link_supplier_branch_404_when_invoice_missing(monkeypatch):
    monkeypatch.setattr(sb, "supabase", StubDB({}))
    monkeypatch.setattr(sb, "_org_for_invoice_extracted", lambda _: None)

    from app.routers.suppliers import SupplierBranchLinkRequest

    payload = SupplierBranchLinkRequest(invoice_extracted_id="nonexistent", supplier_branch_id="branch-1")
    with pytest.raises(HTTPException) as exc_info:
        sb.link_supplier_branch(payload, AUTH)

    assert exc_info.value.status_code == 404


# ── unlink_supplier_branch ───────────────────────────────────────────────────

def test_unlink_supplier_branch_clears_link(monkeypatch):
    db = MemoryDB({
        "invoices_extracted": [{"id": "inv-1", "organisation_id": "org-1", "supplier_branch_id": "branch-1"}],
    })
    monkeypatch.setattr(sb, "supabase", db)
    monkeypatch.setattr(sb, "_org_for_invoice_extracted", lambda _: "org-1")
    monkeypatch.setattr(sb, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(sb, "get_extracted_invoice", lambda _: _invoice())
    monkeypatch.setattr(sb, "log_invoice_event", lambda *_a, **_kw: None)

    from app.routers.suppliers import SupplierBranchUnlinkRequest

    payload = SupplierBranchUnlinkRequest(invoice_extracted_id="inv-1")
    result = sb.unlink_supplier_branch(payload, AUTH)

    assert result["success"] is True


def test_unlink_supplier_branch_404_when_invoice_missing(monkeypatch):
    monkeypatch.setattr(sb, "supabase", StubDB({}))
    monkeypatch.setattr(sb, "_org_for_invoice_extracted", lambda _: None)

    from app.routers.suppliers import SupplierBranchUnlinkRequest

    payload = SupplierBranchUnlinkRequest(invoice_extracted_id="nonexistent")
    with pytest.raises(HTTPException) as exc_info:
        sb.unlink_supplier_branch(payload, AUTH)

    assert exc_info.value.status_code == 404
