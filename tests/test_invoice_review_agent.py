from app.services.invoice_review_agent import (
    agent_status_after_regeneration,
    filter_safe_apply_payload,
    generate_invoice_agent_suggestions,
)


def _by_category(suggestions, category):
    return [item for item in suggestions if item["category"] == category]


def test_agent_flags_missing_supplier_and_coding():
    suggestions = generate_invoice_agent_suggestions(
        invoice={"total_amount": 100, "tax_amount": 15},
        line_items=[
            {"id": "line-1", "description": "Office supplies", "line_total": 100},
        ],
        tracking_dimensions=[{"id": "dim-1", "name": "Cost Centre"}],
    )

    assert _by_category(suggestions, "supplier_identity")
    assert _by_category(suggestions, "account_coding")
    assert _by_category(suggestions, "cost_centre")
    assert _by_category(suggestions, "vat_tax")
    assert _by_category(suggestions, "supplier_identity")[0]["target"] == {
        "tab": "supplier",
        "field": "supplier_name_extracted",
    }


def test_agent_suggests_supplier_default_account_apply_payload():
    suggestions = generate_invoice_agent_suggestions(
        invoice={
            "supplier_id": "supplier-1",
            "supplier_name_extracted": "Example Supplier",
            "invoice_number": "INV-1",
            "invoice_date": "2026-05-27",
            "subtotal": 100,
            "tax_amount": 15,
            "total_amount": 115,
        },
        supplier={
            "id": "supplier-1",
            "supplier_name": "Example Supplier",
            "default_expense_account": "6000",
            "vat_number": "4123456789",
            "bank_account_number": "123456789",
        },
        line_items=[
            {"id": "line-1", "description": "Office supplies", "line_total": 100},
        ],
    )

    coding = _by_category(suggestions, "account_coding")
    assert coding
    assert coding[0]["apply_payload"] == {
        "type": "line_item_patch",
        "line_item_id": "line-1",
        "fields": {"expense_account": "6000"},
    }
    assert coding[0]["target"] == {
        "tab": "line_items",
        "line_item_id": "line-1",
        "field": "expense_account",
    }


def test_agent_flags_unbalanced_allocation_split():
    suggestions = generate_invoice_agent_suggestions(
        invoice={
            "supplier_id": "supplier-1",
            "supplier_name_extracted": "Example Supplier",
            "invoice_number": "INV-1",
            "invoice_date": "2026-05-27",
            "subtotal": 100,
            "tax_amount": 0,
            "total_amount": 100,
        },
        supplier={"id": "supplier-1", "supplier_name": "Example Supplier"},
        line_items=[
            {
                "id": "line-1",
                "description": "Shared software",
                "line_total": 100,
                "allocations": [
                    {"amount": 60, "expense_account": "6000"},
                    {"amount": 30, "expense_account": "6000"},
                ],
            },
        ],
    )

    allocation = _by_category(suggestions, "allocation_splits")
    assert allocation
    assert allocation[0]["severity"] == "critical"
    assert allocation[0]["target"] == {
        "tab": "line_items",
        "line_item_id": "line-1",
        "section": "split",
    }


def test_agent_flags_total_mismatch():
    suggestions = generate_invoice_agent_suggestions(
        invoice={
            "supplier_id": "supplier-1",
            "supplier_name_extracted": "Example Supplier",
            "invoice_number": "INV-1",
            "invoice_date": "2026-05-27",
            "subtotal": 100,
            "tax_amount": 15,
            "total_amount": 120,
        },
        supplier={"id": "supplier-1", "supplier_name": "Example Supplier", "vat_number": "4123456789"},
        line_items=[
            {"id": "line-1", "description": "Office supplies", "line_total": 100, "expense_account": "6000"},
        ],
    )

    totals = _by_category(suggestions, "totals")
    assert totals
    assert totals[0]["severity"] == "warning"
    assert totals[0]["target"] == {
        "tab": "extracted",
        "field": "total_amount",
        "section": "totals",
    }


def test_apply_payload_filter_rejects_calculation_and_approval_fields():
    filtered = filter_safe_apply_payload({
        "type": "invoice_patch",
        "fields": {
            "supplier_name_extracted": "Example Supplier",
            "total_amount": 999,
            "approval_status": "approved",
        },
    })

    assert filtered == {
        "type": "invoice_patch",
        "fields": {"supplier_name_extracted": "Example Supplier"},
    }


def test_checked_findings_reopen_when_regenerated():
    assert agent_status_after_regeneration("checked") == "open"
    assert agent_status_after_regeneration("open") == "open"
    assert agent_status_after_regeneration("dismissed") == "dismissed"
    assert agent_status_after_regeneration("applied") == "applied"
