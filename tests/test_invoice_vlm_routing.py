from app.services.invoice_extraction_service._vlm_routing import (
    is_image_document,
    should_replace_with_vlm,
    should_try_vlm,
    vlm_routing_reasons,
)


def _complete_parse(**updates):
    return {
        "document_type": "tax_invoice",
        "confidence_score": 0.92,
        "supplier_name_extracted": "CAPCO (Pty) Ltd",
        "invoice_number": "INV-1001",
        "total_amount": 115.0,
        "line_items": [{"description": "Steel", "line_total": 100.0}],
        **updates,
    }


def test_complete_high_confidence_digital_parse_does_not_require_vlm():
    assert should_try_vlm(_complete_parse()) is False


def test_visual_source_forces_vlm_even_when_ocr_claims_high_confidence():
    reasons = vlm_routing_reasons(_complete_parse(), force_vlm=True)

    assert "visual_source" in reasons
    assert should_try_vlm(_complete_parse(), force_vlm=True) is True


def test_supplier_matching_own_organisation_forces_visual_recheck():
    organisation = {"name": "Misty Sea Trading 305 (Pty) Ltd"}
    parsed = _complete_parse(supplier_name_extracted="Misty Sea Trading 305 Pty Ltd")

    assert "supplier_matches_organisation" in vlm_routing_reasons(
        parsed,
        organisation=organisation,
    )


def test_location_cluster_supplier_forces_visual_recheck():
    parsed = _complete_parse(supplier_name_extracted="COWIES HILL EURIKA")

    assert "supplier_looks_like_location" in vlm_routing_reasons(parsed)


def test_statement_bypasses_invoice_vlm_routing():
    parsed = _complete_parse(document_type="statement", supplier_name_extracted=None)

    assert vlm_routing_reasons(parsed, force_vlm=True) == []
    assert should_try_vlm(parsed, force_vlm=True) is False


def test_image_detection_uses_mime_or_magic_bytes():
    assert is_image_document(b"not-an-image", "image/jpeg") is True
    assert is_image_document(b"\x89PNG\r\n\x1a\nrest", "application/octet-stream") is True
    assert is_image_document(b"%PDF-1.7", "application/pdf") is False


def test_visual_source_makes_vlm_authoritative_despite_lower_self_confidence():
    assert should_replace_with_vlm(
        "total_amount",
        current_value=999,
        vlm_value=115,
        force_vlm=True,
        routing_reasons=["visual_source"],
        vlm_confidence=0.80,
        text_confidence=0.95,
    ) is True


def test_bad_supplier_identity_makes_visual_supplier_profile_authoritative():
    assert should_replace_with_vlm(
        "supplier_del_address_extracted",
        current_value="Description Quantity VAT Amount",
        vlm_value="1 Steel Road, Johannesburg",
        force_vlm=False,
        routing_reasons=["supplier_looks_like_location"],
        vlm_confidence=0.80,
        text_confidence=0.95,
    ) is True


def test_lower_confidence_vlm_does_not_replace_good_digital_pdf_amount():
    assert should_replace_with_vlm(
        "total_amount",
        current_value=115,
        vlm_value=151,
        force_vlm=False,
        routing_reasons=["missing_supplier"],
        vlm_confidence=0.80,
        text_confidence=0.95,
    ) is False
