import pytest
from fastapi import HTTPException

from app.routers import reports
from app.services.balance_sheet import generate_balance_sheet


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
        self._limit = None
        self.order_fields = []

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
        "organisations": [
            {
                "id": "org-1",
                "financial_year_end": "February",
                "reporting_standard": "ifrs",
                "income_statement_presentation": "function",
            }
        ],
        "accounts": [
            {"id": "cash", "organisation_id": "org-1", "code": "1000", "name": "Bank", "type": "asset", "group_name": "Current Assets", "active": True},
            {"id": "payable", "organisation_id": "org-1", "code": "2100", "name": "Payables", "type": "liability", "group_name": "Current Liabilities", "active": True},
            {"id": "capital", "organisation_id": "org-1", "code": "3000", "name": "Share capital", "type": "equity", "group_name": "Equity", "active": True},
            {"id": "sales", "organisation_id": "org-1", "code": "4000", "name": "Sales", "type": "income", "group_name": "Revenue", "active": True},
            {
                "id": "rent",
                "organisation_id": "org-1",
                "code": "5000",
                "name": "Rent",
                "type": "expense",
                "group_name": "Expenses",
                "active": True,
                "income_statement_nature": "other_operating_expenses",
                "default_income_statement_function": "g_and_a",
                "special_report_classification": "none",
            },
        ],
        "gl_journals": [
            {"id": "opening", "organisation_id": "org-1", "status": "posted", "journal_date": "2026-03-01"},
            {"id": "profit", "organisation_id": "org-1", "status": "posted", "journal_date": "2026-04-30"},
            {"id": "draft", "organisation_id": "org-1", "status": "draft", "journal_date": "2026-04-30"},
        ],
        "gl_journal_lines": [
            {"id": "l-cash-opening", "organisation_id": "org-1", "gl_journal_id": "opening", "account_id": "cash", "debit_amount": 200, "credit_amount": 0},
            {"id": "l-capital-opening", "organisation_id": "org-1", "gl_journal_id": "opening", "account_id": "capital", "debit_amount": 0, "credit_amount": 100},
            {"id": "l-payable-opening", "organisation_id": "org-1", "gl_journal_id": "opening", "account_id": "payable", "debit_amount": 0, "credit_amount": 100},
            {"id": "l-cash-profit", "organisation_id": "org-1", "gl_journal_id": "profit", "account_id": "cash", "debit_amount": 100, "credit_amount": 0},
            {"id": "l-sales-profit", "organisation_id": "org-1", "gl_journal_id": "profit", "account_id": "sales", "debit_amount": 0, "credit_amount": 200},
            {"id": "l-rent-profit", "organisation_id": "org-1", "gl_journal_id": "profit", "account_id": "rent", "debit_amount": 100, "credit_amount": 0},
            {"id": "l-draft-cash", "organisation_id": "org-1", "gl_journal_id": "draft", "account_id": "cash", "debit_amount": 999, "credit_amount": 0},
            {"id": "l-draft-sales", "organisation_id": "org-1", "gl_journal_id": "draft", "account_id": "sales", "debit_amount": 0, "credit_amount": 999},
        ],
        "tracking_dimensions": [],
        "tracking_values": [],
    }


def test_balance_sheet_groups_accounts_and_current_year_profit():
    report = generate_balance_sheet(_DB(_base_tables()), organisation_id="org-1", as_at_date="2026-04-30")

    assert report["summary"] == {
        "total_assets": 300.0,
        "total_liabilities": 100.0,
        "total_equity": 200.0,
        "liabilities_plus_equity": 300.0,
        "variance": 0.0,
        "in_balance": True,
    }
    assert report["sections"]["assets"][0]["name"] == "Bank"
    assert report["sections"]["assets"][0]["amount"] == 300.0
    assert report["sections"]["liabilities"][0]["amount"] == 100.0
    assert any(line["name"] == "Current year profit / (loss)" and line["amount"] == 100.0 for line in report["sections"]["equity"])


def test_balance_sheet_reports_out_of_balance_warning():
    tables = _base_tables()
    tables["gl_journal_lines"].append({
        "id": "bad-line",
        "organisation_id": "org-1",
        "gl_journal_id": "profit",
        "account_id": "cash",
        "debit_amount": 5,
        "credit_amount": 0,
    })

    report = generate_balance_sheet(_DB(tables), organisation_id="org-1", as_at_date="2026-04-30")

    assert report["summary"]["in_balance"] is False
    assert report["summary"]["variance"] == 5.0
    assert any(warning["code"] == "balance_sheet_out_of_balance" for warning in report["warnings"])


def test_balance_sheet_route_uses_reports_view_permission(monkeypatch):
    tables = _base_tables()
    tables["organisation_users"] = [
        {
            "organisation_id": "org-1",
            "user_id": "viewer-1",
            "status": "active",
            "role": "viewer",
            "permissions": {},
        }
    ]

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("balance sheet generation should not run without reports permission")

    monkeypatch.setattr(reports, "generate_balance_sheet", _fail_if_called)

    with pytest.raises(HTTPException) as exc:
        reports.balance_sheet_report(
            auth=("viewer-1", _DB(tables)),
            organisation_id="org-1",
            as_at_date="2026-04-30",
        )

    assert exc.value.status_code == 403
