from types import SimpleNamespace

from app.services.invoice_extraction.entity_detection import classify_document_direction, name_matches_org
from app.services.invoice_extraction.invoice_number_parser import (
    extract_explicit_reference_number,
    extract_invoice_number,
    is_probable_flight_number,
)
from app.services.invoice_extraction.extraction_rules import infer_strong_document_type
from app.services.invoice_extraction.supplier_parser import extract_supplier_name, is_valid_supplier_candidate
from app.services.invoice_extraction.totals_parser import extract_subtotal
from app.services.invoice_extraction.template_cleanups import apply_template_cleanups
from app.services.invoice_extraction_service._supplier_matching import _correct_extracted_supplier
from app.services.invoice_parse_attempts import select_best_parse_attempt


def _direction(*, issuer=None, recipient=None, direction="unknown"):
    return SimpleNamespace(
        issuer_name=issuer,
        recipient_name=recipient,
        document_direction=direction,
    )


def test_organisation_identity_includes_legal_trading_and_director_names():
    organisation = {
        "name": "Misty Sea",
        "legal_name": "Misty Sea Trading (Pty) Ltd",
        "trading_name": "Misty Sea",
        "director_names": ["Daniel Kerr"],
    }

    assert name_matches_org("Misty Sea Trading Pty Ltd", organisation)
    assert name_matches_org("Misty Sea", organisation)
    assert name_matches_org("Daniel Kerr", organisation)
    assert not name_matches_org("Sign Facets Cape Town (Pty) Ltd", organisation)


def test_organisation_name_can_never_survive_as_supplier():
    parsed = {"supplier_name_extracted": "Misty Sea Trading (Pty) Ltd"}
    organisation = {"legal_name": "Misty Sea Trading (Pty) Ltd"}

    reason, rejected = _correct_extracted_supplier(
        parsed,
        _direction(),
        "Misty Sea Trading (Pty) Ltd\nInvoice Date 2026-06-19",
        organisation,
    )

    assert parsed["supplier_name_extracted"] is None
    assert rejected == "Misty Sea Trading (Pty) Ltd"
    assert "selected organisation" in parsed["validation_notes"]
    assert reason is None


def test_address_returned_by_vlm_is_replaced_with_header_issuer():
    parsed = {"supplier_name_extracted": "Glenvista, Gauteng 2058 Store No"}
    issuer = "Sign Facets Cape Town (Pty) Ltd"

    reason, rejected = _correct_extracted_supplier(
        parsed,
        _direction(issuer=issuer, direction="supplier_invoice_payable"),
        f"{issuer}\nGlenvista, Gauteng 2058\nStatement Date 2026/06/19",
        {"legal_name": "Misty Sea Trading (Pty) Ltd"},
    )

    assert parsed["supplier_name_extracted"] == issuer
    assert rejected == "Glenvista, Gauteng 2058 Store No"
    assert "detected invoice issuer" in reason
    assert not is_valid_supplier_candidate("Glenvista, Gauteng 2058 Store No")


def test_contact_or_status_text_cannot_be_a_supplier():
    assert not is_valid_supplier_candidate("steffen signfacets.co.za / renato signfacets.co.za")
    assert not is_valid_supplier_candidate("Status")


def test_flysafair_reference_wins_over_flight_number():
    text = """
    FlySafair
    BOOKING REFERENCE AND TAX INVOICE
    Booking Reference Number: Q7TZ9P
    Flight Number: FA201
    """

    assert is_probable_flight_number("FA201")
    assert extract_explicit_reference_number(text) == "Q7TZ9P"
    assert extract_invoice_number(text) == "Q7TZ9P"
    assert extract_supplier_name(text) == "FlySafair"


def test_sign_facets_document_is_deterministically_a_statement():
    text = """
    Sign Facets Cape Town (Pty) Ltd
    Statement
    Statement Date 2026/06/19
    Inv. #  Inv. Date  Due On  Days Late  Total  Balance
    Current Statement Total Here.
    """

    assert infer_strong_document_type(text) == "statement"


def test_capco_registered_trading_name_is_issuer_and_selected_org_is_recipient():
    text = """
    TAX INVOICE
    MISTY SEA TRADING 305 (PTY) LTD
    VAT REGISTRATION NO. 4170249066
    Capco
    (Reg. 2019/574495/07)
    VAT REG. NO. 4600104667
    DOC NO ING149681
    """
    organisation = {
        "name": "Renegade Rooster / Switch-d On",
        "legal_name": "Misty Sea Trading 305 (Pty) Ltd",
        "trading_name": "Renegade Rooster / Switch-d On",
    }

    result = classify_document_direction(text, organisation)

    assert result.issuer_name == "CAPCO (Pty) Ltd"
    assert result.recipient_name == "Misty Sea Trading 305 (Pty) Ltd"
    assert result.document_direction == "supplier_invoice_payable"
    assert result.validation_status == "passed"


def test_complete_native_pdf_attempt_beats_high_confidence_broken_ocr():
    native_lines = [{"line_total": 100.0}] * 29
    native = {
        "strategy": "pdf_text",
        "confidence_score": 0.80,
        "parsed_data": {
            "invoice_number": "ING149681",
            "invoice_date": "2026-05-06",
            "subtotal": 2900.0,
            "tax_amount": 435.0,
            "total_amount": 3335.0,
            "supplier_name_extracted": "Misty Sea Trading 305 (Pty) Ltd",
        },
        "line_items": native_lines,
        "text_preview": "CAPCO invoice " * 250,
    }
    broken_ocr = {
        "strategy": "deep_region_ocr",
        "confidence_score": 0.95,
        "candidate_score": 0.918,
        "parsed_data": {
            "invoice_number": "305",
            "invoice_date": "2026-05-06",
            "subtotal": 1214.57,
            "tax_amount": 0.0,
            "total_amount": 1.0,
            "supplier_name_extracted": "MISTY",
        },
        "line_items": [{"line_total": 7.85}, {"line_total": 1206.72}],
        "text_preview": "fragmented OCR",
    }

    assert select_best_parse_attempt([broken_ocr, native]) is native


def test_hyphenated_subtotal_preserves_the_printed_cent_value():
    text = "SUB-TOTAL\n  88608.57\nVAT\n  13291.29\nTOTAL\n 101899.85"

    assert extract_subtotal(text, 101899.85, 13291.29) == 88608.57


def test_capco_visual_identity_profile_is_not_confused_with_customer_details():
    parsed = {
        "supplier_name_extracted": "CAPCO CEILING AND PARTITION COMPONENTS",
        "company_registration_number_extracted": "2019/574495/07",
        "supplier_telephone_extracted": "073 376 7337",
        "vat_number_extracted": "4170249066",
        "line_items": [{
            "code": "DWB76CW27N",
            "description": "76MM BELT C & W CHANNEL 2.750M N/A",
            "line_total": 1098.90,
        }],
    }

    result = apply_template_cleanups("CAPCO CEILING AND PARTITION COMPONENTS", parsed)

    assert result["supplier_name_extracted"] == "CAPCO (Pty) Ltd"
    assert result["vat_number_extracted"] == "4600104667"
    assert result["supplier_telephone_extracted"] == "031 569 6090"
    assert result["supplier_fax_extracted"] == "031 569 6096"
    assert result["supplier_pos_address_extracted"] == "P.O. Box 4203, Riverhorse Valley East, 4017"
    assert result["supplier_del_address_extracted"].startswith("2 Corobrik Place")
    assert result["line_items"][0]["description"] == "76MM BETA C & W CHANNEL 2.750M N/A"
