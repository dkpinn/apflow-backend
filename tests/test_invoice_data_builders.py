from app.services.invoice_data_builders import build_reextract_update, clear_organisation_vat_from_supplier
from app.services.invoice_extraction.banking_parser import (
    extract_bank_name,
    normalise_extracted_bank_name,
    reconcile_extracted_bank_name,
)


def test_clear_organisation_vat_from_supplier_removes_matching_org_vat() -> None:
    parsed = {
        "vat_number_extracted": "482 023 8352",
        "validation_notes": "Existing note.",
    }

    result = clear_organisation_vat_from_supplier(
        parsed,
        {"vat_number": "4820238352", "tax_number": None},
    )

    assert parsed["vat_number_extracted"] is None
    assert result == {
        "cleared_vat_number": "482 023 8352",
        "matched_organisation_vat_number": "4820238352",
        "note": "Supplier VAT cleared because OCR matched the selected organisation VAT/tax number.",
    }
    assert "Supplier VAT cleared" in parsed["validation_notes"]


def test_clear_organisation_vat_from_supplier_checks_org_tax_number() -> None:
    parsed = {"vat_number_extracted": "ORG-TAX-123"}

    result = clear_organisation_vat_from_supplier(
        parsed,
        {"vat_number": None, "tax_number": "org tax 123"},
    )

    assert parsed["vat_number_extracted"] is None
    assert result is not None
    assert result["matched_organisation_vat_number"] == "org tax 123"


def test_clear_organisation_vat_from_supplier_keeps_different_supplier_vat() -> None:
    parsed = {"vat_number_extracted": "SUPPLIER-999"}

    result = clear_organisation_vat_from_supplier(
        parsed,
        {"vat_number": "ORG-123", "tax_number": "ORG-TAX-123"},
    )

    assert parsed["vat_number_extracted"] == "SUPPLIER-999"
    assert result is None


def test_reextract_refreshes_valid_fnb_name_without_global_confidence_increase() -> None:
    update, improved, _unchanged = build_reextract_update(
        existing={"bank_name_extracted": "Unknown Bank", "confidence_score": 0.90},
        parsed={"bank_name_extracted": "FNB", "confidence_score": 0.80},
    )

    assert update["bank_name_extracted"] == "FNB"
    assert any(row["field"] == "bank_name_extracted" for row in improved)


def test_first_national_bank_is_normalised_to_fnb_from_scan_text() -> None:
    assert extract_bank_name("Banking Details\nBank: First National Bank") == "FNB"


def test_bank_details_label_is_not_a_valid_bank_name() -> None:
    assert normalise_extracted_bank_name("Details:") is None
    assert normalise_extracted_bank_name("Bank Details") is None


def test_deterministic_fnb_replaces_vlm_details_label_after_merge() -> None:
    parsed = {"bank_name_extracted": "Details:"}
    source = "Bank Details:\nFirst National Bank (FNB)\nAccount: 62300843712\nBranch: 220526"

    result = reconcile_extracted_bank_name(parsed, source)

    assert result == "FNB"
    assert parsed["bank_name_extracted"] == "FNB"


def test_reextract_rejects_generic_details_as_bank_name() -> None:
    update, _improved, _unchanged = build_reextract_update(
        existing={"bank_name_extracted": "FNB", "confidence_score": 0.8},
        parsed={"bank_name_extracted": "Details:", "confidence_score": 0.9},
    )

    assert "bank_name_extracted" not in update


def test_reextract_persists_vat_reconciled_totals_without_global_confidence_increase() -> None:
    update, _improved, _unchanged = build_reextract_update(
        existing={"subtotal": 22000.0, "tax_amount": 2869.57, "confidence_score": 0.90},
        parsed={
            "subtotal": 19130.43,
            "tax_amount": 2869.57,
            "vat_reconciled": True,
            "confidence_score": 0.80,
        },
    )

    assert update["subtotal"] == 19130.43
