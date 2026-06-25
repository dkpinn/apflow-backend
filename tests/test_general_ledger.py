import pytest
from fastapi import HTTPException

from app.routers import reports
from app.services.general_ledger import generate_general_ledger


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, rows):
        self.rows = list(rows or [])
        self.filters = []
        self.in_filters = []
        self.gte_filters = []
        self.lte_filters = []
        self.order_fields = []
        self._limit = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def in_(self, field, values):
        self.in_filters.append((field, {str(value) for value in values}))
        return self

    def gte(self, field, value):
        self.gte_filters.append((field, value))
        return self

    def lte(self, field, value):
        self.lte_filters.append((field, value))
        return self

    def order(self, field, **_kwargs):
        self.order_fields.append(field)
        return self

    def limit(self, value):
        self._limit = value
        return self

    def execute(self):
        rows = self.rows
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        for field, values in self.in_filters:
            rows = [row for row in rows if str(row.get(field)) in values]
        for field, value in self.gte_filters:
            rows = [row for row in rows if row.get(field) >= value]
        for field, value in self.lte_filters:
            rows = [row for row in rows if row.get(field) <= value]
        for field in reversed(self.order_fields):
            rows = sorted(rows, key=lambda row: row.get(field) or "")
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Response(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _base_tables():
    return {
        "organisation_users": [
            {
                "organisation_id": "org-1",
                "user_id": "viewer-1",
                "status": "active",
                "role": "viewer",
                "permissions": {"reports_view": True},
            }
        ],
        "accounts": [
            {"id": "cash", "organisation_id": "org-1", "code": "1000", "name": "Bank", "type": "asset", "group_name": "Current Assets", "active": True},
            {"id": "payable", "organisation_id": "org-1", "code": "2100", "name": "Payables", "type": "liability", "group_name": "Current Liabilities", "active": True},
            {"id": "sales", "organisation_id": "org-1", "code": "4000", "name": "Sales", "type": "income", "group_name": "Revenue", "active": True},
        ],
        "gl_journals": [
            {"id": "opening", "organisation_id": "org-1", "status": "posted", "journal_date": "2026-01-31", "description": "Opening", "source_type": None, "created_at": "2026-01-31T08:00:00"},
            {"id": "sale", "organisation_id": "org-1", "status": "posted", "journal_date": "2026-02-15", "description": "Sale", "source_type": "invoice", "created_at": "2026-02-15T08:00:00"},
            {"id": "payment", "organisation_id": "org-1", "status": "posted", "journal_date": "2026-02-20", "description": "Payment", "source_type": "bank_transaction", "created_at": "2026-02-20T08:00:00"},
            {"id": "draft", "organisation_id": "org-1", "status": "draft", "journal_date": "2026-02-25", "description": "Draft", "source_type": None, "created_at": "2026-02-25T08:00:00"},
            {"id": "after", "organisation_id": "org-1", "status": "posted", "journal_date": "2026-03-01", "description": "After period", "source_type": None, "created_at": "2026-03-01T08:00:00"},
        ],
        "gl_journal_lines": [
            {"id": "opening-cash", "organisation_id": "org-1", "gl_journal_id": "opening", "account_id": "cash", "description": "Opening cash", "debit_amount": 100, "credit_amount": 0, "sort_order": 1},
            {"id": "opening-payable", "organisation_id": "org-1", "gl_journal_id": "opening", "account_id": "payable", "description": "Opening payable", "debit_amount": 0, "credit_amount": 100, "sort_order": 2},
            {"id": "sale-cash", "organisation_id": "org-1", "gl_journal_id": "sale", "account_id": "cash", "description": "Cash sale", "debit_amount": 200, "credit_amount": 0, "sort_order": 1},
            {"id": "sale-sales", "organisation_id": "org-1", "gl_journal_id": "sale", "account_id": "sales", "description": "Revenue", "debit_amount": 0, "credit_amount": 200, "sort_order": 2},
            {"id": "payment-payable", "organisation_id": "org-1", "gl_journal_id": "payment", "account_id": "payable", "description": "Pay supplier", "debit_amount": 40, "credit_amount": 0, "sort_order": 1},
            {"id": "payment-cash", "organisation_id": "org-1", "gl_journal_id": "payment", "account_id": "cash", "description": "Cash out", "debit_amount": 0, "credit_amount": 40, "sort_order": 2},
            {"id": "draft-cash", "organisation_id": "org-1", "gl_journal_id": "draft", "account_id": "cash", "description": "Draft", "debit_amount": 999, "credit_amount": 0, "sort_order": 1},
            {"id": "after-cash", "organisation_id": "org-1", "gl_journal_id": "after", "account_id": "cash", "description": "After", "debit_amount": 999, "credit_amount": 0, "sort_order": 1},
        ],
    }


def _account(report, account_id):
    return next(section for section in report["accounts"] if section["account_id"] == account_id)


def test_general_ledger_calculates_opening_running_and_closing_balances():
    report = generate_general_ledger(
        _DB(_base_tables()),
        organisation_id="org-1",
        date_from="2026-02-01",
        date_to="2026-02-28",
    )

    cash = _account(report, "cash")
    assert cash["opening_balance"] == 100.0
    assert cash["period_debit"] == 200.0
    assert cash["period_credit"] == 40.0
    assert cash["closing_balance"] == 260.0
    assert [line["running_balance"] for line in cash["lines"]] == [300.0, 260.0]
    assert report["summary"]["total_debits"] == 240.0
    assert report["summary"]["total_credits"] == 240.0


def test_general_ledger_respects_account_filter_and_credit_normal_balance():
    report = generate_general_ledger(
        _DB(_base_tables()),
        organisation_id="org-1",
        date_from="2026-02-01",
        date_to="2026-02-28",
        account_id="payable",
    )

    assert len(report["accounts"]) == 1
    payable = report["accounts"][0]
    assert payable["opening_balance"] == 100.0
    assert payable["period_debit"] == 40.0
    assert payable["period_credit"] == 0.0
    assert payable["closing_balance"] == 60.0
    assert payable["lines"][0]["running_balance"] == 60.0


def test_general_ledger_rejects_missing_account_filter():
    with pytest.raises(ValueError, match="Account not found"):
        generate_general_ledger(
            _DB(_base_tables()),
            organisation_id="org-1",
            date_from="2026-02-01",
            date_to="2026-02-28",
            account_id="missing",
        )


def test_general_ledger_route_uses_reports_view_permission(monkeypatch):
    tables = _base_tables()
    tables["organisation_users"][0]["permissions"] = {}

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("general ledger generation should not run without reports permission")

    monkeypatch.setattr(reports, "generate_general_ledger", _fail_if_called)

    with pytest.raises(HTTPException) as exc:
        reports.general_ledger_report(
            auth=("viewer-1", _DB(tables)),
            organisation_id="org-1",
            date_from="2026-02-01",
            date_to="2026-02-28",
        )

    assert exc.value.status_code == 403
