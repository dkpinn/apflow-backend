"""Tests for app/routers/invoices_queue.py

Uses monkeypatch to replace module-level supabase and service calls so
no real DB connection is needed.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.invoices_queue as q
from tests.conftest import StubDB, MemoryDB

AUTH = ("user-1", None)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _raw_invoice(raw_id="raw-1", org="org-1"):
    return {"id": raw_id, "organisation_id": org, "file_path": "path/to/file.pdf"}


def _allow_auth(monkeypatch):
    monkeypatch.setattr(q, "ensure_org_read", lambda *_args: None)
    monkeypatch.setattr(q, "ensure_org_write", lambda *_args: None)


# ── get_invoice_audit_events ─────────────────────────────────────────────────

def test_get_invoice_audit_events_returns_events(monkeypatch):
    db = StubDB({
        "invoice_audit_events": [
            {"invoice_raw_id": "raw-1", "event_type": "queued"},
            {"invoice_raw_id": "raw-1", "event_type": "completed"},
            {"invoice_raw_id": "raw-2", "event_type": "queued"},  # different invoice
        ],
    })
    monkeypatch.setattr(q, "supabase", db)
    monkeypatch.setattr(q, "get_raw_invoice", lambda raw_id: _raw_invoice(raw_id))
    _allow_auth(monkeypatch)

    result = q.get_invoice_audit_events("raw-1", AUTH)

    assert result["success"] is True
    assert result["invoice_raw_id"] == "raw-1"
    assert result["organisation_id"] == "org-1"
    assert result["event_count"] == 2
    assert all(e["invoice_raw_id"] == "raw-1" for e in result["events"])


def test_get_invoice_audit_events_empty(monkeypatch):
    db = StubDB({"invoice_audit_events": []})
    monkeypatch.setattr(q, "supabase", db)
    monkeypatch.setattr(q, "get_raw_invoice", lambda raw_id: _raw_invoice(raw_id))
    _allow_auth(monkeypatch)

    result = q.get_invoice_audit_events("raw-99", AUTH)

    assert result["event_count"] == 0
    assert result["events"] == []


# ── get_extract_status ───────────────────────────────────────────────────────

def test_get_extract_status_returns_status(monkeypatch):
    job = {"id": "job-1", "status": "completed", "invoice_raw_id": "raw-1", "organisation_id": "org-1"}
    monkeypatch.setattr(q, "get_processing_job", lambda job_id: job)
    monkeypatch.setattr(q, "build_extract_job_status", lambda j: {"job_id": j["id"], "status": j["status"]})
    _allow_auth(monkeypatch)

    result = q.get_extract_status("job-1", AUTH)

    assert result["job_id"] == "job-1"
    assert result["status"] == "completed"


def test_get_extract_status_404_when_missing(monkeypatch):
    monkeypatch.setattr(q, "get_processing_job", lambda job_id: None)

    with pytest.raises(HTTPException) as exc_info:
        q.get_extract_status("nonexistent-job", AUTH)

    assert exc_info.value.status_code == 404


# ── get_re_extract_status ────────────────────────────────────────────────────

def test_get_re_extract_status_returns_status(monkeypatch):
    monkeypatch.setattr(q, "get_reextract_job_status", lambda job_id: {"job_id": job_id, "status": "completed", "organisation_id": "org-1"})
    _allow_auth(monkeypatch)

    result = q.get_re_extract_status("job-1", AUTH)

    assert result["status"] == "completed"


def test_get_re_extract_status_404_when_missing(monkeypatch):
    monkeypatch.setattr(q, "get_reextract_job_status", lambda job_id: None)

    with pytest.raises(HTTPException) as exc_info:
        q.get_re_extract_status("nonexistent", AUTH)

    assert exc_info.value.status_code == 404


# ── queue_invoice ────────────────────────────────────────────────────────────

def test_queue_invoice_returns_job_info(monkeypatch):
    def _fake_queue(*, invoice_raw_id, organisation_id, batch_id, extraction_strategy, priority):
        return {
            "id": "job-99",
            "invoice_raw_id": invoice_raw_id,
            "organisation_id": organisation_id or "org-1",
        }

    monkeypatch.setattr(q, "queue_invoice_job", _fake_queue)
    monkeypatch.setattr(q, "get_raw_invoice", lambda raw_id: _raw_invoice(raw_id))
    _allow_auth(monkeypatch)

    from app.routers.invoices_queue import QueueInvoiceRequest
    payload = QueueInvoiceRequest(invoice_raw_id="raw-1", organisation_id="org-1")
    result = q.queue_invoice(payload, AUTH)

    assert result["success"] is True
    assert result["job_id"] == "job-99"
    assert result["invoice_raw_id"] == "raw-1"


def test_queue_invoice_requires_write_access_before_queueing(monkeypatch):
    queued = []

    monkeypatch.setattr(q, "get_raw_invoice", lambda raw_id: _raw_invoice(raw_id))
    monkeypatch.setattr(q, "ensure_org_write", lambda *_args: (_ for _ in ()).throw(HTTPException(status_code=403, detail="denied")))
    monkeypatch.setattr(q, "queue_invoice_job", lambda **kwargs: queued.append(kwargs))

    from app.routers.invoices_queue import QueueInvoiceRequest
    payload = QueueInvoiceRequest(invoice_raw_id="raw-1", organisation_id="org-1")

    with pytest.raises(HTTPException) as exc_info:
        q.queue_invoice(payload, AUTH)

    assert exc_info.value.status_code == 403
    assert queued == []


# ── extract_invoice ─────────────────────────────────────────────────────────

def test_extract_invoice_requires_write_access_before_queueing(monkeypatch):
    queued = []

    monkeypatch.setattr(q, "get_raw_invoice", lambda raw_id: _raw_invoice(raw_id))
    monkeypatch.setattr(q, "ensure_org_write", lambda *_args: (_ for _ in ()).throw(HTTPException(status_code=403, detail="denied")))
    monkeypatch.setattr(q, "queue_invoice_job", lambda **kwargs: queued.append(kwargs))

    from app.routers.invoices_queue import ExtractInvoiceRequest
    from fastapi import BackgroundTasks

    payload = ExtractInvoiceRequest(invoice_raw_id="raw-1", organisation_id="org-1")

    with pytest.raises(HTTPException) as exc_info:
        q.extract_invoice(payload, BackgroundTasks(), AUTH)

    assert exc_info.value.status_code == 403
    assert queued == []


# ── re_extract_invoice (sync path) ──────────────────────────────────────────

def test_re_extract_invoice_sync_calls_service(monkeypatch):
    called = {}

    def _fake_reextract(*, invoice_raw_id, organisation_id, force_update):
        called.update(invoice_raw_id=invoice_raw_id, force_update=force_update)
        return {"success": True}

    monkeypatch.setattr(q, "run_invoice_re_extraction", _fake_reextract)
    monkeypatch.setattr(q, "get_raw_invoice", lambda raw_id: _raw_invoice(raw_id))
    _allow_auth(monkeypatch)

    from app.routers.invoices_queue import ReExtractInvoiceRequest
    from fastapi import BackgroundTasks

    payload = ReExtractInvoiceRequest(invoice_raw_id="raw-1", organisation_id="org-1")
    result = q.re_extract_invoice(payload, BackgroundTasks(), AUTH, sync=True)

    assert result == {"success": True}
    assert called["invoice_raw_id"] == "raw-1"
    assert called["force_update"] is False


def test_re_extract_invoice_requires_file_path(monkeypatch):
    """Queued re-extract should 400 if the raw row has no file_path."""
    monkeypatch.setattr(q, "get_raw_invoice", lambda raw_id: {"id": raw_id, "organisation_id": "org-1", "file_path": None})
    monkeypatch.setattr(q, "log_reextract_failure", lambda **_kw: None)
    _allow_auth(monkeypatch)

    from app.routers.invoices_queue import ReExtractInvoiceRequest
    from fastapi import BackgroundTasks

    payload = ReExtractInvoiceRequest(invoice_raw_id="raw-1", organisation_id="org-1")

    with pytest.raises(HTTPException) as exc_info:
        q.re_extract_invoice(payload, BackgroundTasks(), AUTH, sync=False)

    assert exc_info.value.status_code == 400
    assert "file_path" in exc_info.value.detail
