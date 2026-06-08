from app.services.invoice_data_builders import clear_organisation_vat_from_supplier


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
