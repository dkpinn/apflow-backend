from app.services.invoice_extraction_service._vat_reconciliation import _auto_reconcile_vat


def test_explicit_vat_is_reconciled_even_when_ocr_misses_supplier_vat_number():
    parsed = {
        "vat_number_extracted": None,
        "subtotal": 22000.0,
        "tax_amount": 2869.57,
        "total_amount": 22000.0,
        "line_items": [{"description": "Consulting Fees Income", "unit_price": 22000.0, "line_total": 22000.0}],
    }

    _auto_reconcile_vat(parsed)

    assert parsed["prices_include_vat_detected"] == "inclusive"
    assert parsed["subtotal"] == 19130.43
    assert parsed["tax_amount"] == 2869.57
    assert parsed["line_items"][0]["line_total"] == 19130.43


def test_missing_vat_number_and_missing_tax_remains_non_vat():
    parsed = {
        "vat_number_extracted": None,
        "subtotal": 100.0,
        "tax_amount": None,
        "total_amount": 100.0,
        "line_items": [{"description": "No VAT", "unit_price": 100.0, "line_total": 100.0}],
    }

    _auto_reconcile_vat(parsed)

    assert parsed["prices_include_vat_detected"] is None
    assert parsed["subtotal"] == 100.0
    assert parsed["tax_amount"] == 0.0
