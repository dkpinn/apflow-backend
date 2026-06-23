"""Tests for app/routers/supplier_allocation_rules.py

Like supplier_kyc.py, this module imports `supabase` from suppliers.py.
Monkeypatching `supplier_allocation_rules.supabase` replaces the name binding
in this module's namespace. `_rule_with_splits` is defined in this module and
can be patched the same way.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.supplier_allocation_rules as ar
from tests.conftest import MemoryDB, StubDB


AUTH = ("user-1", None)

TABLE = "supplier_line_item_allocation_rules"


def _rule(rule_id="rule-1", supplier_id="sup-1", org="org-1"):
    return {
        "id": rule_id,
        "organisation_id": org,
        "supplier_id": supplier_id,
        "name": "Default rule",
        "active": True,
        "match_type": "keyword",
        "match_field": "description",
        "pattern": "office",
        "priority": 100,
        "splits": [],
    }


# ── list_supplier_allocation_rules ───────────────────────────────────────────

def test_list_supplier_allocation_rules_returns_rules(monkeypatch):
    monkeypatch.setattr(ar, "supabase", StubDB({}))
    monkeypatch.setattr(ar, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(ar, "ensure_org_read", lambda *_: None)
    monkeypatch.setattr(ar, "fetch_supplier_allocation_rules", lambda *_a, **_kw: [_rule()])

    result = ar.list_supplier_allocation_rules("sup-1", "org-1", AUTH)

    assert result["success"] is True
    assert len(result["rules"]) == 1
    assert result["rules"][0]["supplier_id"] == "sup-1"


def test_list_supplier_allocation_rules_404_org_mismatch(monkeypatch):
    monkeypatch.setattr(ar, "supabase", StubDB({}))
    monkeypatch.setattr(ar, "_org_for_supplier", lambda _: "different-org")

    with pytest.raises(HTTPException) as exc_info:
        ar.list_supplier_allocation_rules("sup-1", "org-1", AUTH)

    assert exc_info.value.status_code == 404


# ── create_supplier_allocation_rule ─────────────────────────────────────────

def test_create_supplier_allocation_rule_inserts(monkeypatch):
    db = MemoryDB({TABLE: []})
    monkeypatch.setattr(ar, "supabase", db)
    monkeypatch.setattr(ar, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(ar, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(ar, "_rule_with_splits", lambda r: {**r, "splits": []})

    from app.routers.suppliers import SupplierAllocationRuleRequest

    payload = SupplierAllocationRuleRequest(
        organisation_id="org-1",
        supplier_id="sup-1",
        name="Rent",
    )
    result = ar.create_supplier_allocation_rule("sup-1", payload, AUTH)

    assert result["success"] is True
    assert len(db.tables[TABLE]) == 1
    assert db.tables[TABLE][0]["name"] == "Rent"


def test_create_supplier_allocation_rule_400_org_mismatch(monkeypatch):
    monkeypatch.setattr(ar, "supabase", MemoryDB({}))
    monkeypatch.setattr(ar, "_org_for_supplier", lambda _: "other-org")

    from app.routers.suppliers import SupplierAllocationRuleRequest

    payload = SupplierAllocationRuleRequest(
        organisation_id="org-1",
        supplier_id="sup-1",
        name="Rent",
    )
    with pytest.raises(HTTPException) as exc_info:
        ar.create_supplier_allocation_rule("sup-1", payload, AUTH)

    assert exc_info.value.status_code == 400


def test_create_supplier_allocation_rule_400_supplier_mismatch(monkeypatch):
    monkeypatch.setattr(ar, "supabase", MemoryDB({}))
    monkeypatch.setattr(ar, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(ar, "ensure_org_write", lambda *_: None)

    from app.routers.suppliers import SupplierAllocationRuleRequest

    payload = SupplierAllocationRuleRequest(
        organisation_id="org-1",
        supplier_id="different-sup",  # mismatch with path param
        name="Rent",
    )
    with pytest.raises(HTTPException) as exc_info:
        ar.create_supplier_allocation_rule("sup-1", payload, AUTH)

    assert exc_info.value.status_code == 400
    assert "mismatch" in exc_info.value.detail.lower()


# ── update_supplier_allocation_rule ─────────────────────────────────────────

def test_update_supplier_allocation_rule_updates_name(monkeypatch):
    db = MemoryDB({TABLE: [_rule()]})
    monkeypatch.setattr(ar, "supabase", db)
    monkeypatch.setattr(ar, "_org_for_allocation_rule", lambda _: "org-1")
    monkeypatch.setattr(ar, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(ar, "_rule_with_splits", lambda r: {**r, "splits": []})

    from app.routers.suppliers import SupplierAllocationRuleUpdateRequest

    payload = SupplierAllocationRuleUpdateRequest(name="Updated rule")
    result = ar.update_supplier_allocation_rule("rule-1", payload, AUTH)

    assert result["success"] is True
    assert result["rule"]["name"] == "Updated rule"


def test_update_supplier_allocation_rule_404_when_missing(monkeypatch):
    monkeypatch.setattr(ar, "supabase", MemoryDB({}))
    monkeypatch.setattr(ar, "_org_for_allocation_rule", lambda _: None)

    from app.routers.suppliers import SupplierAllocationRuleUpdateRequest

    with pytest.raises(HTTPException) as exc_info:
        ar.update_supplier_allocation_rule("nonexistent", SupplierAllocationRuleUpdateRequest(name="x"), AUTH)

    assert exc_info.value.status_code == 404


# ── delete_supplier_allocation_rule ─────────────────────────────────────────

def test_delete_supplier_allocation_rule_removes_row(monkeypatch):
    db = MemoryDB({TABLE: [_rule()]})
    monkeypatch.setattr(ar, "supabase", db)
    monkeypatch.setattr(ar, "_org_for_allocation_rule", lambda _: "org-1")
    monkeypatch.setattr(ar, "ensure_org_write", lambda *_: None)

    result = ar.delete_supplier_allocation_rule("rule-1", AUTH)

    assert result["success"] is True


def test_delete_supplier_allocation_rule_404_when_missing(monkeypatch):
    monkeypatch.setattr(ar, "supabase", MemoryDB({}))
    monkeypatch.setattr(ar, "_org_for_allocation_rule", lambda _: None)

    with pytest.raises(HTTPException) as exc_info:
        ar.delete_supplier_allocation_rule("nonexistent", AUTH)

    assert exc_info.value.status_code == 404
