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
        self.update_values = None

    def select(self, *_args):
        return self

    def update(self, values):
        self.update_values = values
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
        if self.update_values is not None:
            for row in rows:
                row.update(self.update_values)
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
        "organisations": [{
            "id": "org-1",
            "vat_registered": True,
            "vat_registration_date": "2026-01-01",
        }],
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


def test_vat_and_creditors_are_split_by_tracking_dimension():
    tables = _tables()
    tables["invoices_extracted"][0].update({
        "subtotal": 300,
        "tax_amount": 45,
        "total_amount": 345,
    })
    tables["invoice_line_items"] = [
        {
            **tables["invoice_line_items"][0],
            "id": "line-1",
            "line_total": 100,
            "tracking": {"department": "north"},
        },
        {
            **tables["invoice_line_items"][0],
            "id": "line-2",
            "description": "Second expense",
            "line_total": 200,
            "tracking": {"department": "south"},
        },
    ]

    prepared = prepare_invoice_gl_posting(
        _DB(tables), invoice_id="invoice-1", org_id="org-1"
    )

    vat_lines = [
        row for row in prepared["journal_lines"] if row["account_id"] == "vat-id"
    ]
    creditor_lines = [
        row for row in prepared["journal_lines"] if row["account_id"] == "payable-id"
    ]
    assert [(row["tracking"], row["debit_amount"]) for row in vat_lines] == [
        ({"department": "north"}, 15.0),
        ({"department": "south"}, 30.0),
    ]
    assert [(row["tracking"], row["credit_amount"]) for row in creditor_lines] == [
        ({"department": "north"}, 115.0),
        ({"department": "south"}, 230.0),
    ]
    assert prepared["total_debit"] == prepared["total_credit"] == 345


def test_preparation_rejects_subtotal_plus_vat_that_differs_from_document_total():
    tables = _tables()
    tables["invoices_extracted"][0].update({
        "subtotal": 22000.0,
        "tax_amount": 2869.57,
        "total_amount": 22000.0,
    })
    tables["invoice_line_items"][0]["line_total"] = 22000.0

    with pytest.raises(ValueError, match="subtotal plus VAT does not match"):
        prepare_invoice_gl_posting(_DB(tables), invoice_id="invoice-1", org_id="org-1")


@pytest.mark.parametrize(
    ("stored_tax", "expected_adjustment"),
    [(14.99, 0.01), (15.01, -0.01)],
)
def test_preparation_absorbs_cent_level_document_difference_into_vat(
    stored_tax,
    expected_adjustment,
):
    tables = _tables()
    tables["invoices_extracted"][0]["tax_amount"] = stored_tax

    prepared = prepare_invoice_gl_posting(
        _DB(tables), invoice_id="invoice-1", org_id="org-1"
    )

    vat_line = next(
        row for row in prepared["journal_lines"] if row["account_id"] == "vat-id"
    )
    creditor_line = prepared["journal_lines"][-1]
    assert vat_line["debit_amount"] == 15
    assert creditor_line["credit_amount"] == 115
    assert prepared["total_debit"] == prepared["total_credit"] == 115
    assert prepared["gross_total"] == 115
    assert prepared["vat_rounding_adjustment"] == expected_adjustment


def test_preparation_does_not_hide_non_vat_cent_difference():
    tables = _tables()
    tables["invoices_extracted"][0].update({"tax_amount": 0, "total_amount": 100.01})

    with pytest.raises(ValueError, match="subtotal plus VAT does not match"):
        prepare_invoice_gl_posting(_DB(tables), invoice_id="invoice-1", org_id="org-1")


def test_persistence_saves_the_absorbed_vat_cent_on_the_invoice():
    tables = _tables()
    tables["invoices_extracted"][0]["tax_amount"] = 14.99
    prepared = prepare_invoice_gl_posting(
        _DB(tables), invoice_id="invoice-1", org_id="org-1"
    )
    db = _DB(
        tables,
        rpc_result={
            "journal_id": "journal-1",
            "total_debit": 115,
            "total_credit": 115,
            "lines": 3,
        },
    )

    persist_prepared_invoice_posting(db, prepared=prepared, user_id="user-1")

    assert tables["invoices_extracted"][0]["tax_amount"] == 15


def test_preparation_rejects_saved_lines_that_differ_from_subtotal():
    tables = _tables()
    tables["invoice_line_items"][0]["line_total"] = 99.0

    with pytest.raises(ValueError, match="saved line items do not match"):
        prepare_invoice_gl_posting(_DB(tables), invoice_id="invoice-1", org_id="org-1")


def test_split_level_vat_treatment_claims_and_expenses_mixed_vat():
    tables = _tables(allocations=[
        {"invoice_line_item_id": "line-1", "organisation_id": "org-1", "expense_account": "6000", "amount": 70, "percent": 70, "vat_treatment": "full", "tracking": {}, "sort_order": 0},
        {"invoice_line_item_id": "line-1", "organisation_id": "org-1", "expense_account": "6000", "amount": 30, "percent": 30, "vat_treatment": "blocked", "tracking": {}, "sort_order": 1},
    ])

    prepared = prepare_invoice_gl_posting(_DB(tables), invoice_id="invoice-1", org_id="org-1")

    expense_lines = [row for row in prepared["journal_lines"] if row["account_id"] == "expense-id"]
    assert [row["debit_amount"] for row in expense_lines] == [70.0, 34.5]
    vat_line = next(row for row in prepared["journal_lines"] if row["account_id"] == "vat-id")
    assert vat_line["debit_amount"] == 10.5


def test_supplier_invoice_vat_is_expensed_before_vat_registration_date():
    tables = _tables()
    tables["organisations"][0]["vat_registration_date"] = "2026-07-01"

    prepared = prepare_invoice_gl_posting(
        _DB(tables),
        invoice_id="invoice-1",
        org_id="org-1",
    )

    assert [line["account_id"] for line in prepared["journal_lines"]] == [
        "expense-id",
        "payable-id",
    ]
    assert prepared["journal_lines"][0]["debit_amount"] == 115.0
    assert prepared["total_debit"] == 115
    assert prepared["vat_control_account"] is None


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


def test_preparation_rejects_system_accounts_as_invoice_expenses():
    tables = _tables()
    tables["accounts"].append({
        "id": "asset-expense-id",
        "organisation_id": "org-1",
        "code": None,
        "name": "Depreciation on Computer Equipment",
        "system_key": "asset_type:type-1:expense",
        "is_system": True,
    })
    tables["invoice_line_items"][0]["expense_account"] = "asset-expense-id"

    with pytest.raises(ValueError, match="no expense account"):
        prepare_invoice_gl_posting(
            _DB(tables),
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


def test_persistence_blocks_supplier_invoice_inside_locked_period():
    prepared = prepare_invoice_gl_posting(
        _DB(_tables()),
        invoice_id="invoice-1",
        org_id="org-1",
    )
    prepared["journal_date"] = "2026-05-31"
    db = _DB(
        {
            "organisation_accounting_periods": [{
                "organisation_id": "org-1",
                "status": "locked",
                "lock_date": "2026-05-31",
            }],
        },
        rpc_result={"journal_id": "journal-1"},
    )

    with pytest.raises(ValueError, match="accounting lock date"):
        persist_prepared_invoice_posting(
            db,
            prepared=prepared,
            user_id="user-1",
        )

    assert db.rpc_call is None
