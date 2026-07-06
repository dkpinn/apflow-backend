"""Tests for statement-period date normalization.

The statement period is ground truth for the year: VLM extractions frequently
attach the wrong year (e.g. December 2025 on a Dec-2024 statement) and nothing
downstream validated dates. normalize_dates_from_period recomputes the year
from the period and flags anything it still cannot place inside it.
"""
from decimal import Decimal

from app.services.bank_statement_service import normalize_dates_from_period
from app.services.bank_statement_extraction.models import ParsedBankLine

ACCT = "acct-1"


def _line(date_str, *, desc="txn", amount="-100.00") -> ParsedBankLine:
    amt = Decimal(amount)
    return ParsedBankLine(
        line_date=date_str,
        value_date=None,
        description=desc,
        reference=None,
        counterparty=None,
        debit_amount=abs(amt) if amt < 0 else Decimal("0"),
        credit_amount=amt if amt > 0 else Decimal("0"),
        signed_amount=amt,
        balance_amount=None,
        currency="ZAR",
        transaction_hash="orig-hash",
    )


def _period(frm, to):
    return {"statement_period_from": frm, "statement_period_to": to}


def test_full_wrong_year_is_corrected_to_period():
    # VLM returned December 2025 on a Dec-2024 statement.
    lines = [_line("2025-12-12")]
    summary = normalize_dates_from_period(lines, _period("2024-12-11", "2025-01-11"), bank_account_id=ACCT)
    assert lines[0].line_date == "2024-12-12"
    assert summary["corrections_applied"] == 1
    assert summary["out_of_period_count"] == 0
    # hash recomputed (date is part of the fingerprint)
    assert lines[0].transaction_hash != "orig-hash"


def test_year_boundary_dec_then_jan():
    lines = [
        _line("2025-12-20", desc="december txn"),
        _line("2024-01-05", desc="january txn"),  # both years wrong
    ]
    summary = normalize_dates_from_period(lines, _period("2024-12-11", "2025-01-11"), bank_account_id=ACCT)
    assert lines[0].line_date == "2024-12-20"
    assert lines[1].line_date == "2025-01-05"
    assert summary["corrections_applied"] == 2


def test_correct_dates_are_left_untouched():
    lines = [_line("2024-12-12"), _line("2025-01-03")]
    summary = normalize_dates_from_period(lines, _period("2024-12-11", "2025-01-11"), bank_account_id=ACCT)
    assert lines[0].line_date == "2024-12-12"
    assert lines[1].line_date == "2025-01-03"
    assert summary["corrections_applied"] == 0


def test_out_of_period_date_is_flagged_not_forced():
    # A June date cannot be placed in a Dec–Jan period; keep it but flag it.
    lines = [_line("2024-06-15")]
    summary = normalize_dates_from_period(lines, _period("2024-12-11", "2025-01-11"), bank_account_id=ACCT)
    assert summary["out_of_period_count"] == 1
    assert 0 in summary["out_of_period_row_indexes"]


def test_missing_period_is_a_noop():
    lines = [_line("2025-12-12")]
    summary = normalize_dates_from_period(lines, _period(None, None), bank_account_id=ACCT)
    assert lines[0].line_date == "2025-12-12"
    assert summary["status"] == "no_period"
    assert summary["corrections_applied"] == 0


def test_same_year_period_forces_that_year():
    lines = [_line("2023-03-14")]  # wrong year, single-year period
    summary = normalize_dates_from_period(lines, _period("2024-03-01", "2024-03-31"), bank_account_id=ACCT)
    assert lines[0].line_date == "2024-03-14"
    assert summary["corrections_applied"] == 1
