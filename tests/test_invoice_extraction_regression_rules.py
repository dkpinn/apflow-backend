from app.services.invoice_extraction.contact_parser import (
    extract_supplier_delivery_address,
    extract_vat_number,
)
from app.services.invoice_extraction.supplier_parser import (
    extract_supplier_name,
    is_valid_supplier_candidate,
)
from app.services.invoice_ocr_pipeline import parse_invoice_fields


PRODEC_AEP_TEXT = """
PRODEC PAINTS CC
8 GOSHAWK ROAD
FALCON PARK
NEW GERMANY
3620
Vat Registration No: 4520103989
CK No: 1987/017328/23
email info.kzn@prodecpaints.co.za
P O BOX 378
NEW GERMANY
3620
Tel: 031 705-4666
Fax: 031 705-3947
Date
Page
29/10/2024
1
Invoice Number
IN290651
TAX INVOICE
AEP PROPERTIES CC
762 OLD MAIN ROAD
COWIES HILL
DURBAN 3610
SOUTH AFRICA
Deliver To:
EURIKA
31 CAVERSHAM ROAD
ATT: AUBREY - 079 249 1209
DURBAN 3610
SOUTH AFRICA
Vat Registration No
4920218528
Customer No.
AEP050
SalesPerson
103
Terms
1
PO Number
Reference
Qty Ship
Item Number
Description
Unit Price
Disc Price
Extended Price
"""


def test_prodec_aep_invoice_extracts_supplier_identity_from_supplier_header():
    parsed = parse_invoice_fields(PRODEC_AEP_TEXT)

    assert parsed["supplier_name_extracted"] == "PRODEC PAINTS CC"
    assert parsed["vat_number_extracted"] == "4520103989"
    assert parsed["company_registration_number_extracted"] == "1987/017328/23"
    assert parsed["supplier_telephone_extracted"] == "031 705-4666"
    assert parsed["supplier_fax_extracted"] == "031 705-3947"
    assert parsed["supplier_email_extracted"] == "info.kzn@prodecpaints.co.za"


def test_prodec_aep_delivery_address_does_not_absorb_document_metadata():
    address = extract_supplier_delivery_address(PRODEC_AEP_TEXT)

    assert address is not None
    assert "8 GOSHAWK ROAD" in address
    assert "Date" not in address
    assert "Page" not in address
    assert "Invoice Number" not in address
    assert "Description" not in address
    assert "Unit Price" not in address
    assert "31 CAVERSHAM ROAD" not in address


def test_address_shaped_recipient_block_is_not_a_supplier_name():
    assert is_valid_supplier_candidate("SOUTH AFRICA EURIKA 31 CAVERSHAM ROAD") is False

    text = """
    TAX INVOICE
    Deliver To:
    SOUTH AFRICA
    EURIKA
    31 CAVERSHAM ROAD
    DURBAN 3610
    """

    assert extract_supplier_name(text) is None


def test_vat_selection_prefers_supplier_context_over_customer_context():
    text = """
    Bill To:
    AEP PROPERTIES CC
    VAT Number: 4920218528
    From:
    PRODEC PAINTS CC
    Vat Registration No: 4520103989
    CK No: 1987/017328/23
    Tel: 031 705-4666
    """

    assert extract_vat_number(text) == "4520103989"
