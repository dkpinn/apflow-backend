"""Tests for the running-balance integrity analyzer.

The running balance is an arithmetic checksum: prev + credit - debit == curr.
These tests lock in the two failure modes that were reaching users unflagged —
a misread amount, and a silently dropped (missing) row across a page boundary —
as well as the enforcement wiring in the quality gate.
"""
from decimal import Decimal

from app.services.bank_statement_service import analyze_balance_integrity
from app.services.bank_extraction_validation import validate_extracted_statement_quality
from app.services.bank_statement_extraction.models import ParsedBankLine


def _line(*, date, debit, credit, balance, description="txn") -> ParsedBankLine:
    d = Decimal(str(debit))
    c = Decimal(str(credit))
    return ParsedBankLine(
        line_date=date,
        value_date=None,
        description=description,
        reference=None,
        counterparty=None,
        debit_amount=d,
        credit_amount=c,
        signed_amount=c - d,
        balance_amount=Decimal(str(balance)) if balance is not None else None,
        currency="ZAR",
    )


def _header(opening, closing=None):
    return {"opening_balance": Decimal(str(opening)), "closing_balance": Decimal(str(closing)) if closing is not None else None}


def test_clean_statement_reconciles():
    lines = [
        _line(date="2024-02-11", debit=0, credit=5000, balance=6328.19),
        _line(date="2024-02-12", debit=0, credit=500, balance=6828.19),
        _line(date="2024-02-13", debit=1236.91, credit=0, balance=5591.28),
    ]
    result = analyze_balance_integrity(lines, _header(1328.19))
    assert result["balance_walk_status"] == "balanced"
    assert result["balance_walk_mismatches"] == 0
    assert result["missing_row_suspected"] is False
    assert all(r["status"] == "ok" for r in result["rows"])


def test_misread_amount_is_flagged():
    # Row 2's amount says +500 but the printed balance moved by +600 → amount wrong.
    lines = [
        _line(date="2024-02-11", debit=0, credit=5000, balance=6328.19),
        _line(date="2024-02-12", debit=0, credit=500, balance=6928.19),
    ]
    result = analyze_balance_integrity(lines, _header(1328.19))
    assert result["balance_walk_status"] == "balance_walk_failed"
    assert result["first_break_row_index"] == 1
    assert result["rows"][1]["status"] in {"amount_wrong", "row_missing_before"}


def test_missing_row_is_detected_across_the_break():
    # A transaction was dropped: the balance jumps by 800 but the row that would
    # explain it is absent, so the next row carrying a balance fails to reconcile.
    lines = [
        _line(date="2024-02-11", debit=0, credit=5000, balance=6328.19),
        # (missing here: a +800 deposit that would land the balance at 7128.19)
        _line(date="2024-02-13", debit=100, credit=0, balance=7028.19),
    ]
    result = analyze_balance_integrity(lines, _header(1328.19))
    assert result["balance_walk_status"] == "balance_walk_failed"
    assert result["missing_row_suspected"] is True


def test_missing_balance_is_reported_not_silently_skipped():
    lines = [
        _line(date="2024-02-11", debit=0, credit=5000, balance=6328.19),
        _line(date="2024-02-12", debit=0, credit=500, balance=None),
    ]
    result = analyze_balance_integrity(lines, _header(1328.19))
    assert result["balance_missing_count"] == 1
    assert any(r["status"] == "balance_missing" for r in result["rows"])


def test_quality_gate_hard_blocks_on_balance_break():
    lines = [
        _line(date="2024-02-11", debit=0, credit=5000, balance=6328.19),
        _line(date="2024-02-12", debit=0, credit=500, balance=6928.19),
    ]
    header = {
        "statement_period_from": "2024-02-01",
        "statement_period_to": "2024-02-29",
        "opening_balance": 1328.19,
        "closing_balance": 6928.19,
        "source_format": "csv",
        "parser_strategy": "csv",
    }
    integrity = analyze_balance_integrity(lines, {"opening_balance": Decimal("1328.19"), "closing_balance": Decimal("6928.19")})
    result = validate_extracted_statement_quality(
        extracted_lines=lines,
        header=header,
        duplicate_summary={"duplicate_line_count": 0},
        balance_summary={"balance_status": "balanced"},
        balance_integrity=integrity,
    )
    assert result["can_allocate"] is False
    assert any("does not reconcile" in e for e in result["critical_errors"])
