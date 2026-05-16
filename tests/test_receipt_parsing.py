from app.services.invoice_extraction.contact_parser import (
    extract_supplier_cell,
    extract_supplier_telephone,
)
from app.services.invoice_extraction.entity_detection import classify_document_direction
from app.services.invoice_extraction.supplier_parser import extract_supplier_from_receipt_text
from app.services.invoice_extraction.template_cleanups import apply_template_cleanups


BUILDERS_RECEIPT_TEXT = """
Massmart Retailer (Pty) Ltd t/a Builders Warehouse
VAT 4880254414
Reg Number 2008/011666/07
Pinetown
3610
Tax Invoice
Receipt
Cash Sale
Telephone 0860994195
Total 123.45
"""


def test_builders_receipt_supplier_prefers_full_legal_trading_name():
    assert (
        extract_supplier_from_receipt_text(BUILDERS_RECEIPT_TEXT)
        == "Massmart Retailer (Pty) Ltd t/a Builders Warehouse"
    )


def test_builders_receipt_defaults_to_cash_card_recipient():
    result = classify_document_direction(
        BUILDERS_RECEIPT_TEXT,
        {
            "name": "APFlow Demo",
            "legal_name": "APFlow Demo (Pty) Ltd",
            "trading_name": "APFlow",
        },
    )

    assert result.issuer_name == "Massmart Retailer (Pty) Ltd t/a Builders Warehouse"
    assert result.recipient_name == "Cash/Card"
    assert result.document_direction == "supplier_invoice_payable"
    assert result.organisation_match_status == "cash_card_receipt"


def test_builders_receipt_cleanup_clears_cash_customer_code_and_unprinted_fields():
    parsed = apply_template_cleanups(
        BUILDERS_RECEIPT_TEXT,
        {
            "supplier_name_extracted": "Builders",
            "supplier_telephone_extracted": "0860994195",
            "supplier_cell_extracted": "0860994195",
            "supplier_email_extracted": None,
            "supplier_acc_email_extracted": None,
            "supplier_fax_extracted": None,
            "supplier_website_extracted": None,
            "supplier_pos_address_extracted": "Pinetown 3610",
            "cus_code_extracted": "Receipt",
        },
    )

    assert parsed["supplier_name_extracted"] == "Massmart Retailer (Pty) Ltd t/a Builders Warehouse"
    assert parsed["issuer_name_extracted"] == parsed["supplier_name_extracted"]
    assert parsed["recipient_name_extracted"] == "Cash/Card"
    assert parsed["cus_code_extracted"] is None
    assert parsed["supplier_telephone_extracted"] == "0860994195"
    assert parsed["supplier_cell_extracted"] is None
    assert parsed["supplier_email_extracted"] is None
    assert parsed["supplier_acc_email_extracted"] is None
    assert parsed["supplier_fax_extracted"] is None
    assert parsed["supplier_website_extracted"] is None
    assert parsed["supplier_del_address_extracted"] == "Pinetown, 3610"
    assert parsed["supplier_pos_address_extracted"] is None


def test_unlabelled_telephone_is_not_copied_to_cell():
    assert extract_supplier_telephone(BUILDERS_RECEIPT_TEXT) == "0860994195"
    assert extract_supplier_cell(BUILDERS_RECEIPT_TEXT) is None
