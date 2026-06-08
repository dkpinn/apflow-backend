from __future__ import annotations

import csv
import io
from typing import Any, Optional

from .models import ParsedBankLine
from .tabular import parse_tabular_rows


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
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    fieldnames = [str(field) for field in (reader.fieldnames or []) if field is not None]
    return parse_tabular_rows(
        reader,
        fieldnames=fieldnames,
        bank_account_id=bank_account_id,
        currency=currency,
        source_format="csv",
        parser_strategy="deterministic_csv",
        confidence_score=0.95,
        line_confidence_score=0.98,
    )
