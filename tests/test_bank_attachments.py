"""Tests for app/routers/bank_attachments.py

Routes: register / list / delete attachments on a bank statement line.
Auth, storage signed-URL, and audit logging are monkeypatched on the `ba`
module so each test stays focused on routing and DB state.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.bank_attachments as ba
from tests.conftest import MemoryDB


AUTH = ("user-1", None)

ORG_ID = "00000000-0000-0000-0000-000000000001"
ACCOUNT_ID = "00000000-0000-0000-0000-000000000002"
LINE_ID = "00000000-0000-0000-0000-000000000003"


def _line_row(**overrides):
    return {
        "id": LINE_ID,
        "organisation_id": ORG_ID,
        "bank_account_id": ACCOUNT_ID,
        **overrides,
    }


def _patch_common(monkeypatch, db):
    monkeypatch.setattr(ba, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(ba, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(ba, "ensure_org_read", lambda *_: None)
    monkeypatch.setattr(ba, "log_bank_event", lambda *a, **k: None)
    monkeypatch.setattr(ba, "_storage_signed_url", lambda _db, **_kw: ("https://signed/url", None))


def test_create_attachment_registers_row(monkeypatch):
    db = MemoryDB({"bank_statement_lines": [_line_row()], "bank_line_attachments": []})
    _patch_common(monkeypatch, db)

    payload = ba.BankAttachmentCreate(
        organisation_id=ORG_ID,
        storage_path=f"{ORG_ID}/attachments/{LINE_ID}/receipt.pdf",
        original_filename="receipt.pdf",
        mime_type="application/pdf",
        doc_kind="receipt",
    )
    result = ba.create_bank_line_attachment(LINE_ID, payload, AUTH)

    assert result["success"] is True
    assert result["attachment"]["signed_url"] == "https://signed/url"
    assert len(db.tables["bank_line_attachments"]) == 1
    assert db.tables["bank_line_attachments"][0]["doc_kind"] == "receipt"
    assert db.tables["bank_line_attachments"][0]["bank_statement_line_id"] == LINE_ID


def test_create_attachment_coerces_unknown_doc_kind(monkeypatch):
    db = MemoryDB({"bank_statement_lines": [_line_row()], "bank_line_attachments": []})
    _patch_common(monkeypatch, db)

    payload = ba.BankAttachmentCreate(
        organisation_id=ORG_ID,
        storage_path="p/x.pdf",
        doc_kind="totally-made-up",
    )
    ba.create_bank_line_attachment(LINE_ID, payload, AUTH)
    assert db.tables["bank_line_attachments"][0]["doc_kind"] == "other"


def test_create_attachment_404_when_line_missing(monkeypatch):
    db = MemoryDB({"bank_statement_lines": [], "bank_line_attachments": []})
    _patch_common(monkeypatch, db)

    payload = ba.BankAttachmentCreate(organisation_id=ORG_ID, storage_path="p/x.pdf")
    with pytest.raises(HTTPException) as exc_info:
        ba.create_bank_line_attachment(LINE_ID, payload, AUTH)
    assert exc_info.value.status_code == 404


def test_list_attachments_returns_signed_urls(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_line_attachments": [
            {
                "id": "att-1",
                "organisation_id": ORG_ID,
                "bank_statement_line_id": LINE_ID,
                "storage_bucket": "statement-files",
                "storage_path": "p/x.pdf",
                "doc_kind": "note",
            }
        ],
    })
    _patch_common(monkeypatch, db)

    result = ba.list_bank_line_attachments(LINE_ID, ORG_ID, AUTH)
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["signed_url"] == "https://signed/url"


def test_delete_attachment_removes_row(monkeypatch):
    db = MemoryDB({
        "bank_line_attachments": [
            {
                "id": "att-1",
                "organisation_id": ORG_ID,
                "bank_statement_line_id": LINE_ID,
                "storage_bucket": "statement-files",
                "storage_path": "p/x.pdf",
                "doc_kind": "receipt",
            }
        ],
    })
    _patch_common(monkeypatch, db)

    # No db.storage on MemoryDB — the endpoint must tolerate the storage removal
    # failing and still delete the row.
    result = ba.delete_bank_line_attachment("att-1", ORG_ID, AUTH)
    assert result["success"] is True
    assert db.tables["bank_line_attachments"] == []


def test_delete_attachment_404_when_missing(monkeypatch):
    db = MemoryDB({"bank_line_attachments": []})
    _patch_common(monkeypatch, db)

    with pytest.raises(HTTPException) as exc_info:
        ba.delete_bank_line_attachment("nope", ORG_ID, AUTH)
    assert exc_info.value.status_code == 404
