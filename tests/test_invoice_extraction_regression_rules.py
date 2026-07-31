from app.services.invoice_extraction.contact_parser import (
    reconcile_supplier_addresses,
    extract_supplier_delivery_address,
    extract_supplier_postal_address,
    extract_vat_number,
)
from app.services.invoice_extraction.supplier_parser import (
    extract_supplier_name,
    is_valid_supplier_candidate,
)
from app.services.invoice_ocr_pipeline import parse_invoice_fields
from app.services.invoice_extraction.banking_parser import extract_bank_account_number
from app.services.invoice_extraction.contact_parser import extract_vat_number_excluding
from app.services.invoice_extraction.entity_detection import classify_document_direction


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


TOOSH_TEXT = """
TAX INVOICE
Misty Sea Trading 305 PTY Ltd
VAT Number: 4170249066
Invoice Date 8 May 2026
Invoice Number INV-0847
Lukky Vonadik Design (Pty) Ltd t/a Toosh Seating
Suite 116
Private Bag X9951
Sandton
2146
Description Quantity Unit Price VAT Amount ZAR
Fabric: Supalleha, Colour: SL78 8.00 190.00 15% 1,520.00
Subtotal 1,520.00
TOTAL VAT 228.00
TOTAL ZAR 1,748.00
"""


def test_private_bag_postal_address_stops_before_invoice_table():
    assert extract_supplier_postal_address(TOOSH_TEXT) == (
        "Suite 116\nPrivate Bag X9951\nSandton\n2146"
    )


def test_contaminated_addresses_are_reconciled_from_strict_evidence():
    parsed = {
        "supplier_del_address_extracted": "INVOICE, Misty, Sea, Trading, 305, PTY, Ltd",
        "supplier_pos_address_extracted": "Private Bag X9951, Sandton, 2146, Description, Quantity, Unit Price, VAT, Amount ZAR",
    }

    result = reconcile_supplier_addresses(
        parsed,
        TOOSH_TEXT,
        "Lukky Vonadik Design (Pty) Ltd t/a Toosh Seating",
    )

    assert result["supplier_del_address_extracted"] is None
    assert result["supplier_pos_address_extracted"] == "Suite 116\nPrivate Bag X9951\nSandton\n2146"


TOOSH_NATIVE_PDF_TEXT = """Company Registration No: 2014/248504/07.
TAX INVOICE
Misty Sea Trading 305 PTY Ltd
VAT Number: 4170249066
Invoice Date
8 May 2026
Invoice Number
INV-0847
VAT Number
4580271056
Lukky Vonakki Design
(Pty) Ltd t/a Toosh Seating
Suite 116
Private Bag X9951
Sandton
2146
Description
Quantity
Unit Price
VAT
Amount ZAR
Fabric: Supaletha, Colour: SL78
8.00
190.00
15%
1,520.00
Delivery: Local - JHB Customer Address Delivery: Local -
JHB Customer Address
0.00
950.00
0.00
Unwrap and set up
0.00
16.50
0.00
Install bases onto tops
0.00
125.00
0.00
Subtotal
1,520.00
TOTAL VAT
228.00
TOTAL ZAR
1,748.00
Due Date: 15 May 2026
Banking Details:
Lukky Vonakki Design (Pty) Ltd t/a Toosh Seating
First National Bank
Acc: 62728865827
Branch: 250 655
"""


def test_toosh_native_pdf_text_extracts_all_accounting_fields():
    parsed = parse_invoice_fields(TOOSH_NATIVE_PDF_TEXT)
    organisation = {
        "name": "Misty Sea Trading 305 (Pty) Ltd",
        "legal_name": "Misty Sea Trading 305 (Pty) Ltd",
        "vat_number": "4170249066",
    }
    direction = classify_document_direction(
        TOOSH_NATIVE_PDF_TEXT,
        organisation,
        issuer_hint=parsed.get("supplier_name_extracted"),
    )

    assert direction.issuer_name == "Lukky Vonakki Design (Pty) Ltd t/a Toosh Seating"
    assert direction.recipient_name == "Misty Sea Trading 305 PTY Ltd"
    assert parsed["invoice_number"] == "INV-0847"
    assert parsed["invoice_date"] == "2026-05-08"
    assert parsed["due_date"] == "2026-05-15"
    assert parsed["subtotal"] == 1520
    assert parsed["tax_amount"] == 228
    assert parsed["total_amount"] == 1748
    assert extract_vat_number_excluding(TOOSH_NATIVE_PDF_TEXT, [organisation["vat_number"]]) == "4580271056"
    assert extract_bank_account_number(TOOSH_NATIVE_PDF_TEXT) == "62728865827"
    assert parsed["bank_branch_code_extracted"] == "250655"
    assert parsed["supplier_pos_address_extracted"] == "Suite 116\nPrivate Bag X9951\nSandton\n2146"
    assert len(parsed["line_items"]) == 4
    assert parsed["line_items"][0] == {
        "code": None,
        "description": "Fabric: Supaletha, Colour: SL78",
        "quantity": 8.0,
        "unit_price": 190.0,
        "line_total": 1520.0,
        "raw_line": "Fabric: Supaletha, Colour: SL78 | 8.00 | 190.00 | 15% | 1,520.00",
        "tax_amount": 228.0,
        "pricing_notes": {"vat_rate": 15.0},
    }
