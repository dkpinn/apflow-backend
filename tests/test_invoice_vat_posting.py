from app.services.invoice_gl_posting import build_invoice_debit_lines


def _invoice(*, supplier_name="Example Supplier"):
    return {
        "id": "invoice-1",
        "supplier_name_extracted": supplier_name,
        "invoice_number": "INV-1",
        "tax_amount": 15,
    }


def test_invoice_posting_claims_only_full_vat_and_expenses_blocked_vat():
    result = build_invoice_debit_lines(
        organisation_id="org-1",
        invoice=_invoice(),
        line_items=[
            {
                "id": "full-line",
                "description": "Office supplies",
                "line_total": 70,
                "tax_amount": None,
                "vat_treatment": "full",
                "expense_account": "expense-full",
                "tracking": {},
            },
            {
                "id": "blocked-line",
                "description": "Entertainment",
                "line_total": 30,
                "tax_amount": None,
                "vat_treatment": "blocked",
                "expense_account": "expense-blocked",
                "tracking": {},
            },
        ],
        allocations_by_line={},
        supplier_has_vat_number=True,
        vat_control_account_id="vat-control",
    )

    by_account = {row["account_id"]: row for row in result["journal_lines"]}
    assert by_account["expense-full"]["debit_amount"] == 70.0
    assert by_account["expense-blocked"]["debit_amount"] == 34.5
    assert by_account["vat-control"]["debit_amount"] == 10.5
    assert result["claimable_tax"] == 10.5
    assert result["blocked_tax"] == 4.5
    assert sum(row["debit_amount"] for row in result["journal_lines"]) == 115.0


def test_invoice_posting_expenses_all_vat_for_non_vat_supplier():
    result = build_invoice_debit_lines(
        organisation_id="org-1",
        invoice=_invoice(),
        line_items=[
            {
                "id": "full-line",
                "description": "Office supplies",
                "line_total": 100,
                "tax_amount": None,
                "vat_treatment": "full",
                "expense_account": "expense-full",
                "tracking": {},
            }
        ],
        allocations_by_line={},
        supplier_has_vat_number=False,
        vat_control_account_id="vat-control",
    )

    by_account = {row["account_id"]: row for row in result["journal_lines"]}
    assert by_account["expense-full"]["debit_amount"] == 115.0
    assert "vat-control" not in by_account
    assert result["claimable_tax"] == 0
    assert result["blocked_tax"] == 15.0


def test_blocked_vat_follows_invoice_allocation_split():
    result = build_invoice_debit_lines(
        organisation_id="org-1",
        invoice=_invoice(),
        line_items=[
            {
                "id": "blocked-line",
                "description": "Entertainment",
                "line_total": 100,
                "tax_amount": None,
                "vat_treatment": "blocked",
                "expense_account": None,
                "tracking": {},
            }
        ],
        allocations_by_line={
            "blocked-line": [
                {
                    "expense_account": "expense-a",
                    "amount": 60,
                    "tracking": {"cost-centre": "a"},
                },
                {
                    "expense_account": "expense-b",
                    "amount": 40,
                    "tracking": {"cost-centre": "b"},
                },
            ]
        },
        supplier_has_vat_number=True,
        vat_control_account_id="vat-control",
    )

    by_account = {row["account_id"]: row for row in result["journal_lines"]}
    assert by_account["expense-a"]["debit_amount"] == 69.0
    assert by_account["expense-b"]["debit_amount"] == 46.0
    assert by_account["expense-a"]["tracking"] == {"cost-centre": "a"}
    assert "vat-control" not in by_account
    assert sum(row["debit_amount"] for row in result["journal_lines"]) == 115.0


def test_exempt_and_zero_rated_lines_do_not_claim_vat():
    result = build_invoice_debit_lines(
        organisation_id="org-1",
        invoice=_invoice(),
        line_items=[
            {
                "id": "exempt-line",
                "description": "Exempt service",
                "line_total": 60,
                "tax_amount": None,
                "vat_treatment": "exempt",
                "expense_account": "expense-exempt",
                "tracking": {},
            },
            {
                "id": "zero-line",
                "description": "Zero-rated goods",
                "line_total": 40,
                "tax_amount": None,
                "vat_treatment": "zero_rated",
                "expense_account": "expense-zero",
                "tracking": {},
            },
        ],
        allocations_by_line={},
        supplier_has_vat_number=True,
        vat_control_account_id="vat-control",
    )

    assert result["claimable_tax"] == 0
    assert result["blocked_tax"] == 15.0
    assert sum(row["debit_amount"] for row in result["journal_lines"]) == 115.0
