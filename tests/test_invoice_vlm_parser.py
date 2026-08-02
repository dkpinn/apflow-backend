import json

from app.services.invoice_extraction.vlm_parser import (
    VLM_MERGE_FIELDS,
    normalise_vlm_json_response,
    vlm_invoice_json_schema,
)


def test_vlm_schema_extracts_a_reference_separately_from_invoice_number():
    parsed = normalise_vlm_json_response(json.dumps({
        "invoice_number": "INV-1001",
        "document_reference": "PO-9007",
    }))

    assert parsed["invoice_number"] == "INV-1001"
    assert parsed["document_reference"] == "PO-9007"
    assert "document_reference" in VLM_MERGE_FIELDS
    assert "document_reference" in vlm_invoice_json_schema()["properties"]


def test_vlm_schema_preserves_mixed_line_vat_evidence():
    parsed = normalise_vlm_json_response(json.dumps({
        "line_items": [
            {
                "description": "Standard-rated item",
                "line_total": 100,
                "tax_amount": 15,
                "vat_treatment": "full",
                "source_bbox": [10, 20, 90, 30],
                "source_page": 2,
            },
            {
                "description": "Zero-rated item",
                "line_total": 50,
                "tax_amount": 0,
                "vat_treatment": "zero_rated",
            },
        ],
    }))

    assert parsed["line_items"][0]["tax_amount"] == 15
    assert parsed["line_items"][0]["vat_treatment"] == "full"
    assert parsed["line_items"][0]["source_page"] == 2
    assert parsed["line_items"][1]["tax_amount"] == 0
    assert parsed["line_items"][1]["vat_treatment"] == "zero_rated"
