from pathlib import Path

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
                {"id": "upload-1", "organisation_id": "org-1", "bank_account_id": "bank-1", "original_filename": "april.pdf", "uploaded_at": "2026-05-31T12:00:00Z"},
                {"id": "upload-2", "organisation_id": "org-1", "bank_account_id": "bank-1", "original_filename": "may.pdf", "uploaded_at": "2026-05-31T13:00:00Z"},
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


class _Rpc:
    def __init__(self, db, name, params):
        self.db = db
        self.name = name
        self.params = params

    def execute(self):
        self.db.rpc_calls.append((self.name, self.params))
        if self.db.rpc_error:
            raise self.db.rpc_error
        return _Response(self.db.rpc_result)


class _RpcDB:
    def __init__(self, rpc_result=None, rpc_error=None):
        self.rpc_result = rpc_result or []
        self.rpc_error = rpc_error
        self.rpc_calls = []

    def rpc(self, name, params):
        return _Rpc(self, name, params)


def _patch_write_auth(monkeypatch, db):
    monkeypatch.setattr(bank, "_auth", lambda _auth: ("11111111-1111-1111-1111-111111111111", db))
    monkeypatch.setattr(bank, "ensure_org_write", lambda *_args: None)


def test_explicit_bulk_line_delete_route_calls_atomic_rpc(monkeypatch):
    db = _RpcDB(rpc_result=[{"deleted_count": 2}])
    _patch_write_auth(monkeypatch, db)
    payload = bank.BulkDeleteLinesRequest(
        organisation_id="22222222-2222-2222-2222-222222222222",
        line_ids=[
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        ],
    )

    result = bank.bulk_delete_bank_lines(payload, auth=("user", None))

    assert result == {"success": True, "deleted_count": 2}
    assert db.rpc_calls == [(
        "delete_bank_statement_lines_atomic",
        {
            "p_org_id": "22222222-2222-2222-2222-222222222222",
            "p_line_ids": [
                "33333333-3333-3333-3333-333333333333",
                "44444444-4444-4444-4444-444444444444",
            ],
            "p_actor_user_id": "11111111-1111-1111-1111-111111111111",
        },
    )]
    routes = {(route.path, ",".join(sorted(route.methods or []))) for route in bank.router.routes}
    assert ("/api/bank/lines/bulk-delete", "POST") in routes


def test_bulk_line_delete_returns_409_with_blocked_details(monkeypatch):
    db = _RpcDB(
        rpc_error=Exception({
            "message": "Bank statement deletion blocked by posted or reversed journal history",
            "details": '[{"line_id":"33333333-3333-3333-3333-333333333333"}]',
        })
    )
    _patch_write_auth(monkeypatch, db)
    payload = bank.BulkDeleteLinesRequest(
        organisation_id="22222222-2222-2222-2222-222222222222",
        line_ids=["33333333-3333-3333-3333-333333333333"],
    )

    with pytest.raises(HTTPException) as exc:
        bank.bulk_delete_bank_lines(payload, auth=("user", None))

    assert exc.value.status_code == 409
    assert exc.value.detail["blocked"][0]["line_id"] == "33333333-3333-3333-3333-333333333333"


def test_compatibility_delete_line_endpoint_uses_same_atomic_path(monkeypatch):
    db = _RpcDB(rpc_result=[{"deleted_count": 1}])
    _patch_write_auth(monkeypatch, db)
    payload = bank.BulkDeleteLinesRequest(
        organisation_id="22222222-2222-2222-2222-222222222222",
        line_ids=["33333333-3333-3333-3333-333333333333"],
    )

    assert bank.delete_bank_lines(payload, auth=("user", None))["deleted_count"] == 1
    assert db.rpc_calls[0][0] == "delete_bank_statement_lines_atomic"


def test_explicit_bulk_upload_delete_route_calls_atomic_rpc(monkeypatch):
    db = _RpcDB(rpc_result=[{"deleted_count": 2, "files": []}])
    _patch_write_auth(monkeypatch, db)
    payload = bank.BulkDeleteUploadsRequest(
        organisation_id="22222222-2222-2222-2222-222222222222",
        upload_ids=[
            "55555555-5555-5555-5555-555555555555",
            "66666666-6666-6666-6666-666666666666",
        ],
    )

    result = bank.bulk_delete_bank_uploads(payload, auth=("user", None))

    assert result == {
        "success": True,
        "deleted_count": 2,
        "storage_cleanup_failures": [],
    }
    assert db.rpc_calls[0][0] == "delete_bank_statement_uploads_atomic"
    routes = {(route.path, ",".join(sorted(route.methods or []))) for route in bank.router.routes}
    assert ("/api/bank/uploads/bulk-delete", "POST") in routes


def test_c19_migration_contains_atomic_guards_and_draft_cleanup():
    migration = (
            Path(__file__).parents[1]
            / "app"
            / "db"
            / "applied"
            / "bank_deletion_balance_hardening_phase_c19.sql"
        ).read_text(encoding="utf-8")

    assert "delete_bank_statement_lines_atomic" in migration
    assert "delete_bank_statement_uploads_atomic" in migration
    assert "journal.status IN ('posted', 'reversed')" in migration
    assert "journal.status = 'draft'" in migration
    assert "refresh_bank_account_statement_state" in migration
    assert "get_bank_account_balance_summary" in migration
