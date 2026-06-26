from uuid import UUID

import pytest
from fastapi import HTTPException

import app.routers.bank_rules as bank_rules
from app.services.bank_rules import list_bank_rules, preview_bank_rule_matches
from app.services.bank_statement_service import bank_rule_matches, score_rule_suggestions
from tests.conftest import MemoryDB


ORG_ID = "00000000-0000-0000-0000-000000000001"
ORG_UUID = UUID(ORG_ID)


def _fake_auth(db):
    return lambda _auth_tuple: ("user-1", db)


def _rule(**overrides):
    row = {
        "id": "rule-1",
        "organisation_id": ORG_ID,
        "bank_account_id": "bank-1",
        "name": "Rent receipts",
        "active": True,
        "priority": 10,
        "amount_direction": "money_in",
        "match_type": "contains",
        "criteria_mode": "and",
        "criteria": [{"field": "raw_text", "operator": "contains", "value": "Flat No 8"}],
        "description_pattern": None,
        "reference_pattern": None,
        "counterparty_pattern": None,
        "min_amount": None,
        "max_amount": None,
        "gl_account_id": "income-1",
        "tracking": {},
        "tax_treatment": None,
    }
    row.update(overrides)
    return row


def _line(**overrides):
    row = {
        "id": "line-1",
        "organisation_id": ORG_ID,
        "bank_account_id": "bank-1",
        "line_date": "2026-06-01",
        "description": "Immediate Trf Cr Nk",
        "raw_text": "Nedbank Flat No 8 Zodwa 1741591307",
        "counterparty": "Nedbank Flat No 8 Zodwa",
        "reference": "Nk",
        "bank_reference": "1741591307",
        "signed_amount": 7735.0,
        "reviewed_at": "2026-06-02T08:00:00Z",
    }
    row.update(overrides)
    return row


def test_shared_bank_rule_matcher_drives_suggestions():
    rule = _rule()
    line = _line()
    assert bank_rule_matches(rule, bank_account_id="bank-1", line=line) is True

    db = MemoryDB({"bank_transaction_rules": [rule]})
    suggestions = score_rule_suggestions(
        db,
        organisation_id=ORG_ID,
        bank_account_id="bank-1",
        line=line,
    )

    assert suggestions[0]["suggested_account_id"] == "income-1"


def test_list_bank_rules_adds_usage_stats():
    db = MemoryDB({
        "bank_transaction_rules": [_rule()],
        "bank_statement_lines": [
            _line(accepted_rule_id="rule-1", reviewed_at="2026-06-02T08:00:00Z"),
            _line(id="line-2", accepted_rule_id="rule-1", reviewed_at="2026-06-03T08:00:00Z"),
        ],
    })

    rules = list_bank_rules(db, organisation_id=ORG_ID)

    assert rules[0]["usage_count"] == 2
    assert rules[0]["last_used_at"] == "2026-06-03T08:00:00Z"


def test_test_bank_rule_returns_matching_recent_lines():
    db = MemoryDB({
        "bank_statement_lines": [
            _line(),
            _line(id="line-2", raw_text="Card purchase", signed_amount=-100),
        ],
    })

    result = preview_bank_rule_matches(db, organisation_id=ORG_ID, rule=_rule(), limit=50)

    assert result["tested_count"] == 2
    assert result["match_count"] == 1
    assert result["matches"][0]["id"] == "line-1"


def test_list_rules_route_enforces_read_and_returns_rules(monkeypatch):
    db = MemoryDB({"bank_transaction_rules": [_rule()]})
    calls = []
    monkeypatch.setattr(bank_rules, "_auth", _fake_auth(db))
    monkeypatch.setattr(bank_rules, "ensure_org_read", lambda user_id, org_id: calls.append((user_id, org_id)))

    result = bank_rules.list_rules(ORG_ID, auth=("user-1", None))

    assert result["success"] is True
    assert result["rules"][0]["id"] == "rule-1"
    assert calls == [("user-1", ORG_ID)]


def test_update_rule_route_patches_rule_and_logs_event(monkeypatch):
    db = MemoryDB({
        "bank_transaction_rules": [_rule()],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bank_rules, "_auth", _fake_auth(db))
    monkeypatch.setattr(bank_rules, "ensure_org_write", lambda *_args: None)

    result = bank_rules.update_rule(
        "rule-1",
        bank_rules.BankRuleUpdate(
            organisation_id=ORG_UUID,
            active=False,
            priority=5,
            criteria=[{"field": "raw_text", "operator": "contains", "value": "Flat No 8"}],
        ),
        auth=("user-1", None),
    )

    assert result["rule"]["active"] is False
    assert result["rule"]["priority"] == 5
    assert db.tables["bank_audit_events"][0]["event_type"] == "bank_rule_updated"


def test_test_rule_route_logs_match_summary(monkeypatch):
    db = MemoryDB({
        "bank_transaction_rules": [_rule()],
        "bank_statement_lines": [_line()],
        "bank_audit_events": [],
    })
    monkeypatch.setattr(bank_rules, "_auth", _fake_auth(db))
    monkeypatch.setattr(bank_rules, "ensure_org_read", lambda *_args: None)

    result = bank_rules.test_rule(
        "rule-1",
        bank_rules.BankRuleTestRequest(organisation_id=ORG_UUID, limit=50),
        auth=("user-1", None),
    )

    assert result["match_count"] == 1
    assert db.tables["bank_audit_events"][0]["event_type"] == "bank_rule_tested"


def test_update_rule_route_404_when_missing(monkeypatch):
    db = MemoryDB({"bank_transaction_rules": []})
    monkeypatch.setattr(bank_rules, "_auth", _fake_auth(db))
    monkeypatch.setattr(bank_rules, "ensure_org_write", lambda *_args: None)

    with pytest.raises(HTTPException) as exc_info:
        bank_rules.update_rule(
            "missing",
            bank_rules.BankRuleUpdate(organisation_id=ORG_UUID, active=False),
            auth=("user-1", None),
        )

    assert exc_info.value.status_code == 404
