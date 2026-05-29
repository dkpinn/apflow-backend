from app.services.invoice_extraction.line_item_parser import extract_line_items
from app.services.invoice_supplier_rules import apply_supplier_processing_rules
from app.services.invoice_line_items import build_line_item_payload


def test_extracts_discounted_unit_price_columns():
    text = """
    Code Description Qty Unit Price Disc Price Extended Price
    0900005 MORTAR MODIFIER 2.00 720,00 504,00 1 008,00
    """

    items = extract_line_items(text, layout_type="row_table")

    assert len(items) == 1
    assert items[0]["unit_price"] == 720.0
    assert items[0]["discounted_unit_price"] == 504.0
    assert items[0]["discount_amount"] == 432.0
    assert items[0]["line_total"] == 1008.0
    assert items[0]["pricing_basis"] == "discounted_unit_price"


def test_infers_discount_when_extended_price_differs_from_unit_times_quantity():
    text = "MORTAR MODIFIER 2.00 720,00 1 008,00"

    items = extract_line_items(text, layout_type="row_table")

    assert len(items) == 1
    assert items[0]["unit_price"] == 720.0
    assert items[0]["discounted_unit_price"] == 504.0
    assert items[0]["discount_amount"] == 432.0
    assert items[0]["line_total"] == 1008.0
    assert items[0]["pricing_basis"] == "extended_price_inferred_discount"


def test_vat_rule_keeps_discounted_lines_that_already_match_subtotal():
    parsed = {
        "subtotal": 1008.0,
        "tax_amount": 151.2,
        "total_amount": 1159.2,
        "line_items": [
            {
                "description": "MORTAR MODIFIER",
                "quantity": 2,
                "unit_price": 720.0,
                "discounted_unit_price": 504.0,
                "discount_amount": 432.0,
                "line_total": 1008.0,
            },
        ],
    }

    result = apply_supplier_processing_rules(parsed, {"line_items_include_vat": True})

    assert result["line_items"][0]["unit_price"] == 720.0
    assert result["line_items"][0]["discounted_unit_price"] == 504.0
    assert result["line_items"][0]["line_total"] == 1008.0


def test_line_item_payload_persists_discount_fields():
    payload = build_line_item_payload(
        invoice_extracted_id="invoice-1",
        organisation_id="org-1",
        line_items=[
            {
                "description": "MORTAR MODIFIER",
                "quantity": 2,
                "unit_price": 720.0,
                "discounted_unit_price": 504.0,
                "discount_amount": 432.0,
                "discount_percent": 30.0,
                "line_total": 1008.0,
                "pricing_basis": "discounted_unit_price",
                "pricing_notes": {"discount_column_mode": "discounted_unit_price"},
            },
        ],
    )

    assert payload[0]["discounted_unit_price"] == 504.0
    assert payload[0]["discount_amount"] == 432.0
    assert payload[0]["discount_percent"] == 30.0
    assert payload[0]["pricing_basis"] == "discounted_unit_price"
    assert payload[0]["pricing_notes"]["discount_column_mode"] == "discounted_unit_price"
