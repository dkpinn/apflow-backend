from decimal import Decimal

from app.services.bank_statement_service import (
    detect_line_duplicates,
    journal_lines_for_bank_transaction,
    line_to_insert,
    money,
    parse_csv_statement,
    parse_text_statement_from_text,
    reversal_lines_for_journal,
    rule_matches_criteria,
    score_rule_suggestions,
    validate_balances,
)
from app.services.extractor_registry import select_bank_cash_extractor


class _Response:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, data=None):
        self.data = data or []

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def in_(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        return _Response(self.data)


class _DB:
    def __init__(self, tables=None):
        self.tables = tables or {}

    def table(self, name):
        return _Query(self.tables.get(name, []))


def test_parse_csv_statement_infers_columns_and_balances():
    csv_bytes = (
        "Date,Description,Reference,Debit,Credit,Balance\n"
        "2026-01-01,Opening supplier payment,INV-100,100.00,,900.00\n"
        "2026-01-02,Customer receipt,RCPT-1,,250.00,1150.00\n"
    ).encode()

    header, lines = parse_csv_statement(csv_bytes, bank_account_id="bank-1", currency="ZAR")

    assert header["statement_period_from"] == "2026-01-01"
    assert header["statement_period_to"] == "2026-01-02"
    assert header["opening_balance"] == 1000.0
    assert header["closing_balance"] == 1150.0
    assert lines[0].signed_amount == Decimal("-100.00")
    assert lines[1].signed_amount == Decimal("250.00")
    assert lines[0].transaction_hash


def test_parse_text_statement_groups_bank_continuation_lines():
    text = """
1/04/2024 Stop Order To Settlement 4 250.00 208 593.69
    Absa Bank E Van Resnsburg
1/04/2024 Ext Stop Order To Settlement 4 250.00 204 343.69
    Std S.A. Pj Steyn
1/04/2024 Ext Stop Order To Settlement 4 250.00 200 093.69
    Firstrand A J Garrett
1/04/2024 Transaction Charge Headoffice * 188.00 199 905.69
1/04/2024 Admin Charge Headoffice * 292.50 199 613.19
    See Charge Statement Detail
1/04/2024 Monthly Acc Fee Headoffice * 190.00 199 423.19
1/04/2024 Immediate Trf Cr Nk 7 735.00 207 158.19
    Nedbank Flat No 8 ..Zodwa..0 1741591307
"""

    header, lines = parse_text_statement_from_text(text, bank_account_id="bank-1", currency="ZAR")

    assert header["extractor_type"] == "bank_statement"
    assert header["parser_strategy"] == "pdf_text_blocks"
    assert len(lines) == 7
    assert lines[1].transaction_type == "Ext Stop Order To"
    assert lines[1].reference == "Settlement"
    assert lines[1].counterparty == "Std S.A. Pj Steyn"
    assert "Firstrand A J Garrett" in lines[2].description
    assert lines[-1].transaction_type == "Immediate Trf Cr"
    assert lines[-1].bank_reference == "1741591307"
    assert lines[-1].credit_amount == Decimal("7735.00")
    assert lines[-1].balance_amount == Decimal("207158.19")
    assert lines[3].debit_amount == Decimal("188.00")
    assert lines[4].raw_lines == [
        "1/04/2024 Admin Charge Headoffice * 292.50 199 613.19",
        "See Charge Statement Detail",
    ]


def test_bank_cash_extractor_registry_keeps_domain_profiles_separate():
    bank = select_bank_cash_extractor(account_type="bank", filename="stmt.pdf", mime_type="application/pdf")
    credit_card = select_bank_cash_extractor(account_type="credit_card", filename="card.csv", mime_type="text/csv")
    loan = select_bank_cash_extractor(account_type="mortgage", filename="bond.pdf", mime_type="application/pdf")

    assert bank.profile.key == "bank_statement"
    assert bank.profile.implemented is True
    assert bank.parser_strategy == "pdf_text_blocks_then_vlm"
    assert credit_card.profile.key == "credit_card_statement"
    assert credit_card.profile.implemented is True
    assert credit_card.parser_strategy == "deterministic_csv"
    assert loan.profile.key == "loan_statement"
    assert loan.profile.implemented is False


def test_detect_line_duplicates_marks_same_upload_and_existing_hashes():
    _header, lines = parse_csv_statement(
        (
            "Date,Description,Amount\n"
            "2026-01-01,Monthly fee,-50.00\n"
            "2026-01-01,Monthly fee,-50.00\n"
        ).encode(),
        bank_account_id="bank-1",
    )
    db = _DB({"bank_statement_lines": [{"transaction_hash": lines[0].transaction_hash}]})

    wrapped, summary = detect_line_duplicates(
        db=db,
        organisation_id="org-1",
        bank_account_id="bank-1",
        lines=lines,
    )

    assert [row["duplicate_status"] for row in wrapped] == ["possible_duplicate", "possible_duplicate"]
    assert summary["duplicate_line_count"] == 2
    assert summary["duplicate_status"] == "possible_duplicates"


def test_validate_balances_checks_opening_and_closing():
    header, lines = parse_csv_statement(
        (
            "Date,Description,Debit,Credit,Balance\n"
            "2026-01-01,Supplier A,100.00,,900.00\n"
            "2026-01-02,Customer B,,250.00,1150.00\n"
        ).encode(),
        bank_account_id="bank-1",
    )

    assert validate_balances(account_current_balance=money("1000"), header=header, lines=lines)["balance_status"] == "balanced"
    assert (
        validate_balances(account_current_balance=money("999.00"), header=header, lines=lines)["balance_status"]
        == "opening_mismatch"
    )

    header["closing_balance"] = 1200.0
    assert (
        validate_balances(account_current_balance=money("1000"), header=header, lines=lines)["balance_status"]
        == "closing_mismatch"
    )


def test_rule_suggestions_apply_direction_patterns_and_amount_limits():
    line = {
        "description": "BANK CHARGES MONTHLY SERVICE FEE",
        "reference": "FEE-01",
        "counterparty": None,
        "signed_amount": -85.5,
    }
    db = _DB(
        {
            "bank_transaction_rules": [
                {
                    "id": "rule-1",
                    "name": "Monthly bank fees",
                    "active": True,
                    "amount_direction": "money_out",
                    "match_type": "contains",
                    "description_pattern": "bank charges",
                    "gl_account_id": "expense-bank-fees",
                    "tracking": {"department": "finance"},
                    "tax_treatment": "standard",
                }
            ]
        }
    )

    suggestions = score_rule_suggestions(db, organisation_id="org-1", bank_account_id="bank-1", line=line)

    assert suggestions[0]["suggestion_type"] == "rule"
    assert suggestions[0]["suggested_account_id"] == "expense-bank-fees"
    assert suggestions[0]["confidence_score"] == 0.85


def test_structured_rule_criteria_supports_and_or_only_matching():
    line = {
        "description": "Immediate Trf Cr Nk",
        "raw_text": "Nedbank Flat No 8 Zodwa 1741591307",
        "counterparty": "Nedbank Flat No 8 Zodwa",
        "reference": "Nk",
        "bank_reference": "1741591307",
    }

    assert rule_matches_criteria(
        {
            "criteria_mode": "and",
            "criteria": [
                {"field": "raw_text", "operator": "contains", "value": "Flat No 8"},
                {"field": "bank_reference", "operator": "contains", "value": "1741591307"},
            ],
        },
        line,
    )
    assert rule_matches_criteria(
        {
            "criteria_mode": "or",
            "criteria": [
                {"field": "counterparty", "operator": "contains", "value": "Missing"},
                {"field": "raw_text", "operator": "contains", "value": "Zodwa"},
            ],
        },
        line,
    )
    assert not rule_matches_criteria(
        {
            "criteria_mode": "only",
            "criteria": [{"field": "description", "operator": "contains", "value": "Flat No 8"}],
        },
        line,
    )


def test_rule_suggestions_use_structured_criteria_against_raw_bank_text():
    line = {
        "description": "Immediate Trf Cr Nk",
        "raw_text": "Nedbank Flat No 8 Zodwa 1741591307",
        "counterparty": "Nedbank Flat No 8 Zodwa",
        "reference": "Nk",
        "bank_reference": "1741591307",
        "signed_amount": 7735.0,
    }
    db = _DB(
        {
            "bank_transaction_rules": [
                {
                    "id": "rule-2",
                    "name": "Flat 8 rental",
                    "active": True,
                    "amount_direction": "money_in",
                    "criteria_mode": "and",
                    "criteria": [{"field": "raw_text", "operator": "contains", "value": "Flat No 8"}],
                    "gl_account_id": "rental-income",
                }
            ]
        }
    )

    suggestions = score_rule_suggestions(db, organisation_id="org-1", bank_account_id="bank-1", line=line)

    assert suggestions[0]["suggested_account_id"] == "rental-income"
    assert "raw_text contains" in suggestions[0]["rationale"]


def test_journal_lines_balance_bank_receipts_and_payments():
    receipt = journal_lines_for_bank_transaction(
        organisation_id="org-1",
        bank_account_gl_id="bank-gl",
        allocation_account_id="sales-gl",
        amount=Decimal("250.00"),
        description="Customer receipt",
    )
    payment = journal_lines_for_bank_transaction(
        organisation_id="org-1",
        bank_account_gl_id="bank-gl",
        allocation_account_id="expense-gl",
        amount=Decimal("-100.00"),
        description="Supplier payment",
    )

    assert sum(row["debit_amount"] for row in receipt) == sum(row["credit_amount"] for row in receipt)
    assert receipt[0]["account_id"] == "bank-gl"
    assert receipt[0]["debit_amount"] == 250.0
    assert sum(row["debit_amount"] for row in payment) == sum(row["credit_amount"] for row in payment)
    assert payment[0]["account_id"] == "expense-gl"
    assert payment[0]["debit_amount"] == 100.0


def test_reversal_lines_swap_debits_and_credits_and_keep_tracking():
    original = [
        {"organisation_id": "org-1", "account_id": "bank-gl", "debit_amount": 250.0, "credit_amount": 0, "tracking": {}, "sort_order": 0},
        {"organisation_id": "org-1", "account_id": "sales-gl", "debit_amount": 0, "credit_amount": 250.0, "tracking": {"dim-1": "val-1"}, "sort_order": 1},
    ]

    reversed_rows = reversal_lines_for_journal(original, description="Reversal")

    assert reversed_rows[0]["account_id"] == "bank-gl"
    assert reversed_rows[0]["credit_amount"] == 250.0
    assert reversed_rows[1]["account_id"] == "sales-gl"
    assert reversed_rows[1]["debit_amount"] == 250.0
    assert reversed_rows[1]["tracking"] == {"dim-1": "val-1"}


def test_line_to_insert_preserves_audit_link_fields():
    _header, lines = parse_csv_statement(
        b"Date,Description,Amount\n2026-01-01,Transfer to savings,-25.00\n",
        bank_account_id="bank-1",
    )

    row = line_to_insert(
        lines[0],
        organisation_id="org-1",
        bank_account_id="bank-1",
        upload_id="upload-1",
        duplicate_status="clear",
    )

    assert row["organisation_id"] == "org-1"
    assert row["bank_statement_upload_id"] == "upload-1"
    assert row["signed_amount"] == -25.0
    assert row["allocation_status"] == "unallocated"
    assert row["posting_status"] == "unposted"
    assert row["raw_lines"]
    assert "extraction_warnings" in row
