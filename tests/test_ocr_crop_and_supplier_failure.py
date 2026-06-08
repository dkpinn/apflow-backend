from PIL import Image, ImageDraw

from app.services.invoice_data_builders import (
    MISSING_SUPPLIER_VALIDATION_STATUS,
    apply_missing_supplier_failure,
    merge_supplier_recovery_fields,
)
from app.services.invoice_extraction.receipt_preprocessing import find_document_or_receipt_crop


def _synthetic_receipt_page(*, include_header: bool = True) -> Image.Image:
    image = Image.new("RGB", (1000, 1400), "white")
    draw = ImageDraw.Draw(image)

    if include_header:
        draw.rectangle((360, 420, 650, 455), fill="black")
        draw.text((370, 430), "FONTANA ROSEBANK", fill="white")
        draw.text((360, 485), "VAT NO: 4460107974", fill="black")
        draw.text((360, 515), "ADDRESS: 177 Oxford Rd", fill="black")

    for index in range(9):
        y = 760 + (index * 38)
        draw.text((330, y), f"SPAR MIN WATER       {27.99 + index:.2f}", fill="black")
    draw.line((310, 1120, 720, 1120), fill="black", width=3)
    draw.text((330, 1160), "TOTAL FOR 1 ITEMS        27.99", fill="black")
    return image


def test_receipt_crop_unions_header_with_lower_body():
    crop = find_document_or_receipt_crop(_synthetic_receipt_page(include_header=True))

    assert crop is not None
    _x1, y1, _x2, y2 = crop
    assert y1 < 420
    assert y2 > 1120


def test_receipt_crop_rejects_body_only_crop_that_starts_too_low():
    crop = find_document_or_receipt_crop(_synthetic_receipt_page(include_header=False))

    assert crop is None


def test_supplier_recovery_does_not_change_calculation_fields():
    parsed = {
        "supplier_name_extracted": None,
        "total_amount": 99.99,
        "tax_amount": 13.04,
    }
    text_result = {
        "pages": [
            {
                "supplier_recovery_ocr": {
                    "text": """
                    FONTANA ROSEBANK
                    VAT NO: 4460107974
                    TEL NO: (010) 006 5054
                    TOTAL 27.99
                    """
                }
            }
        ]
    }

    result = merge_supplier_recovery_fields(parsed, text_result)

    assert result["applied"] is True
    assert parsed["supplier_name_extracted"]
    assert parsed["total_amount"] == 99.99
    assert parsed["tax_amount"] == 13.04


def test_missing_supplier_failure_keeps_invoice_editable_status_data():
    parsed = {
        "supplier_name_extracted": None,
        "validation_status": "needs_review",
        "validation_notes": "Low confidence.",
        "confidence_score": 0.82,
    }

    applied = apply_missing_supplier_failure(parsed)

    assert applied is True
    assert parsed["validation_status"] == MISSING_SUPPLIER_VALIDATION_STATUS
    assert "Manual supplier editing is required" in parsed["validation_notes"]
    assert parsed["confidence_score"] == 0.45
