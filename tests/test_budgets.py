import pytest
from fastapi import HTTPException

from app.routers import budgets as budgets_router
from app.services.budgets import get_budget_grid


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, db, table_name):
        self.db = db
        self.table_name = table_name
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
        rows = list(self.db.tables.get(self.table_name, []))
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        for field, values in self.in_filters:
            rows = [row for row in rows if str(row.get(field)) in values]
        for field, value in self.gte_filters:
            rows = [row for row in rows if str(row.get(field) or "") >= value]
        for field, value in self.lte_filters:
            rows = [row for row in rows if str(row.get(field) or "") <= value]
        for field in reversed(self.order_fields):
            rows = sorted(rows, key=lambda row: (row.get(field) is None, row.get(field)))
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Response(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self, name)


def _tables(permissions=None):
    return {
        "organisation_users": [
            {
                "organisation_id": "org-1",
                "user_id": "viewer-1",
                "status": "active",
                "role": "viewer",
                "permissions": permissions or {},
            }
        ],
        "accounts": [
            {
                "id": "sales",
                "organisation_id": "org-1",
                "code": "4000",
                "name": "Sales",
                "type": "income",
                "group_name": "Revenue",
                "active": True,
            },
            {
                "id": "rent",
                "organisation_id": "org-1",
                "code": "5000",
                "name": "Rent",
                "type": "expense",
                "group_name": "Operating Expenses",
                "active": True,
            },
            {
                "id": "bank",
                "organisation_id": "org-1",
                "code": "1000",
                "name": "Bank",
                "type": "asset",
                "group_name": "Current Assets",
                "active": True,
            },
        ],
        "account_budgets": [
            {
                "id": "budget-sales-jun",
                "organisation_id": "org-1",
                "account_id": "sales",
                "period_start": "2026-06-01",
                "amount": 1000,
            },
            {
                "id": "budget-rent-jun",
                "organisation_id": "org-1",
                "account_id": "rent",
                "period_start": "2026-06-01",
                "amount": 250,
            },
        ],
    }


def test_get_budget_grid_returns_income_expense_months_and_totals():
    grid = get_budget_grid(
        _DB(_tables()),
        organisation_id="org-1",
        year_start="2026-06-01",
        year_end="2026-07-31",
    )

    assert [account["id"] for account in grid["accounts"]] == ["rent", "sales"]
    assert grid["months"] == [{"year": 2026, "month": 6}, {"year": 2026, "month": 7}]
    assert grid["grand_total"] == 1250.0
    assert grid["column_totals"][0]["total"] == 1250.0
    assert grid["column_totals"][1]["total"] == 0.0


def test_budget_grid_route_allows_reports_view_permission():
    result = budgets_router.budget_grid(
        auth=("viewer-1", _DB(_tables({"reports_view": True}))),
        organisation_id="org-1",
        year_start="2026-06-01",
        year_end="2026-06-30",
    )

    assert result["success"] is True
    assert result["grid"]["grand_total"] == 1250.0


def test_budget_grid_route_rejects_viewer_without_reports_permission(monkeypatch):
    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("budget grid should not load without reports permission")

    monkeypatch.setattr(budgets_router, "get_budget_grid", _fail_if_called)

    with pytest.raises(HTTPException) as exc:
        budgets_router.budget_grid(
            auth=("viewer-1", _DB(_tables())),
            organisation_id="org-1",
            year_start="2026-06-01",
            year_end="2026-06-30",
        )

    assert exc.value.status_code == 403
    assert "permission to view budgets" in exc.value.detail
