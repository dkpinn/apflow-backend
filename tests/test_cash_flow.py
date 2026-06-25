import pytest
from fastapi import HTTPException

from app.routers import reports
from app.services.cash_flow import generate_cash_flow


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
        "bank_accounts": [
            {
                "id": "bank-1",
                "organisation_id": "org-1",
                "name": "Main Bank",
                "account_type": "bank",
                "gl_account_id": "cash",
                "active": True,
            },
            {
                "id": "loan-bank",
                "organisation_id": "org-1",
                "name": "Loan Account",
                "account_type": "loan",
                "gl_account_id": "loan-control",
                "active": True,
            },
        ],
        "accounts": [
            {"id": "cash", "organisation_id": "org-1", "code": "1000", "name": "Bank", "type": "asset", "group_name": "Current Assets", "active": True},
            {"id": "sales", "organisation_id": "org-1", "code": "4000", "name": "Sales", "type": "income", "group_name": "Revenue", "active": True},
            {"id": "rent", "organisation_id": "org-1", "code": "5000", "name": "Rent", "type": "expense", "group_name": "Operating Expenses", "active": True},
            {"id": "ppe", "organisation_id": "org-1", "code": "1500", "name": "Computer Equipment", "type": "asset", "group_name": "Non-current Assets", "active": True},
            {"id": "loan", "organisation_id": "org-1", "code": "2500", "name": "Business Loan", "type": "liability", "group_name": "Non-current Liabilities", "active": True},
            {"id": "loan-control", "organisation_id": "org-1", "code": "2510", "name": "Loan Bank", "type": "liability", "group_name": "Non-current Liabilities", "active": True},
        ],
        "gl_journals": [
            {"id": "receipt", "organisation_id": "org-1", "status": "posted", "journal_date": "2026-06-01", "description": "Customer receipt", "source_type": "customer_receipt", "created_at": "2026-06-01T08:00:00"},
            {"id": "supplier", "organisation_id": "org-1", "status": "posted", "journal_date": "2026-06-05", "description": "Pay rent", "source_type": "bank_transaction", "created_at": "2026-06-05T08:00:00"},
            {"id": "asset", "organisation_id": "org-1", "status": "posted", "journal_date": "2026-06-10", "description": "Buy computer", "source_type": "manual", "created_at": "2026-06-10T08:00:00"},
            {"id": "loan", "organisation_id": "org-1", "status": "posted", "journal_date": "2026-06-15", "description": "Loan proceeds", "source_type": "manual", "created_at": "2026-06-15T08:00:00"},
            {"id": "transfer", "organisation_id": "org-1", "status": "posted", "journal_date": "2026-06-20", "description": "Bank transfer", "source_type": "manual", "created_at": "2026-06-20T08:00:00"},
            {"id": "draft", "organisation_id": "org-1", "status": "draft", "journal_date": "2026-06-25", "description": "Draft", "source_type": "manual", "created_at": "2026-06-25T08:00:00"},
        ],
        "gl_journal_lines": [
            {"id": "receipt-cash", "organisation_id": "org-1", "gl_journal_id": "receipt", "account_id": "cash", "description": "Cash in", "debit_amount": 500, "credit_amount": 0, "sort_order": 1},
            {"id": "receipt-sales", "organisation_id": "org-1", "gl_journal_id": "receipt", "account_id": "sales", "description": "Sales", "debit_amount": 0, "credit_amount": 500, "sort_order": 2},
            {"id": "supplier-rent", "organisation_id": "org-1", "gl_journal_id": "supplier", "account_id": "rent", "description": "Rent", "debit_amount": 100, "credit_amount": 0, "sort_order": 1},
            {"id": "supplier-cash", "organisation_id": "org-1", "gl_journal_id": "supplier", "account_id": "cash", "description": "Cash out", "debit_amount": 0, "credit_amount": 100, "sort_order": 2},
            {"id": "asset-ppe", "organisation_id": "org-1", "gl_journal_id": "asset", "account_id": "ppe", "description": "Computer", "debit_amount": 300, "credit_amount": 0, "sort_order": 1},
            {"id": "asset-cash", "organisation_id": "org-1", "gl_journal_id": "asset", "account_id": "cash", "description": "Cash out", "debit_amount": 0, "credit_amount": 300, "sort_order": 2},
            {"id": "loan-cash", "organisation_id": "org-1", "gl_journal_id": "loan", "account_id": "cash", "description": "Cash in", "debit_amount": 1000, "credit_amount": 0, "sort_order": 1},
            {"id": "loan-loan", "organisation_id": "org-1", "gl_journal_id": "loan", "account_id": "loan", "description": "Loan", "debit_amount": 0, "credit_amount": 1000, "sort_order": 2},
            {"id": "transfer-cash", "organisation_id": "org-1", "gl_journal_id": "transfer", "account_id": "cash", "description": "Cash", "debit_amount": 0, "credit_amount": 50, "sort_order": 1},
            {"id": "transfer-cash2", "organisation_id": "org-1", "gl_journal_id": "transfer", "account_id": "cash", "description": "Cash", "debit_amount": 50, "credit_amount": 0, "sort_order": 2},
            {"id": "draft-cash", "organisation_id": "org-1", "gl_journal_id": "draft", "account_id": "cash", "description": "Draft", "debit_amount": 999, "credit_amount": 0, "sort_order": 1},
        ],
    }


def test_cash_flow_summarises_posted_cash_movements_by_section():
    report = generate_cash_flow(
        _DB(_base_tables()),
        organisation_id="org-1",
        date_from="2026-06-01",
        date_to="2026-06-30",
    )

    assert report["summary"] == {
        "cash_in": 1500.0,
        "cash_out": 400.0,
        "net_cash_flow": 1100.0,
        "movement_count": 4,
    }
    assert report["sections"]["operating"]["net_cash_flow"] == 400.0
    assert report["sections"]["investing"]["net_cash_flow"] == -300.0
    assert report["sections"]["financing"]["net_cash_flow"] == 1000.0
    assert report["cash_accounts"][0]["account_id"] == "cash"
    assert any(warning["code"] == "cash_transfers_excluded" for warning in report["warnings"])


def test_cash_flow_reports_no_cash_accounts_warning():
    tables = _base_tables()
    tables["bank_accounts"] = []

    report = generate_cash_flow(
        _DB(tables),
        organisation_id="org-1",
        date_from="2026-06-01",
        date_to="2026-06-30",
    )

    assert report["summary"]["movement_count"] == 0
    assert report["warnings"][0]["code"] == "no_cash_accounts"


def test_cash_flow_rejects_invalid_date_range():
    with pytest.raises(ValueError, match="From date"):
        generate_cash_flow(
            _DB(_base_tables()),
            organisation_id="org-1",
            date_from="2026-07-01",
            date_to="2026-06-30",
        )


def test_cash_flow_route_uses_reports_view_permission(monkeypatch):
    tables = _base_tables()
    tables["organisation_users"][0]["permissions"] = {}

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("cash flow generation should not run without reports permission")

    monkeypatch.setattr(reports, "generate_cash_flow", _fail_if_called)

    with pytest.raises(HTTPException) as exc:
        reports.cash_flow_report(
            auth=("viewer-1", _DB(tables)),
            organisation_id="org-1",
            date_from="2026-06-01",
            date_to="2026-06-30",
        )

    assert exc.value.status_code == 403
