from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Optional

from app.services.extraction_foundation import extraction_metadata, warning

from .common import (
    MONEY_ZERO,
    dec_to_float,
    extract_bank_reference,
    infer_signed_amount,
    money,
    normalize_text,
    parse_date,
    split_transaction_type_and_reference,
    transaction_fingerprint,
)
from .models import ParsedBankLine

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover - optional at runtime
    fitz = None  # type: ignore


DATE_ANCHOR_RE = re.compile(r"^(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b")
MONEY_TOKEN_RE = re.compile(r"(?:[A-Z]{3}\s*)?-?\(?[A-Z$R]?\s?\d[\d\s,]*[.,]\d{2}\)?")


def extract_pdf_text(file_bytes: bytes) -> str:
    if fitz is None:
        return ""
    try:
        document = fitz.open(stream=file_bytes, filetype="pdf")
        return "\n".join(page.get_text("text") for page in document)
    except Exception:
        return ""


def parse_transaction_blocks(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None

    for raw in text.splitlines():
        line = normalize_text(raw)
        if not line:
            continue
        date_match = DATE_ANCHOR_RE.match(line)
        money_matches = list(MONEY_TOKEN_RE.finditer(line))
        starts_transaction = bool(date_match and len(money_matches) >= 2)
        if starts_transaction:
            if current:
                blocks.append(current)
            assert date_match is not None
            amount_match = money_matches[-2]
            balance_match = money_matches[-1]
            prefix = re.sub(r"\s+\*\s*$", "", normalize_text(line[date_match.end():amount_match.start()]))
            transaction_type, reference = split_transaction_type_and_reference(prefix)
            current = {
                "date": date_match.group("date"),
                "prefix": prefix,
                "transaction_type": transaction_type or prefix,
                "reference": reference,
                "amount": money(amount_match.group(0)),
                "balance": money(balance_match.group(0)),
                "raw_lines": [line],
                "continuation_lines": [],
            }
        elif current:
            current["raw_lines"].append(line)
            current["continuation_lines"].append(line)

    if current:
        blocks.append(current)
    return blocks


def parse_text_statement_from_text(
    text: str,
    *,
    bank_account_id: str,
    currency: Optional[str] = None,
) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    blocks = parse_transaction_blocks(text)
    warnings: list[dict[str, Any]] = []
    if not blocks:
        no_blocks = warning("no_transaction_blocks", "No transaction blocks could be detected in PDF text.")
        return {
            "statement_period_from": None,
            "statement_period_to": None,
            "opening_balance": None,
            "closing_balance": None,
            "currency": currency,
            "confidence_score": 0,
            "extractor": "bank_statement",
            "extractor_type": "bank_statement",
            "extractor_version": "v1",
            "source_format": "pdf",
            "parser_strategy": "pdf_text_blocks",
            "extraction_warnings": [no_blocks],
            "raw_extraction": extraction_metadata(
                extractor_type="bank_statement",
                extractor_version="v1",
                source_format="pdf",
                parser_strategy="pdf_text_blocks",
                confidence_score=0,
                warnings=[no_blocks],
                raw_preview=text,
            ),
        }, []

    lines: list[ParsedBankLine] = []
    previous_balance: Optional[Decimal] = None
    for row_index, block in enumerate(blocks):
        block_warnings: list[dict[str, Any]] = []
        signed, direction_warnings = infer_signed_amount(
            transaction_type=block["transaction_type"],
            amount=block["amount"],
            previous_balance=previous_balance,
            balance=block["balance"],
        )
        block_warnings.extend(direction_warnings)
        continuation = " ".join(block["continuation_lines"])
        if not continuation and re.search(
            r"\b(to|pmt|payment|transfer|trf|cr)\b",
            block["transaction_type"],
            re.IGNORECASE,
        ):
            block_warnings.append(
                warning(
                    "missing_continuation_detail",
                    "Transaction appears to need beneficiary/reference continuation detail.",
                )
            )

        debit = abs(signed) if signed < 0 else MONEY_ZERO
        credit = signed if signed >= 0 else MONEY_ZERO
        description = normalize_text(" ".join([block["prefix"], continuation]))
        bank_reference = extract_bank_reference(block["reference"], description, continuation)
        counterparty = normalize_text(continuation) or None
        confidence = 0.92
        if block_warnings:
            confidence = 0.72
            warnings.extend(block_warnings)

        parsed = ParsedBankLine(
            line_date=parse_date(block["date"]),
            value_date=None,
            description=description or block["transaction_type"],
            reference=block["reference"],
            counterparty=counterparty,
            debit_amount=debit,
            credit_amount=credit,
            signed_amount=signed,
            balance_amount=block["balance"],
            currency=currency,
            transaction_type=block["transaction_type"],
            bank_reference=bank_reference,
            raw_text="\n".join(block["raw_lines"]),
            raw_lines=block["raw_lines"],
            source_row_index=row_index,
            extraction_confidence=confidence,
            extraction_warnings=block_warnings,
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
        previous_balance = block["balance"]

    opening_balance = None
    if lines and lines[0].balance_amount is not None:
        opening_balance = dec_to_float(lines[0].balance_amount - lines[0].signed_amount)
    confidence_score = min((line.extraction_confidence or 0.65 for line in lines), default=0.65)
    header = {
        "statement_period_from": next((line.line_date for line in lines if line.line_date), None),
        "statement_period_to": next((line.line_date for line in reversed(lines) if line.line_date), None),
        "opening_balance": opening_balance,
        "closing_balance": dec_to_float(lines[-1].balance_amount) if lines[-1].balance_amount is not None else None,
        "currency": currency,
        "confidence_score": confidence_score,
        "extractor": "bank_statement",
        "extractor_type": "bank_statement",
        "extractor_version": "v1",
        "source_format": "pdf",
        "parser_strategy": "pdf_text_blocks",
        "extraction_warnings": warnings,
        "raw_extraction": extraction_metadata(
            extractor_type="bank_statement",
            extractor_version="v1",
            source_format="pdf",
            parser_strategy="pdf_text_blocks",
            confidence_score=confidence_score,
            warnings=warnings,
            raw_preview=text,
            extra={"detected_transaction_blocks": len(blocks), "line_count": len(lines)},
        ),
    }
    return header, lines


def parse_text_statement(
    file_bytes: bytes,
    *,
    bank_account_id: str,
    currency: Optional[str] = None,
) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    text = extract_pdf_text(file_bytes)
    return parse_text_statement_from_text(text, bank_account_id=bank_account_id, currency=currency)
