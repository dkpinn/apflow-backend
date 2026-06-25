import pytest
from fastapi import HTTPException

from app.routers import reports
from app.services.aged_receivables import generate_aged_receivables


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, rows):
        self.rows = list(rows or [])
        self.filters = []
        self.in_filters = []
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
        for field in reversed(self.order_fields):
            rows = sorted(rows, key=lambda row: (row.get(field) is None, row.get(field)))
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Response(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _invoice(
    invoice_id,
    *,
    customer_id="customer-1",
    number="INV",
    issue_date="2026-06-01",
    due_date="2026-07-01",
    total=100,
    paid=0,
    credited=0,
    outstanding=100,
    status="issued",
    payment_status="unpaid",
    document_type="invoice",
):
    return {
        "id": invoice_id,
        "organisation_id": "org-1",
        "customer_id": customer_id,
        "invoice_number": number,
        "issue_date": issue_date,
        "due_date": due_date,
        "currency": "ZAR",
        "total_amount": total,
        "amount_paid": paid,
        "amount_credited": credited,
        "amount_outstanding": outstanding,
        "status": status,
        "payment_status": payment_status,
        "document_type": document_type,
    }


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
        "customers": [
            {
                "id": "customer-1",
                "organisation_id": "org-1",
                "legal_name": "Alpha Customer",
                "trading_name": None,
                "customer_code": "ALPHA",
            },
            {
                "id": "customer-2",
                "organisation_id": "org-1",
                "legal_name": "Beta Customer",
                "trading_name": None,
                "customer_code": "BETA",
            },
        ],
        "sales_invoices": [
            _invoice("current", number="CUR-1", issue_date="2026-06-20", due_date="2026-07-20", outstanding=115, total=115),
            _invoice(
                "old-45",
                number="OLD-45",
                issue_date="2026-04-01",
                due_date="2026-05-11",
                total=200,
                paid=50,
                outstanding=150,
                payment_status="partial",
            ),
            _invoice(
                "old-100",
                customer_id="customer-2",
                number="OLD-100",
                issue_date="2026-02-01",
                due_date="2026-03-17",
                total=300,
                outstanding=300,
                payment_status="overdue",
            ),
            _invoice("draft", number="DRAFT", status="draft", due_date="2026-05-31", outstanding=999),
            _invoice("paid", number="PAID", due_date="2026-05-31", outstanding=0, payment_status="paid"),
            _invoice("future", number="FUT", issue_date="2026-07-01", due_date="2026-07-31", outstanding=500),
            _invoice(
                "credit",
                number="CN-1",
                due_date="2026-05-31",
                outstanding=100,
                document_type="credit_note",
            ),
        ],
    }


def test_aged_receivables_buckets_open_issued_customer_invoices():
    report = generate_aged_receivables(
        _DB(_base_tables()),
        organisation_id="org-1",
        as_at_date="2026-06-25",
    )

    assert report["summary"]["open_invoice_count"] == 3
    assert report["summary"]["total_outstanding"] == 565.0
    assert report["summary"]["buckets"] == {
        "current": 115.0,
        "days_1_30": 0.0,
        "days_31_60": 150.0,
        "days_61_90": 0.0,
        "days_90_plus": 300.0,
    }
    assert any(warning["code"] == "future_invoices_excluded" for warning in report["warnings"])
    assert any(warning["code"] == "zero_balance_excluded" for warning in report["warnings"])

    alpha = next(group for group in report["customers"] if group["customer_id"] == "customer-1")
    assert alpha["customer_name"] == "Alpha Customer"
    assert alpha["customer_code"] == "ALPHA"
    assert alpha["total_outstanding"] == 265.0
    assert [invoice["invoice_number"] for invoice in alpha["invoices"]] == ["OLD-45", "CUR-1"]
    assert alpha["invoices"][0]["paid_amount"] == 50.0


def test_aged_receivables_includes_undated_issued_invoices_as_current():
    tables = _base_tables()
    tables["sales_invoices"].append(
        _invoice("undated", number="NO-DATE", issue_date=None, due_date=None, outstanding=25, total=25)
    )

    report = generate_aged_receivables(_DB(tables), organisation_id="org-1", as_at_date="2026-06-25")

    assert report["summary"]["buckets"]["current"] == 140.0
    assert any(warning["code"] == "undated_invoices_included" for warning in report["warnings"])


def test_aged_receivables_rejects_invalid_as_at_date():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        generate_aged_receivables(
            _DB(_base_tables()),
            organisation_id="org-1",
            as_at_date="25/06/2026",
        )


def test_aged_receivables_route_uses_reports_view_permission(monkeypatch):
    tables = _base_tables()
    tables["organisation_users"][0]["permissions"] = {}

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("aged receivables generation should not run without reports permission")

    monkeypatch.setattr(reports, "generate_aged_receivables", _fail_if_called)

    with pytest.raises(HTTPException) as exc:
        reports.aged_receivables_report(
            auth=("viewer-1", _DB(tables)),
            organisation_id="org-1",
            as_at_date="2026-06-25",
        )

    assert exc.value.status_code == 403
