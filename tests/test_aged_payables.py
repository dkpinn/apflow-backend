import pytest
from fastapi import HTTPException

from app.routers import reports
from app.services.aged_payables import generate_aged_payables


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
        "suppliers": [
            {
                "id": "supplier-1",
                "organisation_id": "org-1",
                "supplier_name": "Acme Supplies",
                "trading_name": None,
            },
            {
                "id": "supplier-2",
                "organisation_id": "org-1",
                "supplier_name": "Beta Services",
                "trading_name": None,
            },
        ],
        "invoices_extracted": [
            {
                "id": "inv-current",
                "organisation_id": "org-1",
                "supplier_id": "supplier-1",
                "supplier_name_extracted": "Acme OCR",
                "invoice_number": "CUR-1",
                "invoice_date": "2026-06-20",
                "due_date": "2026-07-20",
                "total_amount": 115,
                "currency": "ZAR",
                "posting_status": "posted",
                "document_type": "tax_invoice",
            },
            {
                "id": "inv-45",
                "organisation_id": "org-1",
                "supplier_id": "supplier-1",
                "supplier_name_extracted": "Acme OCR",
                "invoice_number": "OLD-45",
                "invoice_date": "2026-04-01",
                "due_date": "2026-05-11",
                "total_amount": 200,
                "currency": "ZAR",
                "posting_status": "posted",
                "document_type": "tax_invoice",
            },
            {
                "id": "inv-100",
                "organisation_id": "org-1",
                "supplier_id": "supplier-2",
                "supplier_name_extracted": "Beta OCR",
                "invoice_number": "OLD-100",
                "invoice_date": "2026-02-01",
                "due_date": "2026-03-17",
                "total_amount": 300,
                "currency": "ZAR",
                "posting_status": "posted",
                "document_type": "tax_invoice",
            },
            {
                "id": "draft",
                "organisation_id": "org-1",
                "supplier_id": "supplier-1",
                "invoice_number": "DRAFT",
                "invoice_date": "2026-05-01",
                "due_date": "2026-05-31",
                "total_amount": 999,
                "posting_status": "unposted",
                "document_type": "tax_invoice",
            },
            {
                "id": "receipt",
                "organisation_id": "org-1",
                "supplier_id": "supplier-1",
                "invoice_number": "REC",
                "invoice_date": "2026-05-01",
                "due_date": "2026-05-01",
                "total_amount": 50,
                "posting_status": "posted",
                "document_type": "card_receipt",
            },
        ],
        "reconciliation_lines": [
            {
                "id": "pay-1",
                "organisation_id": "org-1",
                "invoice_extracted_id": "inv-45",
                "statement_line_id": "stmt-1",
                "payment_id": None,
                "match_status": "matched",
                "matched_amount": 50,
                "expected_amount": 200,
            },
            {
                "id": "pay-future",
                "organisation_id": "org-1",
                "invoice_extracted_id": "inv-100",
                "statement_line_id": "stmt-future",
                "payment_id": None,
                "match_status": "matched",
                "matched_amount": 300,
                "expected_amount": 300,
            },
            {
                "id": "pay-exception",
                "organisation_id": "org-1",
                "invoice_extracted_id": "inv-current",
                "statement_line_id": "stmt-exception",
                "payment_id": None,
                "match_status": "exception",
                "matched_amount": 115,
                "expected_amount": 115,
            },
        ],
        "statement_lines": [
            {"id": "stmt-1", "organisation_id": "org-1", "line_date": "2026-06-20"},
            {"id": "stmt-future", "organisation_id": "org-1", "line_date": "2026-07-01"},
            {"id": "stmt-exception", "organisation_id": "org-1", "line_date": "2026-06-20"},
        ],
        "payments": [],
    }


def test_aged_payables_buckets_open_posted_supplier_invoices():
    report = generate_aged_payables(
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
    assert any(warning["code"] == "receipts_excluded" for warning in report["warnings"])
    assert any(warning["code"] == "future_payments_excluded" for warning in report["warnings"])

    acme = next(group for group in report["suppliers"] if group["supplier_id"] == "supplier-1")
    assert acme["supplier_name"] == "Acme Supplies"
    assert acme["total_outstanding"] == 265.0
    assert [invoice["invoice_number"] for invoice in acme["invoices"]] == ["OLD-45", "CUR-1"]


def test_aged_payables_uses_payment_date_when_present():
    tables = _base_tables()
    tables["reconciliation_lines"].append(
        {
            "id": "pay-payment-table",
            "organisation_id": "org-1",
            "invoice_extracted_id": "inv-current",
            "statement_line_id": None,
            "payment_id": "payment-1",
            "match_status": "matched",
            "matched_amount": 15,
            "expected_amount": 15,
        }
    )
    tables["payments"].append(
        {
            "id": "payment-1",
            "organisation_id": "org-1",
            "payment_date": "2026-06-24",
        }
    )

    report = generate_aged_payables(_DB(tables), organisation_id="org-1", as_at_date="2026-06-25")

    current = next(
        invoice
        for supplier in report["suppliers"]
        for invoice in supplier["invoices"]
        if invoice["invoice_id"] == "inv-current"
    )
    assert current["paid_amount"] == 15.0
    assert current["outstanding_amount"] == 100.0


def test_aged_payables_rejects_invalid_as_at_date():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        generate_aged_payables(_DB(_base_tables()), organisation_id="org-1", as_at_date="25/06/2026")


def test_aged_payables_route_uses_reports_view_permission(monkeypatch):
    tables = _base_tables()
    tables["organisation_users"][0]["permissions"] = {}

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("aged payables generation should not run without reports permission")

    monkeypatch.setattr(reports, "generate_aged_payables", _fail_if_called)

    with pytest.raises(HTTPException) as exc:
        reports.aged_payables_report(
            auth=("viewer-1", _DB(tables)),
            organisation_id="org-1",
            as_at_date="2026-06-25",
        )

    assert exc.value.status_code == 403
