import pytest
from fastapi import HTTPException

from app.routers import go_live_checklist
from app.services.go_live_checklist import generate_go_live_checklist


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, rows):
        self.rows = list(rows or [])
        self.filters = []
        self._limit = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def limit(self, value):
        self._limit = value
        return self

    def execute(self):
        rows = self.rows
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Response(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _ready_tables():
    return {
        "organisations": [
            {
                "id": "org-1",
                "name": "Acme",
                "legal_name": "Acme Pty Ltd",
                "registration_number": "2026/123/07",
                "vat_number": "4123456789",
                "tax_number": None,
                "country": "South Africa",
                "currency": "ZAR",
                "base_currency": "ZAR",
                "financial_year_end": "February",
            }
        ],
        "accounts": [
            {"id": "a1", "organisation_id": "org-1", "active": True, "system_key": "trade_receivables"},
            {"id": "a2", "organisation_id": "org-1", "active": True, "system_key": "trade_payables"},
            {"id": "a3", "organisation_id": "org-1", "active": True, "system_key": "vat_control"},
            {"id": "a4", "organisation_id": "org-1", "active": True, "system_key": None},
            {"id": "a5", "organisation_id": "org-1", "active": True, "system_key": None},
        ],
        "bank_accounts": [
            {
                "id": "bank-1",
                "organisation_id": "org-1",
                "active": True,
                "gl_account_id": "a4",
            }
        ],
        "organisation_users": [
            {"id": "user-1", "organisation_id": "org-1", "role": "owner", "status": "active"}
        ],
        "tracking_dimensions": [{"id": "dim-1", "organisation_id": "org-1", "active": True}],
        "suppliers": [{"id": "supplier-1", "organisation_id": "org-1", "active": True}],
        "customers": [{"id": "customer-1", "organisation_id": "org-1", "active": True}],
        "inventory_items": [{"id": "item-1", "organisation_id": "org-1", "active": True}],
        "organisation_invoice_branding": [
            {
                "organisation_id": "org-1",
                "bank_name": "Bank",
                "account_holder": "Acme",
                "account_number": "1234",
            }
        ],
        "gl_journals": [
            {
                "id": "journal-1",
                "organisation_id": "org-1",
                "status": "posted",
                "source_type": "opening_balance",
            }
        ],
    }


def test_go_live_checklist_marks_required_setup_ready():
    checklist = generate_go_live_checklist(_DB(_ready_tables()), organisation_id="org-1")

    assert checklist["readiness"]["ready"] is True
    assert checklist["readiness"]["percent"] == 100
    assert checklist["readiness"]["blocking_count"] == 0
    statuses = {item["id"]: item["status"] for item in checklist["items"]}
    assert statuses["organisation_details"] == "complete"
    assert statuses["chart_of_accounts"] == "complete"
    assert statuses["bank_accounts"] == "complete"
    assert statuses["test_transaction"] == "complete"


def test_go_live_checklist_identifies_blockers_and_optional_attention():
    tables = _ready_tables()
    tables["organisations"][0]["financial_year_end"] = None
    tables["accounts"] = tables["accounts"][:2]
    tables["bank_accounts"][0]["gl_account_id"] = None
    tables["gl_journals"] = []
    tables["tracking_dimensions"] = []
    tables["organisation_invoice_branding"] = []

    checklist = generate_go_live_checklist(_DB(tables), organisation_id="org-1")

    assert checklist["readiness"]["ready"] is False
    assert checklist["readiness"]["blocking_count"] == 4
    assert checklist["readiness"]["optional_attention_count"] >= 2
    next_action_ids = [item["id"] for item in checklist["next_actions"]]
    assert "organisation_details" in next_action_ids
    assert "chart_of_accounts" in next_action_ids
    assert "bank_accounts" in next_action_ids
    assert "test_transaction" in next_action_ids


def test_go_live_checklist_route_uses_org_read_permission(monkeypatch):
    calls = []

    def _ensure(user_id, organisation_id):
        calls.append((user_id, organisation_id))
        raise HTTPException(status_code=403, detail="Nope")

    monkeypatch.setattr(go_live_checklist, "ensure_org_read", _ensure)

    with pytest.raises(HTTPException) as exc:
        go_live_checklist.go_live_checklist(auth=("viewer-1", _DB(_ready_tables())), organisation_id="org-1")

    assert exc.value.status_code == 403
    assert calls == [("viewer-1", "org-1")]
