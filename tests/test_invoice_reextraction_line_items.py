from types import SimpleNamespace

from app.services.invoice_extraction_service import _reextraction as rx


class _StorageBucket:
    def download(self, _path):
        return b"invoice-bytes"


class _Storage:
    def from_(self, _bucket):
        return _StorageBucket()


def test_re_extract_uses_vlm_when_deep_ocr_has_no_line_items(memory_db, monkeypatch):
    db = memory_db({
        "invoices_extracted": [{
            "id": "invoice-1",
            "organisation_id": "org-1",
            "invoice_raw_id": "raw-1",
            "supplier_id": "supplier-1",
            "supplier_name_extracted": "Example Supplier",
            "invoice_number": "INV-1",
            "subtotal": 300.0,
            "total_amount": 300.0,
            "confidence_score": 0.9,
        }],
        "suppliers": [{
            "id": "supplier-1",
            "organisation_id": "org-1",
            "supplier_name": "Example Supplier",
            "parse_line_items": True,
            "line_items_include_vat": False,
        }],
        "invoice_line_items": [{
            "id": "line-summary",
            "invoice_extracted_id": "invoice-1",
            "organisation_id": "org-1",
            "description": "Purchase from Example Supplier",
            "quantity": 1,
            "line_total": 300.0,
        }],
        "invoice_parse_attempts": [],
        "invoice_audit_events": [],
    })
    db.storage = _Storage()
    monkeypatch.setattr(rx, "supabase", db)
    monkeypatch.setattr(
        rx,
        "get_raw_invoice",
        lambda _raw_id: {
            "id": "raw-1",
            "organisation_id": "org-1",
            "file_path": "org-1/invoice.pdf",
            "file_type": "application/pdf",
        },
    )
    monkeypatch.setattr(rx, "get_organisation", lambda _org_id: {})
    monkeypatch.setattr(
        rx,
        "classify_document_direction",
        lambda *_args: SimpleNamespace(
            issuer_name="Example Supplier",
            recipient_name="Customer",
            document_direction="supplier_invoice_payable",
            organisation_match_status="passed",
            validation_status="passed",
            validation_notes=None,
        ),
    )
    monkeypatch.setattr(rx, "evaluate_invoice_readiness", lambda *_args, **_kw: {"ready": True})

    deep_parsed = {
        "supplier_name_extracted": "Example Supplier",
        "invoice_number": "INV-1",
        "subtotal": 300.0,
        "total_amount": 300.0,
        "currency": "ZAR",
        "confidence_score": 0.95,
        "line_items": [],
    }
    monkeypatch.setattr(
        rx,
        "build_deep_region_parse_attempt",
        lambda *_args: (
            {
                "strategy": "deep_region_ocr",
                "parsed_data": dict(deep_parsed),
                "line_items": [],
                "confidence_score": deep_parsed["confidence_score"],
                "text_preview": "Example Supplier INV-1 total 300",
            },
            {
                "parsed_data": dict(deep_parsed),
                "text": "Example Supplier INV-1 total 300",
                "method": "deep_region_ocr",
                "ocr_confidence": 0.95,
                "regions_attempted": [],
                "confidence_by_region": {},
                "region_ocr": {},
            },
            None,
        ),
    )

    vlm_calls = []

    def _fake_vlm(*_args, **_kwargs):
        vlm_calls.append(True)
        return {
            "data": {
                **deep_parsed,
                "confidence_score": 0.96,
                "line_items": [
                    {
                        "description": "Item A",
                        "quantity": 1,
                        "unit_price": 100.0,
                        "line_total": 100.0,
                        "pricing_notes": "VLM row evidence",
                        "source_bbox": [10, 20, 80, 30],
                    },
                    {"description": "Item B", "quantity": 2, "unit_price": 100.0, "line_total": 200.0},
                ],
            },
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "attempts": [],
        }

    monkeypatch.setattr(rx, "extract_with_vlm_fallback", _fake_vlm)

    result = rx.run_invoice_re_extraction(
        invoice_raw_id="raw-1",
        organisation_id="org-1",
    )

    assert vlm_calls == [True]
    assert result["raw_line_items_found_count"] == 2
    assert result["parse_snapshot_available"] is True
    assert result["line_items_reextract_status"] == "line_items_rebuilt"
    assert result["line_items_inserted_count"] == 2
    assert [row["description"] for row in db.tables["invoice_line_items"]] == ["Item A", "Item B"]
    assert db.tables["invoice_line_items"][0]["pricing_notes"] == {
        "note": "VLM row evidence",
        "source_bbox": [10, 20, 80, 30],
    }
    assert len(db.tables["invoice_parse_attempts"]) == 1
    assert db.tables["invoice_parse_attempts"][0]["line_items"][0]["description"] == "Item A"
