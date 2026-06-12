import json
from pathlib import Path

from app.services.bank_extraction_validation import evaluate_extracted_against_gold

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "bank_extraction"


def _load(name: str) -> dict:
    with open(FIXTURES_DIR / name, "r", encoding="utf-8") as handle:
        return json.load(handle)


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
