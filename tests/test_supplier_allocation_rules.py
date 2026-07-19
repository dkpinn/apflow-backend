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
    db = MemoryDB({
        TABLE: [],
        "supplier_line_item_allocation_rule_splits": [],
        "tracking_dimensions": [{"id": "division", "organisation_id": "org-1", "name": "Company Division", "active": True}],
        "tracking_values": [{"id": "division-jhb", "dimension_id": "division", "name": "Johannesburg", "active": True}],
    })
    monkeypatch.setattr(ar, "supabase", db)
    monkeypatch.setattr(ar, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(ar, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(ar, "_rule_with_splits", lambda r: {**r, "splits": []})

    from app.routers.suppliers import SupplierAllocationRuleRequest

    payload = SupplierAllocationRuleRequest(
        organisation_id="org-1",
        supplier_id="sup-1",
        name="Rent",
        splits=[{
            "expense_account": "6000",
            "tracking": {"division": "division-jhb"},
            "vat_treatment": "full",
            "percent": 100,
        }],
    )
    result = ar.create_supplier_allocation_rule("sup-1", payload, AUTH)

    assert result["success"] is True
    assert len(db.tables[TABLE]) == 1
    assert db.tables[TABLE][0]["name"] == "Rent"
    saved_split = db.tables["supplier_line_item_allocation_rule_splits"][0]
    assert saved_split["tracking"] == {"division": "division-jhb"}
    assert saved_split["vat_treatment"] == "full"


def test_allocation_rule_options_include_tracking_and_vat(monkeypatch):
    db = MemoryDB({
        "tracking_dimensions": [{"id": "division", "organisation_id": "org-1", "name": "Company Division", "position": 1, "active": True}],
        "tracking_values": [{"id": "division-cpt", "dimension_id": "division", "name": "Cape Town", "active": True}],
        "accounts": [{"id": "account-1", "organisation_id": "org-1", "code": "6000", "name": "Rent", "type": "expense", "active": True, "vat_treatment": "full"}],
    })
    monkeypatch.setattr(ar, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(ar, "ensure_org_read", lambda *_: None)

    result = ar.get_supplier_allocation_rule_options("sup-1", "org-1", ("user-1", db))

    assert result["tracking_dimensions"][0]["name"] == "Company Division"
    assert result["tracking_dimensions"][0]["values"][0]["name"] == "Cape Town"
    assert {option["value"] for option in result["vat_treatments"]} == {"full", "blocked", "zero_rated", "exempt"}
    assert result["accounts"][0]["vat_treatment"] == "full"


def test_create_rejects_tracking_value_from_wrong_dimension(monkeypatch):
    db = MemoryDB({
        TABLE: [],
        "tracking_dimensions": [{"id": "division", "organisation_id": "org-1", "name": "Company Division", "position": 1, "active": True}],
        "tracking_values": [{"id": "other-value", "dimension_id": "other", "name": "Wrong", "active": True}],
    })
    monkeypatch.setattr(ar, "supabase", db)
    monkeypatch.setattr(ar, "_org_for_supplier", lambda _: "org-1")
    monkeypatch.setattr(ar, "ensure_org_write", lambda *_: None)

    from app.routers.suppliers import SupplierAllocationRuleRequest

    payload = SupplierAllocationRuleRequest(
        organisation_id="org-1",
        supplier_id="sup-1",
        name="Invalid tracking",
        splits=[{"tracking": {"division": "other-value"}, "percent": 100}],
    )
    with pytest.raises(HTTPException, match="tracking allocation"):
        ar.create_supplier_allocation_rule("sup-1", payload, AUTH)


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
