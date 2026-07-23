"""Tests for app/routers/bank_journals.py

Uses monkeypatch to replace _auth (which normally calls bank.svc() for a real
Supabase client) with a MemoryDB, so all DB operations are in-memory.
journal_preview_lines and build_journal_rows_for_line are also monkeypatched
because they depend on additional GL account table lookups.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import HTTPException

import app.routers.bank_journals as bj
from tests.conftest import MemoryDB


# Organisation UUID must match what str(payload.organisation_id) produces
ORG_ID = "00000000-0000-0000-0000-000000000001"
ORG_UUID = UUID(ORG_ID)
GL_UUID = UUID("00000000-0000-0000-0000-000000000002")


def _fake_auth(db):
    """Replace _auth so it returns (user_id, MemoryDB) without hitting Supabase."""
    return lambda _auth_tuple: ("user-1", db)


def _journal(journal_id="journal-1", status="draft", source_id="line-1"):
    return {
        "id": journal_id,
        "organisation_id": ORG_ID,
        "status": status,
        "source_type": "bank_transaction",
        "source_id": source_id,
        "journal_date": "2026-06-01",
        "description": "Test journal",
        "total_debit": 100.0,
        "total_credit": 100.0,
    }


def _line(line_id="line-1", posting_status="unposted"):
    return {
        "id": line_id,
        "organisation_id": ORG_ID,
        "bank_account_id": "ba-1",
        "bank_statement_upload_id": "upload-1",
        "signed_amount": 100.0,
        "description": "Test txn",
        "posting_status": posting_status,
        "gl_journal_id": None,
    }


def _upload(status="extracted"):
    return {
        "id": "upload-1",
        "organisation_id": ORG_ID,
        "bank_account_id": "ba-1",
        "extraction_status": status,
    }


def _gold_file():
    return {
        "id": "gold-1",
        "organisation_id": ORG_ID,
        "document_id": "corrected-statement",
        "gold_json": {
            "_apflow_source_upload_id": "upload-1",
            "transactions": [{"transaction_index": 1}],
        },
    }


def _benchmark_run(can_allocate=False):
    return {
        "organisation_id": ORG_ID,
        "bank_statement_upload_id": "upload-1",
        "document_id": "corrected-statement",
        "can_allocate": can_allocate,
    }


def _draft_payload():
    return bj.DraftJournalRequest(organisation_id=ORG_UUID, gl_account_id=GL_UUID)


def _post_payload():
    return bj.PostJournalRequest(organisation_id=ORG_UUID)


def test_list_posted_bank_lines_replaces_serialized_description_with_reference(monkeypatch):
    serialized = (
        "{'Date': '15 Jul 2026', 'Reference': 'oThongathi TapnGo 485442*5359 13 JUL', "
        "'Source': 'Bank Feed', 'Amount': '(15.50)'}"
    )
    db = MemoryDB({
        "bank_statement_lines": [{
            **_line(posting_status="posted"),
            "line_date": "2026-07-15",
            "description": serialized,
            "reference": "oThongathi TapnGo 485442*5359 13 JUL",
            "counterparty": None,
            "gl_journal_id": "journal-1",
        }],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_read", lambda *_: None)

    result = bj.list_posted_bank_lines("ba-1", ORG_ID, auth=("user-1", None))

    assert result["lines"][0]["description"] == "oThongathi TapnGo 485442*5359 13 JUL"


# ── list_bank_journal_lines ──────────────────────────────────────────────────

def test_list_bank_journal_lines_returns_lines(monkeypatch):
    db = MemoryDB({
        "gl_journals": [_journal()],
        "gl_journal_lines": [
            {"gl_journal_id": "journal-1", "account_id": "acc-1", "debit_amount": 100.0, "credit_amount": 0, "sort_order": 0},
        ],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_read", lambda *_: None)
    monkeypatch.setattr(bj, "journal_preview_lines", lambda _db, _org, rows: rows)

    result = bj.list_bank_journal_lines("journal-1", ORG_ID, auth=("user-1", None))

    assert result["success"] is True
    assert len(result["lines"]) == 1
    assert result["lines"][0]["gl_journal_id"] == "journal-1"


def test_list_bank_journal_lines_404_when_journal_missing(monkeypatch):
    db = MemoryDB({"gl_journals": []})
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_read", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.list_bank_journal_lines("nonexistent", ORG_ID, auth=("user-1", None))

    assert exc_info.value.status_code == 404


# ── draft_bank_journal ───────────────────────────────────────────────────────

def test_draft_bank_journal_creates_journal(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line()],
        "bank_statement_uploads": [_upload()],
        "gl_journals": [],
        "gl_journal_lines": [],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bj, "build_journal_rows_for_line", lambda _db, **_kw: [
        {"account_id": "acc-1", "debit_amount": 100.0, "credit_amount": 0, "sort_order": 0},
        {"account_id": "bank-gl-1", "debit_amount": 0, "credit_amount": 100.0, "sort_order": 1},
    ])
    monkeypatch.setattr(bj, "journal_preview_lines", lambda _db, _org, rows: rows)
    monkeypatch.setattr(bj, "log_bank_event", lambda *_a, **_kw: None)

    result = bj.draft_bank_journal("line-1", _draft_payload(), auth=("user-1", None))

    assert result["success"] is True
    assert "journal" in result
    assert db.tables["gl_journals"], "Journal should have been inserted"
    assert db.tables["gl_journals"][0]["description"] == "Test txn"
    assert db.tables["gl_journal_lines"], "Journal lines should have been inserted"


def test_draft_bank_journal_uses_description_override(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line()],
        "bank_statement_uploads": [_upload()],
        "gl_journals": [],
        "gl_journal_lines": [],
    })
    captured_kwargs = {}

    def fake_build_rows(_db, **kwargs):
        captured_kwargs.update(kwargs)
        return [
            {"account_id": "acc-1", "debit_amount": 100.0, "credit_amount": 0, "sort_order": 0},
            {"account_id": "bank-gl-1", "debit_amount": 0, "credit_amount": 100.0, "sort_order": 1},
        ]

    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bj, "build_journal_rows_for_line", fake_build_rows)
    monkeypatch.setattr(bj, "journal_preview_lines", lambda _db, _org, rows: rows)
    monkeypatch.setattr(bj, "log_bank_event", lambda *_a, **_kw: None)

    payload = bj.DraftJournalRequest(
        organisation_id=ORG_UUID,
        gl_account_id=GL_UUID,
        description_override="Detailed narration",
    )
    result = bj.draft_bank_journal("line-1", payload, auth=("user-1", None))

    assert result["success"] is True
    assert db.tables["gl_journals"][0]["description"] == "Detailed narration"
    assert captured_kwargs["description_override"] == "Detailed narration"


def test_draft_bank_journal_uses_stored_narration(monkeypatch):
    """With no override, the journal header falls back to the line's allocation_narration."""
    db = MemoryDB({
        "bank_statement_lines": [{**_line(), "allocation_narration": "School fees term 1"}],
        "bank_statement_uploads": [_upload()],
        "gl_journals": [],
        "gl_journal_lines": [],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bj, "build_journal_rows_for_line", lambda _db, **_kw: [
        {"account_id": "acc-1", "debit_amount": 100.0, "credit_amount": 0, "sort_order": 0},
        {"account_id": "bank-gl-1", "debit_amount": 0, "credit_amount": 100.0, "sort_order": 1},
    ])
    monkeypatch.setattr(bj, "journal_preview_lines", lambda _db, _org, rows: rows)
    monkeypatch.setattr(bj, "log_bank_event", lambda *_a, **_kw: None)

    bj.draft_bank_journal("line-1", _draft_payload(), auth=("user-1", None))

    assert db.tables["gl_journals"][0]["description"] == "School fees term 1"


def test_draft_bank_journal_returns_existing_draft(monkeypatch):
    """If the line already has a draft journal, return it without recreating."""
    existing_journal = _journal(status="draft")
    existing_line = {**_line(), "posting_status": "draft", "gl_journal_id": "journal-1"}
    db = MemoryDB({
        "bank_statement_lines": [existing_line],
        "bank_statement_uploads": [_upload()],
        "gl_journals": [existing_journal],
        "gl_journal_lines": [
            {"gl_journal_id": "journal-1", "account_id": "acc-1", "debit_amount": 100.0, "credit_amount": 0, "sort_order": 0},
        ],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bj, "journal_preview_lines", lambda _db, _org, rows: rows)

    result = bj.draft_bank_journal("line-1", _draft_payload(), auth=("user-1", None))

    assert result["success"] is True
    assert result["journal"]["id"] == "journal-1"


def test_draft_bank_journal_400_when_posted(monkeypatch):
    posted_line = {**_line(), "posting_status": "posted"}
    db = MemoryDB({
        "bank_statement_lines": [posted_line],
        "bank_statement_uploads": [_upload()],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.draft_bank_journal("line-1", _draft_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "unposted" in exc_info.value.detail.lower()


def test_draft_bank_journal_blocks_bank_control_allocation(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line()],
        "bank_statement_uploads": [_upload()],
        "bank_accounts": [{"id": "ba-2", "organisation_id": ORG_ID, "gl_account_id": str(GL_UUID), "active": True}],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.draft_bank_journal("line-1", _draft_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "bank/cash control account" in exc_info.value.detail


def test_draft_bank_journal_blocks_unapproved_upload(monkeypatch):
    db = MemoryDB({
        "bank_statement_lines": [_line()],
        "bank_statement_uploads": [_upload(status="needs_review")],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.draft_bank_journal("line-1", _draft_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "reviewed and approved" in exc_info.value.detail


def test_draft_bank_journal_blocks_failed_corrected_fixture_benchmark(monkeypatch):
    monkeypatch.setenv("BANK_REQUIRE_GOLD_FIXTURE", "1")  # strict-mode coverage; default is optional/internal
    db = MemoryDB({
        "bank_statement_lines": [_line()],
        "bank_statement_uploads": [_upload()],
        "bank_statement_gold_files": [_gold_file()],
        "bank_statement_extraction_runs": [_benchmark_run(can_allocate=False)],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.draft_bank_journal("line-1", _draft_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "benchmark did not match" in exc_info.value.detail


# ── post_bank_journal ────────────────────────────────────────────────────────

def test_post_bank_journal_marks_journal_posted(monkeypatch):
    db = MemoryDB({
        "gl_journals": [_journal(status="draft")],
        "gl_journal_lines": [
            {"gl_journal_id": "journal-1", "account_id": "acc-1", "tracking": {}, "sort_order": 0},
        ],
        "bank_statement_lines": [_line()],
        "bank_statement_uploads": [_upload()],
        "bank_accounts": [{"id": "ba-1", "organisation_id": ORG_ID, "gl_account_id": "bank-gl-1"}],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(bj, "required_tracking_dimensions", lambda *_a, **_kw: [])
    monkeypatch.setattr(bj, "validate_bank_allocation_tracking", lambda **_kw: None)
    monkeypatch.setattr(bj, "log_bank_event", lambda *_a, **_kw: None)

    result = bj.post_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert result["success"] is True
    updated = db.tables["gl_journals"][0]
    assert updated["status"] == "posted"


def test_post_bank_journal_blocks_unapproved_upload(monkeypatch):
    db = MemoryDB({
        "gl_journals": [_journal(status="draft")],
        "gl_journal_lines": [
            {"gl_journal_id": "journal-1", "account_id": "acc-1", "tracking": {}, "sort_order": 0},
        ],
        "bank_statement_lines": [_line()],
        "bank_statement_uploads": [_upload(status="needs_review")],
        "bank_accounts": [{"id": "ba-1", "organisation_id": ORG_ID, "gl_account_id": "bank-gl-1"}],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.post_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "reviewed and approved" in exc_info.value.detail


def test_post_bank_journal_blocks_failed_corrected_fixture_benchmark(monkeypatch):
    monkeypatch.setenv("BANK_REQUIRE_GOLD_FIXTURE", "1")  # strict-mode coverage; default is optional/internal
    db = MemoryDB({
        "gl_journals": [_journal(status="draft")],
        "gl_journal_lines": [
            {"gl_journal_id": "journal-1", "account_id": "acc-1", "tracking": {}, "sort_order": 0},
        ],
        "bank_statement_lines": [_line()],
        "bank_statement_uploads": [_upload()],
        "bank_statement_gold_files": [_gold_file()],
        "bank_statement_extraction_runs": [_benchmark_run(can_allocate=False)],
        "bank_accounts": [{"id": "ba-1", "organisation_id": ORG_ID, "gl_account_id": "bank-gl-1"}],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.post_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "benchmark did not match" in exc_info.value.detail


def test_post_bank_journal_400_when_not_draft(monkeypatch):
    db = MemoryDB({"gl_journals": [_journal(status="posted")]})
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.post_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "draft" in exc_info.value.detail.lower()


def test_post_bank_journal_400_when_period_locked(monkeypatch):
    db = MemoryDB({
        "gl_journals": [{**_journal(status="draft", source_id=None), "journal_date": "2026-05-31"}],
        "organisation_accounting_periods": [{
            "organisation_id": ORG_ID,
            "status": "locked",
            "lock_date": "2026-05-31",
        }],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.post_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "accounting lock date" in exc_info.value.detail


# ── unpost_bank_journal ──────────────────────────────────────────────────────

def test_unpost_bank_journal_creates_reversal(monkeypatch):
    db = MemoryDB({
        "gl_journals": [_journal(status="posted")],
        "gl_journal_lines": [
            {"gl_journal_id": "journal-1", "account_id": "acc-1", "debit_amount": 100.0, "credit_amount": 0, "sort_order": 0},
        ],
        "bank_statement_lines": [_line(posting_status="posted")],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(
        "app.services.bank.journals.reversal_lines_for_journal",
        lambda lines, description: [
            {**line, "debit_amount": line.get("credit_amount", 0), "credit_amount": line.get("debit_amount", 0)}
            for line in lines
        ],
    )
    monkeypatch.setattr(bj, "journal_preview_lines", lambda _db, _org, rows: rows)
    monkeypatch.setattr(bj, "log_bank_event", lambda *_a, **_kw: None)

    result = bj.unpost_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert result["success"] is True
    assert "reversal_journal" in result
    journals = db.tables["gl_journals"]
    original = next(j for j in journals if j["id"] == "journal-1")
    assert original["status"] == "reversed"


def test_unpost_bank_journal_allows_remediation_after_failed_benchmark(monkeypatch):
    db = MemoryDB({
        "gl_journals": [_journal(status="posted")],
        "gl_journal_lines": [
            {"gl_journal_id": "journal-1", "account_id": "acc-1", "debit_amount": 100.0, "credit_amount": 0, "sort_order": 0},
        ],
        "bank_statement_lines": [_line(posting_status="posted")],
        "bank_statement_uploads": [_upload()],
        "bank_statement_gold_files": [_gold_file()],
        "bank_statement_extraction_runs": [_benchmark_run(can_allocate=False)],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(
        "app.services.bank.journals.reversal_lines_for_journal",
        lambda lines, description: [
            {**line, "debit_amount": line.get("credit_amount", 0), "credit_amount": line.get("debit_amount", 0)}
            for line in lines
        ],
    )
    monkeypatch.setattr(bj, "journal_preview_lines", lambda _db, _org, rows: rows)
    monkeypatch.setattr(bj, "log_bank_event", lambda *_a, **_kw: None)

    result = bj.unpost_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert result["success"] is True
    original = next(j for j in db.tables["gl_journals"] if j["id"] == "journal-1")
    assert original["status"] == "reversed"
    bank_line = db.tables["bank_statement_lines"][0]
    assert bank_line["posting_status"] == "unposted"


def test_unpost_bank_journal_pauses_auto_post(monkeypatch):
    db = MemoryDB({
        "gl_journals": [_journal(status="posted")],
        "gl_journal_lines": [
            {"gl_journal_id": "journal-1", "account_id": "acc-1", "debit_amount": 100.0, "credit_amount": 0, "sort_order": 0},
        ],
        "bank_statement_lines": [_line(posting_status="posted")],
        "bank_accounts": [{"id": "ba-1", "organisation_id": ORG_ID, "auto_post_paused": False}],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)
    monkeypatch.setattr(
        "app.services.bank.journals.reversal_lines_for_journal",
        lambda lines, description: [
            {**line, "debit_amount": line.get("credit_amount", 0), "credit_amount": line.get("debit_amount", 0)}
            for line in lines
        ],
    )
    monkeypatch.setattr(bj, "journal_preview_lines", lambda _db, _org, rows: rows)
    monkeypatch.setattr(bj, "log_bank_event", lambda *_a, **_kw: None)

    bj.unpost_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    account = db.tables["bank_accounts"][0]
    assert account["auto_post_paused"] is True
    assert account["auto_post_paused_reason"] == "manual_unpost"


def test_resume_auto_post_clears_flag(monkeypatch):
    db = MemoryDB({
        "bank_accounts": [{"id": "ba-1", "organisation_id": ORG_ID, "auto_post_paused": True}],
        "bank_statement_lines": [_line(posting_status="unposted")],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)
    calls = {}
    monkeypatch.setattr(
        bj,
        "auto_post_matched_lines",
        lambda _db, **kw: calls.setdefault("kw", kw) or {"posted_count": 0},
    )

    result = bj.resume_account_auto_post("ba-1", _post_payload(), auth=("user-1", None))

    assert result == {"resumed": True, "posted_count": 0}
    assert db.tables["bank_accounts"][0]["auto_post_paused"] is False
    assert calls["kw"]["bank_account_id"] == "ba-1"


def test_unpost_bank_journal_400_when_not_posted(monkeypatch):
    db = MemoryDB({"gl_journals": [_journal(status="draft")]})
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.unpost_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "posted" in exc_info.value.detail.lower()


def test_unpost_bank_journal_400_when_period_locked(monkeypatch):
    db = MemoryDB({
        "gl_journals": [{**_journal(status="posted"), "journal_date": "2026-05-31"}],
        "organisation_accounting_periods": [{
            "organisation_id": ORG_ID,
            "status": "locked",
            "lock_date": "2026-05-31",
        }],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.unpost_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "accounting lock date" in exc_info.value.detail


def test_unpost_bank_journal_400_when_already_reversed(monkeypatch):
    db = MemoryDB({
        "gl_journals": [
            _journal(status="posted"),
            {"id": "reversal-1", "organisation_id": ORG_ID, "reversal_of_journal_id": "journal-1"},
        ],
    })
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.unpost_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "reversed" in exc_info.value.detail.lower()
