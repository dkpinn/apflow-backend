from __future__ import annotations

from app.services.cash_flow_forecast import generate_cash_flow_forecast


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, rows):
        self.rows = list(rows or [])
        self.filters = []
        self.in_filters = []

    def select(self, *_args, **_kwargs): return self
    def order(self, *_args, **_kwargs): return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def in_(self, field, values):
        self.in_filters.append((field, {str(value) for value in values}))
        return self

    def gt(self, *_args, **_kwargs): return self
    def gte(self, *_args, **_kwargs): return self
    def lte(self, *_args, **_kwargs): return self

    def execute(self):
        rows = self.rows
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        for field, values in self.in_filters:
            rows = [row for row in rows if str(row.get(field)) in values]
        return _Response(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _invoice(invoice_id, total, *, document_type="tax_invoice", number=None):
    return {
        "id": invoice_id,
        "organisation_id": "org-1",
        "supplier_id": "supplier-1",
        "supplier_name_extracted": "Supplier OCR",
        "invoice_number": number or invoice_id,
        "invoice_date": "2026-06-15",
        "due_date": "2026-07-10",
        "total_amount": total,
        "currency": "ZAR",
        "posting_status": "posted",
        "document_type": document_type,
        "document_direction": "payable",
    }


def _payment(line_id, invoice_id, amount):
    return {
        "id": line_id,
        "organisation_id": "org-1",
        "invoice_extracted_id": invoice_id,
        "statement_line_id": f"statement-{line_id}",
        "payment_id": None,
        "match_status": "matched",
        "matched_amount": amount,
        "expected_amount": amount,
    }


def test_forecast_uses_supplier_invoice_outstanding_balances():
    payments = [
        _payment("partial", "partial-invoice", 40),
        _payment("paid", "paid-invoice", 50),
        _payment("overpaid", "overpaid-invoice", 40),
        _payment("future", "unpaid-invoice", 30),
        _payment("undated", "unpaid-invoice", 10),
    ]
    tables = {
        "bank_accounts": [],
        "sales_invoices": [],
        "recurring_transaction_templates": [],
        "suppliers": [{
            "id": "supplier-1",
            "organisation_id": "org-1",
            "supplier_name": "Acme Supplies",
            "trading_name": None,
        }],
        "invoices_extracted": [
            _invoice("partial-invoice", 100, number="PARTIAL"),
            _invoice("paid-invoice", 50, number="PAID"),
            _invoice("overpaid-invoice", 30, number="OVERPAID"),
            _invoice("unpaid-invoice", 80, number="UNPAID"),
            _invoice("receipt", 999, document_type="card_receipt"),
        ],
        "reconciliation_lines": payments,
        "statement_lines": [
            {
                "id": row["statement_line_id"],
                "organisation_id": "org-1",
                "line_date": "2026-07-05" if row["id"] == "future" else "2026-07-01",
            }
            for row in payments
            if row["id"] != "undated"
        ],
        "payments": [],
    }

    forecast = generate_cash_flow_forecast(
        _DB(tables),
        organisation_id="org-1",
        as_at_date="2026-07-01",
        forecast_days=30,
    )

    payable_items = [
        item
        for week in forecast["weeks"]
        for item in week["items"]
        if item["type"] == "payable"
    ]
    assert forecast["summary"]["total_outflows"] == 140.0
    assert {item["description"]: item["amount"] for item in payable_items} == {
        "Acme Supplies – PARTIAL": 60.0,
        "Acme Supplies – UNPAID": 80.0,
    }
    assert "outstanding balances after matched payments" in forecast["disclaimer"]
