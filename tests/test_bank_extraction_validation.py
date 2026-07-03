import json
from pathlib import Path
from decimal import Decimal

from app.services.bank_extraction_validation import (
    evaluate_extracted_against_gold,
    validate_extracted_statement_quality,
    validate_gold_document_integrity,
)
from app.services.bank_statement_extraction.models import ParsedBankLine

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "bank_extraction"


def _load(name: str) -> dict:
    with open(FIXTURES_DIR / name, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _line(**overrides) -> ParsedBankLine:
    return ParsedBankLine(
        line_date=overrides.get("line_date", "2024-01-02"),
        value_date=None,
        description=overrides.get("description", "Card payment"),
        reference=None,
        counterparty=None,
        debit_amount=overrides.get("debit_amount", Decimal("100.00")),
        credit_amount=overrides.get("credit_amount", Decimal("0.00")),
        signed_amount=overrides.get("signed_amount", Decimal("-100.00")),
        balance_amount=overrides.get("balance_amount", Decimal("900.00")),
        currency="ZAR",
        extraction_warnings=overrides.get("extraction_warnings"),
    )


def test_perfect_match_can_allocate():
    gold = _load("example_absa_gold.json")
    extracted = _load("example_absa_extracted_perfect.json")

    result = evaluate_extracted_against_gold(extracted, gold)

    assert result["can_allocate"] is True
    assert result["amount_accuracy"] == 1
    assert result["date_accuracy"] == 1
    assert result["balance_accuracy"] == 1
    assert result["transaction_count_matches"] is True


def test_missing_transaction_blocks_allocation():
    gold = _load("example_absa_gold.json")
    extracted = _load("example_absa_extracted_missing_transaction.json")

    result = evaluate_extracted_against_gold(extracted, gold)

    assert result["can_allocate"] is False
    assert result["missing_transaction_count"] > 0
    assert result["transaction_count_matches"] is False


def test_wrong_amount_blocks_allocation():
    gold = _load("example_absa_gold.json")
    extracted = _load("example_absa_extracted_wrong_amount.json")

    result = evaluate_extracted_against_gold(extracted, gold)

    assert result["can_allocate"] is False
    assert result["amount_accuracy"] < 1
    assert result["critical_errors"]


def test_description_difference_does_not_block_allocation():
    gold = _load("example_absa_gold.json")
    extracted = _load("example_absa_extracted_description_difference.json")

    result = evaluate_extracted_against_gold(extracted, gold)

    assert result["can_allocate"] is True
    assert result["description_accuracy"] < 1
    assert result["warnings"]


def test_closing_balance_mismatch_blocks_allocation():
    gold = _load("example_absa_gold.json")
    extracted = _load("example_absa_extracted_perfect.json")
    extracted["closing_balance"] = float(gold["closing_balance"]) - 1000

    result = evaluate_extracted_against_gold(extracted, gold)

    assert result["can_allocate"] is False
    assert result["critical_errors"]


def test_gold_document_integrity_accepts_reconciled_gold_json():
    gold = _load("example_absa_gold.json")

    assert validate_gold_document_integrity(gold) == []


def test_gold_document_integrity_blocks_unreconciled_gold_json():
    gold = _load("example_absa_gold.json")
    gold["transactions"][0]["running_balance"] = float(gold["transactions"][0]["running_balance"]) + 123
    gold["closing_balance"] = float(gold["closing_balance"]) + 456

    blockers = validate_gold_document_integrity(gold)

    assert any("running balances" in blocker for blocker in blockers)
    assert any("closing balance" in blocker for blocker in blockers)


def test_quality_blocks_missing_statement_balances():
    result = validate_extracted_statement_quality(
        extracted_lines=[_line(balance_amount=None)],
        header={
            "statement_period_from": "2024-01-01",
            "statement_period_to": "2024-01-31",
            "opening_balance": None,
            "closing_balance": None,
            "source_format": "pdf",
            "parser_strategy": "pdf_text_blocks",
        },
        duplicate_summary={"duplicate_line_count": 0},
        balance_summary={"balance_status": "missing_balance"},
    )

    assert result["can_allocate"] is False
    assert "Opening or closing balance missing from statement header" in result["critical_errors"]
    assert any("running-balance evidence" in error for error in result["critical_errors"])


def test_quality_blocks_balanced_vlm_until_manually_reviewed():
    result = validate_extracted_statement_quality(
        extracted_lines=[_line()],
        header={
            "statement_period_from": "2024-01-01",
            "statement_period_to": "2024-01-31",
            "opening_balance": 1000,
            "closing_balance": 900,
            "source_format": "vlm",
            "parser_strategy": "vlm",
        },
        duplicate_summary={"duplicate_line_count": 0},
        balance_summary={"balance_status": "balanced"},
    )

    assert result["can_allocate"] is False
    assert "PDF/image/VLM bank statement extraction requires manual review before allocation" in result["critical_errors"]


def test_quality_blocks_balanced_pdf_until_manually_reviewed():
    result = validate_extracted_statement_quality(
        extracted_lines=[_line()],
        header={
            "statement_period_from": "2024-01-01",
            "statement_period_to": "2024-01-31",
            "opening_balance": 1000,
            "closing_balance": 900,
            "source_format": "pdf",
            "parser_strategy": "pdf_text_blocks",
        },
        duplicate_summary={"duplicate_line_count": 0},
        balance_summary={"balance_status": "balanced"},
    )

    assert result["can_allocate"] is False
    assert "PDF/image/VLM bank statement extraction requires manual review before allocation" in result["critical_errors"]


def test_quality_blocks_balanced_image_until_manually_reviewed():
    result = validate_extracted_statement_quality(
        extracted_lines=[_line()],
        header={
            "statement_period_from": "2024-01-01",
            "statement_period_to": "2024-01-31",
            "opening_balance": 1000,
            "closing_balance": 900,
            "source_format": "image",
            "parser_strategy": "vlm_image",
        },
        duplicate_summary={"duplicate_line_count": 0},
        balance_summary={"balance_status": "balanced"},
    )

    assert result["can_allocate"] is False
    assert "PDF/image/VLM bank statement extraction requires manual review before allocation" in result["critical_errors"]


def test_quality_blocks_unknown_vlm_strategy_until_manually_reviewed():
    result = validate_extracted_statement_quality(
        extracted_lines=[_line()],
        header={
            "statement_period_from": "2024-01-01",
            "statement_period_to": "2024-01-31",
            "opening_balance": 1000,
            "closing_balance": 900,
            "source_format": "unknown",
            "parser_strategy": "vlm_unknown",
        },
        duplicate_summary={"duplicate_line_count": 0},
        balance_summary={"balance_status": "balanced"},
    )

    assert result["can_allocate"] is False
    assert "PDF/image/VLM bank statement extraction requires manual review before allocation" in result["critical_errors"]


def test_quality_blocks_line_level_extraction_warnings():
    result = validate_extracted_statement_quality(
        extracted_lines=[
            _line(extraction_warnings=[{"code": "amount_direction_inferred", "message": "Direction inferred"}])
        ],
        header={
            "statement_period_from": "2024-01-01",
            "statement_period_to": "2024-01-31",
            "opening_balance": 1000,
            "closing_balance": 900,
            "source_format": "pdf",
            "parser_strategy": "pdf_text_blocks",
        },
        duplicate_summary={"duplicate_line_count": 0},
        balance_summary={"balance_status": "balanced"},
    )

    assert result["can_allocate"] is False
    assert any("line-level extraction warning" in error for error in result["critical_errors"])
