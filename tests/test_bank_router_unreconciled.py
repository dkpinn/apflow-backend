import pytest
from fastapi import HTTPException

from app.routers import bank


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, rows):
        self.rows = list(rows or [])
        self.filters = []
        self.in_filters = []
        self._limit = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def in_(self, field, values):
        self.in_filters.append((field, set(values)))
        return self

    def limit(self, value):
        self._limit = value
        return self

    def execute(self):
        rows = self.rows
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        for field, values in self.in_filters:
            rows = [row for row in rows if row.get(field) in values]
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Response(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _patch_auth(monkeypatch, db, calls):
    monkeypatch.setattr(bank, "_auth", lambda _auth: ("user-1", db))
    monkeypatch.setattr(bank, "ensure_org_read", lambda user_id, org_id: calls.append((user_id, org_id)))


def test_unreconciled_helper_includes_any_incomplete_status():
    assert not bank.is_unreconciled_bank_line(
        {"posting_status": "posted", "allocation_status": "allocated", "review_status": "reviewed"}
    )
    assert bank.is_unreconciled_bank_line(
        {"posting_status": "draft", "allocation_status": "allocated", "review_status": "reviewed"}
    )
    assert bank.is_unreconciled_bank_line(
        {"posting_status": "posted", "allocation_status": "unallocated", "review_status": "reviewed"}
    )
    assert bank.is_unreconciled_bank_line(
        {"posting_status": "posted", "allocation_status": "allocated", "review_status": "pending"}
    )


def test_account_unreconciled_endpoint_filters_org_account_status_and_enriches_upload(monkeypatch):
    db = _DB(
        {
            "bank_accounts": [
                {"id": "bank-1", "organisation_id": "org-1", "name": "Cheque Account"},
            ],
            "bank_statement_lines": [
                {
                    "id": "posted",
                    "organisation_id": "org-1",
                    "bank_account_id": "bank-1",
                    "bank_statement_upload_id": "upload-1",
                    "line_date": "2024-04-01",
                    "source_row_index": 1,
                    "posting_status": "posted",
                    "allocation_status": "allocated",
                    "review_status": "reviewed",
                },
                {
                    "id": "draft",
                    "organisation_id": "org-1",
                    "bank_account_id": "bank-1",
                    "bank_statement_upload_id": "upload-1",
                    "line_date": "2024-04-01",
                    "source_row_index": 0,
                    "posting_status": "draft",
                    "allocation_status": "allocated",
                    "review_status": "reviewed",
                },
                {
                    "id": "pending",
                    "organisation_id": "org-1",
                    "bank_account_id": "bank-1",
                    "bank_statement_upload_id": "upload-2",
                    "line_date": "2024-04-02",
                    "source_row_index": 0,
                    "posting_status": "unposted",
                    "allocation_status": "unallocated",
                    "review_status": "pending",
                },
                {
                    "id": "other-account",
                    "organisation_id": "org-1",
                    "bank_account_id": "bank-2",
                    "posting_status": "unposted",
                    "allocation_status": "unallocated",
                    "review_status": "pending",
                },
                {
                    "id": "other-org",
                    "organisation_id": "org-2",
                    "bank_account_id": "bank-1",
                    "posting_status": "unposted",
                    "allocation_status": "unallocated",
                    "review_status": "pending",
                },
            ],
            "bank_statement_uploads": [
                {"id": "upload-1", "organisation_id": "org-1", "original_filename": "april.pdf", "uploaded_at": "2026-05-31T12:00:00Z"},
                {"id": "upload-2", "organisation_id": "org-1", "original_filename": "may.pdf", "uploaded_at": "2026-05-31T13:00:00Z"},
            ],
        }
    )
    calls = []
    _patch_auth(monkeypatch, db, calls)

    result = bank.list_bank_account_unreconciled_lines("bank-1", "org-1", auth=("user-1", None))

    assert calls == [("user-1", "org-1")]
    assert result["account"]["name"] == "Cheque Account"
    assert [line["id"] for line in result["lines"]] == ["draft", "pending"]
    assert result["lines"][0]["upload_original_filename"] == "april.pdf"
    assert result["lines"][1]["upload_uploaded_at"] == "2026-05-31T13:00:00Z"


def test_account_unreconciled_endpoint_rejects_account_from_other_org(monkeypatch):
    db = _DB(
        {
            "bank_accounts": [{"id": "bank-1", "organisation_id": "org-2", "name": "Other Org"}],
            "bank_statement_lines": [],
            "bank_statement_uploads": [],
        }
    )
    _patch_auth(monkeypatch, db, [])

    with pytest.raises(HTTPException) as exc:
        bank.list_bank_account_unreconciled_lines("bank-1", "org-1", auth=("user-1", None))

    assert exc.value.status_code == 404
