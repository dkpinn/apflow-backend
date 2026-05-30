from app.services.invoice_readiness import build_invoice_readiness_decision


def _ready_invoice(**overrides):
    invoice = {
        "id": "invoice-1",
        "organisation_id": "org-1",
        "invoice_raw_id": "raw-1",
        "supplier_id": "supplier-1",
        "supplier_name_extracted": "Example Supplier",
        "invoice_number": "INV-1",
        "invoice_date": "2026-05-30",
        "subtotal": 100.0,
        "tax_amount": 0.0,
        "total_amount": 100.0,
        "confidence_score": 0.92,
        "validation_status": "passed",
        "document_direction": "supplier_invoice_payable",
        "document_type": "tax_invoice",
        "document_count": 1,
        "expense_account": "6000/Office",
        "bank_name_extracted": "FNB",
        "bank_account_number_extracted": "123456789",
        "bank_branch_code_extracted": "250655",
    }
    invoice.update(overrides)
    return invoice


def _ready_supplier():
    return {
        "id": "supplier-1",
        "supplier_name": "Example Supplier",
        "bank_name": "FNB",
        "bank_account_number": "123456789",
        "bank_branch_code": "250655",
    }


def test_readiness_promotes_clean_repeat_supplier_invoice():
    decision = build_invoice_readiness_decision(
        invoice=_ready_invoice(),
        supplier=_ready_supplier(),
        line_items=[{
            "description": "Office expense",
            "line_total": 100.0,
            "expense_account": "6000/Office",
        }],
    )

    assert decision["ready"] is True
    assert decision["review_status"] == "reviewed"
    assert decision["blockers"] == []


def test_readiness_blocks_missing_supplier():
    decision = build_invoice_readiness_decision(
        invoice=_ready_invoice(supplier_id=None),
        supplier=None,
        line_items=[{"description": "Office expense", "line_total": 100.0}],
    )

    assert decision["ready"] is False
    assert decision["review_status"] == "needs_info"
    assert any(item["category"] == "required_fields" for item in decision["blockers"])


def test_readiness_blocks_sales_invoice_direction():
    decision = build_invoice_readiness_decision(
        invoice=_ready_invoice(
            document_direction="customer_sales_invoice",
            validation_status="needs_review",
            validation_notes="Selected organisation appears to be the invoice issuer.",
        ),
        supplier=_ready_supplier(),
        line_items=[{"description": "Office expense", "line_total": 100.0, "expense_account": "6000/Office"}],
    )

    assert decision["ready"] is False
    assert any(item["category"] == "document_direction" for item in decision["blockers"])


def test_readiness_blocks_duplicate_reference():
    decision = build_invoice_readiness_decision(
        invoice=_ready_invoice(),
        supplier=_ready_supplier(),
        line_items=[{"description": "Office expense", "line_total": 100.0, "expense_account": "6000/Office"}],
        duplicate_count=1,
    )

    assert decision["ready"] is False
    assert any(item["category"] == "duplicate_risk" for item in decision["blockers"])


def test_readiness_accepts_credit_note_with_positive_allocation_magnitudes():
    decision = build_invoice_readiness_decision(
        invoice=_ready_invoice(
            document_type="credit_note",
            subtotal=-100.0,
            total_amount=-100.0,
        ),
        supplier=_ready_supplier(),
        line_items=[{
            "description": "Credit reversal",
            "line_total": -100.0,
            "expense_account": "6000/Office",
            "allocations": [
                {"expense_account": "6000/Office", "amount": 60.0},
                {"expense_account": "6000/Office", "amount": 40.0},
            ],
        }],
    )

    assert decision["ready"] is True
