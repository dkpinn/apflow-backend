from uuid import UUID

import pytest
from fastapi import HTTPException

import app.routers.opening_balances as ob_router
from app.services.opening_balances import (
    get_account_opening_balance,
    post_opening_balance,
    preview_opening_balance,
    upsert_account_opening_balance,
)
from tests.conftest import MemoryDB


ORG_ID = "00000000-0000-0000-0000-000000000001"
ORG_UUID = UUID(ORG_ID)
ASSET_ID = "00000000-0000-0000-0000-000000000101"
LIABILITY_ID = "00000000-0000-0000-0000-000000000201"
RETAINED_ID = "00000000-0000-0000-0000-000000000301"


def _tables():
    return {
        "accounts": [
            {
                "id": "asset-1",
                "organisation_id": ORG_ID,
                "code": "1000",
                "name": "Bank",
                "type": "asset",
                "active": True,
            },
            {
                "id": "equity-1",
                "organisation_id": ORG_ID,
                "code": "3000",
                "name": "Opening Equity",
                "type": "equity",
                "active": True,
            },
            {
                "id": "retained-1",
                "organisation_id": ORG_ID,
                "code": "7100",
                "name": "Retained Earnings",
                "type": "equity",
                "active": True,
                "system_key": "retained_earnings",
            },
        ],
        "gl_journals": [],
        "gl_journal_lines": [],
    }


def _account_level_tables():
    return {
        "accounts": [
            {
                "id": ASSET_ID,
                "organisation_id": ORG_ID,
                "code": "1000",
                "name": "Bank",
                "type": "asset",
                "active": True,
            },
            {
                "id": LIABILITY_ID,
                "organisation_id": ORG_ID,
                "code": "2100",
                "name": "Trade Creditors",
                "type": "liability",
                "active": True,
            },
            {
                "id": RETAINED_ID,
                "organisation_id": ORG_ID,
                "code": "7100",
                "name": "Retained Earnings",
                "type": "equity",
                "active": True,
                "system_key": "retained_earnings",
            },
        ],
        "gl_journals": [],
        "gl_journal_lines": [],
        "bank_accounts": [],
    }


def _lines():
    return [
        {"account_code": "1000", "debit": "1000", "credit": "0"},
        {"account_id": "equity-1", "debit": "0", "credit": "1000"},
    ]


def test_preview_opening_balance_resolves_codes_and_validates_balance():
    preview = preview_opening_balance(
        MemoryDB(_tables()),
        organisation_id=ORG_ID,
        as_at_date="2026-06-30",
        lines=_lines(),
    )

    assert preview["summary"] == {
        "line_count": 2,
        "total_debit": 1000.0,
        "total_credit": 1000.0,
        "difference": 0.0,
        "in_balance": True,
    }
    assert preview["lines"][0]["account_id"] == "asset-1"


def test_preview_opening_balance_reports_out_of_balance():
    preview = preview_opening_balance(
        MemoryDB(_tables()),
        organisation_id=ORG_ID,
        as_at_date="2026-06-30",
        lines=[{"account_code": "1000", "debit": "1000", "credit": "0"}],
    )

    assert preview["summary"]["in_balance"] is False
    assert preview["warnings"][0]["code"] == "out_of_balance"


def test_preview_opening_balance_allows_bank_control_account_from_coa():
    tables = _tables()
    tables["bank_accounts"] = [{
        "id": "bank-1",
        "organisation_id": ORG_ID,
        "gl_account_id": "asset-1",
        "name": "Main Bank",
        "active": True,
    }]
    db = MemoryDB(tables)

    preview = preview_opening_balance(
        db,
        organisation_id=ORG_ID,
        as_at_date="2026-06-30",
        lines=[
            {"account_id": "asset-1", "debit": "100", "credit": "0"},
            {"account_id": "equity-1", "debit": "0", "credit": "100"},
        ],
    )

    assert preview["summary"]["in_balance"] is True
    assert {line["account_id"] for line in preview["lines"]} == {"asset-1", "equity-1"}


def test_post_opening_balance_creates_posted_journal_and_lines():
    db = MemoryDB(_tables())

    result = post_opening_balance(
        db,
        organisation_id=ORG_ID,
        as_at_date="2026-06-30",
        lines=_lines(),
        user_id="user-1",
        description="Opening TB",
    )

    assert result["success"] is True
    assert db.tables["gl_journals"][0]["status"] == "posted"
    assert db.tables["gl_journals"][0]["source_type"] == "opening_balance"
    assert len(db.tables["gl_journal_lines"]) == 2


def test_post_opening_balance_blocks_duplicate_date():
    tables = _tables()
    tables["gl_journals"].append({
        "id": "journal-1",
        "organisation_id": ORG_ID,
        "source_type": "opening_balance",
        "journal_date": "2026-06-30",
        "status": "posted",
    })

    with pytest.raises(ValueError, match="already exists"):
        post_opening_balance(
            MemoryDB(tables),
            organisation_id=ORG_ID,
            as_at_date="2026-06-30",
            lines=_lines(),
            user_id="user-1",
        )


def test_post_opening_balance_respects_accounting_lock_date():
    tables = _tables()
    tables["organisation_accounting_periods"] = [{
        "organisation_id": ORG_ID,
        "status": "locked",
        "lock_date": "2026-06-30",
    }]

    with pytest.raises(ValueError, match="accounting lock date"):
        post_opening_balance(
            MemoryDB(tables),
            organisation_id=ORG_ID,
            as_at_date="2026-06-30",
            lines=_lines(),
            user_id="user-1",
        )


def test_preview_route_enforces_read_permission(monkeypatch):
    db = MemoryDB(_tables())
    calls = []
    monkeypatch.setattr(ob_router, "ensure_org_read", lambda user_id, org_id: calls.append((user_id, org_id)))
    payload = ob_router.OpeningBalanceRequest(
        organisation_id=ORG_UUID,
        as_at_date="2026-06-30",
        lines=[
            ob_router.OpeningBalanceLine(account_code="1000", debit="1000"),
            ob_router.OpeningBalanceLine(account_code="3000", credit="1000"),
        ],
    )

    result = ob_router.preview_opening_balances(payload, auth=("user-1", db))

    assert result["success"] is True
    assert calls == [("user-1", ORG_ID)]


def test_post_route_returns_400_for_unbalanced_payload(monkeypatch):
    db = MemoryDB(_tables())
    monkeypatch.setattr(ob_router, "ensure_org_write", lambda *_args: None)
    payload = ob_router.OpeningBalanceRequest(
        organisation_id=ORG_UUID,
        as_at_date="2026-06-30",
        lines=[ob_router.OpeningBalanceLine(account_code="1000", debit="1000")],
    )

    with pytest.raises(HTTPException) as exc_info:
        ob_router.post_opening_balances(payload, auth=("user-1", db))

    assert exc_info.value.status_code == 400
    assert "must balance" in exc_info.value.detail


def test_upsert_account_opening_balance_creates_balanced_journal():
    db = MemoryDB(_account_level_tables())

    result = upsert_account_opening_balance(
        db,
        organisation_id=ORG_ID,
        account_id=ASSET_ID,
        as_at_date="2026-06-30",
        side="debit",
        amount="1250",
        user_id="user-1",
    )

    assert result["success"] is True
    assert db.tables["gl_journals"][0]["source_type"] == "opening_balance"
    assert db.tables["gl_journals"][0]["total_debit"] == 1250.0
    assert db.tables["gl_journals"][0]["total_credit"] == 1250.0
    by_account = {row["account_id"]: row for row in db.tables["gl_journal_lines"]}
    assert by_account[ASSET_ID]["debit_amount"] == 1250.0
    assert by_account[RETAINED_ID]["credit_amount"] == 1250.0


def test_upsert_account_opening_balance_recalculates_retained_earnings():
    db = MemoryDB(_account_level_tables())
    upsert_account_opening_balance(
        db,
        organisation_id=ORG_ID,
        account_id=ASSET_ID,
        as_at_date="2026-06-30",
        side="debit",
        amount="1250",
        user_id="user-1",
    )

    upsert_account_opening_balance(
        db,
        organisation_id=ORG_ID,
        account_id=LIABILITY_ID,
        as_at_date="2026-06-30",
        side="credit",
        amount="300",
        user_id="user-1",
    )

    by_account = {row["account_id"]: row for row in db.tables["gl_journal_lines"]}
    assert by_account[ASSET_ID]["debit_amount"] == 1250.0
    assert by_account[LIABILITY_ID]["credit_amount"] == 300.0
    assert by_account[RETAINED_ID]["credit_amount"] == 950.0
    assert db.tables["gl_journals"][0]["total_debit"] == 1250.0
    assert db.tables["gl_journals"][0]["total_credit"] == 1250.0


def test_upsert_account_opening_balance_clears_line_and_balancing_entry():
    db = MemoryDB(_account_level_tables())
    upsert_account_opening_balance(
        db,
        organisation_id=ORG_ID,
        account_id=ASSET_ID,
        as_at_date="2026-06-30",
        side="debit",
        amount="1250",
        user_id="user-1",
    )

    upsert_account_opening_balance(
        db,
        organisation_id=ORG_ID,
        account_id=ASSET_ID,
        as_at_date="2026-06-30",
        side="debit",
        amount="0",
        user_id="user-1",
    )

    assert db.tables["gl_journal_lines"] == []
    assert db.tables["gl_journals"][0]["total_debit"] == 0.0
    assert db.tables["gl_journals"][0]["total_credit"] == 0.0


def test_upsert_account_opening_balance_allows_bank_control_account_from_coa():
    tables = _account_level_tables()
    tables["bank_accounts"] = [{
        "id": "bank-1",
        "organisation_id": ORG_ID,
        "gl_account_id": ASSET_ID,
        "name": "Main Bank",
        "opening_balance": "500",
        "active": True,
    }]
    db = MemoryDB(tables)

    result = upsert_account_opening_balance(
        db,
        organisation_id=ORG_ID,
        account_id=ASSET_ID,
        as_at_date="2026-06-30",
        side="debit",
        amount="500",
        user_id="user-1",
    )

    assert result["success"] is True
    assert result["opening_balance"]["editable"] is True
    assert result["opening_balance"]["protected_reason"] is None
    assert result["opening_balance"]["warnings"] == []
    by_account = {row["account_id"]: row for row in db.tables["gl_journal_lines"]}
    assert by_account[ASSET_ID]["debit_amount"] == 500.0


def test_upsert_account_opening_balance_still_blocks_system_account_linked_to_bank():
    tables = _account_level_tables()
    tables["accounts"][0]["system_key"] = "cash_control"
    tables["bank_accounts"] = [{
        "id": "bank-1",
        "organisation_id": ORG_ID,
        "gl_account_id": ASSET_ID,
        "name": "Main Bank",
        "active": True,
    }]
    db = MemoryDB(tables)

    with pytest.raises(ValueError, match="system account"):
        upsert_account_opening_balance(
            db,
            organisation_id=ORG_ID,
            account_id=ASSET_ID,
            as_at_date="2026-06-30",
            side="debit",
            amount="500",
            user_id="user-1",
        )


def test_upsert_account_opening_balance_blocks_retained_earnings_edit():
    db = MemoryDB(_account_level_tables())

    with pytest.raises(ValueError, match="Retained earnings is calculated"):
        upsert_account_opening_balance(
            db,
            organisation_id=ORG_ID,
            account_id=RETAINED_ID,
            as_at_date="2026-06-30",
            side="credit",
            amount="100",
            user_id="user-1",
        )


def test_upsert_account_opening_balance_blocks_locked_period():
    tables = _account_level_tables()
    tables["organisation_accounting_periods"] = [{
        "organisation_id": ORG_ID,
        "status": "locked",
        "lock_date": "2026-06-30",
    }]

    with pytest.raises(ValueError, match="accounting lock date"):
        upsert_account_opening_balance(
            MemoryDB(tables),
            organisation_id=ORG_ID,
            account_id=ASSET_ID,
            as_at_date="2026-06-30",
            side="debit",
            amount="100",
            user_id="user-1",
        )


def test_upsert_account_opening_balance_route_enforces_write_permission(monkeypatch):
    db = MemoryDB(_account_level_tables())
    calls = []
    monkeypatch.setattr(ob_router, "ensure_org_write", lambda user_id, org_id: calls.append((user_id, org_id)))
    payload = ob_router.AccountOpeningBalanceRequest(
        organisation_id=ORG_UUID,
        account_id=UUID(ASSET_ID),
        as_at_date="2026-06-30",
        side="debit",
        amount="100",
    )

    result = ob_router.upsert_account_opening_balance_route(payload, auth=("user-1", db))

    assert result["success"] is True
    assert calls == [("user-1", ORG_ID)]
