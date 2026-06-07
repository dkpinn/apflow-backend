import pytest

from app.services.invoice_gl_posting import (
    persist_prepared_invoice_posting,
    prepare_invoice_gl_posting,
)


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)
        self.filters = []
        self.ids = None

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def neq(self, key, value):
        self.filters.append((key, value, True))
        return self

    def in_(self, key, values):
        self.ids = (key, {str(value) for value in values})
        return self

    def limit(self, *_args):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def execute(self):
        rows = self.rows
        for item in self.filters:
            key, value = item[:2]
            if len(item) == 3:
                rows = [row for row in rows if row.get(key) != value]
            else:
                rows = [row for row in rows if row.get(key) == value]
        if self.ids:
            key, values = self.ids
            rows = [row for row in rows if str(row.get(key)) in values]
        return _Result(rows)


class _DB:
    def __init__(self, tables, rpc_result=None):
        self.tables = tables
        self.rpc_result = rpc_result
        self.rpc_call = None

    def table(self, name):
        return _Query(self.tables.get(name, []))

    def rpc(self, name, params):
        self.rpc_call = (name, params)
        return _ResultQuery(self.rpc_result)


class _ResultQuery:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return _Result(self.data)


def _tables(*, tracking_enabled=False, line_tracking=None, allocations=None):
    return {
        "invoices_extracted": [{
            "id": "invoice-1",
            "organisation_id": "org-1",
            "posting_status": "unposted",
            "supplier_id": "supplier-1",
            "supplier_name_extracted": "Supplier",
            "invoice_number": "INV-1",
            "invoice_date": "2026-06-06",
            "subtotal": 100,
            "tax_amount": 15,
            "total_amount": 115,
        }],
        "invoice_line_items": [{
            "id": "line-1",
            "invoice_extracted_id": "invoice-1",
            "organisation_id": "org-1",
            "description": "Expense",
            "line_total": 100,
            "tax_amount": None,
            "vat_treatment": "full",
            "expense_account": "6000",
            "tracking": line_tracking or {},
        }],
        "invoice_line_item_allocations": allocations or [],
        "suppliers": [{
            "id": "supplier-1",
            "organisation_id": "org-1",
            "vat_number": "4111111111",
        }],
        "accounts": [
            {"id": "expense-id", "organisation_id": "org-1", "code": "6000", "name": "Expense", "system_key": None},
            {"id": "vat-id", "organisation_id": "org-1", "code": "8100", "name": "VAT", "system_key": "vat_control"},
            {"id": "payable-id", "organisation_id": "org-1", "code": "2100", "name": "Payables", "system_key": "trade_payables"},
        ],
        "organisation_module_settings": [{
            "organisation_id": "org-1",
            "module_key": "supplier",
            "tracking_enabled": tracking_enabled,
            "required_tracking_dimension_ids": ["department"] if tracking_enabled else [],
        }],
        "tracking_dimensions": [{
            "id": "department",
            "organisation_id": "org-1",
            "name": "Department",
            "active": True,
        }],
    }


def test_preparation_rejects_duplicate_supplier_reference():
    tables = _tables()
    tables["invoices_extracted"].append({
        **tables["invoices_extracted"][0],
        "id": "invoice-2",
    })
    with pytest.raises(ValueError, match="Duplicate invoices"):
        prepare_invoice_gl_posting(
            _DB(tables),
            invoice_id="invoice-1",
            org_id="org-1",
        )


def test_prepared_journal_is_the_complete_vat_aware_posting_preview():
    prepared = prepare_invoice_gl_posting(
        _DB(_tables()),
        invoice_id="invoice-1",
        org_id="org-1",
    )
    assert prepared["gross_total"] == 115
    assert prepared["total_debit"] == 115
    assert [line["account_id"] for line in prepared["journal_lines"]] == [
        "expense-id",
        "vat-id",
        "payable-id",
    ]


def test_preparation_enforces_tracking_and_balanced_allocations():
    with pytest.raises(ValueError, match="Department"):
        prepare_invoice_gl_posting(
            _DB(_tables(tracking_enabled=True)),
            invoice_id="invoice-1",
            org_id="org-1",
        )

    allocations = [{
        "invoice_line_item_id": "line-1",
        "organisation_id": "org-1",
        "expense_account": "6000",
        "amount": 90,
        "tracking": {},
    }]
    with pytest.raises(ValueError, match="do not balance"):
        prepare_invoice_gl_posting(
            _DB(_tables(allocations=allocations)),
            invoice_id="invoice-1",
            org_id="org-1",
        )


def test_persistence_uses_the_atomic_rpc_and_preserves_response_shape():
    prepared = prepare_invoice_gl_posting(
        _DB(_tables()),
        invoice_id="invoice-1",
        org_id="org-1",
    )
    db = _DB(
        {},
        rpc_result={
            "journal_id": "journal-1",
            "total_debit": 115,
            "total_credit": 115,
            "lines": 3,
        },
    )
    result = persist_prepared_invoice_posting(
        db,
        prepared=prepared,
        user_id="user-1",
    )
    assert db.rpc_call[0] == "post_invoice_to_gl_atomic"
    assert result == {
        "success": True,
        "journal_id": "journal-1",
        "total_debit": 115.0,
        "total_credit": 115.0,
        "lines": 3,
        "trade_payables_account": "2100",
        "vat_control_account": "8100",
    }
