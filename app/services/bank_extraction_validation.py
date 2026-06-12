"""Validation and benchmarking layer for bank statement extraction.

Compares extracted bank statement data against manually verified "gold standard"
files and runs internal consistency checks (running balance continuity, closing
balance reconciliation, duplicate detection). The result of these checks is the
control point that determines whether extracted data is allowed to flow into
allocation/posting (`can_allocate`) — not the extraction engine itself.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Optional

from app.services.bank_statement_extraction.common import dec_to_float, money, normalize_text, parse_date
from app.services.bank_statement_extraction.models import ParsedBankLine

TOLERANCE = Decimal("0.01")

# Amount and balance accuracy matter most; description accuracy matters least.
_WEIGHT_AMOUNT = Decimal("0.35")
_WEIGHT_BALANCE = Decimal("0.30")
_WEIGHT_DATE = Decimal("0.20")
_WEIGHT_DESCRIPTION = Decimal("0.15")


def normalise_gold_transaction(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a gold/extracted transaction record (JSON or CSV row) into a
    canonical dict with Decimal amounts."""
    amount = raw.get("amount")
    if amount is None:
        debit = raw.get("debit")
        credit = raw.get("credit")
        if credit not in (None, ""):
            amount = money(credit)
        elif debit not in (None, ""):
            amount = -money(debit)
        else:
            amount = Decimal("0.00")
    else:
        amount = money(amount)

    running_balance_raw = raw.get("running_balance")
    running_balance = money(running_balance_raw) if running_balance_raw not in (None, "") else None

    page_number = raw.get("page_number")
    try:
        page_number = int(page_number) if page_number not in (None, "") else None
    except (TypeError, ValueError):
        page_number = None

    transaction_index = raw.get("transaction_index")
    try:
        transaction_index = int(transaction_index) if transaction_index not in (None, "") else None
    except (TypeError, ValueError):
        transaction_index = None

    return {
        "transaction_index": transaction_index,
        "date": parse_date(raw.get("date")),
        "description": normalize_text(raw.get("description")),
        "amount": amount,
        "running_balance": running_balance,
        "page_number": page_number,
        "source_reference": normalize_text(raw.get("source_reference")) or None,
    }


def load_gold_json(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise a gold (or extracted) statement document matching the gold JSON
    schema: header fields plus a list of transactions."""
    transactions = [normalise_gold_transaction(item) for item in data.get("transactions") or []]
    opening_balance = data.get("opening_balance")
    closing_balance = data.get("closing_balance")
    return {
        "document_id": data.get("document_id"),
        "bank": data.get("bank"),
        "account_type": data.get("account_type"),
        "document_variant": data.get("document_variant"),
        "statement_start_date": parse_date(data.get("statement_start_date")),
        "statement_end_date": parse_date(data.get("statement_end_date")),
        "opening_balance": money(opening_balance) if opening_balance is not None else None,
        "closing_balance": money(closing_balance) if closing_balance is not None else None,
        "transactions": transactions,
    }


def parsed_lines_to_gold_transactions(lines: Iterable[ParsedBankLine]) -> list[dict[str, Any]]:
    """Convert pipeline-extracted ``ParsedBankLine`` records into the gold-JSON
    transaction shape used by ``bank_statement_gold_files.gold_json``."""
    transactions = []
    for index, line in enumerate(lines, start=1):
        transactions.append(
            {
                "transaction_index": index,
                "date": line.line_date,
                "description": line.description,
                "amount": dec_to_float(line.signed_amount),
                "debit": dec_to_float(line.debit_amount) if line.debit_amount else None,
                "credit": dec_to_float(line.credit_amount) if line.credit_amount else None,
                "running_balance": dec_to_float(line.balance_amount),
                "page_number": line.source_page,
                "source_reference": f"row-{line.source_row_index}" if line.source_row_index is not None else None,
            }
        )
    return transactions


def build_extracted_document(
    *,
    document_id: str,
    bank: Optional[str],
    account_type: Optional[str],
    document_variant: Optional[str],
    header: dict[str, Any],
    lines: Iterable[ParsedBankLine],
) -> dict[str, Any]:
    """Build the gold-JSON-shaped document for an extracted statement, ready to
    be passed to ``evaluate_extracted_against_gold`` or returned as a draft."""
    return {
        "document_id": document_id,
        "bank": bank,
        "account_type": account_type,
        "document_variant": document_variant,
        "statement_start_date": header.get("statement_period_from"),
        "statement_end_date": header.get("statement_period_to"),
        "opening_balance": header.get("opening_balance"),
        "closing_balance": header.get("closing_balance"),
        "transactions": parsed_lines_to_gold_transactions(lines),
    }


def load_gold_csv(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise gold transaction rows read via ``csv.DictReader``."""
    return [normalise_gold_transaction(row) for row in rows]


def compare_extracted_to_gold(extracted: list[dict[str, Any]], gold: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare normalised extracted transactions against normalised gold
    transactions, matched by ``transaction_index`` (falling back to position)."""

    def _key(txn: dict[str, Any], fallback_index: int) -> Any:
        index = txn.get("transaction_index")
        return index if index is not None else fallback_index

    gold_by_key = {_key(txn, i + 1): txn for i, txn in enumerate(gold)}
    extracted_by_key = {_key(txn, i + 1): txn for i, txn in enumerate(extracted)}

    matched_keys = sorted(set(gold_by_key) & set(extracted_by_key), key=str)
    missing_keys = sorted(set(gold_by_key) - set(extracted_by_key), key=str)
    extra_keys = sorted(set(extracted_by_key) - set(gold_by_key), key=str)

    date_matches = amount_matches = description_matches = 0
    balance_matches = balance_pairs = 0

    for key in matched_keys:
        gold_txn = gold_by_key[key]
        extracted_txn = extracted_by_key[key]

        if gold_txn["date"] == extracted_txn["date"]:
            date_matches += 1

        if abs(gold_txn["amount"] - extracted_txn["amount"]) <= TOLERANCE:
            amount_matches += 1

        if gold_txn["running_balance"] is not None and extracted_txn["running_balance"] is not None:
            balance_pairs += 1
            if abs(gold_txn["running_balance"] - extracted_txn["running_balance"]) <= TOLERANCE:
                balance_matches += 1

        if normalize_text(gold_txn["description"]).lower() == normalize_text(extracted_txn["description"]).lower():
            description_matches += 1

    matched_count = len(matched_keys)
    return {
        "matched_count": matched_count,
        "missing_count": len(missing_keys),
        "extra_count": len(extra_keys),
        "missing_keys": missing_keys,
        "extra_keys": extra_keys,
        "date_accuracy": (date_matches / matched_count) if matched_count else 1.0,
        "amount_accuracy": (amount_matches / matched_count) if matched_count else 1.0,
        "description_accuracy": (description_matches / matched_count) if matched_count else 1.0,
        "balance_accuracy": (balance_matches / balance_pairs) if balance_pairs else 1.0,
        "balance_pairs_checked": balance_pairs,
        "amount_mismatches": matched_count - amount_matches,
        "description_mismatches": matched_count - description_matches,
    }


def _check_running_balance_continuity(
    opening_balance: Optional[Decimal],
    transactions: list[dict[str, Any]],
    critical_errors: list[str],
) -> bool:
    """Check that each transaction's running balance follows from the opening
    balance plus the cumulative sum of amounts so far."""
    if opening_balance is None:
        return True
    if all(txn["running_balance"] is None for txn in transactions):
        return True

    running = opening_balance
    for txn in transactions:
        running = running + txn["amount"]
        if txn["running_balance"] is not None and abs(running - txn["running_balance"]) > TOLERANCE:
            critical_errors.append(
                "Running balance does not reconcile with cumulative transaction amounts"
            )
            return False
    return True


def validate_extracted_statement_quality(
    *,
    extracted_lines: list[ParsedBankLine],
    header: dict[str, Any],
    duplicate_summary: Optional[dict[str, Any]],
    balance_summary: dict[str, Any],
) -> dict[str, Any]:
    """Internal consistency checks for an extracted statement, independent of
    any gold file (used as the observational hook in the extraction flow)."""
    critical_errors: list[str] = []
    warnings: list[str] = []

    duplicate_count = (duplicate_summary or {}).get("duplicate_line_count", 0)
    if duplicate_count:
        critical_errors.append(f"{duplicate_count} duplicate transaction(s) detected")

    balance_status = balance_summary.get("balance_status")
    closing_balance_passed = balance_status == "balanced"
    if balance_status == "closing_mismatch":
        critical_errors.append("Closing balance does not reconcile with extracted transactions")
    elif balance_status == "opening_mismatch":
        critical_errors.append("Opening balance does not match the bank account's reconciled balance")
    elif balance_status == "missing_balance":
        warnings.append("Opening or closing balance missing from statement header")

    opening_balance = money(header.get("opening_balance")) if header.get("opening_balance") is not None else None
    transactions = [
        {"amount": line.signed_amount, "running_balance": line.balance_amount}
        for line in extracted_lines
    ]
    running_balance_passed = _check_running_balance_continuity(opening_balance, transactions, critical_errors)

    return {
        "extracted_transaction_count": len(extracted_lines),
        "running_balance_passed": running_balance_passed,
        "closing_balance_passed": closing_balance_passed,
        "duplicate_count": duplicate_count,
        "critical_errors": critical_errors,
        "warnings": warnings,
    }


def calculate_extraction_score(comparison: Optional[dict[str, Any]], quality: dict[str, Any]) -> float:
    """Weighted accuracy score. Amount and balance accuracy are weighted higher
    than description accuracy."""
    if comparison is None:
        score = Decimal("1.0")
        if not quality.get("closing_balance_passed", True):
            score -= Decimal("0.4")
        if not quality.get("running_balance_passed", True):
            score -= Decimal("0.4")
        if quality.get("duplicate_count", 0):
            score -= Decimal("0.2")
        return float(max(score, Decimal("0.0")))

    score = (
        _WEIGHT_AMOUNT * Decimal(str(comparison["amount_accuracy"]))
        + _WEIGHT_BALANCE * Decimal(str(comparison["balance_accuracy"]))
        + _WEIGHT_DATE * Decimal(str(comparison["date_accuracy"]))
        + _WEIGHT_DESCRIPTION * Decimal(str(comparison["description_accuracy"]))
    )

    total_expected = comparison["matched_count"] + comparison["missing_count"]
    if total_expected and (comparison["missing_count"] or comparison["extra_count"]):
        penalty = Decimal(comparison["missing_count"] + comparison["extra_count"]) / Decimal(total_expected)
        score -= penalty * Decimal("0.5")

    if not quality.get("closing_balance_passed", True):
        score -= Decimal("0.2")
    if not quality.get("running_balance_passed", True):
        score -= Decimal("0.2")

    return float(max(min(score, Decimal("1.0")), Decimal("0.0")))


def build_extraction_validation_result(
    *,
    expected_transaction_count: Optional[int],
    extracted_transaction_count: int,
    comparison: Optional[dict[str, Any]] = None,
    quality: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble the final validation result. ``comparison`` is the output of
    `compare_extracted_to_gold` (omit when no gold file is available);
    ``quality`` is the output of `validate_extracted_statement_quality` or an
    equivalent dict with `running_balance_passed`, `closing_balance_passed`,
    `duplicate_count`, `critical_errors`, `warnings`."""
    quality = quality or {}
    critical_errors = list(quality.get("critical_errors") or [])
    warnings = list(quality.get("warnings") or [])

    transaction_count_matches: Optional[bool] = None
    matched_transaction_count: Optional[int] = None
    missing_transaction_count: Optional[int] = None
    extra_transaction_count: Optional[int] = None
    date_accuracy = amount_accuracy = balance_accuracy = description_accuracy = None

    if comparison is not None:
        transaction_count_matches = expected_transaction_count == extracted_transaction_count
        matched_transaction_count = comparison["matched_count"]
        missing_transaction_count = comparison["missing_count"]
        extra_transaction_count = comparison["extra_count"]
        date_accuracy = comparison["date_accuracy"]
        amount_accuracy = comparison["amount_accuracy"]
        balance_accuracy = comparison["balance_accuracy"]
        description_accuracy = comparison["description_accuracy"]

        if not transaction_count_matches:
            critical_errors.append(
                f"Expected {expected_transaction_count} transactions but extracted {extracted_transaction_count}"
            )
        if comparison["amount_mismatches"]:
            critical_errors.append(
                f"{comparison['amount_mismatches']} transaction(s) have amount mismatches"
            )
        elif comparison["description_mismatches"]:
            warnings.append(
                f"{comparison['description_mismatches']} transaction(s) have description differences"
            )

    running_balance_passed = quality.get("running_balance_passed", True)
    closing_balance_passed = quality.get("closing_balance_passed", True)

    can_allocate = True
    if comparison is not None and not transaction_count_matches:
        can_allocate = False
    if not closing_balance_passed:
        can_allocate = False
    if not running_balance_passed:
        can_allocate = False
    if quality.get("duplicate_count", 0):
        can_allocate = False
    if critical_errors:
        can_allocate = False

    return {
        "expected_transaction_count": expected_transaction_count,
        "extracted_transaction_count": extracted_transaction_count,
        "transaction_count_matches": transaction_count_matches,
        "matched_transaction_count": matched_transaction_count,
        "missing_transaction_count": missing_transaction_count,
        "extra_transaction_count": extra_transaction_count,
        "date_accuracy": date_accuracy,
        "amount_accuracy": amount_accuracy,
        "balance_accuracy": balance_accuracy,
        "description_accuracy": description_accuracy,
        "running_balance_passed": running_balance_passed,
        "closing_balance_passed": closing_balance_passed,
        "critical_errors": critical_errors,
        "warnings": warnings,
        "can_allocate": can_allocate,
        "overall_score": calculate_extraction_score(comparison, quality),
    }


def evaluate_extracted_against_gold(extracted_doc: dict[str, Any], gold_doc: dict[str, Any]) -> dict[str, Any]:
    """Convenience orchestrator: load both documents, compare transactions, run
    closing/running balance checks, and build the final validation result."""
    extracted = load_gold_json(extracted_doc)
    gold = load_gold_json(gold_doc)

    comparison = compare_extracted_to_gold(extracted["transactions"], gold["transactions"])

    critical_errors: list[str] = []
    warnings: list[str] = []

    running_balance_passed = _check_running_balance_continuity(
        extracted["opening_balance"], extracted["transactions"], critical_errors
    )

    closing_balance_passed = True
    if extracted["closing_balance"] is not None and gold["closing_balance"] is not None:
        if abs(extracted["closing_balance"] - gold["closing_balance"]) > TOLERANCE:
            closing_balance_passed = False
            critical_errors.append("Closing balance does not match the gold closing balance")

    quality = {
        "running_balance_passed": running_balance_passed,
        "closing_balance_passed": closing_balance_passed,
        "duplicate_count": 0,
        "critical_errors": critical_errors,
        "warnings": warnings,
    }

    return build_extraction_validation_result(
        expected_transaction_count=len(gold["transactions"]),
        extracted_transaction_count=len(extracted["transactions"]),
        comparison=comparison,
        quality=quality,
    )
