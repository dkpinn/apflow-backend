from fastapi import HTTPException

from app.routers import bank
from app.services.bank.parsing_rules import lookup_parsing_hint
from tests.conftest import MemoryDB


ORG_ID = "00000000-0000-0000-0000-000000000001"
AUTH = ("user-1", None)


def _patch_auth(monkeypatch, db):
    monkeypatch.setattr(bank, "_auth", lambda _auth: ("user-1", db))
    monkeypatch.setattr(bank, "ensure_org_read", lambda *_args: None)
    monkeypatch.setattr(bank, "ensure_org_write", lambda *_args: None)


def test_lookup_parsing_hint_uses_most_specific_active_rule():
    db = MemoryDB({
        "bank_parsing_rules": [
            {
                "organisation_id": ORG_ID,
                "institution_name": None,
                "account_type": None,
                "parsing_hint": "generic",
                "active": True,
            },
            {
                "organisation_id": ORG_ID,
                "institution_name": "Standard",
                "account_type": None,
                "parsing_hint": "standard generic",
                "active": True,
            },
            {
                "organisation_id": ORG_ID,
                "institution_name": "Standard Bank",
                "account_type": "cheque",
                "parsing_hint": "standard cheque",
                "active": True,
            },
            {
                "organisation_id": ORG_ID,
                "institution_name": "Standard Bank",
                "account_type": "cheque",
                "parsing_hint": "inactive",
                "active": False,
            },
        ],
    })

    hint = lookup_parsing_hint(
        db,
        organisation_id=ORG_ID,
        institution_name="STANDARD",
        account_type="cheque",
    )

    assert hint == "standard cheque"


def test_parsing_rule_routes_create_update_delete(monkeypatch):
    db = MemoryDB({"bank_parsing_rules": []})
    _patch_auth(monkeypatch, db)

    created = bank.create_parsing_rule(
        bank.ParsingRuleCreate(
            organisation_id=ORG_ID,
            institution_name="ABSA",
            account_type="business",
            parsing_hint="Use ABSA business parser",
        ),
        AUTH,
    )

    rule_id = created["rule"]["id"]
    assert created["success"] is True
    assert db.tables["bank_parsing_rules"][0]["created_by"] == "user-1"

    listed = bank.list_parsing_rules(ORG_ID, AUTH)
    assert [rule["id"] for rule in listed["rules"]] == [rule_id]

    updated = bank.update_parsing_rule(
        rule_id,
        bank.ParsingRuleUpdate(
            organisation_id=ORG_ID,
            parsing_hint="Updated hint",
            active=False,
        ),
        AUTH,
    )

    assert updated["rule"]["parsing_hint"] == "Updated hint"
    assert updated["rule"]["active"] is False
    assert updated["rule"]["institution_name"] == "ABSA"
    assert updated["rule"]["updated_at"]

    assert bank.delete_parsing_rule(rule_id, ORG_ID, AUTH) == {"success": True}
    assert db.tables["bank_parsing_rules"] == []


def test_delete_parsing_rule_404_when_missing(monkeypatch):
    db = MemoryDB({"bank_parsing_rules": []})
    _patch_auth(monkeypatch, db)

    try:
        bank.delete_parsing_rule("missing", ORG_ID, AUTH)
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("Expected HTTPException")
