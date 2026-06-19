from decimal import Decimal
from datetime import date
from pathlib import Path

import fitz
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import authenticated_user
from app.routers import sales_invoices as sales_invoice_router
from app.services.bank_statement_service import score_invoice_suggestions
from app.services.sales_invoice_documents import render_sales_invoice_pdf
from app.services.sales_invoices import (
    build_rebill_lines,
    calculate_sales_invoice,
    calculate_sales_line,
    issue_sales_invoice,
    post_customer_receipt,
)

PROJECT_ROOT = Path(__file__).parents[1]
CUSTOMER_INVOICING_MIGRATION = (
    PROJECT_ROOT / "app" / "db" / "applied" / "20260611150000_customer_invoicing_ar.sql"
)
SALES_INVOICE_ISSUE_GUARD_MIGRATION = (
    PROJECT_ROOT
    / "supabase"
    / "migrations"
    / "20260618120000_fix_sales_invoice_issue_guard.sql"
)


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)
        self.filters = []
        self.range_filters = []
        self.orders = []
        self.limit_count = None

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def gte(self, key, value):
        self.range_filters.append((key, ">=", value))
        return self

    def lte(self, key, value):
        self.range_filters.append((key, "<=", value))
        return self

    def order(self, field, *_args, desc=False, **_kwargs):
        self.orders.append((field, desc))
        return self

    def limit(self, count):
        self.limit_count = count
        return self

    def execute(self):
        rows = self.rows
        for key, value in self.filters:
            rows = [row for row in rows if row.get(key) == value]
        for key, op, value in self.range_filters:
            if op == ">=":
                rows = [row for row in rows if row.get(key) is not None and str(row.get(key)) >= str(value)]
            elif op == "<=":
                rows = [row for row in rows if row.get(key) is not None and str(row.get(key)) <= str(value)]
        for field, desc in reversed(self.orders):
            rows = sorted(
                rows,
                key=lambda row: (row.get(field) is None, row.get(field)),
                reverse=desc,
            )
        if self.limit_count is not None:
            rows = rows[: self.limit_count]
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


def _sales_invoice_row(
    invoice_id,
    *,
    number,
    customer,
    issue_date,
    due_date,
    total,
    outstanding,
    status="issued",
    payment_status="unpaid",
    created_at="2026-06-01T00:00:00Z",
):
    return {
        "id": invoice_id,
        "organisation_id": "org-1",
        "invoice_number": number,
        "issue_date": issue_date,
        "due_date": due_date,
        "total_amount": total,
        "amount_outstanding": outstanding,
        "status": status,
        "payment_status": payment_status,
        "created_at": created_at,
        "customer_reference": None,
        "purchase_order_number": None,
        "customers": {"legal_name": customer, "trading_name": None, "customer_code": None},
    }


def test_sales_invoice_list_filters_by_issue_date_range(monkeypatch):
    db = _DB(
        {
            "sales_invoices": [
                _sales_invoice_row("old", number="INV-001", customer="Alpha", issue_date="2026-05-31", due_date="2026-06-30", total=100, outstanding=100),
                _sales_invoice_row("in-1", number="INV-002", customer="Beta", issue_date="2026-06-01", due_date="2026-07-01", total=200, outstanding=50),
                _sales_invoice_row("in-2", number="INV-003", customer="Gamma", issue_date="2026-06-30", due_date="2026-07-30", total=300, outstanding=0),
                _sales_invoice_row("new", number="INV-004", customer="Delta", issue_date="2026-07-01", due_date="2026-08-01", total=400, outstanding=400),
            ]
        }
    )
    monkeypatch.setattr(sales_invoice_router, "ensure_org_read", lambda *_args: None)

    result = sales_invoice_router.list_sales_invoices(
        "org-1",
        auth=("user-1", db),
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        sort_by="issue_date",
        sort_dir="asc",
    )

    assert [row["id"] for row in result] == ["in-1", "in-2"]


def test_sales_invoice_list_sorts_supported_invoice_fields(monkeypatch):
    db = _DB(
        {
            "sales_invoices": [
                _sales_invoice_row("a", number="INV-003", customer="Charlie", issue_date="2026-06-03", due_date="2026-07-03", total=300, outstanding=20, status="issued", payment_status="partial"),
                _sales_invoice_row("b", number="INV-001", customer="Alpha", issue_date="2026-06-01", due_date="2026-07-01", total=100, outstanding=90, status="draft", payment_status="unpaid"),
                _sales_invoice_row("c", number="INV-002", customer="Bravo", issue_date="2026-06-02", due_date="2026-07-02", total=200, outstanding=0, status="approved", payment_status="paid"),
            ]
        }
    )
    monkeypatch.setattr(sales_invoice_router, "ensure_org_read", lambda *_args: None)

    expected_first = {
        "invoice_number": "b",
        "issue_date": "b",
        "due_date": "b",
        "total_amount": "b",
        "amount_outstanding": "c",
        "status": "c",
        "payment_status": "c",
    }
    directions = {
        "invoice_number": "asc",
        "issue_date": "asc",
        "due_date": "asc",
        "total_amount": "asc",
        "amount_outstanding": "asc",
        "status": "asc",
        "payment_status": "asc",
    }

    for field, first_id in expected_first.items():
        result = sales_invoice_router.list_sales_invoices(
            "org-1",
            auth=("user-1", db),
            sort_by=field,
            sort_dir=directions[field],
        )
        assert result[0]["id"] == first_id


def test_sales_invoice_list_sorts_by_nested_customer_name(monkeypatch):
    db = _DB(
        {
            "sales_invoices": [
                _sales_invoice_row("charlie", number="INV-003", customer="Charlie Co", issue_date="2026-06-03", due_date="2026-07-03", total=300, outstanding=20),
                _sales_invoice_row("alpha", number="INV-001", customer="Alpha Co", issue_date="2026-06-01", due_date="2026-07-01", total=100, outstanding=90),
                _sales_invoice_row("bravo", number="INV-002", customer="Bravo Co", issue_date="2026-06-02", due_date="2026-07-02", total=200, outstanding=0),
            ]
        }
    )
    monkeypatch.setattr(sales_invoice_router, "ensure_org_read", lambda *_args: None)

    asc = sales_invoice_router.list_sales_invoices(
        "org-1",
        auth=("user-1", db),
        sort_by="customer",
        sort_dir="asc",
    )
    desc = sales_invoice_router.list_sales_invoices(
        "org-1",
        auth=("user-1", db),
        sort_by="customer",
        sort_dir="desc",
    )

    assert [row["id"] for row in asc] == ["alpha", "bravo", "charlie"]
    assert [row["id"] for row in desc] == ["charlie", "bravo", "alpha"]


def test_sales_invoice_list_rejects_invalid_sort_by(monkeypatch):
    app = FastAPI()
    app.include_router(sales_invoice_router.router)
    db = _DB({"sales_invoices": []})
    app.dependency_overrides[authenticated_user] = lambda: ("user-1", db)
    monkeypatch.setattr(sales_invoice_router, "ensure_org_read", lambda *_args: None)
    client = TestClient(app)

    response = client.get(
        "/api/sales-invoices",
        params={"organisation_id": "org-1", "sort_by": "not_a_column"},
    )

    assert response.status_code == 422


def test_item_code_endpoint_returns_distinct_org_codes(monkeypatch):
    db = _DB(
        {
            "sales_invoice_lines": [
                {"organisation_id": "org-1", "item_code": "SVC-001"},
                {"organisation_id": "org-1", "item_code": "CONSULT"},
                {"organisation_id": "org-2", "item_code": "OTHER"},
                {"organisation_id": "org-1", "item_code": ""},
            ],
            "invoice_line_items": [
                {"organisation_id": "org-1", "code": "SVC-001"},
                {"organisation_id": "org-1", "code": "HOSTING"},
                {"organisation_id": "org-2", "code": "OUTSIDE"},
            ],
        }
    )
    calls = []
    monkeypatch.setattr(
        sales_invoice_router,
        "ensure_org_read",
        lambda user_id, org_id: calls.append((user_id, org_id)),
    )

    result = sales_invoice_router.list_sales_invoice_item_codes("org-1", auth=("user-1", db))

    assert calls == [("user-1", "org-1")]
    assert result == {"item_codes": ["CONSULT", "HOSTING", "SVC-001"]}


def test_rebill_bucket_excludes_used_lines_and_other_orgs(monkeypatch):
    db = _DB(
        {
            "sales_invoice_lines": [
                {"organisation_id": "org-1", "source_invoice_line_id": "used-line"},
            ],
            "invoice_line_items": [
                {
                    "id": "used-line",
                    "organisation_id": "org-1",
                    "invoice_extracted_id": "inv-1",
                    "description": "Already billed",
                    "code": "USED",
                    "quantity": 1,
                    "line_total": 100,
                    "invoices_extracted": {
                        "id": "inv-1",
                        "organisation_id": "org-1",
                        "invoice_number": "SUP-001",
                        "invoice_date": "2026-06-01",
                        "supplier_id": "supplier-1",
                        "supplier_name_extracted": "Supplier One",
                    },
                },
                {
                    "id": "open-line",
                    "organisation_id": "org-1",
                    "invoice_extracted_id": "inv-2",
                    "description": "Hosting",
                    "code": "HOSTING",
                    "quantity": 2,
                    "line_total": 200,
                    "invoices_extracted": {
                        "id": "inv-2",
                        "organisation_id": "org-1",
                        "invoice_number": "SUP-002",
                        "invoice_date": "2026-06-02",
                        "supplier_id": "supplier-1",
                        "supplier_name_extracted": "Supplier One",
                    },
                },
                {
                    "id": "other-org-line",
                    "organisation_id": "org-2",
                    "description": "Outside",
                    "code": "OUTSIDE",
                    "line_total": 50,
                    "invoices_extracted": {
                        "id": "inv-3",
                        "organisation_id": "org-2",
                        "supplier_id": "supplier-2",
                    },
                },
            ],
        }
    )
    monkeypatch.setattr(sales_invoice_router, "ensure_org_read", lambda *_args: None)

    result = sales_invoice_router.list_rebill_bucket_items(
        "org-1",
        supplier_id="supplier-1",
        search="host",
        auth=("user-1", db),
    )

    assert [item["id"] for item in result["items"]] == ["open-line"]
    assert result["items"][0]["supplier_name"] == "Supplier One"
    assert result["items"][0]["item_code"] == "HOSTING"
    assert result["items"][0]["source_unit_cost"] == 100


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
    migration = CUSTOMER_INVOICING_MIGRATION.read_text(encoding="utf-8")

    assert "issue_sales_invoice_atomic" in migration
    assert "allocate_sales_document_number" in migration
    assert "post_customer_receipt_atomic" in migration
    assert "prevent_issued_sales_invoice_mutation" in migration
    assert "sales_invoices_select_member" in migration
    assert "customer_receipts_select_member" in migration
    assert "customer-documents" in migration


def test_sales_invoice_issue_guard_migration_allows_atomic_issue_bypass():
    migration = SALES_INVOICE_ISSUE_GUARD_MIGRATION.read_text(encoding="utf-8")

    assert "create or replace function public.prevent_issued_sales_invoice_mutation()" in migration
    assert "Sales invoices must be issued through the atomic issue function" in migration
    assert "Submitted sales invoices cannot be edited" in migration
    assert (
        "if old.status <> 'draft'\n"
        "     and current_setting('app.sales_invoice_issue', true) is distinct from 'on'\n"
        "     and ("
    ) in migration
