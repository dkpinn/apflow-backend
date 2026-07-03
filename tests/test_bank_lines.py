"""Tests for app/routers/bank_lines.py

Routes: delete/bulk-delete lines, suggest, review, skip, bulk-allocate.

Service functions and RPC helpers are monkeypatched on the `bl` module so each
test stays focused on routing logic and DB state.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routers.bank_lines as bl
from tests.conftest import MemoryDB, StubDB


AUTH = ("user-1", None)

# Valid UUIDs required by Pydantic models with UUID-typed fields
ORG_ID = "00000000-0000-0000-0000-000000000001"
ACCOUNT_ID = "00000000-0000-0000-0000-000000000002"
LINE_ID = "00000000-0000-0000-0000-000000000003"
GL_ID = "00000000-0000-0000-0000-000000000004"
UPLOAD_ID = "00000000-0000-0000-0000-000000000099"


def _line_row(**overrides):
    return {
        "id": LINE_ID,
        "organisation_id": ORG_ID,
        "bank_account_id": ACCOUNT_ID,
        "bank_statement_upload_id": UPLOAD_ID,
        "line_date": "2024-01-05",
        "signed_amount": -120.0,
        "description": "Coffee Shop",
        "review_status": "pending",
        "allocation_status": "unallocated",
        **overrides,
    }


def _upload_row(**overrides):
    return {
        "id": UPLOAD_ID,
        "organisation_id": ORG_ID,
        "bank_account_id": ACCOUNT_ID,
        "extraction_status": "extracted",
        **overrides,
    }


def _gold_file_row(**overrides):
    return {
        "id": "gold-1",
        "organisation_id": ORG_ID,
        "document_id": "corrected-statement",
        "gold_json": {
            "_apflow_source_upload_id": UPLOAD_ID,
            "transactions": [{"transaction_index": 1}],
        },
        **overrides,
    }


def _benchmark_run_row(**overrides):
    return {
        "organisation_id": ORG_ID,
        "bank_statement_upload_id": UPLOAD_ID,
        "document_id": "corrected-statement",
        "can_allocate": False,
        **overrides,
    }


# ── delete_bank_lines ─────────────────────────────────────────────────────────

def test_delete_bank_lines_delegates_to_bulk(monkeypatch):
    db = StubDB({})
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "_delete_bank_lines_rpc", lambda _db, **_kw: {"deleted_count": 3})

    from app.routers.bank import BulkDeleteLinesRequest
    payload = BulkDeleteLinesRequest(organisation_id=ORG_ID, line_ids=[LINE_ID])
    result = bl.delete_bank_lines(payload, AUTH)

    assert result == {"success": True, "deleted_count": 3}


# ── bulk_delete_bank_lines ────────────────────────────────────────────────────

def test_bulk_delete_bank_lines_calls_rpc(monkeypatch):
    db = StubDB({})
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "_delete_bank_lines_rpc", lambda _db, **_kw: {"deleted_count": 2})

    from app.routers.bank import BulkDeleteLinesRequest
    payload = BulkDeleteLinesRequest(organisation_id=ORG_ID, line_ids=[LINE_ID, "00000000-0000-0000-0000-000000000099"])
    result = bl.bulk_delete_bank_lines(payload, AUTH)

    assert result == {"success": True, "deleted_count": 2}


def test_bulk_delete_bank_lines_409_when_blocked(monkeypatch):
    def _blocked(_db, **_kw):
        raise Exception({"message": "Bank statement deletion blocked by posted or reversed journal history", "details": None})

    db = StubDB({})
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "_delete_bank_lines_rpc", _blocked)

    from app.routers.bank import BulkDeleteLinesRequest
    payload = BulkDeleteLinesRequest(organisation_id=ORG_ID, line_ids=[LINE_ID])
    with pytest.raises(HTTPException) as exc_info:
        bl.bulk_delete_bank_lines(payload, AUTH)

    assert exc_info.value.status_code == 409


# ── suggest_bank_line ─────────────────────────────────────────────────────────

def test_suggest_bank_line_inserts_suggestions(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row()],
        "bank_statement_gold_files": [],
        "bank_statement_extraction_runs": [],
        "bank_audit_events": [],
        "bank_transaction_suggestions": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "score_invoice_suggestions", lambda _db, **_kw: [{"suggested_account_id": "acc-1", "confidence_score": 0.9}])
    monkeypatch.setattr(bl, "score_rule_suggestions", lambda _db, **_kw: [])

    from app.routers.bank import ExtractUploadRequest
    result = bl.suggest_bank_line(LINE_ID, ExtractUploadRequest(organisation_id=ORG_ID), AUTH)

    assert result["success"] is True
    assert len(result["suggestions"]) == 1
    assert len(db.tables["bank_transaction_suggestions"]) == 1


def test_suggest_bank_line_no_write_when_no_suggestions(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row()],
        "bank_statement_gold_files": [],
        "bank_statement_extraction_runs": [],
        "bank_audit_events": [],
        "bank_transaction_suggestions": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "score_invoice_suggestions", lambda _db, **_kw: [])
    monkeypatch.setattr(bl, "score_rule_suggestions", lambda _db, **_kw: [])

    from app.routers.bank import ExtractUploadRequest
    result = bl.suggest_bank_line(LINE_ID, ExtractUploadRequest(organisation_id=ORG_ID), AUTH)

    assert result["suggestions"] == []
    assert len(db.tables["bank_transaction_suggestions"]) == 0


def test_suggest_bank_line_blocks_unapproved_upload(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row(extraction_status="needs_review")],
        "bank_transaction_suggestions": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ExtractUploadRequest
    with pytest.raises(HTTPException) as exc_info:
        bl.suggest_bank_line(LINE_ID, ExtractUploadRequest(organisation_id=ORG_ID), AUTH)

    assert exc_info.value.status_code == 400
    assert "reviewed and approved" in exc_info.value.detail


def test_suggest_bank_line_404_when_line_missing(monkeypatch):
    db = MemoryDB({"bank_statement_lines": []})
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ExtractUploadRequest
    with pytest.raises(HTTPException) as exc_info:
        bl.suggest_bank_line("nonexistent", ExtractUploadRequest(organisation_id=ORG_ID), AUTH)

    assert exc_info.value.status_code == 404


# ── review_bank_line ──────────────────────────────────────────────────────────

def test_review_bank_line_sets_reviewed_status(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row()],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "log_bank_event", lambda _db, **_kw: None)
    monkeypatch.setattr(bl, "now_iso", lambda: "2024-01-05T12:00:00+00:00")

    from app.routers.bank import ReviewLineRequest
    result = bl.review_bank_line(LINE_ID, ReviewLineRequest(organisation_id=ORG_ID), AUTH)

    assert result["success"] is True
    line = db.tables["bank_statement_lines"][0]
    assert line["review_status"] == "reviewed"
    assert line["reviewed_by"] == "user-1"


def test_review_bank_line_creates_rule(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row()],
        "bank_transaction_rules": [],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "normalize_rule_criteria", lambda _: [{"field": "description", "value": "Coffee"}])
    monkeypatch.setattr(bl, "log_bank_event", lambda _db, **_kw: None)
    monkeypatch.setattr(bl, "now_iso", lambda: "2024-01-05T12:00:00+00:00")

    from app.routers.bank import ReviewLineRequest
    payload = ReviewLineRequest(
        organisation_id=ORG_ID,
        gl_account_id=GL_ID,
        create_rule=True,
        rule_name="Coffee Rule",
        rule_criteria=[{"field": "description", "value": "Coffee"}],
        criteria_mode="and",
    )
    result = bl.review_bank_line(LINE_ID, payload, AUTH)

    assert result["success"] is True
    assert len(db.tables["bank_transaction_rules"]) == 1
    assert db.tables["bank_transaction_rules"][0]["name"] == "Coffee Rule"


def test_review_bank_line_400_on_invalid_criteria_mode(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row()],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "normalize_rule_criteria", lambda _: [])
    monkeypatch.setattr(bl, "default_rule_criteria_from_line", lambda _: [])

    from app.routers.bank import ReviewLineRequest
    payload = ReviewLineRequest(
        organisation_id=ORG_ID,
        create_rule=True,
        criteria_mode="bad-mode",
    )
    with pytest.raises(HTTPException) as exc_info:
        bl.review_bank_line(LINE_ID, payload, AUTH)

    assert exc_info.value.status_code == 400


def test_review_bank_line_400_when_upload_not_approved(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row(extraction_status="needs_review")],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ReviewLineRequest
    with pytest.raises(HTTPException) as exc_info:
        bl.review_bank_line(LINE_ID, ReviewLineRequest(organisation_id=ORG_ID), AUTH)

    assert exc_info.value.status_code == 400
    assert "reviewed and approved" in exc_info.value.detail


def test_review_bank_line_blocks_failed_corrected_fixture_benchmark(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row()],
        "bank_statement_gold_files": [_gold_file_row()],
        "bank_statement_extraction_runs": [_benchmark_run_row()],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ReviewLineRequest
    with pytest.raises(HTTPException) as exc_info:
        bl.review_bank_line(LINE_ID, ReviewLineRequest(organisation_id=ORG_ID), AUTH)

    assert exc_info.value.status_code == 400
    assert "benchmark did not match" in exc_info.value.detail


def test_review_bank_line_blocks_failed_fixture_linked_by_audit_event(monkeypatch):
    gold_file = _gold_file_row(gold_json={"transactions": [{"transaction_index": 1}]})
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row()],
        "bank_statement_gold_files": [gold_file],
        "bank_statement_extraction_runs": [_benchmark_run_row()],
        "bank_audit_events": [
            {
                "organisation_id": ORG_ID,
                "event_type": "bank_statement_gold_file_created",
                "bank_statement_upload_id": UPLOAD_ID,
                "details": {"gold_file_id": gold_file["id"]},
            }
        ],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ReviewLineRequest
    with pytest.raises(HTTPException) as exc_info:
        bl.review_bank_line(LINE_ID, ReviewLineRequest(organisation_id=ORG_ID), AUTH)

    assert exc_info.value.status_code == 400
    assert "benchmark did not match" in exc_info.value.detail


def test_review_bank_line_404_when_line_missing(monkeypatch):
    db = MemoryDB({"bank_statement_lines": []})
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ReviewLineRequest
    with pytest.raises(HTTPException) as exc_info:
        bl.review_bank_line("nonexistent", ReviewLineRequest(organisation_id=ORG_ID), AUTH)

    assert exc_info.value.status_code == 404


# ── skip_bank_line ────────────────────────────────────────────────────────────

def test_skip_bank_line_logs_deferred_event(monkeypatch):
    db = MemoryDB({"bank_statement_lines": [_line_row()]})
    events = []
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "log_bank_event", lambda _db, **kw: events.append(kw))

    from app.models.schemas import LineSkipRequest
    result = bl.skip_bank_line(LINE_ID, LineSkipRequest(organisation_id=ORG_ID), AUTH)

    assert result == {"success": True}
    assert len(events) == 1
    assert events[0]["event_type"] == "bank_line_deferred"
    assert events[0]["bank_statement_line_id"] == LINE_ID


def test_skip_bank_line_does_not_change_review_status(monkeypatch):
    db = MemoryDB({"bank_statement_lines": [_line_row(review_status="pending")]})
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "log_bank_event", lambda *_a, **_kw: None)

    from app.models.schemas import LineSkipRequest
    bl.skip_bank_line(LINE_ID, LineSkipRequest(organisation_id=ORG_ID), AUTH)

    assert db.tables["bank_statement_lines"][0]["review_status"] == "pending"


def test_skip_bank_line_404_when_missing(monkeypatch):
    db = MemoryDB({"bank_statement_lines": []})
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.models.schemas import LineSkipRequest
    with pytest.raises(HTTPException) as exc_info:
        bl.skip_bank_line("nonexistent", LineSkipRequest(organisation_id=ORG_ID), AUTH)

    assert exc_info.value.status_code == 404


# ── bulk_allocate_bank_lines ──────────────────────────────────────────────────

def test_bulk_allocate_calls_atomic_rpc(monkeypatch):
    rpc_result = {
        "created_count": 1,
        "items": [{"line_id": LINE_ID, "journal_id": "journal-1", "lines": []}],
    }
    db = StubDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row()],
    }, rpc_result=rpc_result)
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.models.schemas import BulkAllocateRequest
    payload = BulkAllocateRequest(
        organisation_id=ORG_ID,
        items=[{
            "line_id": LINE_ID,
            "allocations": [{"account_id": GL_ID, "gross_amount": 120}],
        }],
    )
    result = bl.bulk_allocate_bank_lines(payload, AUTH)

    assert result["success"] is True
    assert result["created_count"] == 1
    assert db.rpc_calls[0][0] == "create_bank_draft_journals_atomic"


def test_bulk_allocate_400_on_rpc_error(monkeypatch):
    class _FailDB:
        def __init__(self):
            self._stub = StubDB({
                "bank_statement_lines": [_line_row()],
                "bank_statement_uploads": [_upload_row()],
            })

        def rpc(self, _name, _params):
            raise Exception({"message": "Allocations do not balance", "details": None})

        def table(self, name):
            return self._stub.table(name)

    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", _FailDB()))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.models.schemas import BulkAllocateRequest
    payload = BulkAllocateRequest(
        organisation_id=ORG_ID,
        items=[{
            "line_id": LINE_ID,
            "allocations": [{"account_id": GL_ID, "gross_amount": 50}],
        }],
    )
    with pytest.raises(HTTPException) as exc_info:
        bl.bulk_allocate_bank_lines(payload, AUTH)

    assert exc_info.value.status_code == 400
    assert "Allocations do not balance" in exc_info.value.detail["message"]
