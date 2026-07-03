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


class _MemoryDBWithSignedUrl(MemoryDB):
    class _Bucket:
        def create_signed_url(self, path, expires_in):
            return {"signedURL": f"https://signed.example/{path}?ttl={expires_in}"}

    class _Storage:
        def from_(self, _bucket):
            return _MemoryDBWithSignedUrl._Bucket()

    storage = _Storage()


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
    val_result = {
        "can_allocate": False,
        "critical_errors": ["PDF/image/VLM bank statement extraction requires manual review before allocation"],
        "closing_balance_passed": True,
        "running_balance_passed": True,
    }

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
    assert upload["extraction_status"] == "needs_review"
    evidence = upload["extraction_evidence"]
    assert evidence["raw_extracted_transaction_count"] == 2
    assert evidence["stored_line_count"] == 1
    assert evidence["nil_line_count"] == 1
    assert evidence["dropped_line_count"] == 1
    snapshot = evidence["review_snapshot"]
    assert snapshot["raw_extracted_transaction_count"] == 2
    assert [line["import_status"] for line in snapshot["lines"]] == ["stored", "dropped_zero_or_opening"]
    assert snapshot["lines"][0]["description"] == "Coffee"
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


def test_get_bank_upload_extraction_review_returns_source_snapshot_and_lines(monkeypatch):
    upload = _reviewable_upload()
    stored_line = {
        "id": "line-1",
        "organisation_id": ORG_ID,
        "bank_statement_upload_id": UPLOAD_ID,
        "line_date": "2024-01-05",
        "description": "Coffee",
    }
    db = _MemoryDBWithSignedUrl({
        "bank_statement_uploads": [upload],
        "bank_accounts": [_account_row()],
        "bank_statement_lines": [stored_line],
    })
    monkeypatch.setattr(bu, "_auth", lambda _: ("reviewer-1", db))
    monkeypatch.setattr(bu, "ensure_org_read", lambda *_: None)

    result = bu.get_bank_upload_extraction_review(UPLOAD_ID, ORG_ID, AUTH)

    assert result["success"] is True
    assert result["upload"]["id"] == UPLOAD_ID
    assert result["account"]["id"] == ACCOUNT_ID
    assert result["source_file"]["signed_url"].startswith("https://signed.example/")
    assert result["review_snapshot"]["lines"][0]["import_status"] == "stored"
    assert result["stored_lines"][0]["description"] == "Coffee"
    assert result["approval_blockers"] == []


def test_get_bank_upload_extraction_review_404_when_upload_missing(monkeypatch):
    db = MemoryDB({
        "bank_statement_uploads": [],
        "bank_accounts": [],
        "bank_statement_lines": [],
    })
    monkeypatch.setattr(bu, "_auth", lambda _: ("reviewer-1", db))
    monkeypatch.setattr(bu, "ensure_org_read", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bu.get_bank_upload_extraction_review(UPLOAD_ID, ORG_ID, AUTH)

    assert exc_info.value.status_code == 404


def test_get_bank_upload_gold_draft_uses_review_snapshot(monkeypatch):
    upload = _reviewable_upload(
        original_filename="Bad ABSA Import.pdf",
        source_format="pdf",
        statement_period_from="2024-01-01",
        statement_period_to="2024-01-31",
        opening_balance=1000.0,
        closing_balance=875.0,
        extraction_evidence={
            "extracted_by": "extractor-1",
            "raw_extracted_transaction_count": 3,
            "validation": {
                "closing_balance_passed": True,
                "running_balance_passed": True,
            },
            "running_balance": {"balance_walk_status": "balanced"},
            "review_snapshot": {
                "lines": [
                    {
                        "row_number": 1,
                        "import_status": "dropped_zero_or_opening",
                        "line_date": "2024-01-01",
                        "description": "Balance brought forward",
                        "signed_amount": 0,
                        "balance_amount": 1000,
                    },
                    {
                        "row_number": 2,
                        "import_status": "stored",
                        "line_date": "2024-01-05",
                        "description": "Coffee",
                        "signed_amount": -50,
                        "debit_amount": 50,
                        "credit_amount": 0,
                        "balance_amount": 950,
                        "source_row_index": 12,
                    },
                    {
                        "row_number": 3,
                        "import_status": "duplicate_filtered",
                        "line_date": "2024-01-06",
                        "description": "Fuel",
                        "signed_amount": -75,
                        "debit_amount": 75,
                        "credit_amount": 0,
                        "balance_amount": 875,
                    },
                ]
            },
        },
    )
    db = MemoryDB({
        "bank_statement_uploads": [upload],
        "bank_accounts": [_account_row(institution_name="ABSA", account_type="current_account")],
    })
    monkeypatch.setattr(bu, "_auth", lambda _: ("reviewer-1", db))
    monkeypatch.setattr(bu, "ensure_org_read", lambda *_: None)

    result = bu.get_bank_upload_gold_draft(UPLOAD_ID, ORG_ID, AUTH)

    draft = result["draft"]
    assert draft["document_id"] == "bad-absa-import"
    assert draft["bank"] == "ABSA"
    assert draft["account_type"] == "current_account"
    assert draft["document_variant"] == "pdf"
    assert draft["statement_start_date"] == "2024-01-01"
    assert draft["statement_end_date"] == "2024-01-31"
    assert draft["opening_balance"] == 1000.0
    assert draft["closing_balance"] == 875.0
    assert draft["needs_manual_correction"] is True
    assert [row["description"] for row in draft["transactions"]] == ["Coffee", "Fuel"]
    assert draft["transactions"][0]["source_reference"] == "row-12"


def test_get_bank_upload_gold_draft_blocks_when_no_transactions(monkeypatch):
    upload = _reviewable_upload(
        extraction_evidence={
            "review_snapshot": {
                "lines": [
                    {
                        "row_number": 1,
                        "import_status": "dropped_zero_or_opening",
                        "signed_amount": 0,
                    }
                ]
            }
        }
    )
    db = MemoryDB({
        "bank_statement_uploads": [upload],
        "bank_accounts": [_account_row()],
    })
    monkeypatch.setattr(bu, "_auth", lambda _: ("reviewer-1", db))
    monkeypatch.setattr(bu, "ensure_org_read", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bu.get_bank_upload_gold_draft(UPLOAD_ID, ORG_ID, AUTH)

    assert exc_info.value.status_code == 400
    assert "No non-zero" in exc_info.value.detail


def test_create_bank_upload_gold_file_saves_corrected_json(monkeypatch):
    gold_json = {
        "document_id": "corrected-statement",
        "bank": "ABSA",
        "account_type": "current_account",
        "document_variant": "corrected_pdf",
        "statement_start_date": "2024-01-01",
        "statement_end_date": "2024-01-31",
        "opening_balance": 1000.0,
        "closing_balance": 950.0,
        "transactions": [
            {
                "transaction_index": 1,
                "date": "2024-01-05",
                "description": "Coffee corrected",
                "amount": -50,
                "running_balance": 950,
            }
        ],
    }
    db = MemoryDB({
        "bank_statement_uploads": [_upload_row(storage_bucket="statement-files", storage_path="org/april.pdf")],
        "bank_accounts": [_account_row(institution_name="ABSA", account_type="bank")],
        "bank_statement_gold_files": [],
    })
    events = []
    monkeypatch.setattr(bu, "_auth", lambda _: ("reviewer-1", db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bu, "log_bank_event", lambda _db, **kw: events.append(kw))
    monkeypatch.setattr(bu, "now_iso", lambda: "2026-07-03T12:00:00+02:00")

    result = bu.create_bank_upload_gold_file(
        UPLOAD_ID,
        bu.CreateGoldFileFromUploadRequest(organisation_id=ORG_ID, gold_json=gold_json),
        AUTH,
    )

    assert result["success"] is True
    gold_file = db.tables["bank_statement_gold_files"][0]
    assert gold_file["organisation_id"] == ORG_ID
    assert gold_file["document_id"] == "corrected-statement"
    assert gold_file["gold_json"] == gold_json
    assert gold_file["gold_pdf_storage_bucket"] == "statement-files"
    assert gold_file["gold_pdf_storage_path"] == "org/april.pdf"
    assert gold_file["verified_by"] == "reviewer-1"
    assert events[0]["event_type"] == "bank_statement_gold_file_created"


def test_create_bank_upload_gold_file_rejects_empty_gold_json(monkeypatch):
    db = MemoryDB({
        "bank_statement_uploads": [_upload_row()],
        "bank_accounts": [_account_row()],
        "bank_statement_gold_files": [],
    })
    monkeypatch.setattr(bu, "_auth", lambda _: ("reviewer-1", db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bu.create_bank_upload_gold_file(
            UPLOAD_ID,
            bu.CreateGoldFileFromUploadRequest(organisation_id=ORG_ID, gold_json={"transactions": []}),
            AUTH,
        )

    assert exc_info.value.status_code == 400
    assert "transactions" in exc_info.value.detail


def _reviewable_upload(**overrides):
    row = {
        "extraction_status": "needs_review",
        "uploaded_by": "uploader-1",
        "balance_status": "balanced",
        "closing_balance": 1200.0,
        "extracted_line_count": 2,
        "duplicate_line_count": 0,
        "duplicate_summary": {"duplicate_line_count": 0},
        "extraction_evidence": {
            "extracted_by": "extractor-1",
            "raw_extracted_transaction_count": 2,
            "validation": {
                "closing_balance_passed": True,
                "running_balance_passed": True,
            },
            "running_balance": {"balance_walk_status": "balanced"},
            "review_snapshot": {
                "lines": [
                    {"row_number": 1, "import_status": "stored"},
                    {"row_number": 2, "import_status": "stored"},
                ]
            },
        },
    }
    row.update(overrides)
    return _upload_row(**row)


def _approval_payload(**overrides):
    payload = {
        "organisation_id": ORG_ID,
        "source_document_checked": True,
        "transaction_count_checked": True,
        "amounts_and_dates_checked": True,
        "balances_checked": True,
        "reviewer_note": "Checked against source PDF",
    }
    payload.update(overrides)
    return bu.ApproveExtractionRequest(**payload)


def test_approve_bank_upload_extraction_records_independent_review(monkeypatch):
    db = MemoryDB({
        "bank_statement_uploads": [_reviewable_upload()],
        "bank_accounts": [_account_row(current_reconciled_balance=1000.0)],
        "bank_audit_events": [],
    })
    events = []
    monkeypatch.setattr(bu, "_auth", lambda _: ("reviewer-1", db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bu, "log_bank_event", lambda _db, **kw: events.append(kw))
    monkeypatch.setattr(bu, "now_iso", lambda: "2026-07-03T12:00:00+02:00")

    result = bu.approve_bank_upload_extraction(UPLOAD_ID, _approval_payload(), AUTH)

    assert result["success"] is True
    upload = db.tables["bank_statement_uploads"][0]
    assert upload["extraction_status"] == "extracted"
    manual_review = upload["extraction_evidence"]["manual_review"]
    assert manual_review["approved_by"] == "reviewer-1"
    assert manual_review["source_document_checked"] is True
    assert manual_review["transaction_count_checked"] is True
    assert manual_review["amounts_and_dates_checked"] is True
    assert manual_review["balances_checked"] is True
    assert manual_review["reviewer_note"] == "Checked against source PDF"
    account = db.tables["bank_accounts"][0]
    assert account["current_reconciled_balance"] == 1200.0
    assert account["last_statement_upload_id"] == UPLOAD_ID
    assert events[0]["event_type"] == "bank_statement_extraction_approved"


def test_approve_bank_upload_extraction_requires_attestation(monkeypatch):
    db = MemoryDB({"bank_statement_uploads": [_reviewable_upload()]})
    monkeypatch.setattr(bu, "_auth", lambda _: ("reviewer-1", db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bu.approve_bank_upload_extraction(
            UPLOAD_ID,
            _approval_payload(transaction_count_checked=False),
            AUTH,
        )

    assert exc_info.value.status_code == 400
    assert "transaction count" in exc_info.value.detail["blockers"][0]
    assert db.tables["bank_statement_uploads"][0]["extraction_status"] == "needs_review"


def test_approve_bank_upload_extraction_blocks_self_approval(monkeypatch):
    db = MemoryDB({"bank_statement_uploads": [_reviewable_upload()]})
    monkeypatch.setattr(bu, "_auth", lambda _: ("extractor-1", db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bu.approve_bank_upload_extraction(UPLOAD_ID, _approval_payload(), AUTH)

    assert exc_info.value.status_code == 400
    assert "different reviewer" in exc_info.value.detail


def test_approve_bank_upload_extraction_requires_complete_review_snapshot(monkeypatch):
    upload = _reviewable_upload(
        extraction_evidence={
            "extracted_by": "extractor-1",
            "raw_extracted_transaction_count": 2,
            "validation": {
                "closing_balance_passed": True,
                "running_balance_passed": True,
            },
            "running_balance": {"balance_walk_status": "balanced"},
            "review_snapshot": {"lines": [{"row_number": 1, "import_status": "stored"}]},
        }
    )
    db = MemoryDB({"bank_statement_uploads": [upload]})
    monkeypatch.setattr(bu, "_auth", lambda _: ("reviewer-1", db))
    monkeypatch.setattr(bu, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bu.approve_bank_upload_extraction(UPLOAD_ID, _approval_payload(), AUTH)

    assert exc_info.value.status_code == 400
    assert "review snapshot" in exc_info.value.detail["blockers"][0]
