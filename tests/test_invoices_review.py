"""Tests for app/routers/invoices_review.py

get_invoice_review_data is tested with a StubDB (SELECT-only queries).
Agent-review routes all call _fetch_agent_context — this is monkeypatched to
return a canned context dict so each route test stays focused on its own logic.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.invoices_review as rv
from tests.conftest import MemoryDB, StubDB


AUTH = ("user-1", None)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _invoice_row(inv_id="inv-1", raw_id="raw-1", org="org-1", supplier_id=None):
    return {
        "id": inv_id,
        "invoice_raw_id": raw_id,
        "organisation_id": org,
        "supplier_id": supplier_id,
        "invoice_number": "INV-001",
    }


def _fake_context(**overrides):
    return {
        "success": True,
        "resolved_by": "invoice_extracted_id",
        "organisation_id": "org-1",
        "invoice_extracted_id": "inv-1",
        "invoice_raw_id": "raw-1",
        "invoice": _invoice_row(),
        "supplier": None,
        "supplier_branch": None,
        "supplier_branches": [],
        "accounts": [],
        "tracking_dimensions": [],
        "tracking_values": [],
        "duplicate_count": 0,
        "line_items": [],
        "audit_events": [],
        "parse_attempts": [],
        **overrides,
    }


def _suggestion(sug_id="sug-1", status="open", action_type="invoice_patch"):
    return {
        "id": sug_id,
        "organisation_id": "org-1",
        "invoice_extracted_id": "inv-1",
        "invoice_raw_id": "raw-1",
        "status": status,
        "severity": "warning",
        "message": "Check this",
        "apply_payload": {"type": action_type, "fields": {"invoice_number": "INV-002"}, "line_item_id": None, "supplier_id": None},
    }


# ── get_invoice_review_data ───────────────────────────────────────────────────

def test_get_invoice_review_data_found_by_extracted_id(monkeypatch):
    db = StubDB({
        "invoices_extracted": [_invoice_row()],
        "invoices_raw": [],
        "suppliers": [],
        "supplier_branches": [],
        "document_pages": [],
        "invoice_line_items": [],
        "invoice_audit_events": [],
    })
    monkeypatch.setattr(rv, "supabase", db)
    monkeypatch.setattr(rv, "fetch_parse_attempts", lambda db, invoice_raw_id: ([], None))
    monkeypatch.setattr(rv, "build_extracted_document_profile", lambda inv: {"line_items": []})
    monkeypatch.setattr(rv, "build_extracted_supplier_profile", lambda inv: {})
    monkeypatch.setattr(rv, "build_supplier_create_payload", lambda **_kw: {})
    monkeypatch.setattr(rv, "ensure_org_read", lambda *_args: None)

    result = rv.get_invoice_review_data("inv-1", AUTH)

    assert result["success"] is True
    assert result["invoice_extracted_id"] == "inv-1"
    assert result["resolved_by"] == "invoice_extracted_id"


def test_get_invoice_review_data_falls_back_to_raw_id(monkeypatch):
    db = StubDB({
        "invoices_extracted": [_invoice_row()],
        "invoices_raw": [],
        "invoice_audit_events": [],
        "invoice_line_items": [],
        "document_pages": [],
        "supplier_branches": [],
    })
    # StubDB: first query (by id="raw-1") won't match invoice_extracted.id
    # second query (by invoice_raw_id="raw-1") will match
    monkeypatch.setattr(rv, "supabase", db)
    monkeypatch.setattr(rv, "fetch_parse_attempts", lambda db, invoice_raw_id: ([], None))
    monkeypatch.setattr(rv, "build_extracted_document_profile", lambda inv: {"line_items": []})
    monkeypatch.setattr(rv, "build_extracted_supplier_profile", lambda inv: {})
    monkeypatch.setattr(rv, "build_supplier_create_payload", lambda **_kw: {})
    monkeypatch.setattr(rv, "ensure_org_read", lambda *_args: None)

    result = rv.get_invoice_review_data("raw-1", AUTH)

    assert result["success"] is True
    assert result["resolved_by"] == "invoice_raw_id"
    assert result["invoice_extracted_id"] == "inv-1"


def test_get_invoice_review_data_404_when_not_found(monkeypatch):
    db = StubDB({"invoices_extracted": []})
    monkeypatch.setattr(rv, "supabase", db)

    with pytest.raises(HTTPException) as exc_info:
        rv.get_invoice_review_data("nonexistent", AUTH)

    assert exc_info.value.status_code == 404


def test_get_invoice_review_data_requires_org_read(monkeypatch):
    db = StubDB({
        "invoices_extracted": [_invoice_row()],
        "invoices_raw": [],
        "suppliers": [],
        "supplier_branches": [],
        "document_pages": [],
        "invoice_line_items": [],
        "invoice_audit_events": [],
    })
    monkeypatch.setattr(rv, "supabase", db)
    monkeypatch.setattr(rv, "fetch_parse_attempts", lambda db, invoice_raw_id: ([], None))
    monkeypatch.setattr(rv, "build_extracted_document_profile", lambda inv: {"line_items": []})
    monkeypatch.setattr(rv, "build_extracted_supplier_profile", lambda inv: {})
    monkeypatch.setattr(rv, "build_supplier_create_payload", lambda **_kw: {})
    monkeypatch.setattr(rv, "ensure_org_read", lambda *_args: (_ for _ in ()).throw(HTTPException(status_code=403, detail="denied")))

    with pytest.raises(HTTPException) as exc_info:
        rv.get_invoice_review_data("inv-1", AUTH)

    assert exc_info.value.status_code == 403


# ── get_invoice_agent_review ─────────────────────────────────────────────────

def test_get_invoice_agent_review_returns_suggestions(monkeypatch):
    suggestions = [_suggestion(), _suggestion("sug-2", status="dismissed")]
    db = StubDB({"invoice_agent_suggestions": suggestions})
    monkeypatch.setattr(rv, "supabase", db)
    monkeypatch.setattr(rv, "_fetch_agent_context", lambda _: _fake_context())
    monkeypatch.setattr(rv, "_ensure_agent_read_access", lambda *_: None)

    result = rv.get_invoice_agent_review("inv-1", AUTH)

    assert result["success"] is True
    assert len(result["suggestions"]) == 2
    assert result["summary"]["open"] == 1
    assert result["summary"]["dismissed"] == 1


# ── apply_agent_suggestion ───────────────────────────────────────────────────

def test_apply_agent_suggestion_applies_invoice_patch(monkeypatch):
    sug = _suggestion()
    db = MemoryDB({
        "invoice_agent_suggestions": [sug],
        "invoices_extracted": [_invoice_row()],
        "invoice_audit_events": [],
    })
    monkeypatch.setattr(rv, "supabase", db)
    monkeypatch.setattr(rv, "_get_agent_suggestion_or_404", lambda _: sug)
    monkeypatch.setattr(rv, "_ensure_agent_write_access", lambda *_: None)
    monkeypatch.setattr(rv, "filter_safe_apply_payload", lambda p: {
        "type": "invoice_patch",
        "fields": {"invoice_number": "INV-002"},
        "line_item_id": None,
    })
    monkeypatch.setattr(rv, "log_invoice_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(rv, "evaluate_invoice_readiness", lambda *_a, **_kw: {"ready": True})

    result = rv.apply_agent_suggestion("sug-1", AUTH)

    assert result["success"] is True
    assert result["readiness"] == {"ready": True}


def test_apply_agent_suggestion_409_when_not_open(monkeypatch):
    applied_sug = _suggestion(status="applied")
    monkeypatch.setattr(rv, "supabase", StubDB({}))
    monkeypatch.setattr(rv, "_get_agent_suggestion_or_404", lambda _: applied_sug)
    monkeypatch.setattr(rv, "_ensure_agent_write_access", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        rv.apply_agent_suggestion("sug-1", AUTH)

    assert exc_info.value.status_code == 409


def test_apply_agent_suggestion_404_when_missing(monkeypatch):
    def _not_found(_):
        raise HTTPException(status_code=404, detail="Agent suggestion not found")

    monkeypatch.setattr(rv, "supabase", StubDB({}))
    monkeypatch.setattr(rv, "_get_agent_suggestion_or_404", _not_found)

    with pytest.raises(HTTPException) as exc_info:
        rv.apply_agent_suggestion("nonexistent", AUTH)

    assert exc_info.value.status_code == 404


def test_apply_agent_suggestion_422_when_no_safe_payload(monkeypatch):
    sug = _suggestion()
    monkeypatch.setattr(rv, "supabase", StubDB({}))
    monkeypatch.setattr(rv, "_get_agent_suggestion_or_404", lambda _: sug)
    monkeypatch.setattr(rv, "_ensure_agent_write_access", lambda *_: None)
    monkeypatch.setattr(rv, "filter_safe_apply_payload", lambda _: None)

    with pytest.raises(HTTPException) as exc_info:
        rv.apply_agent_suggestion("sug-1", AUTH)

    assert exc_info.value.status_code == 422


# ── dismiss_agent_suggestion ─────────────────────────────────────────────────

def test_dismiss_agent_suggestion_sets_dismissed(monkeypatch):
    sug = _suggestion()
    db = MemoryDB({"invoice_agent_suggestions": [sug], "invoice_audit_events": []})
    monkeypatch.setattr(rv, "supabase", db)
    monkeypatch.setattr(rv, "_get_agent_suggestion_or_404", lambda _: sug)
    monkeypatch.setattr(rv, "_ensure_agent_write_access", lambda *_: None)
    monkeypatch.setattr(rv, "log_invoice_event", lambda *_a, **_kw: None)

    result = rv.dismiss_agent_suggestion("sug-1", AUTH)

    assert result["success"] is True
    assert result["suggestion"]["status"] == "dismissed"


# ── ignore / undo supplier-comparison-ignores ─────────────────────────────────

def test_ignore_supplier_comparison_field_inserts(monkeypatch):
    db = MemoryDB({"invoice_supplier_comparison_ignores": [], "invoice_audit_events": []})

    # MemoryQuery doesn't support upsert; wrap supabase with a simple adapter
    class _UpsertMemoryQuery:
        def __init__(self, inner):
            self._inner = inner
        def upsert(self, payload, **_kw):
            return self._inner.insert(payload)
        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _UpsertDB:
        def __init__(self, db):
            self._db = db
        def table(self, name):
            q = self._db.table(name)
            return _UpsertMemoryQuery(q)

    wrapped_db = _UpsertDB(db)
    monkeypatch.setattr(rv, "supabase", wrapped_db)
    monkeypatch.setattr(rv, "_fetch_agent_context", lambda _: _fake_context())
    monkeypatch.setattr(rv, "_ensure_agent_write_access", lambda *_: None)
    monkeypatch.setattr(rv, "log_invoice_event", lambda *_a, **_kw: None)

    from app.routers.invoices_review import SupplierComparisonIgnoreRequest

    payload = SupplierComparisonIgnoreRequest(field_key="supplier_name_extracted")
    result = rv.ignore_supplier_comparison_field("inv-1", payload, AUTH)

    assert result["success"] is True
    assert result["ignore"]["field_key"] == "supplier_name_extracted"


def test_ignore_supplier_comparison_field_422_for_invalid_field(monkeypatch):
    monkeypatch.setattr(rv, "supabase", StubDB({}))
    monkeypatch.setattr(rv, "_fetch_agent_context", lambda _: _fake_context())
    monkeypatch.setattr(rv, "_ensure_agent_write_access", lambda *_: None)

    from app.routers.invoices_review import SupplierComparisonIgnoreRequest

    payload = SupplierComparisonIgnoreRequest(field_key="invoice_total")
    with pytest.raises(HTTPException) as exc_info:
        rv.ignore_supplier_comparison_field("inv-1", payload, AUTH)

    assert exc_info.value.status_code == 422


def test_undo_supplier_comparison_ignore_removes(monkeypatch):
    db = MemoryDB({
        "invoice_supplier_comparison_ignores": [
            {"invoice_extracted_id": "inv-1", "organisation_id": "org-1", "field_key": "supplier_name_extracted"},
        ],
        "invoice_audit_events": [],
    })
    monkeypatch.setattr(rv, "supabase", db)
    monkeypatch.setattr(rv, "_fetch_agent_context", lambda _: _fake_context())
    monkeypatch.setattr(rv, "_ensure_agent_write_access", lambda *_: None)
    monkeypatch.setattr(rv, "log_invoice_event", lambda *_a, **_kw: None)

    result = rv.undo_supplier_comparison_ignore("inv-1", "supplier_name_extracted", AUTH)

    assert result["success"] is True


def test_undo_supplier_comparison_ignore_422_for_invalid_field(monkeypatch):
    monkeypatch.setattr(rv, "supabase", StubDB({}))
    monkeypatch.setattr(rv, "_fetch_agent_context", lambda _: _fake_context())
    monkeypatch.setattr(rv, "_ensure_agent_write_access", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        rv.undo_supplier_comparison_ignore("inv-1", "not_a_valid_field", AUTH)

    assert exc_info.value.status_code == 422
