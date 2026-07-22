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
SUPPLIER_ID = "00000000-0000-0000-0000-000000000005"
CUSTOMER_ID = "00000000-0000-0000-0000-000000000006"
UPLOAD_ID = "00000000-0000-0000-0000-000000000099"
SUGGESTION_ID = "00000000-0000-0000-0000-000000000007"
INVOICE_ID = "00000000-0000-0000-0000-000000000008"


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


def test_suggest_bank_line_blocks_pdf_without_corrected_fixture(monkeypatch):
    monkeypatch.setenv("BANK_REQUIRE_GOLD_FIXTURE", "1")  # strict-mode coverage; default is optional/internal
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [
            _upload_row(
                source_format="pdf",
                extraction_evidence={"parser_strategy": "pdf_text_blocks"},
            )
        ],
        "bank_statement_gold_files": [],
        "bank_statement_extraction_runs": [],
        "bank_audit_events": [],
        "bank_transaction_suggestions": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ExtractUploadRequest
    with pytest.raises(HTTPException) as exc_info:
        bl.suggest_bank_line(LINE_ID, ExtractUploadRequest(organisation_id=ORG_ID), AUTH)

    assert exc_info.value.status_code == 400
    assert "corrected gold fixture" in exc_info.value.detail


def test_suggest_bank_line_404_when_line_missing(monkeypatch):
    db = MemoryDB({"bank_statement_lines": []})
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ExtractUploadRequest
    with pytest.raises(HTTPException) as exc_info:
        bl.suggest_bank_line("nonexistent", ExtractUploadRequest(organisation_id=ORG_ID), AUTH)

    assert exc_info.value.status_code == 404


# ── suggest_bank_line_ai ──────────────────────────────────────────────────────

def test_suggest_bank_line_ai_inserts_and_reports_available(monkeypatch):
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
    monkeypatch.setattr(
        bl,
        "score_ai_suggestions",
        lambda _db, **_kw: [{"suggestion_type": "ai", "suggested_account_id": "acc-1", "confidence_score": 0.8}],
    )
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")

    from app.routers.bank import ExtractUploadRequest
    result = bl.suggest_bank_line_ai(LINE_ID, ExtractUploadRequest(organisation_id=ORG_ID), AUTH)

    assert result["success"] is True
    assert result["ai_available"] is True
    assert len(result["suggestions"]) == 1
    assert db.tables["bank_transaction_suggestions"][0]["suggestion_type"] == "ai"


def test_suggest_bank_line_ai_reports_unavailable_without_key(monkeypatch):
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
    # Scorer returns nothing (AI unavailable) — endpoint must not raise and must
    # report ai_available=False so the UI can message it cleanly.
    monkeypatch.setattr(bl, "score_ai_suggestions", lambda _db, **_kw: [])
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    from app.routers.bank import ExtractUploadRequest
    result = bl.suggest_bank_line_ai(LINE_ID, ExtractUploadRequest(organisation_id=ORG_ID), AUTH)

    assert result["success"] is True
    assert result["ai_available"] is False
    assert result["suggestions"] == []
    assert len(db.tables["bank_transaction_suggestions"]) == 0


def test_suggest_bank_line_ai_replaces_only_ai_suggestions(monkeypatch):
    # A prior rule suggestion must survive a re-run of AI suggest.
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row()],
        "bank_statement_gold_files": [],
        "bank_statement_extraction_runs": [],
        "bank_audit_events": [],
        "bank_transaction_suggestions": [
            {
                "id": "sug-rule",
                "organisation_id": ORG_ID,
                "bank_statement_line_id": LINE_ID,
                "suggestion_type": "rule",
                "status": "open",
            },
            {
                "id": "sug-ai-old",
                "organisation_id": ORG_ID,
                "bank_statement_line_id": LINE_ID,
                "suggestion_type": "ai",
                "status": "open",
            },
        ],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(
        bl,
        "score_ai_suggestions",
        lambda _db, **_kw: [{"suggestion_type": "ai", "suggested_account_id": "acc-9", "confidence_score": 0.7}],
    )

    from app.routers.bank import ExtractUploadRequest
    bl.suggest_bank_line_ai(LINE_ID, ExtractUploadRequest(organisation_id=ORG_ID), AUTH)

    rows = db.tables["bank_transaction_suggestions"]
    types = sorted(r["suggestion_type"] for r in rows)
    assert types == ["ai", "rule"]  # old ai replaced, rule preserved
    assert any(r.get("suggested_account_id") == "acc-9" for r in rows)


def test_score_ai_suggestions_returns_empty_without_key(monkeypatch):
    from app.services.bank import ai_suggestions

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    result = ai_suggestions.score_ai_suggestions(
        db=None,
        organisation_id=ORG_ID,
        line=_line_row(),
    )
    assert result == []


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


def test_review_bank_line_persists_customer_and_narration(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line_row(supplier_id="old-supplier")],
        "bank_statement_uploads": [_upload_row()],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "log_bank_event", lambda _db, **_kw: None)
    monkeypatch.setattr(bl, "now_iso", lambda: "2024-01-05T12:00:00+00:00")

    from app.routers.bank import ReviewLineRequest
    payload = ReviewLineRequest(
        organisation_id=ORG_ID,
        gl_account_id=GL_ID,
        customer_id=CUSTOMER_ID,
        narration="  School fees term 1  ",
    )
    result = bl.review_bank_line(LINE_ID, payload, AUTH)

    assert result["success"] is True
    line = db.tables["bank_statement_lines"][0]
    assert line["customer_id"] == CUSTOMER_ID
    # Tagging a customer clears any prior supplier tag (one contact only).
    assert line["supplier_id"] is None
    assert line["allocation_narration"] == "School fees term 1"


def test_review_bank_line_blank_narration_stored_as_none(monkeypatch):
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
    bl.review_bank_line(
        LINE_ID, ReviewLineRequest(organisation_id=ORG_ID, narration="   "), AUTH
    )

    assert db.tables["bank_statement_lines"][0]["allocation_narration"] is None


def test_review_bank_line_accepts_supplier_invoice_suggestion(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line_row(signed_amount=-410.55)],
        "bank_statement_uploads": [_upload_row()],
        "bank_transaction_suggestions": [
            {
                "id": SUGGESTION_ID,
                "organisation_id": ORG_ID,
                "bank_statement_line_id": LINE_ID,
                "suggestion_type": "supplier_invoice",
                "matched_invoice_id": INVOICE_ID,
                "matched_invoice_number": "10415",
                "confidence_score": 0.3,
                "status": "open",
            }
        ],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "log_bank_event", lambda _db, **_kw: None)
    monkeypatch.setattr(bl, "now_iso", lambda: "2024-01-05T12:00:00+00:00")

    from app.routers.bank import ReviewLineRequest
    result = bl.review_bank_line(
        LINE_ID,
        ReviewLineRequest(organisation_id=ORG_ID, suggestion_id=SUGGESTION_ID),
        AUTH,
    )

    assert result["success"] is True
    suggestion = db.tables["bank_transaction_suggestions"][0]
    assert suggestion["status"] == "accepted"
    line = db.tables["bank_statement_lines"][0]
    assert line["accepted_suggestion_id"] == SUGGESTION_ID
    assert line["match_status"] == "matched"
    assert line["allocation_status"] == "allocated"
    assert line["review_status"] == "reviewed"


def test_reject_invoice_match_persists_status_and_audit(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_transaction_suggestions": [{
            "id": SUGGESTION_ID,
            "organisation_id": ORG_ID,
            "bank_statement_line_id": LINE_ID,
            "suggestion_type": "supplier_invoice",
            "matched_invoice_id": INVOICE_ID,
            "status": "open",
        }],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ExtractUploadRequest
    result = bl.reject_invoice_match(
        SUGGESTION_ID,
        ExtractUploadRequest(organisation_id=ORG_ID),
        AUTH,
    )

    assert result == {"success": True, "status": "rejected"}
    assert db.tables["bank_transaction_suggestions"][0]["status"] == "rejected"
    assert db.tables["bank_audit_events"][0]["event_type"] == "bank_invoice_match_rejected"


def test_refresh_does_not_resuggest_a_rejected_invoice_pair(monkeypatch):
    rejected = {
        "id": SUGGESTION_ID,
        "organisation_id": ORG_ID,
        "bank_statement_line_id": LINE_ID,
        "suggestion_type": "supplier_invoice",
        "matched_invoice_id": INVOICE_ID,
        "status": "rejected",
    }
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row()],
        "bank_statement_gold_files": [],
        "bank_statement_extraction_runs": [],
        "bank_transaction_suggestions": [rejected],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "score_invoice_suggestions", lambda *_a, **_kw: [{
        "suggestion_type": "supplier_invoice",
        "matched_invoice_id": INVOICE_ID,
        "matched_invoice_number": "INV-580",
        "confidence_score": 0.9,
    }])
    monkeypatch.setattr(bl, "score_rule_suggestions", lambda *_a, **_kw: [])

    from app.routers.bank import ExtractUploadRequest
    result = bl.suggest_bank_line(
        LINE_ID,
        ExtractUploadRequest(organisation_id=ORG_ID),
        AUTH,
    )

    assert result["suggestions"] == []
    assert db.tables["bank_transaction_suggestions"] == [rejected]


def test_invoice_match_preview_returns_captured_invoice_and_lines(monkeypatch):
    db = MemoryDB({
        "bank_transaction_suggestions": [{
            "id": SUGGESTION_ID,
            "organisation_id": ORG_ID,
            "bank_statement_line_id": LINE_ID,
            "suggestion_type": "supplier_invoice",
            "matched_invoice_id": INVOICE_ID,
            "status": "open",
        }],
        "invoices_extracted": [{
            "id": INVOICE_ID,
            "organisation_id": ORG_ID,
            "invoice_raw_id": "raw-1",
            "invoice_number": "INV-580",
            "supplier_name_extracted": "SM Caminsky",
            "invoice_date": "2026-03-01",
            "total_amount": 22000,
        }],
        "invoice_line_items": [{
            "id": "item-1",
            "organisation_id": ORG_ID,
            "invoice_extracted_id": INVOICE_ID,
            "description": "Consulting fees",
            "line_total": 19130.43,
        }],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bl, "ensure_org_read", lambda *_: None)

    result = bl.get_invoice_match_preview(SUGGESTION_ID, ORG_ID, AUTH)

    assert result["invoice"]["invoice_number"] == "INV-580"
    assert result["invoice"]["supplier_name_extracted"] == "SM Caminsky"
    assert result["line_items"][0]["description"] == "Consulting fees"


def test_review_line_request_rejects_supplier_and_customer_together():
    from app.routers.bank import ReviewLineRequest
    with pytest.raises(ValueError):
        ReviewLineRequest(
            organisation_id=ORG_ID,
            supplier_id=SUPPLIER_ID,
            customer_id=CUSTOMER_ID,
        )


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
    monkeypatch.setenv("BANK_REQUIRE_GOLD_FIXTURE", "1")  # strict-mode coverage; default is optional/internal
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


def test_review_bank_line_blocks_stale_benchmark_after_gold_correction(monkeypatch):
    monkeypatch.setenv("BANK_REQUIRE_GOLD_FIXTURE", "1")  # strict-mode coverage; default is optional/internal
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row(extracted_at="2026-07-03T10:00:00+00:00")],
        "bank_statement_gold_files": [
            _gold_file_row(verified_at="2026-07-03T12:00:00+00:00"),
        ],
        "bank_statement_extraction_runs": [
            _benchmark_run_row(can_allocate=True, created_at="2026-07-03T11:00:00+00:00"),
        ],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ReviewLineRequest
    with pytest.raises(HTTPException) as exc_info:
        bl.review_bank_line(LINE_ID, ReviewLineRequest(organisation_id=ORG_ID), AUTH)

    assert exc_info.value.status_code == 400
    assert "stale" in exc_info.value.detail
    assert "latest correction" in exc_info.value.detail


def test_review_bank_line_blocks_stale_benchmark_after_reextraction(monkeypatch):
    monkeypatch.setenv("BANK_REQUIRE_GOLD_FIXTURE", "1")  # strict-mode coverage; default is optional/internal
    db = MemoryDB({
        "bank_statement_lines": [_line_row()],
        "bank_statement_uploads": [_upload_row(extracted_at="2026-07-03T12:00:00+00:00")],
        "bank_statement_gold_files": [
            _gold_file_row(verified_at="2026-07-03T10:00:00+00:00"),
        ],
        "bank_statement_extraction_runs": [
            _benchmark_run_row(can_allocate=True, created_at="2026-07-03T11:00:00+00:00"),
        ],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bl, "_auth", lambda _: ("user-1", db))
    monkeypatch.setattr(bl, "ensure_org_write", lambda *_: None)

    from app.routers.bank import ReviewLineRequest
    with pytest.raises(HTTPException) as exc_info:
        bl.review_bank_line(LINE_ID, ReviewLineRequest(organisation_id=ORG_ID), AUTH)

    assert exc_info.value.status_code == 400
    assert "stale" in exc_info.value.detail
    assert "latest extraction" in exc_info.value.detail


def test_review_bank_line_blocks_failed_fixture_linked_by_audit_event(monkeypatch):
    monkeypatch.setenv("BANK_REQUIRE_GOLD_FIXTURE", "1")  # strict-mode coverage; default is optional/internal
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
