from app.services.invoice_extraction_benchmark import (
    build_invoice_benchmark_snapshot,
    build_pilot_accuracy_summary,
    evaluate_invoice_against_gold,
    validate_gold_snapshot,
)


def _snapshot(*, description="Steel track", total=115.0):
    return {
        "schema_version": 1,
        "document": {
            "document_type": "tax_invoice",
            "document_direction": "supplier_invoice_payable",
            "supplier_name_extracted": "CAPCO (Pty) Ltd",
            "invoice_number": "ING149681",
            "invoice_date": "2026-05-06",
            "due_date": None,
            "currency": "ZAR",
            "subtotal": 100.0,
            "tax_amount": 15.0,
            "total_amount": total,
            "vat_number_extracted": "4600104667",
            "company_registration_number_extracted": "2019/574495/07",
            "cus_code_extracted": "ORG136240",
        },
        "line_items": [{
            "code": "DWUT5230",
            "description": description,
            "quantity": 1,
            "unit_price": 100.0,
            "discount_percent": None,
            "discount_amount": None,
            "tax_amount": 15.0,
            "line_total": 100.0,
        }],
        "source": {},
    }


def test_exact_invoice_scores_one_hundred_percent():
    gold = _snapshot()

    result = evaluate_invoice_against_gold(gold, gold)

    assert result["accuracy"] == 1
    assert result["correction_count"] == 0
    assert result["exact_document"] is True
    assert result["critical_error_count"] == 0


def test_spelling_difference_is_feedback_but_not_critical():
    gold = _snapshot(description="76MM BETA TRACK")
    actual = _snapshot(description="76MM BELT TRACK")

    result = evaluate_invoice_against_gold(actual, gold)

    assert result["correction_count"] == 1
    assert result["within_two_corrections"] is True
    assert result["critical_error_count"] == 0
    assert result["discrepancies"][0]["field"] == "description"


def test_amount_difference_is_a_critical_error():
    gold = _snapshot(total=115.0)
    actual = _snapshot(total=116.0)

    result = evaluate_invoice_against_gold(actual, gold)

    assert result["critical_error_count"] == 1
    assert result["critical_errors"][0]["field"] == "total_amount"


def test_missing_line_is_counted_as_critical_and_inaccurate():
    gold = _snapshot()
    actual = _snapshot()
    actual["line_items"] = []

    result = evaluate_invoice_against_gold(actual, gold)

    assert result["extracted_line_count"] == 0
    assert result["critical_error_count"] == 1
    assert result["accuracy"] < 1


def test_snapshot_preserves_line_sort_order():
    snapshot = build_invoice_benchmark_snapshot(
        invoice={"id": "i1", "invoice_raw_id": "r1", **_snapshot()["document"]},
        raw={"file_name": "invoice.pdf", "file_type": "application/pdf"},
        line_items=[
            {"id": "b", "sort_order": 1, "description": "second"},
            {"id": "a", "sort_order": 0, "description": "first"},
        ],
    )

    assert [row["description"] for row in snapshot["line_items"]] == ["first", "second"]


def test_gold_validation_requires_core_truth_fields():
    gold = _snapshot()
    gold["document"]["supplier_name_extracted"] = None

    assert "Gold document is missing supplier_name_extracted." in validate_gold_snapshot(gold)


def test_pilot_gate_requires_balanced_locked_sample_and_zero_critical_errors():
    cases = []
    runs = []
    index = 0
    for kind in ("invoice", "credit_note", "receipt"):
        for source_format in ("pdf", "image"):
            for _ in range(20):
                index += 1
                case_id = f"case-{index}"
                cases.append({
                    "id": case_id,
                    "dataset_split": "locked",
                    "document_kind": kind,
                    "source_format": source_format,
                })
                runs.append({
                    "gold_document_id": case_id,
                    "correct_values": 100,
                    "total_values": 100,
                    "within_two_corrections": True,
                    "critical_error_count": 0,
                })

    summary = build_pilot_accuracy_summary(cases, runs)

    assert summary["sample_ready"] is True
    assert summary["pilot_gate_passed"] is True
    assert summary["accuracy_percent"] == 100
