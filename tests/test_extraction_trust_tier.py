"""Tests for trust-tiered bank-statement import gating.

A clean text PDF whose running balance reconciles is trusted enough to auto-allocate
(land in `extracted` and flow straight to reconciliation). Any VLM/scanned source, or
a text PDF whose balance chain is broken, stays untrusted and is routed to manual
review. See `extraction_is_trusted_deterministic` and the gate in
`validate_extracted_statement_quality`.
"""
from decimal import Decimal

from app.services.bank_extraction_validation import (
    extraction_is_trusted_deterministic,
    validate_extracted_statement_quality,
)
from app.services.bank_statement_extraction.models import ParsedBankLine

ACCT = "acct-1"


def _line(amount="-100.00", *, balance="900.00", date="2024-12-12", desc="txn") -> ParsedBankLine:
    amt = Decimal(amount)
    return ParsedBankLine(
        line_date=date,
        value_date=None,
        description=desc,
        reference=None,
        counterparty=None,
        debit_amount=abs(amt) if amt < 0 else Decimal("0"),
        credit_amount=amt if amt > 0 else Decimal("0"),
        signed_amount=amt,
        balance_amount=Decimal(balance) if balance is not None else None,
        currency="ZAR",
        transaction_hash="h",
    )


def _header(**overrides):
    header = {
        "source_format": "pdf",
        "parser_strategy": "pdf_text_blocks",
        "pdf_rescue": {"selected": "deterministic"},
        "opening_balance": 1000.00,
        "closing_balance": 900.00,
        "statement_period_from": "2024-12-01",
        "statement_period_to": "2024-12-31",
    }
    header.update(overrides)
    return header


BALANCED_SUMMARY = {"balance_status": "balanced"}
BALANCED_INTEGRITY = {"balance_walk_status": "balanced", "balance_missing_count": 0}


# --- the predicate ---------------------------------------------------------

def test_clean_text_pdf_is_trusted():
    assert extraction_is_trusted_deterministic(_header(), BALANCED_SUMMARY, BALANCED_INTEGRITY)


def test_vlm_source_is_not_trusted():
    header = _header(parser_strategy="vlm", pdf_rescue={"selected": "vlm"})
    assert not extraction_is_trusted_deterministic(header, BALANCED_SUMMARY, BALANCED_INTEGRITY)


def test_deterministic_but_unreconciled_is_not_trusted():
    # Balance walk broke — even though the text-layer parser was used, don't trust it.
    integrity = {"balance_walk_status": "balance_walk_failed", "first_break_row_index": 0}
    assert not extraction_is_trusted_deterministic(_header(), BALANCED_SUMMARY, integrity)


def test_image_source_is_never_trusted():
    header = _header(source_format="image", parser_strategy="vlm_image", pdf_rescue={"selected": "vlm"})
    assert not extraction_is_trusted_deterministic(header, BALANCED_SUMMARY, BALANCED_INTEGRITY)


def test_rescue_selected_vlm_is_not_trusted():
    # Text parse existed but was rejected in favour of the VLM re-read.
    header = _header(parser_strategy="pdf_text_blocks_then_vlm", pdf_rescue={"selected": "vlm"})
    assert not extraction_is_trusted_deterministic(header, BALANCED_SUMMARY, BALANCED_INTEGRITY)


# --- the gate --------------------------------------------------------------

def test_trusted_text_pdf_can_allocate():
    result = validate_extracted_statement_quality(
        extracted_lines=[_line()],
        header=_header(),
        duplicate_summary={"duplicate_line_count": 0},
        balance_summary=BALANCED_SUMMARY,
        balance_integrity=BALANCED_INTEGRITY,
    )
    assert result["can_allocate"] is True
    assert not result["critical_errors"]


def test_vlm_pdf_forced_to_manual_review():
    result = validate_extracted_statement_quality(
        extracted_lines=[_line()],
        header=_header(parser_strategy="vlm", pdf_rescue={"selected": "vlm"}),
        duplicate_summary={"duplicate_line_count": 0},
        balance_summary=BALANCED_SUMMARY,
        balance_integrity=BALANCED_INTEGRITY,
    )
    assert result["can_allocate"] is False
    assert any("manual review" in e for e in result["critical_errors"])


def test_unreconciled_text_pdf_falls_back_to_review():
    # Deterministic parse, but the balance walk is broken → still needs review.
    integrity = {"balance_walk_status": "balance_walk_failed", "first_break_row_index": 0, "missing_row_suspected": True}
    result = validate_extracted_statement_quality(
        extracted_lines=[_line(balance="950.00")],  # 1000 - 100 != 950
        header=_header(),
        duplicate_summary={"duplicate_line_count": 0},
        balance_summary=BALANCED_SUMMARY,
        balance_integrity=integrity,
    )
    assert result["can_allocate"] is False
