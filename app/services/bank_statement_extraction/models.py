from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional


@dataclass
class ParsedBankLine:
    line_date: Optional[str]
    value_date: Optional[str]
    description: str
    reference: Optional[str]
    counterparty: Optional[str]
    debit_amount: Decimal
    credit_amount: Decimal
    signed_amount: Decimal
    balance_amount: Optional[Decimal]
    currency: Optional[str]
    transaction_hash: str = ""
    transaction_type: Optional[str] = None
    bank_reference: Optional[str] = None
    raw_text: Optional[str] = None
    raw_lines: list[str] | None = None
    source_page: Optional[int] = None
    source_row_index: Optional[int] = None
    extraction_confidence: Optional[float] = None
    extraction_warnings: list[dict[str, Any]] | None = None
