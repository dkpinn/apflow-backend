from decimal import Decimal
from pathlib import Path

import fitz
import pytest

from app.services.bank_statement_service import score_invoice_suggestions
from app.services.sales_invoice_documents import render_sales_invoice_pdf
from app.services.sales_invoices import (
    build_rebill_lines,
    calculate_sales_invoice,
    calculate_sales_line,
    issue_sales_invoice,
    post_customer_receipt,
)


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)
        self.filters = []

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        rows = self.rows
        for key, value in self.filters:
            rows = [row for row in rows if row.get(key) == value]
        return _Result(rows)


class _Rpc:
    def __init__(self, db, name, params):
        self.db = db
        self.name = name
        self.params = params

    def execute(self):
        self.db.calls.append((self.name, self.params))
        return _Result(self.db.rpc_results.get(self.name, {}))


class _DB:
    def __init__(self, tables=None, rpc_results=None):
        self.tables = tables or {}
        self.rpc_results = rpc_results or {}
        self.calls = []

    def table(self, name):
        return _Query(self.tables.get(name, []))

    def rpc(self, name, params):
        return _Rpc(self, name, params)


def test_sales_line_calculation_handles_exclusive_inclusive_and_non_taxable():
    exclusive = calculate_sales_line(
        {
            "description": "Consulting",
            "quantity": 2,
            "unit_price": 100,
            "discount_percent": 10,
            "vat_treatment": "standard",
            "vat_rate": 15,
        }
    )
    assert exclusive["net_amount"] == 180
    assert exclusive["tax_amount"] == 27
    assert exclusive["gross_amount"] == 207

    inclusive = calculate_sales_line(
        {
            "description": "Inclusive service",
            "quantity": 1,
            "unit_price": 115,
            "prices_include_vat": True,
            "vat_treatment": "standard",
            "vat_rate": 15,
        }
    )
    assert inclusive["net_amount"] == 100
    assert inclusive["tax_amount"] == 15
    assert inclusive["gross_amount"] == 115

    exempt = calculate_sales_line(
        {
            "description": "Exempt service",
            "quantity": 1,
            "unit_price": 100,
            "vat_treatment": "exempt",
            "vat_rate": 15,
        }
    )
    assert exempt["tax_amount"] == 0
    assert exempt["gross_amount"] == 100


def test_invoice_calculation_balances_mixed_lines_and_rounding():
    calculated = calculate_sales_invoice(
        [
            {
                "description": "Standard",
                "quantity": 3,
                "unit_price": Decimal("33.3333"),
                "vat_treatment": "standard",
                "vat_rate": 15,
            },
            {
                "description": "Zero",
                "quantity": 1,
                "unit_price": 50,
                "vat_treatment": "zero_rated",
            },
        ]
    )
    assert calculated["subtotal"] == 150
    assert calculated["tax_total"] == 15
    assert calculated["total_amount"] == 165


def test_rebill_lines_preserve_cost_provenance_and_apply_markup():
    lines = build_rebill_lines(
        [
            {
                "id": "supplier-line-1",
                "invoice_extracted_id": "supplier-invoice-1",
                "description": "Hosting",
                "quantity": 2,
                "line_total": 200,
            }
        ],
        default_revenue_account_id="revenue-1",
        markup_percent=25,
    )
    assert lines[0]["source_invoice_line_id"] == "supplier-line-1"
    assert lines[0]["source_unit_cost"] == Decimal("100")
    assert lines[0]["unit_price"] == 125
    assert lines[0]["net_amount"] == 250
    assert lines[0]["margin_amount"] == 50


def test_issue_and_receipt_services_use_atomic_rpcs():
    db = _DB(
        rpc_results={
            "issue_sales_invoice_atomic": {"journal_id": "journal-1"},
            "post_customer_receipt_atomic": {"receipt_id": "receipt-1"},
        }
    )
    issued = issue_sales_invoice(
        db,
        organisation_id="org-1",
        sales_invoice_id="sales-1",
        actor_user_id="user-1",
    )
    receipt = post_customer_receipt(
        db,
        organisation_id="org-1",
        customer_id="customer-1",
        bank_account_id="bank-1",
        receipt_date="2026-06-11",
        amount=50,
        currency="ZAR",
        reference="INV-000001",
        notes=None,
        allocations=[{"sales_invoice_id": "sales-1", "amount": 50}],
        actor_user_id="user-1",
        idempotency_key="bank-line-1",
    )
    assert issued["journal_id"] == "journal-1"
    assert receipt["receipt_id"] == "receipt-1"
    assert [call[0] for call in db.calls] == [
        "issue_sales_invoice_atomic",
        "post_customer_receipt_atomic",
    ]


def test_bank_receipt_suggestion_matches_open_sales_invoice():
    db = _DB(
        {
            "sales_invoices": [
                {
                    "id": "sales-1",
                    "organisation_id": "org-1",
                    "document_type": "invoice",
                    "status": "issued",
                    "invoice_number": "INV-000123",
                    "customer_id": "customer-1",
                    "total_amount": 115,
                    "amount_outstanding": 115,
                    "customer_snapshot": {"legal_name": "Acme Client"},
                }
            ],
            "accounts": [
                {
                    "id": "receivables-1",
                    "organisation_id": "org-1",
                    "system_key": "trade_receivables",
                }
            ],
        }
    )
    suggestions = score_invoice_suggestions(
        db,
        organisation_id="org-1",
        line={
            "signed_amount": 115,
            "reference": "Payment INV-000123",
            "counterparty": "Acme Client",
        },
    )
    assert suggestions[0]["suggestion_type"] == "receivable_invoice"
    assert suggestions[0]["matched_sales_invoice_id"] == "sales-1"
    assert suggestions[0]["suggested_account_id"] == "receivables-1"


def test_pdf_contains_required_sales_document_text_and_multiple_pages():
    invoice = {
        "id": "sales-1",
        "organisation_id": "org-1",
        "document_type": "invoice",
        "invoice_number": "INV-000001",
        "issue_date": "2026-06-11",
        "due_date": "2026-07-11",
        "currency": "ZAR",
        "subtotal": 2500,
        "tax_total": 375,
        "total_amount": 2875,
        "issuer_snapshot": {
            "name": "APPayPal Test Company",
            "vat_number": "4123456789",
            "address_line_1": "1 Main Road",
        },
        "customer_snapshot": {
            "legal_name": "Customer One",
            "vat_number": "4987654321",
            "billing_address": "2 Client Road",
        },
        "branding_snapshot": {},
    }
    lines = [
        {
            "description": f"Service {index}",
            "quantity": 1,
            "net_amount": 100,
            "tax_amount": 15,
            "gross_amount": 115,
        }
        for index in range(25)
    ]
    payload = render_sales_invoice_pdf(invoice, lines)
    assert payload == render_sales_invoice_pdf(invoice, lines)
    document = fitz.open(stream=payload, filetype="pdf")
    assert document.page_count == 2
    text = "\n".join(page.get_text() for page in document)
    assert "TAX INVOICE" in text
    assert "INV-000001" in text
    assert "APPayPal Test Company" in text
    assert "Customer One" in text
    document.close()


def test_sales_line_rejects_invalid_quantity_and_discount():
    with pytest.raises(ValueError, match="quantity"):
        calculate_sales_line({"description": "Bad", "quantity": 0, "unit_price": 10})
    with pytest.raises(ValueError, match="percentage"):
        calculate_sales_line(
            {
                "description": "Bad",
                "quantity": 1,
                "unit_price": 10,
                "discount_percent": 101,
            }
        )


def test_customer_invoicing_migration_contains_atomic_and_rls_guards():
    migration = (
        Path(__file__).parents[1]
        / "supabase"
        / "migrations"
        / "20260611150000_customer_invoicing_ar.sql"
    ).read_text(encoding="utf-8")

    assert "issue_sales_invoice_atomic" in migration
    assert "allocate_sales_document_number" in migration
    assert "post_customer_receipt_atomic" in migration
    assert "prevent_issued_sales_invoice_mutation" in migration
    assert "sales_invoices_select_member" in migration
    assert "customer_receipts_select_member" in migration
    assert "customer-documents" in migration
