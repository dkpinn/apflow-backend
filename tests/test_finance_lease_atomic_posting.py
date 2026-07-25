from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers import finance_leases


ORG_ID = "11111111-1111-1111-1111-111111111111"
LEASE_ID = "22222222-2222-2222-2222-222222222222"
BANK_ID = "33333333-3333-3333-3333-333333333333"
LIABILITY_ID = "44444444-4444-4444-4444-444444444444"
INTEREST_ID = "55555555-5555-5555-5555-555555555555"
DEP_EXPENSE_ID = "66666666-6666-6666-6666-666666666666"
ACCUM_DEP_ID = "77777777-7777-7777-7777-777777777777"
AUTH = ("88888888-8888-8888-8888-888888888888", None)


class _Result:
    def __init__(self, data=None):
        self.data = data


class _ScheduleQuery:
    def __init__(self, row):
        self.row = row

    def select(self, *_args, **_kwargs): return self
    def eq(self, *_args, **_kwargs): return self
    def single(self): return self
    def execute(self): return _Result(self.row)


class _RPC:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return _Result(self.result)


class _DB:
    def __init__(self, schedule, *, rpc_error=None):
        self.schedule = schedule
        self.rpc_error = rpc_error
        self.rpc_calls = []

    def table(self, name):
        assert name == "finance_lease_schedule"
        return _ScheduleQuery(self.schedule)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        return _RPC(
            {
                "payment_journal_id": "payment-journal",
                "depreciation_journal_id": "depreciation-journal",
                "period_number": 1,
                "lease_status": "active",
            },
            self.rpc_error,
        )


def _lease():
    return {
        "id": LEASE_ID,
        "status": "active",
        "asset_description": "Office lease",
        "liability_lt_account_id": LIABILITY_ID,
        "interest_expense_account_id": INTEREST_ID,
        "depreciation_expense_account_id": DEP_EXPENSE_ID,
        "accum_depreciation_account_id": ACCUM_DEP_ID,
        "rou_asset_cost": "1200.00",
        "lease_term_months": 12,
    }


def _schedule():
    return {
        "id": "schedule-1",
        "period_number": 1,
        "payment_amount": "110.00",
        "principal_amount": "100.00",
        "interest_amount": "10.00",
        "additional_debit": "0.00",
        "additional_credit": "0.00",
        "posted": False,
    }


def _payload():
    return finance_leases.PostPaymentRequest(
        organisation_id=ORG_ID,
        journal_date=date(2026, 7, 25),
        bank_account_id=BANK_ID,
        post_depreciation=True,
    )


def _arrange(monkeypatch, db):
    monkeypatch.setattr(finance_leases, "_auth", lambda _auth: (AUTH[0], db))
    monkeypatch.setattr(finance_leases, "ensure_org_write", lambda *_args: None)
    monkeypatch.setattr(finance_leases, "_get_lease", lambda *_args, **_kwargs: _lease())
    monkeypatch.setattr(finance_leases, "_validate_accounts", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(finance_leases, "assert_accounting_period_unlocked", lambda *_args, **_kwargs: None)


def test_payment_endpoint_delegates_all_posting_to_one_atomic_rpc(monkeypatch):
    db = _DB(_schedule())
    _arrange(monkeypatch, db)

    result = finance_leases.post_payment_journal(LEASE_ID, 1, _payload(), AUTH)

    assert result["payment_journal_id"] == "payment-journal"
    assert result["depreciation_journal_id"] == "depreciation-journal"
    assert len(db.rpc_calls) == 1
    rpc_name, params = db.rpc_calls[0]
    assert rpc_name == "post_lease_payment_atomic"
    assert params["p_org_id"] == ORG_ID
    assert params["p_lease_id"] == LEASE_ID
    assert params["p_period_number"] == 1
    assert params["p_post_depreciation"] is True
    assert len(json.loads(params["p_payment_lines"])) == 3
    assert len(json.loads(params["p_depreciation_lines"])) == 2


def test_concurrent_duplicate_reported_by_rpc_returns_conflict(monkeypatch):
    db = _DB(_schedule(), rpc_error=RuntimeError("Finance lease period 1 has already been posted"))
    _arrange(monkeypatch, db)

    with pytest.raises(HTTPException) as exc_info:
        finance_leases.post_payment_journal(LEASE_ID, 1, _payload(), AUTH)

    assert exc_info.value.status_code == 409
    assert len(db.rpc_calls) == 1


def test_atomic_payment_migration_contains_required_transaction_guards():
    migration = (
        Path(__file__).resolve().parents[1]
        / "app/db/applied/20260725100000_atomic_finance_lease_payment_posting.sql"
    ).read_text(encoding="utf-8").lower()

    assert "for update" in migration
    assert "finance_lease_gl_postings_one_period_event" in migration
    assert "create unique index" in migration
    assert "organisation_accounting_periods" in migration
    assert "cross-organisation account" in migration
    assert "does not match the locked lease schedule" in migration
    assert "depreciation amount does not match the lease schedule" in migration
    assert "payment journal must use one bank account" in migration
    assert "insert into public.gl_journals" in migration
    assert "insert into public.gl_journal_lines" in migration
    assert "insert into public.finance_lease_gl_postings" in migration
    assert "update public.finance_lease_schedule" in migration
    assert "update public.finance_leases" in migration
    assert "revoke all on function public.post_lease_payment_atomic" in migration
