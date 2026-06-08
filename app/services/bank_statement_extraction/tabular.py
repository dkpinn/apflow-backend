from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional

from app.services.extraction_foundation import extraction_metadata

from .common import (
    MONEY_ZERO,
    dec_to_float,
    extract_bank_reference,
    infer_column,
    money,
    normalize_text,
    parse_date,
    transaction_fingerprint,
)
from .models import ParsedBankLine


COLUMN_CANDIDATES: dict[str, list[str]] = {
    "date": ["date", "transaction date", "posted date", "posting date"],
    "value_date": ["value date", "effective date"],
    "description": ["description", "narrative", "details", "transaction details", "memo"],
    "transaction_type": ["transaction type", "type", "transaction", "code"],
    "reference": ["reference", "ref", "document number", "transaction id"],
    "bank_reference": ["bank reference", "bank ref", "bank transaction id", "trace number", "fit id"],
    "counterparty": ["counterparty", "beneficiary", "payee", "payer", "recipient"],
    "debit": ["debit", "withdrawal", "money out", "paid out", "payments"],
    "credit": ["credit", "deposit", "money in", "paid in", "receipts"],
    "amount": ["amount", "transaction amount"],
    "balance": ["balance", "running balance", "closing balance"],
    "currency": ["currency", "currency code"],
}


def infer_tabular_columns(fieldnames: Iterable[str]) -> dict[str, Optional[str]]:
    names = list(fieldnames)
    return {
        field: infer_column(names, candidates)
        for field, candidates in COLUMN_CANDIDATES.items()
    }


def recognizable_header_score(fieldnames: Iterable[str]) -> int:
    columns = infer_tabular_columns(fieldnames)
    has_date = bool(columns["date"])
    has_amount = bool(columns["amount"] or columns["debit"] or columns["credit"])
    date_columns = {columns["date"], columns["value_date"]}
    identity_columns = {
        columns["description"],
        columns["reference"],
        columns["counterparty"],
        columns["transaction_type"],
    } - date_columns - {None}
    has_identity = bool(identity_columns)
    if not (has_date and has_amount and has_identity):
        return 0
    return len({column for column in columns.values() if column is not None})


def parse_tabular_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    fieldnames: list[str],
    bank_account_id: str,
    currency: Optional[str],
    source_format: str,
    parser_strategy: str,
    confidence_score: float,
    line_confidence_score: Optional[float] = None,
    metadata_extra: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    columns = infer_tabular_columns(fieldnames)
    lines: list[ParsedBankLine] = []

    for row_index, row in enumerate(rows):
        line_date = parse_date(row.get(columns["date"] or ""))
        description = normalize_text(row.get(columns["description"] or "")) or normalize_text(dict(row))
        transaction_type = normalize_text(row.get(columns["transaction_type"] or "")) or None
        reference = normalize_text(row.get(columns["reference"] or "")) or None
        counterparty = normalize_text(row.get(columns["counterparty"] or "")) or None
        bank_reference = (
            normalize_text(row.get(columns["bank_reference"] or ""))
            or extract_bank_reference(reference, description, counterparty)
        )

        debit = money(row.get(columns["debit"])) if columns["debit"] else MONEY_ZERO
        credit = money(row.get(columns["credit"])) if columns["credit"] else MONEY_ZERO
        if columns["amount"] and debit == MONEY_ZERO and credit == MONEY_ZERO:
            amount = money(row.get(columns["amount"]))
            if amount < 0:
                debit = abs(amount)
            else:
                credit = amount
        signed = credit - debit
        balance_raw = row.get(columns["balance"]) if columns["balance"] else None
        balance = money(balance_raw) if balance_raw not in (None, "") else None
        row_currency = normalize_text(row.get(columns["currency"] or "")) or currency

        if not any([line_date, description, debit, credit, balance]):
            continue

        parsed = ParsedBankLine(
            line_date=line_date,
            value_date=parse_date(row.get(columns["value_date"] or "")),
            description=description,
            reference=reference,
            counterparty=counterparty,
            debit_amount=debit,
            credit_amount=credit,
            signed_amount=signed,
            balance_amount=balance,
            currency=row_currency,
            transaction_type=transaction_type,
            bank_reference=bank_reference,
            raw_text=normalize_text(dict(row)),
            raw_lines=[normalize_text(dict(row))],
            source_row_index=row_index,
            extraction_confidence=line_confidence_score or confidence_score,
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
    extra = {"fieldnames": fieldnames, "line_count": len(lines)}
    if metadata_extra:
        extra.update(metadata_extra)
    header: dict[str, Any] = {
        "statement_period_from": next((line.line_date for line in lines if line.line_date), None),
        "statement_period_to": next((line.line_date for line in reversed(lines) if line.line_date), None),
        "opening_balance": None,
        "closing_balance": dec_to_float(lines[-1].balance_amount) if lines and lines[-1].balance_amount is not None else None,
        "currency": currency,
        "confidence_score": confidence_score,
        "extractor": "bank_statement",
        "extractor_type": "bank_statement",
        "extractor_version": "v1",
        "source_format": source_format,
        "parser_strategy": parser_strategy,
        "extraction_warnings": warnings,
        "raw_extraction": extraction_metadata(
            extractor_type="bank_statement",
            extractor_version="v1",
            source_format=source_format,
            parser_strategy=parser_strategy,
            confidence_score=confidence_score,
            warnings=warnings,
            extra=extra,
        ),
    }
    if lines and lines[0].balance_amount is not None:
        header["opening_balance"] = dec_to_float(lines[0].balance_amount - lines[0].signed_amount)
    return header, lines
