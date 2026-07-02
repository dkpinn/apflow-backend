"""Tests for app/routers/bank_uploads.py

extract_bank_upload is the most complex route in the project (VLM extraction,
duplicate detection, balance validation).  Service functions are monkeypatched
on the `bu` module so each test stays focused on routing/DB logic.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.bank_uploads as bu
from tests.conftest import MemoryDB, StubDB


AUTH = ("user-1", None)

# UUID constants — must be valid UUIDs for Pydantic models that type fields as UUID
ORG_ID = "00000000-0000-0000-0000-000000000001"
ACCOUNT_ID = "00000000-0000-0000-0000-000000000002"
UPLOAD_ID = "00000000-0000-0000-0000-000000000003"


# ── DB helper — adds .storage + no-op neq() to MemoryDB ──────────────────────

class _MemoryDBWithStorage:
    """MemoryDB wrapper that adds .storage access and a no-op .neq() on queries.

    The no-op neq() is safe because the eq("file_sha256", hash) filter already
    returns nothing when the fixture upload has no file_sha256 set.
    """

    class _Bucket:
        def download(self, _path):
            return b"fake-pdf-bytes"
        def remove(self, _paths):
            return None

    class _Storage:
        def from_(self, _bucket):
            return _MemoryDBWithStorage._Bucket()

    storage = _Storage()

    def __init__(self, tables):
        self._mem = MemoryDB(tables)
        self.tables = self._mem.tables

    def table(self, name):
        q = self._mem.table(name)
        q.neq = lambda key, value: q
        return q


def _upload_row(**overrides):
    return {
        "id": UPLOAD_ID,
        "organisation_id": ORG_ID,
        "bank_account_id": ACCOUNT_ID,
        "original_filename": "april.pdf",
        "storage_bucket": "statement-files",
        "storage_path": "org/april.pdf",
        "mime_type": "application/pdf",
        **overrides,
    }


def _account_row(**overrides):
    return {
        "id": ACCOUNT_ID,
        "organisation_id": ORG_ID,
        "name": "Cheque",
        "gl_account_id": "00000000-0000-0000-0000-000000000010",
        "institution_name": "ABSA",
        "account_type": "bank",
        "currency": "ZAR",
        **overrides,
    }


# ── list_bank_uploads ─────────────────────────────────────────────────────────

def test_list_bank_uploads_returns_all_for_org(monkeypatch):
    uploads = [_upload_row(), {**_upload_row(), "id": "00000000-0000-0000-0000-000000000099"}]
    db = StubDB({"bank_statement_uploads": uploads})
    monkeypatch.setattr(bu, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bu, "ensure_org_read", lambda *_: None)

    result = bu.list_bank_uploads(AUTH, organisation_id=ORG_ID)

    assert result["success"] is True
    assert len(result["uploads"]) == 2


def test_list_bank_uploads_filters_by_bank_account(monkeypatch):
    uploads = [
        _upload_row(),
        {**_upload_row(), "id": "00000000-0000-0000-0000-000000000099", "bank_account_id": "other"},
    ]
    db = StubDB({"bank_statement_uploads": uploads})
    monkeypatch.setattr(bu, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bu, "ensure_org_read", lambda *_: None)

    result = bu.list_bank_uploads(AUTH, organisation_id=ORG_ID, bank_account_id=ACCOUNT_ID)

    assert len(result["uploads"]) == 1
    assert result["uploads"][0]["id"] == UPLOAD_ID


# ── create_bank_upload ────────────────────────────────────────────────────────

def test_create_bank_upload_inserts_row(monkeypatch):
    db = MemoryDB({
        "bank_accounts": [_account_row()],
        "bank_statement_uploads": [],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bu, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bu, "log_bank_event", lambda _db, **_kw: None)

    from app.routers.bank import BankUploadCreate
    payload = BankUploadCreate(
        organisation_id=ORG_ID,
        bank_account_id=ACCOUNT_ID,
        original_filename="april.pdf",
        storage_path="org/april.pdf",
    )
    result = bu.create_bank_upload(payload, AUTH)

    assert result["success"] is True
    assert result["upload"]["original_filename"] == "april.pdf"
    assert len(db.tables["bank_statement_uploads"]) == 1


def test_create_bank_upload_404_when_account_missing(monkeypatch):
    db = MemoryDB({"bank_accounts": [], "bank_statement_uploads": []})
    monkeypatch.setattr(bu, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)

    from app.routers.bank import BankUploadCreate
    payload = BankUploadCreate(
        organisation_id=ORG_ID,
        bank_account_id=ACCOUNT_ID,
        original_filename="april.pdf",
        storage_path="org/april.pdf",
    )
    with pytest.raises(HTTPException) as exc_info:
        bu.create_bank_upload(payload, AUTH)

    assert exc_info.value.status_code == 404


# ── extract_bank_upload ───────────────────────────────────────────────────────

def _patch_extract_services(monkeypatch, fresh_db):
    """Monkeypatch all heavy services called inside extract_bank_upload."""
    header = {
        "statement_period_from": "2024-01-01",
        "statement_period_to": "2024-01-31",
        "opening_balance": 1000.0,
        "closing_balance": 1200.0,
        "confidence_score": 0.95,
        "extraction_model": "gemini-2.5-flash",
        "extraction_input_tokens": 500,
        "extraction_output_tokens": 100,
        "extractor": "vlm",
        "extractor_type": "bank_statement",
        "extractor_version": "v1",
        "source_format": "pdf",
        "parser_strategy": "structured",
    }
    fake_line = {"line_date": "2024-01-05", "signed_amount": -50.0, "description": "Coffee"}
    zero_line = {"line_date": "2024-01-01", "signed_amount": 0.0, "description": "Balance brought forward"}
    wrapped_lines = [
        {"line": fake_line, "duplicate_status": "clear"},
        {"line": zero_line, "duplicate_status": "clear"},
    ]
    dup_summary = {"duplicate_status": "clear", "duplicate_line_count": 0}
    bal_summary = {"balance_status": "balanced"}
    val_result = {"can_allocate": True}

    monkeypatch.setattr(bu, "file_sha256", lambda _bytes: "fake-sha256")
    monkeypatch.setattr(bu, "lookup_parsing_hint", lambda *_a, **_kw: None)
    monkeypatch.setattr(bu, "extract_statement", lambda *_a, **_kw: (header, [fake_line, zero_line]))
    monkeypatch.setattr(bu, "get_fresh_supabase_client", lambda: fresh_db)
    monkeypatch.setattr(bu, "detect_line_duplicates", lambda **_kw: (wrapped_lines, dup_summary))
    monkeypatch.setattr(bu, "validate_balances", lambda **_kw: bal_summary)
    monkeypatch.setattr(bu, "validate_extracted_statement_quality", lambda **_kw: val_result)
    monkeypatch.setattr(bu, "line_to_insert", lambda _line, **_kw: {"description": "Coffee"})
    monkeypatch.setattr(bu, "_calc_bank_cost", lambda *_a: 0.001)
    monkeypatch.setattr(bu, "log_bank_event", lambda _db, **_kw: None)


def test_extract_bank_upload_happy_path(monkeypatch):
    initial_db = _MemoryDBWithStorage({
        "bank_statement_uploads": [_upload_row()],
        "bank_accounts": [_account_row()],
    })
    fresh_db = MemoryDB({
        "bank_statement_uploads": [_upload_row()],
        "bank_accounts": [_account_row()],
        "bank_statement_lines": [],
    })
    _patch_extract_services(monkeypatch, fresh_db)
    monkeypatch.setattr(bu, "_auth", lambda _: ("user-1", initial_db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ExtractUploadRequest
    result = bu.extract_bank_upload(
        UPLOAD_ID,
        ExtractUploadRequest(organisation_id=ORG_ID),
        AUTH,
    )

    assert result["success"] is True
    assert result["line_count"] == 1
    assert result["balance_summary"]["balance_status"] == "balanced"
    assert len(fresh_db.tables["bank_statement_lines"]) == 1
    upload = fresh_db.tables["bank_statement_uploads"][0]
    evidence = upload["extraction_evidence"]
    assert evidence["raw_extracted_transaction_count"] == 2
    assert evidence["stored_line_count"] == 1
    assert evidence["nil_line_count"] == 1
    assert evidence["dropped_line_count"] == 1
    assert evidence["amount_correction"]["status"] == "skipped_insufficient_balance_data"


def test_extract_bank_upload_500_on_extract_error(monkeypatch):
    initial_db = _MemoryDBWithStorage({
        "bank_statement_uploads": [_upload_row()],
        "bank_accounts": [_account_row()],
    })
    monkeypatch.setattr(bu, "_auth", lambda _: ("user-1", initial_db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bu, "file_sha256", lambda _: "hash")
    monkeypatch.setattr(bu, "lookup_parsing_hint", lambda *_a, **_kw: None)
    monkeypatch.setattr(bu, "extract_statement", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("VLM timeout")))
    monkeypatch.setattr(bu, "log_bank_event", lambda _db, **_kw: None)

    from app.routers.bank import ExtractUploadRequest
    with pytest.raises(HTTPException) as exc_info:
        bu.extract_bank_upload(
            UPLOAD_ID,
            ExtractUploadRequest(organisation_id=ORG_ID),
            AUTH,
        )

    assert exc_info.value.status_code == 500
    upload = initial_db.tables["bank_statement_uploads"][0]
    assert upload["extraction_status"] == "failed"


def test_extract_bank_upload_404_when_upload_missing(monkeypatch):
    db = _MemoryDBWithStorage({"bank_statement_uploads": [], "bank_accounts": []})
    monkeypatch.setattr(bu, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ExtractUploadRequest
    with pytest.raises(HTTPException) as exc_info:
        bu.extract_bank_upload(
            "nonexistent",
            ExtractUploadRequest(organisation_id=ORG_ID),
            AUTH,
        )

    assert exc_info.value.status_code == 404


# ── delete_bank_upload ────────────────────────────────────────────────────────

def test_delete_bank_upload_calls_rpc(monkeypatch):
    db = StubDB({})
    monkeypatch.setattr(bu, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bu, "_delete_bank_uploads_rpc", lambda _db, **_kw: {"deleted_count": 1, "files": []})
    monkeypatch.setattr(bu, "_remove_bank_upload_files", lambda _db, _files: [])

    result = bu.delete_bank_upload(UPLOAD_ID, ORG_ID, AUTH)

    assert result["success"] is True
    assert result["deleted_count"] == 1


def test_delete_bank_upload_409_when_blocked(monkeypatch):
    def _raise_blocked(_db, **_kw):
        raise Exception({"message": "Bank statement deletion blocked by posted or reversed journal history", "details": None})

    db = StubDB({})
    monkeypatch.setattr(bu, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bu, "_delete_bank_uploads_rpc", _raise_blocked)

    with pytest.raises(HTTPException) as exc_info:
        bu.delete_bank_upload(UPLOAD_ID, ORG_ID, AUTH)

    assert exc_info.value.status_code == 409


# ── bulk_delete_bank_uploads ──────────────────────────────────────────────────

def test_bulk_delete_bank_uploads_calls_rpc(monkeypatch):
    db = StubDB({})
    monkeypatch.setattr(bu, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bu, "_delete_bank_uploads_rpc", lambda _db, **_kw: {"deleted_count": 2, "files": []})
    monkeypatch.setattr(bu, "_remove_bank_upload_files", lambda _db, _files: [])

    from app.routers.bank import BulkDeleteUploadsRequest
    payload = BulkDeleteUploadsRequest(
        organisation_id=ORG_ID,
        upload_ids=[UPLOAD_ID, "00000000-0000-0000-0000-000000000099"],
    )
    result = bu.bulk_delete_bank_uploads(payload, AUTH)

    assert result["success"] is True
    assert result["deleted_count"] == 2
    assert result["storage_cleanup_failures"] == []


# ── list_bank_lines ───────────────────────────────────────────────────────────

def test_list_bank_lines_returns_lines_for_upload(monkeypatch):
    lines = [
        {"id": "line-1", "organisation_id": ORG_ID, "bank_statement_upload_id": UPLOAD_ID, "line_date": "2024-01-05"},
        {"id": "line-2", "organisation_id": ORG_ID, "bank_statement_upload_id": UPLOAD_ID, "line_date": "2024-01-06"},
        {"id": "line-3", "organisation_id": ORG_ID, "bank_statement_upload_id": "other-upload", "line_date": "2024-01-07"},
    ]
    db = StubDB({"bank_statement_lines": lines})
    monkeypatch.setattr(bu, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bu, "ensure_org_read", lambda *_: None)

    result = bu.list_bank_lines(UPLOAD_ID, ORG_ID, AUTH)

    assert result["success"] is True
    assert len(result["lines"]) == 2


# ── get_bank_upload_audit_trail ───────────────────────────────────────────────

def test_get_bank_upload_audit_trail_returns_upload_and_events(monkeypatch):
    events = [
        {"id": "e-1", "organisation_id": ORG_ID, "bank_statement_upload_id": UPLOAD_ID, "event_type": "bank_statement_extracted"},
    ]
    db = StubDB({
        "bank_statement_uploads": [_upload_row()],
        "bank_audit_events": events,
    })
    monkeypatch.setattr(bu, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bu, "ensure_org_read", lambda *_: None)

    result = bu.get_bank_upload_audit_trail(UPLOAD_ID, ORG_ID, AUTH)

    assert result["success"] is True
    assert result["upload"]["id"] == UPLOAD_ID
    assert len(result["events"]) == 1
    assert result["events"][0]["event_type"] == "bank_statement_extracted"
