from app.services.invoice_line_items import build_line_item_payload
from app.services.invoice_supplier_rules import apply_supplier_processing_rules
from app.services.supplier_matcher import attempt_supplier_auto_link


class _SupplierQuery:
    def __init__(self, suppliers):
        self.suppliers = suppliers

    def select(self, *_args):
        return self

    def eq(self, *_args):
        return self

    def execute(self):
        return type("Result", (), {"data": self.suppliers})()


class _SupplierClient:
    def __init__(self, suppliers):
        self.suppliers = suppliers

    def table(self, name):
        assert name == "suppliers"
        return _SupplierQuery(self.suppliers)


def test_supplier_rule_defaults_preserve_extracted_line_items():
    parsed = {
        "supplier_name_extracted": "Example Supplier",
        "total_amount": 230.0,
        "line_items": [
            {"description": "Item A", "quantity": 2, "unit_price": 50.0, "line_total": 100.0},
            {"description": "Item B", "quantity": 1, "unit_price": 130.0, "line_total": 130.0},
        ],
    }

    result = apply_supplier_processing_rules(parsed, {})

    assert result["line_items"] == parsed["line_items"]
    assert result["invoice_patch"] == {}


def test_supplier_auto_link_uses_exact_identifiers_not_name_only():
    supabase = _SupplierClient([
        {
            "id": "supplier-1",
            "supplier_name": "Example Supplier",
            "vat_number": "411 111 1111",
            "company_registration_number": "REG-1",
            "account_number": "ACC-1",
            "bank_account_number": "123-456",
        },
    ])

    assert attempt_supplier_auto_link(
        supabase,
        org_id="org-1",
        supplier_name_extracted="Example Supplier",
    ) is None

    assert attempt_supplier_auto_link(
        supabase,
        org_id="org-1",
        supplier_name_extracted="Different Name",
        vat_number_extracted="4111111111",
    ) == "supplier-1"


def test_supplier_rule_parse_line_items_false_creates_summary_line():
    parsed = {
        "supplier_name_extracted": "Example Supplier",
        "subtotal": 200.0,
        "total_amount": 230.0,
        "line_items": [
            {"description": "Item A", "quantity": 2, "unit_price": 50.0, "line_total": 100.0},
        ],
    }

    result = apply_supplier_processing_rules(parsed, {"parse_line_items": False})

    assert result["line_items"] == [{
        "description": "Purchase from Example Supplier",
        "quantity": 1,
        "unit_price": 200.0,
        "line_total": 200.0,
    }]


def test_supplier_rule_strips_vat_inclusive_unit_prices():
    parsed = {
        "supplier_name_extracted": "Example Supplier",
        "subtotal": 200.0,
        "tax_amount": 30.0,
        "total_amount": 230.0,
        "line_items": [
            {"description": "Item A", "quantity": 2, "unit_price": 115.0, "line_total": 230.0},
        ],
    }

    result = apply_supplier_processing_rules(parsed, {"line_items_include_vat": True})

    assert result["line_items"][0]["unit_price"] == 100.0
    assert result["line_items"][0]["line_total"] == 200.0


def test_supplier_rule_applies_default_expense_account_to_invoice_and_lines():
    parsed = {
        "supplier_name_extracted": "Example Supplier",
        "total_amount": 100.0,
        "line_items": [
            {"description": "Item A", "quantity": 1, "unit_price": 100.0, "line_total": 100.0},
        ],
    }

    result = apply_supplier_processing_rules(
        parsed,
        {"default_expense_account": "6000/Office"},
    )

    assert result["invoice_patch"] == {"expense_account": "6000/Office"}
    assert result["line_items"][0]["expense_account"] == "6000/Office"


def test_line_item_payload_persists_expense_account():
    payload = build_line_item_payload(
        invoice_extracted_id="invoice-1",
        organisation_id="org-1",
        line_items=[{
            "description": "Item A",
            "quantity": 1,
            "unit_price": 100.0,
            "line_total": 100.0,
            "expense_account": "6000/Office",
        }],
    )

    assert payload[0]["expense_account"] == "6000/Office"
