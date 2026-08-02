from app.services.invoice_extraction_benchmark import (
    BENCHMARK_SCHEMA_VERSION,
    build_invoice_benchmark_snapshot,
    build_pilot_accuracy_summary,
    evaluate_invoice_against_gold,
    validate_gold_snapshot,
)


def _snapshot(*, description="Steel track", total=115.0):
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "document": {
            "document_type": "tax_invoice",
            "document_direction": "supplier_invoice_payable",
            "document_count": 1,
            "supplier_name_extracted": "CAPCO (Pty) Ltd",
            "issuer_name_extracted": "CAPCO (Pty) Ltd",
            "recipient_name_extracted": "Misty Sea Trading 305 (Pty) Ltd",
            "invoice_number": "ING149681",
            "document_reference": "PO-1001",
            "invoice_date": "2026-05-06",
            "due_date": None,
            "currency": "ZAR",
            "subtotal": 100.0,
            "tax_amount": 15.0,
            "total_amount": total,
            "vat_number_extracted": "4600104667",
            "company_registration_number_extracted": "2019/574495/07",
            "cus_code_extracted": "ORG136240",
            "supplier_email_extracted": "accounts@capco.example",
            "supplier_acc_email_extracted": "accounts@capco.example",
            "supplier_telephone_extracted": "0115550100",
            "supplier_fax_extracted": None,
            "supplier_cell_extracted": None,
            "supplier_website_extracted": "capco.example",
            "supplier_del_address_extracted": "1 Steel Road, Johannesburg, 2001",
            "supplier_pos_address_extracted": "PO Box 1, Johannesburg, 2000",
            "bank_account_name_extracted": "CAPCO (Pty) Ltd",
            "bank_name_extracted": "FNB",
            "bank_account_number_extracted": "1234567890",
            "bank_branch_code_extracted": "250655",
            "bank_swift_code_extracted": "FIRNZAJJ",
            "prices_include_vat_detected": "exclusive",
        },
        "line_items": [{
            "code": "DWUT5230",
            "description": description,
            "quantity": 1,
            "unit_price": 100.0,
            "discounted_unit_price": None,
            "discount_percent": None,
            "discount_amount": None,
            "tax_amount": 15.0,
            "line_total": 100.0,
            "vat_treatment": "full",
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


def test_missing_middle_line_does_not_shift_every_following_row():
    gold = _snapshot()
    gold["line_items"] = [
        {**gold["line_items"][0], "code": "A", "description": "First", "line_total": 10},
        {**gold["line_items"][0], "code": "B", "description": "Second", "line_total": 20},
        {**gold["line_items"][0], "code": "C", "description": "Third", "line_total": 30},
    ]
    actual = _snapshot()
    actual["line_items"] = [gold["line_items"][0], gold["line_items"][2]]

    result = evaluate_invoice_against_gold(actual, gold)

    assert result["correction_count"] == 1
    assert result["discrepancies"][0]["field"] == "row"
    assert result["discrepancies"][0]["expected_line"] == 2
    assert result["discrepancies"][0]["actual_line"] is None


def test_extra_middle_line_does_not_shift_every_following_row():
    gold = _snapshot()
    first = {**gold["line_items"][0], "code": "A", "description": "First", "line_total": 10}
    third = {**gold["line_items"][0], "code": "C", "description": "Third", "line_total": 30}
    gold["line_items"] = [first, third]
    actual = _snapshot()
    actual["line_items"] = [
        first,
        {**first, "code": "B", "description": "Unexpected", "line_total": 20},
        third,
    ]

    result = evaluate_invoice_against_gold(actual, gold)

    assert result["correction_count"] == 1
    assert result["discrepancies"][0]["field"] == "row"
    assert result["discrepancies"][0]["expected_line"] is None
    assert result["discrepancies"][0]["actual_line"] == 2


def test_vat_treatment_difference_is_critical():
    gold = _snapshot()
    actual = _snapshot()
    actual["line_items"][0]["vat_treatment"] = "zero_rated"

    result = evaluate_invoice_against_gold(actual, gold)

    assert result["critical_error_count"] == 1
    assert result["critical_errors"][0]["field"] == "vat_treatment"


def test_null_vat_treatment_matches_full_database_default():
    gold = _snapshot()
    actual = _snapshot()
    actual["line_items"][0]["vat_treatment"] = None

    result = evaluate_invoice_against_gold(actual, gold)

    assert result["correction_count"] == 0


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


def test_gold_validation_requires_current_schema():
    gold = _snapshot()
    gold["schema_version"] = BENCHMARK_SCHEMA_VERSION - 1

    assert any("schema is outdated" in blocker for blocker in validate_gold_snapshot(gold))


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
                    "verified_at": "2026-07-01T09:00:00+00:00",
                    "gold_json": {"schema_version": BENCHMARK_SCHEMA_VERSION},
                })
                runs.append({
                    "gold_document_id": case_id,
                    "correct_values": 100,
                    "total_values": 100,
                    "within_two_corrections": True,
                    "critical_error_count": 0,
                    "extraction_rerun": True,
                    "created_at": "2026-07-01T10:00:00+00:00",
                })

    summary = build_pilot_accuracy_summary(cases, runs)

    assert summary["sample_ready"] is True
    assert summary["pilot_gate_passed"] is True
    assert summary["strata_gate_passed"] is True
    assert summary["failing_strata"] == []
    assert summary["accuracy_percent"] == 100


def test_pilot_gate_does_not_allow_strong_strata_to_mask_one_weak_stratum():
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
                    "verified_at": "2026-07-01T09:00:00+00:00",
                    "gold_json": {"schema_version": BENCHMARK_SCHEMA_VERSION},
                })
                weak_stratum = kind == "receipt" and source_format == "image"
                runs.append({
                    "gold_document_id": case_id,
                    "correct_values": 80 if weak_stratum else 100,
                    "total_values": 100,
                    "within_two_corrections": True,
                    "critical_error_count": 0,
                    "extraction_rerun": True,
                    "created_at": "2026-07-01T10:00:00+00:00",
                })

    summary = build_pilot_accuracy_summary(cases, runs)

    assert summary["accuracy"] > 0.95
    assert summary["sample_ready"] is True
    assert summary["strata_gate_passed"] is False
    assert summary["failing_strata"] == ["receipt:image"]
    assert summary["pilot_gate_passed"] is False


def test_pilot_gate_rejects_outdated_locked_gold_snapshots():
    case = {
        "id": "case-1",
        "dataset_split": "locked",
        "document_kind": "invoice",
        "source_format": "pdf",
        "verified_at": "2026-07-01T09:00:00+00:00",
        "gold_json": {"schema_version": BENCHMARK_SCHEMA_VERSION - 1},
    }
    run = {
        "gold_document_id": "case-1",
        "correct_values": 100,
        "total_values": 100,
        "within_two_corrections": True,
        "critical_error_count": 0,
        "extraction_rerun": True,
        "created_at": "2026-07-01T10:00:00+00:00",
    }

    summary = build_pilot_accuracy_summary([case], [run])

    assert summary["outdated_gold_documents"] == 1
    assert summary["scored_documents"] == 0
    assert summary["pilot_gate_passed"] is False


def test_pilot_gate_rejects_score_only_runs_against_corrected_current_data():
    case = {
        "id": "case-1",
        "dataset_split": "locked",
        "document_kind": "invoice",
        "source_format": "pdf",
        "verified_at": "2026-07-01T09:00:00+00:00",
    }
    score_only_run = {
        "gold_document_id": "case-1",
        "correct_values": 100,
        "total_values": 100,
        "within_two_corrections": True,
        "critical_error_count": 0,
        "extraction_rerun": False,
        "created_at": "2026-07-01T10:00:00+00:00",
    }

    summary = build_pilot_accuracy_summary([case], [score_only_run])

    assert summary["scored_documents"] == 0
    assert summary["diagnostic_only_documents"] == 1
    assert summary["pilot_gate_passed"] is False


def test_pilot_gate_rejects_reextract_run_older_than_gold_truth():
    case = {
        "id": "case-1",
        "dataset_split": "locked",
        "document_kind": "invoice",
        "source_format": "pdf",
        "verified_at": "2026-07-01T09:00:00+00:00",
        "updated_at": "2026-07-01T11:00:00+00:00",
    }
    stale_run = {
        "gold_document_id": "case-1",
        "correct_values": 100,
        "total_values": 100,
        "within_two_corrections": True,
        "critical_error_count": 0,
        "extraction_rerun": True,
        "created_at": "2026-07-01T10:00:00+00:00",
    }

    summary = build_pilot_accuracy_summary([case], [stale_run])

    assert summary["scored_documents"] == 0
    assert summary["stale_reextract_documents"] == 1
    assert summary["pilot_gate_passed"] is False


def test_accuracy_summary_reports_actionable_failure_fields_and_strata():
    case = {
        "id": "case-1",
        "dataset_split": "locked",
        "document_kind": "credit_note",
        "source_format": "image",
        "verified_at": "2026-07-01T09:00:00+00:00",
    }
    run = {
        "gold_document_id": "case-1",
        "correct_values": 19,
        "total_values": 20,
        "within_two_corrections": True,
        "critical_error_count": 1,
        "extraction_rerun": True,
        "created_at": "2026-07-01T10:00:00+00:00",
        "discrepancies": [{
            "scope": "document",
            "field": "total_amount",
            "critical": True,
            "expected": 100,
            "actual": 99,
        }],
    }

    summary = build_pilot_accuracy_summary([case], [run])

    assert summary["stratum_results"]["credit_note:image"]["accuracy"] == 0.95
    assert summary["top_failure_fields"] == [{
        "scope": "document",
        "field": "total_amount",
        "correction_count": 1,
        "affected_documents": 1,
        "critical_correction_count": 1,
    }]
