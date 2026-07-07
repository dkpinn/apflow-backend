from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.suppliers as suppliers
from app.schemas.suppliers import SupplierLinkRequest, SupplierMatchProfileRequest

AUTH = ("user-1", None)


class _FailDB:
    def table(self, _name):
        raise AssertionError("service-role query should not run before auth")


def test_list_suppliers_requires_read_access_before_query(monkeypatch):
    monkeypatch.setattr(suppliers, "supabase", _FailDB())
    monkeypatch.setattr(
        suppliers,
        "ensure_org_read",
        lambda *_args: (_ for _ in ()).throw(HTTPException(status_code=403, detail="denied")),
    )

    with pytest.raises(HTTPException) as exc_info:
        suppliers.list_suppliers(organisation_id="org-1", auth=AUTH)

    assert exc_info.value.status_code == 403


def test_match_supplier_profile_requires_read_access_before_matching(monkeypatch):
    monkeypatch.setattr(
        suppliers,
        "ensure_org_read",
        lambda *_args: (_ for _ in ()).throw(HTTPException(status_code=403, detail="denied")),
    )
    payload = SupplierMatchProfileRequest(organisation_id="org-1", supplier_name="Acme")

    with pytest.raises(HTTPException) as exc_info:
        suppliers.match_supplier_profile(payload, AUTH)

    assert exc_info.value.status_code == 403


def test_link_supplier_requires_write_access_before_linking(monkeypatch):
    linked = []
    monkeypatch.setattr(suppliers, "get_extracted_invoice", lambda _invoice_id: {"id": "inv-1", "organisation_id": "org-1"})
    monkeypatch.setattr(suppliers, "_org_for_supplier", lambda _supplier_id: "org-1")
    monkeypatch.setattr(
        suppliers,
        "ensure_org_write",
        lambda *_args: (_ for _ in ()).throw(HTTPException(status_code=403, detail="denied")),
    )
    monkeypatch.setattr(suppliers, "_link_supplier_to_invoice", lambda **kwargs: linked.append(kwargs))
    payload = SupplierLinkRequest(supplier_id="sup-1", invoice_extracted_id="inv-1", organisation_id="org-1")

    with pytest.raises(HTTPException) as exc_info:
        suppliers.link_supplier(payload, AUTH)

    assert exc_info.value.status_code == 403
    assert linked == []
