import pytest
from fastapi import HTTPException

from app.schemas.bank import BankAccountCreate
from app.services.bank.accounts import create_bank_account_record


ORG_ID = "11111111-1111-1111-1111-111111111111"
ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"


def _tables():
    return {
        "accounts": [
            {
                "id": ACCOUNT_ID,
                "organisation_id": ORG_ID,
                "is_system": False,
                "system_key": None,
                "active": True,
            }
        ],
        "bank_accounts": [],
        "gl_journals": [],
        "gl_journal_lines": [],
    }


def test_create_bank_account_rejects_setup_opening_balance(memory_db):
    db = memory_db(_tables())
    payload = BankAccountCreate(
        organisation_id=ORG_ID,
        name="Current account",
        gl_account_id=ACCOUNT_ID,
        opening_balance=100,
        opening_balance_date="2026-03-01",
    )

    with pytest.raises(HTTPException) as exc:
        create_bank_account_record(
            db,
            payload=payload,
            organisation_id=ORG_ID,
            user_id="user-1",
        )

    assert exc.value.status_code == 400
    assert "Chart of Accounts" in exc.value.detail
    assert db.tables["bank_accounts"] == []
    assert db.tables["gl_journals"] == []
    assert db.tables["gl_journal_lines"] == []


def test_create_bank_account_stores_legacy_zero_and_does_not_sync_opening_journal(memory_db):
    db = memory_db(_tables())
    payload = BankAccountCreate(
        organisation_id=ORG_ID,
        name="Current account",
        gl_account_id=ACCOUNT_ID,
        opening_balance=0,
        opening_balance_date="2026-03-01",
    )

    account = create_bank_account_record(
        db,
        payload=payload,
        organisation_id=ORG_ID,
        user_id="user-1",
    )

    assert account["opening_balance"] == 0.0
    assert account["current_reconciled_balance"] == 0.0
    assert db.tables["gl_journals"] == []
    assert db.tables["gl_journal_lines"] == []
