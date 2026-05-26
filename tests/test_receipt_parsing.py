from app.services.invoice_extraction.contact_parser import (
    extract_supplier_cell,
    extract_supplier_telephone,
)
from app.services.invoice_extraction.entity_detection import classify_document_direction
from app.services.invoice_extraction.supplier_parser import extract_supplier_from_receipt_text
from app.services.invoice_extraction.template_cleanups import apply_template_cleanups
from app.services.invoice_ocr_pipeline import parse_invoice_fields


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
            "name": "APPayPal Demo",
            "legal_name": "APPayPal Demo (Pty) Ltd",
            "trading_name": "APPayPal",
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


def test_missing_receipt_number_falls_back_to_document_date_time():
    parsed = parse_invoice_fields(
        """
        SUPERSPAR FONTANA ROSEBANK
        VAT NO: 4460107974
        TAX INVOICE
        SPAR MIN WATER       27.99
        SLIP / TILL / CASHIER / DATE / TIME
        5208 / 008 / 237 / 12.02.25 / 16:02
        TOTAL FOR 1 ITEMS    27.99
        """
    )

    assert parsed["invoice_number"] == "202502121602"
    assert parsed["invoice_number_generated_from_datetime"] is True
    assert parsed["document_time_extracted"] == "1602"


def test_missing_receipt_number_uses_midnight_when_time_missing():
    parsed = parse_invoice_fields(
        """
        SUPERSPAR FONTANA ROSEBANK
        VAT NO: 4460107974
        TAX INVOICE
        DATE 12.02.25
        TOTAL FOR 1 ITEMS    27.99
        """
    )

    assert parsed["invoice_number"] == "202502120000"
    assert parsed["invoice_number_generated_from_datetime"] is True
    assert parsed["document_time_extracted"] is None


def test_real_invoice_number_is_preserved_over_datetime_fallback():
    parsed = parse_invoice_fields(
        """
        Example Supplier
        Invoice Number: INV-12345
        Date: 12.02.25
        Time: 16:02
        Total 27.99
        """
    )

    assert parsed["invoice_number"] == "INV-12345"
    assert parsed["invoice_number_generated_from_datetime"] is False


def test_missing_number_and_date_stays_blank():
    parsed = parse_invoice_fields(
        """
        Example Supplier
        Total 27.99
        """
    )

    assert parsed["invoice_number"] is None
    assert parsed["invoice_number_generated_from_datetime"] is False
