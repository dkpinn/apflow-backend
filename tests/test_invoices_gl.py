"""Tests for app/routers/invoices_gl.py

The single route (post_invoice_to_gl) delegates heavy work to invoice_gl_posting
service functions that are lazily imported inside the function body. Monkeypatching
the service module attributes intercepts them before they run.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.invoices_gl as gl
import app.services.invoice_gl_posting as gl_svc
from tests.conftest import StubDB


AUTH = ("user-1", None)


@pytest.fixture(autouse=True)
def _ready_for_posting(monkeypatch):
    monkeypatch.setattr(gl, "evaluate_invoice_readiness", lambda *_a, **_kw: {"ready": True, "blockers": []})


def _prepared(gross_total=500.0):
    return {
        "gross_total": gross_total,
        "journal_lines": [{"account_id": "acc-1", "debit_amount": gross_total, "credit_amount": 0}],
    }


def _payload():
    from app.routers.invoices_gl import PostInvoiceToGLRequest
    return PostInvoiceToGLRequest(organisation_id="org-1")


# ── Happy path ───────────────────────────────────────────────────────────────

def test_post_invoice_to_gl_happy_path(monkeypatch):
    monkeypatch.setattr(gl, "supabase", StubDB({}))
    monkeypatch.setattr(gl, "_handle_invoice_approval_workflow", lambda **_kw: None)
    monkeypatch.setattr(gl, "_enforce_user_limits", lambda **_kw: None)
    monkeypatch.setattr(gl_svc, "prepare_invoice_gl_posting", lambda db, **_kw: _prepared())
    monkeypatch.setattr(gl_svc, "post_invoice_to_gl_service", lambda db, **_kw: {"success": True, "journal_id": "jnl-1"})

    result = gl.post_invoice_to_gl("inv-1", _payload(), auth=AUTH)

    assert result["success"] is True
    assert result["journal_id"] == "jnl-1"


# ── Approval workflow ────────────────────────────────────────────────────────

def test_post_invoice_to_gl_returns_pending_when_approval_required(monkeypatch):
    pending = {"success": True, "status": "pending_approval", "approval_request_id": "req-1", "message": "Submitted."}

    monkeypatch.setattr(gl, "supabase", StubDB({}))
    monkeypatch.setattr(gl, "_handle_invoice_approval_workflow", lambda **_kw: pending)
    monkeypatch.setattr(gl_svc, "prepare_invoice_gl_posting", lambda db, **_kw: _prepared())

    result = gl.post_invoice_to_gl("inv-1", _payload(), auth=AUTH)

    assert result["status"] == "pending_approval"
    assert result["approval_request_id"] == "req-1"


# ── Validation errors ────────────────────────────────────────────────────────

def test_post_invoice_to_gl_400_on_prepare_error(monkeypatch):
    def _fail_prepare(db, **_kw):
        raise ValueError("Invoice already posted")

    monkeypatch.setattr(gl, "supabase", StubDB({}))
    monkeypatch.setattr(gl_svc, "prepare_invoice_gl_posting", _fail_prepare)

    with pytest.raises(HTTPException) as exc_info:
        gl.post_invoice_to_gl("inv-1", _payload(), auth=AUTH)

    assert exc_info.value.status_code == 400
    assert "already posted" in exc_info.value.detail


def test_post_invoice_to_gl_blocks_failed_readiness(monkeypatch):
    monkeypatch.setattr(gl, "supabase", StubDB({}))
    monkeypatch.setattr(gl, "evaluate_invoice_readiness", lambda *_a, **_kw: {
        "ready": False,
        "blockers": [{"message": "Invoice bank name differs from supplier master."}],
    })

    with pytest.raises(HTTPException) as exc_info:
        gl.post_invoice_to_gl("inv-1", _payload(), auth=AUTH)

    assert exc_info.value.status_code == 400
    assert "bank name differs" in exc_info.value.detail


def test_post_invoice_to_gl_400_on_post_error(monkeypatch):
    def _fail_post(db, **_kw):
        raise ValueError("Duplicate journal")

    monkeypatch.setattr(gl, "supabase", StubDB({}))
    monkeypatch.setattr(gl, "_handle_invoice_approval_workflow", lambda **_kw: None)
    monkeypatch.setattr(gl, "_enforce_user_limits", lambda **_kw: None)
    monkeypatch.setattr(gl_svc, "prepare_invoice_gl_posting", lambda db, **_kw: _prepared())
    monkeypatch.setattr(gl_svc, "post_invoice_to_gl_service", _fail_post)

    with pytest.raises(HTTPException) as exc_info:
        gl.post_invoice_to_gl("inv-1", _payload(), auth=AUTH)

    assert exc_info.value.status_code == 400
    assert "Duplicate" in exc_info.value.detail


def test_post_invoice_to_gl_500_when_supabase_not_configured(monkeypatch):
    monkeypatch.setattr(gl, "supabase", None)

    with pytest.raises(HTTPException) as exc_info:
        gl.post_invoice_to_gl("inv-1", _payload(), auth=AUTH)

    assert exc_info.value.status_code == 500
