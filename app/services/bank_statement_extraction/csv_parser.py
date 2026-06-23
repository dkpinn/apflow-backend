from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.services.extraction_foundation import extraction_metadata

from .common import (
    MONEY_ZERO,
    dec_to_float,
    normalize_text,
    transaction_fingerprint,
)
from .models import ParsedBankLine
from .tabular import parse_tabular_rows, recognizable_header_score


MAX_HEADER_SEARCH_ROWS = 50


# ---------------------------------------------------------------------------
# Standard Bank fixed-column format (no header row)
# ---------------------------------------------------------------------------
# Row types:
#   [0]=HIST  [1]=YYYYMMDD  [2]=''   [3]=signed_amount  [4]=description  [5]=''  [6]=D|C  [7]=0
#   [0]=''    [1]=acct_no   [2]=ACC-NO  ...
#   [0]=''    [1]=0         [2]=OPEN    [3]=opening_bal   [4]=OPEN BALANCE
#   [0]=''    [1]=0         [2]=CLOSE   [3]=closing_bal   [4]=CLOSE BALANCE

def _is_stdbank_fixed(rows: list[list[str]]) -> bool:
    return any(
        len(row) >= 7 and row[0].upper() == "HIST"
        for row in rows[:MAX_HEADER_SEARCH_ROWS]
    )


def _safe_decimal(value: str) -> Optional[Decimal]:
    try:
        return Decimal(value.replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None


def _parse_stdbank_fixed_csv(
    rows: list[list[str]],
    *,
    bank_account_id: str,
    currency: Optional[str],
) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    lines: list[ParsedBankLine] = []

    for row in rows:
        if len(row) < 5:
            continue

        marker = row[2].strip().upper() if len(row) > 2 else ""

        if marker == "OPEN":
            amt = _safe_decimal(row[3].strip())
            if amt is not None:
                opening_balance = float(amt)
            continue

        if marker == "CLOSE":
            amt = _safe_decimal(row[3].strip())
            if amt is not None:
                closing_balance = float(amt)
            continue

        if row[0].strip().upper() != "HIST":
            continue

        # Parse transaction row
        raw_date = row[1].strip()
        try:
            from datetime import datetime
            line_date = datetime.strptime(raw_date, "%Y%m%d").date().isoformat()
        except ValueError:
            line_date = None

        raw_amount = row[3].strip()
        signed = _safe_decimal(raw_amount)
        if signed is None:
            continue

        description = normalize_text(row[4]) if len(row) > 4 else ""
        direction = row[6].strip().upper() if len(row) > 6 else ""

        # D = debit/spending (money out, negative signed)
        # C = credit/payment (money in, positive signed)
        if direction == "D" or signed < MONEY_ZERO:
            debit = abs(signed)
            credit = MONEY_ZERO
            signed_amount = -abs(signed)
        else:
            debit = MONEY_ZERO
            credit = abs(signed)
            signed_amount = abs(signed)

        parsed = ParsedBankLine(
            line_date=line_date,
            value_date=None,
            description=description or normalize_text(raw_amount),
            reference=None,
            counterparty=None,
            debit_amount=debit,
            credit_amount=credit,
            signed_amount=Decimal(str(signed_amount)),
            balance_amount=None,
            currency=currency,
            transaction_type=None,
            bank_reference=None,
            raw_text=",".join(row),
            raw_lines=[",".join(row)],
            source_row_index=None,
            extraction_confidence=0.95,
            extraction_warnings=[],
        )
        parsed.transaction_hash = transaction_fingerprint(
            bank_account_id=bank_account_id,
            line_date=parsed.line_date,
            amount=parsed.signed_amount,
            reference=parsed.reference,
            counterparty=parsed.counterparty,
            bank_reference=parsed.bank_reference,
            description=parsed.description,
        )
        lines.append(parsed)

    warnings: list[dict[str, Any]] = []
    header: dict[str, Any] = {
        "statement_period_from": next((l.line_date for l in lines if l.line_date), None),
        "statement_period_to": next((l.line_date for l in reversed(lines) if l.line_date), None),
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "currency": currency,
        "confidence_score": 0.95,
        "extractor": "bank_statement",
        "extractor_type": "bank_statement",
        "extractor_version": "v1",
        "source_format": "csv",
        "parser_strategy": "stdbank_fixed_csv",
        "extraction_warnings": warnings,
        "raw_extraction": extraction_metadata(
            extractor_type="bank_statement",
            extractor_version="v1",
            source_format="csv",
            parser_strategy="stdbank_fixed_csv",
            confidence_score=0.95,
            warnings=warnings,
            extra={"line_count": len(lines)},
        ),
    }
    return header, lines


# ---------------------------------------------------------------------------
# Generic CSV parser — named-column, header-scan approach
# ---------------------------------------------------------------------------

def parse_csv_statement(
    file_bytes: bytes,
    *,
    bank_account_id: str,
    currency: Optional[str] = None,
) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    text = file_bytes.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel

    # Read all rows up-front as plain lists.
    all_rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(text), dialect=dialect):
        all_rows.append([str(cell).strip() for cell in row])

    # Detect Standard Bank fixed-column format (no header row).
    if _is_stdbank_fixed(all_rows):
        return _parse_stdbank_fixed_csv(
            all_rows,
            bank_account_id=bank_account_id,
            currency=currency,
        )

    # Generic path: scan up to MAX_HEADER_SEARCH_ROWS rows to find the real
    # column-header row. SA banks (and others) often prepend metadata lines
    # before the actual transaction table.
    best_score = 0
    best_header_idx = 0
    for row_idx, row in enumerate(all_rows[:MAX_HEADER_SEARCH_ROWS]):
        score = recognizable_header_score(row)
        if score > best_score:
            best_score = score
            best_header_idx = row_idx

    fieldnames = all_rows[best_header_idx] if all_rows else []

    data_rows: list[dict[str, str]] = []
    for row in all_rows[best_header_idx + 1:]:
        if not any(cell for cell in row):
            continue
        data_rows.append({
            fieldnames[i]: row[i] if i < len(row) else ""
            for i in range(len(fieldnames))
        })

    return parse_tabular_rows(
        data_rows,
        fieldnames=fieldnames,
        bank_account_id=bank_account_id,
        currency=currency,
        source_format="csv",
        parser_strategy="deterministic_csv",
        confidence_score=0.95,
        line_confidence_score=0.98,
        metadata_extra={"header_row": best_header_idx},
    )
