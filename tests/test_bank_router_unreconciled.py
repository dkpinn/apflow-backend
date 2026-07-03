from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers import bank
from app.routers import bank_lines
from app.routers import bank_uploads


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, rows):
        self.rows = list(rows or [])
        self.filters = []
        self.in_filters = []
        self._limit = None
        self.orders = []

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

    def order(self, field, desc=False):
        self.orders.append((field, desc))
        return self

    def execute(self):
        rows = self.rows
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        for field, values in self.in_filters:
            rows = [row for row in rows if row.get(field) in values]
        for field, desc in reversed(self.orders):
            rows = sorted(rows, key=lambda row: row.get(field) or 0, reverse=desc)
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
    assert not bank.is_unreconciled_bank_line(
        {"posting_status": "posted", "allocation_status": "split", "review_status": "reviewed"}
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
            "bank_transaction_suggestions": [
                {
                    "id": "suggestion-low",
                    "organisation_id": "org-1",
                    "bank_statement_line_id": "pending",
                    "confidence_score": 0.71,
                    "suggested_account_id": "account-low",
                    "suggested_tax_treatment": "blocked",
                    "status": "open",
                },
                {
                    "id": "suggestion-high",
                    "organisation_id": "org-1",
                    "bank_statement_line_id": "pending",
                    "confidence_score": 0.96,
                    "suggested_account_id": "account-high",
                    "suggested_tax_treatment": "full",
                    "status": "open",
                },
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
    assert result["lines"][0]["recon_confidence"] == 0.5
    assert result["lines"][0]["recon_suggested_account_id"] is None
    assert result["lines"][1]["upload_uploaded_at"] == "2026-05-31T13:00:00Z"
    assert result["lines"][1]["recon_confidence"] == 0.96
    assert result["lines"][1]["recon_suggested_account_id"] == "account-high"
    assert result["lines"][1]["recon_suggested_tax"] == "full"


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
    def __init__(self, rpc_result=None, rpc_error=None, tables=None):
        self.rpc_result = rpc_result or []
        self.rpc_error = rpc_error
        self.rpc_calls = []
        self.tables = {
            "organisations": [{
                "id": "22222222-2222-2222-2222-222222222222",
                "vat_registered": True,
                "vat_registration_date": "2026-01-01",
            }],
            "bank_statement_lines": [{
                "id": "33333333-3333-3333-3333-333333333333",
                "organisation_id": "22222222-2222-2222-2222-222222222222",
                "bank_statement_upload_id": "77777777-7777-7777-7777-777777777777",
                "line_date": "2026-06-30",
            }],
            "bank_statement_uploads": [{
                "id": "77777777-7777-7777-7777-777777777777",
                "organisation_id": "22222222-2222-2222-2222-222222222222",
                "extraction_status": "extracted",
            }],
            **(tables or {}),
        }

    def rpc(self, name, params):
        return _Rpc(self, name, params)

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _patch_write_auth(monkeypatch, db):
    _fake_auth = lambda _auth: ("11111111-1111-1111-1111-111111111111", db)
    _noop = lambda *_args: None
    monkeypatch.setattr(bank, "_auth", _fake_auth)
    monkeypatch.setattr(bank, "ensure_org_write", _noop)
    monkeypatch.setattr(bank_lines, "_auth", _fake_auth)
    monkeypatch.setattr(bank_lines, "ensure_org_write", _noop)
    monkeypatch.setattr(bank_uploads, "_auth", _fake_auth)
    monkeypatch.setattr(bank_uploads, "ensure_org_write", _noop)


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

    result = bank_lines.bulk_delete_bank_lines(payload, auth=("user", None))

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
    routes = {(route.path, ",".join(sorted(route.methods or []))) for route in bank_lines.router.routes}
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
        bank_lines.bulk_delete_bank_lines(payload, auth=("user", None))

    assert exc.value.status_code == 409
    assert exc.value.detail["blocked"][0]["line_id"] == "33333333-3333-3333-3333-333333333333"


def test_compatibility_delete_line_endpoint_uses_same_atomic_path(monkeypatch):
    db = _RpcDB(rpc_result=[{"deleted_count": 1}])
    _patch_write_auth(monkeypatch, db)
    payload = bank.BulkDeleteLinesRequest(
        organisation_id="22222222-2222-2222-2222-222222222222",
        line_ids=["33333333-3333-3333-3333-333333333333"],
    )

    assert bank_lines.delete_bank_lines(payload, auth=("user", None))["deleted_count"] == 1
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

    result = bank_uploads.bulk_delete_bank_uploads(payload, auth=("user", None))

    assert result == {
        "success": True,
        "deleted_count": 2,
        "storage_cleanup_failures": [],
    }
    assert db.rpc_calls[0][0] == "delete_bank_statement_uploads_atomic"
    routes = {(route.path, ",".join(sorted(route.methods or []))) for route in bank_uploads.router.routes}
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


def test_latest_statement_summary_rpc_uses_valid_closing_balance():
    repo_root = Path(__file__).parents[1]
    migration_path = repo_root / "app" / "db" / "20260702_bank_balance_summary_latest_valid_statement.sql"
    if not migration_path.exists():
        migration_path = (
            repo_root
            / "app"
            / "db"
            / "applied"
            / "20260702_bank_balance_summary_latest_valid_statement.sql"
        )
    migration = migration_path.read_text(encoding="utf-8")

    assert "u.extraction_status = 'extracted'" in migration
    assert "u.closing_balance IS NOT NULL" in migration
    assert "latest_upload.balance_status = 'balanced'" in migration
    assert "THEN latest_upload.closing_balance" in migration


def test_skip_is_audited_without_changing_review_status(monkeypatch):
    db = _DB({
        "bank_statement_lines": [{
            "id": "33333333-3333-3333-3333-333333333333",
            "organisation_id": "22222222-2222-2222-2222-222222222222",
            "bank_account_id": "55555555-5555-5555-5555-555555555555",
            "bank_statement_upload_id": "66666666-6666-6666-6666-666666666666",
            "review_status": "pending",
        }],
    })
    _patch_write_auth(monkeypatch, db)
    events = []
    monkeypatch.setattr(bank_lines, "log_bank_event", lambda _db, **details: events.append(details))

    result = bank_lines.skip_bank_line(
        "33333333-3333-3333-3333-333333333333",
        bank.LineSkipRequest(organisation_id="22222222-2222-2222-2222-222222222222"),
        auth=("user", None),
    )

    assert result == {"success": True}
    assert db.tables["bank_statement_lines"][0]["review_status"] == "pending"
    assert events[0]["event_type"] == "bank_line_deferred"


def test_bulk_allocate_calls_atomic_draft_rpc_with_split_vat_payload(monkeypatch):
    db = _RpcDB(rpc_result=[{
        "created_count": 1,
        "items": [{"line_id": "33333333-3333-3333-3333-333333333333", "journal_id": "journal-1", "lines": []}],
    }])
    _patch_write_auth(monkeypatch, db)
    payload = bank.BulkAllocateRequest(
        organisation_id="22222222-2222-2222-2222-222222222222",
        items=[{
            "line_id": "33333333-3333-3333-3333-333333333333",
            "allocations": [
                {
                    "account_id": "44444444-4444-4444-4444-444444444444",
                    "gross_amount": 115,
                    "tracking": {},
                    "vat_treatment": "full",
                    "vat_rate": 15,
                },
                {
                    "account_id": "55555555-5555-5555-5555-555555555555",
                    "gross_amount": 25,
                    "tracking": {},
                    "vat_treatment": "blocked",
                },
            ],
        }],
    )

    result = bank_lines.bulk_allocate_bank_lines(payload, auth=("user", None))

    assert result["success"] is True
    assert result["created_count"] == 1
    rpc_name, params = db.rpc_calls[0]
    assert rpc_name == "create_bank_draft_journals_atomic"
    assert params["p_actor_user_id"] == "11111111-1111-1111-1111-111111111111"
    assert params["p_items"][0]["allocations"][0]["vat_treatment"] == "full"
    assert params["p_items"][0]["allocations"][1]["vat_treatment"] == "blocked"


def test_bulk_allocate_surfaces_atomic_rpc_failure(monkeypatch):
    db = _RpcDB(rpc_error=Exception({"message": "Allocations do not balance", "details": None}))
    _patch_write_auth(monkeypatch, db)
    payload = bank.BulkAllocateRequest(
        organisation_id="22222222-2222-2222-2222-222222222222",
        items=[{
            "line_id": "33333333-3333-3333-3333-333333333333",
            "allocations": [{
                "account_id": "44444444-4444-4444-4444-444444444444",
                "gross_amount": 99,
            }],
        }],
    )

    with pytest.raises(HTTPException) as exc:
        bank_lines.bulk_allocate_bank_lines(payload, auth=("user", None))

    assert exc.value.status_code == 400
    assert "Allocations do not balance" in exc.value.detail["message"]


def test_c21_bulk_draft_migration_enforces_atomic_accounting_guards():
    migration = (
        Path(__file__).parents[1]
        / "app"
        / "db"
        / "applied"
        / "bank_bulk_draft_hardening_phase_c21.sql"
    ).read_text(encoding="utf-8")

    assert "create_bank_draft_journals_atomic" in migration
    assert "FOR UPDATE" in migration
    assert "abs(allocation_total - journal_total) > 0.02" in migration
    assert "account.vat_treatment" in migration
    assert "nullif(trim(org.vat_number), '') IS NOT NULL" in migration
    assert "account.system_key = 'vat_control'" in migration
    assert "Generated journal for bank statement line % is not balanced" in migration
    assert "REVOKE ALL ON FUNCTION" in migration
