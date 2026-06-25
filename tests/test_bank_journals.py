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


def _draft_payload():
    return bj.DraftJournalRequest(organisation_id=ORG_UUID, gl_account_id=GL_UUID)


def _post_payload():
    return bj.PostJournalRequest(organisation_id=ORG_UUID)


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


def test_draft_bank_journal_returns_existing_draft(monkeypatch):
    """If the line already has a draft journal, return it without recreating."""
    existing_journal = _journal(status="draft")
    existing_line = {**_line(), "posting_status": "draft", "gl_journal_id": "journal-1"}
    db = MemoryDB({
        "bank_statement_lines": [existing_line],
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
    db = MemoryDB({"bank_statement_lines": [posted_line]})
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.draft_bank_journal("line-1", _draft_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "unposted" in exc_info.value.detail.lower()


# ── post_bank_journal ────────────────────────────────────────────────────────

def test_post_bank_journal_marks_journal_posted(monkeypatch):
    db = MemoryDB({
        "gl_journals": [_journal(status="draft")],
        "gl_journal_lines": [
            {"gl_journal_id": "journal-1", "account_id": "acc-1", "tracking": {}, "sort_order": 0},
        ],
        "bank_statement_lines": [_line()],
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


def test_post_bank_journal_400_when_not_draft(monkeypatch):
    db = MemoryDB({"gl_journals": [_journal(status="posted")]})
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.post_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "draft" in exc_info.value.detail.lower()


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
    monkeypatch.setattr(bj, "reversal_lines_for_journal", lambda lines, description: [
        {**line, "debit_amount": line.get("credit_amount", 0), "credit_amount": line.get("debit_amount", 0)}
        for line in lines
    ])
    monkeypatch.setattr(bj, "journal_preview_lines", lambda _db, _org, rows: rows)
    monkeypatch.setattr(bj, "log_bank_event", lambda *_a, **_kw: None)

    result = bj.unpost_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert result["success"] is True
    assert "reversal_journal" in result
    journals = db.tables["gl_journals"]
    original = next(j for j in journals if j["id"] == "journal-1")
    assert original["status"] == "reversed"


def test_unpost_bank_journal_400_when_not_posted(monkeypatch):
    db = MemoryDB({"gl_journals": [_journal(status="draft")]})
    monkeypatch.setattr(bj, "_auth", _fake_auth(db))
    monkeypatch.setattr(bj, "ensure_org_write", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        bj.unpost_bank_journal("journal-1", _post_payload(), auth=("user-1", None))

    assert exc_info.value.status_code == 400
    assert "posted" in exc_info.value.detail.lower()


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
