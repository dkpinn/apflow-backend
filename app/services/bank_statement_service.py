from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Optional
from uuid import uuid4

from app.services.extraction_foundation import extraction_metadata, warning
from app.services.extractor_registry import select_bank_cash_extractor

try:  # optional at runtime; CSV extraction does not need PyMuPDF
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None  # type: ignore


MONEY_ZERO = Decimal("0.00")


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


def money(value: Any) -> Decimal:
    if value is None:
        return MONEY_ZERO
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    raw = str(value).strip()
    if not raw:
        return MONEY_ZERO
    neg = raw.startswith("(") and raw.endswith(")")
    cleaned = re.sub(r"[^0-9,.\-]", "", raw)
    if cleaned.count(",") and cleaned.count("."):
        cleaned = cleaned.replace(",", "")
    elif cleaned.count(",") and not cleaned.count("."):
        cleaned = cleaned.replace(",", ".")
    if neg and not cleaned.startswith("-"):
        cleaned = f"-{cleaned}"
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return MONEY_ZERO


def dec_to_float(value: Optional[Decimal]) -> Optional[float]:
    return None if value is None else float(value)


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def parse_date(value: Any) -> Optional[str]:
    raw = normalize_text(value)
    if not raw:
        return None
    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%m/%d/%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return raw if re.match(r"^\d{4}-\d{2}-\d{2}$", raw) else None


def infer_column(fieldnames: Iterable[str], candidates: Iterable[str]) -> Optional[str]:
    normalized = {re.sub(r"[^a-z0-9]", "", f.lower()): f for f in fieldnames}
    for candidate in candidates:
        key = re.sub(r"[^a-z0-9]", "", candidate.lower())
        if key in normalized:
            return normalized[key]
    for norm, original in normalized.items():
        for candidate in candidates:
            key = re.sub(r"[^a-z0-9]", "", candidate.lower())
            if key and key in norm:
                return original
    return None


def extract_bank_reference(*values: Any) -> Optional[str]:
    combined = " ".join(normalize_text(v) for v in values if normalize_text(v))
    match = re.search(r"\b\d{7,}\b", combined)
    return match.group(0) if match else None


def split_transaction_type_and_reference(prefix: str) -> tuple[str, Optional[str]]:
    text = normalize_text(prefix)
    if not text:
        return "", None
    known_refs = {
        "settlement",
        "headoffice",
        "head office",
        "internet",
        "branch",
        "pos",
        "atm",
        "nk",
    }
    lowered = text.lower()
    for ref in sorted(known_refs, key=len, reverse=True):
        if lowered.endswith(f" {ref}"):
            return normalize_text(text[: -len(ref)]), text[-len(ref):].strip()
    return text, None


def infer_signed_amount(*, transaction_type: str, amount: Decimal, previous_balance: Optional[Decimal], balance: Optional[Decimal]) -> tuple[Decimal, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    if previous_balance is not None and balance is not None:
        movement = balance - previous_balance
        if abs(abs(movement) - abs(amount)) <= Decimal("0.01"):
            return movement, warnings
        warnings.append(
            warning(
                "balance_movement_mismatch",
                "Transaction amount does not match movement between adjacent balances.",
                previous_balance=float(previous_balance),
                balance=float(balance),
                amount=float(amount),
                movement=float(movement),
            )
        )

    lowered = transaction_type.lower()
    credit_terms = (" credit", " cr", "deposit", "receipt", "received", "trf cr", "immediate trf cr")
    debit_terms = ("charge", "fee", "stop order", "payment", "debit", "purchase", "withdrawal", "paid")
    if any(term in lowered for term in credit_terms):
        return abs(amount), warnings
    if any(term in lowered for term in debit_terms):
        return -abs(amount), warnings

    warnings.append(warning("amount_direction_inferred", "Could not confidently infer debit/credit direction from statement text."))
    return abs(amount), warnings


def transaction_fingerprint(
    *,
    bank_account_id: str,
    line_date: Optional[str],
    amount: Decimal,
    reference: Optional[str],
    description: str,
    counterparty: Optional[str] = None,
    bank_reference: Optional[str] = None,
) -> str:
    basis = "|".join(
        [
            bank_account_id,
            line_date or "",
            str(amount.quantize(Decimal("0.01"))),
            normalize_text(reference).lower(),
            normalize_text(counterparty).lower(),
            normalize_text(bank_reference).lower(),
            normalize_text(description).lower(),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


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
    fieldnames = reader.fieldnames or []

    date_col = infer_column(fieldnames, ["date", "transaction date", "posted date", "posting date"])
    value_date_col = infer_column(fieldnames, ["value date", "effective date"])
    description_col = infer_column(fieldnames, ["description", "narrative", "details", "transaction details", "memo"])
    transaction_type_col = infer_column(fieldnames, ["transaction type", "type", "transaction", "code"])
    reference_col = infer_column(fieldnames, ["reference", "ref", "document number", "transaction id"])
    bank_reference_col = infer_column(fieldnames, ["bank reference", "bank ref", "bank transaction id", "trace number", "fit id"])
    counterparty_col = infer_column(fieldnames, ["counterparty", "beneficiary", "payee", "payer", "recipient"])
    debit_col = infer_column(fieldnames, ["debit", "withdrawal", "money out", "paid out", "payments"])
    credit_col = infer_column(fieldnames, ["credit", "deposit", "money in", "paid in", "receipts"])
    amount_col = infer_column(fieldnames, ["amount", "transaction amount"])
    balance_col = infer_column(fieldnames, ["balance", "running balance", "closing balance"])
    currency_col = infer_column(fieldnames, ["currency", "currency code"])

    lines: list[ParsedBankLine] = []
    for row_index, row in enumerate(reader):
        line_date = parse_date(row.get(date_col or ""))
        description = normalize_text(row.get(description_col or "")) or normalize_text(row)
        transaction_type = normalize_text(row.get(transaction_type_col or "")) or None
        reference = normalize_text(row.get(reference_col or "")) or None
        counterparty = normalize_text(row.get(counterparty_col or "")) or None
        bank_reference = normalize_text(row.get(bank_reference_col or "")) or extract_bank_reference(reference, description, counterparty)

        debit = money(row.get(debit_col)) if debit_col else MONEY_ZERO
        credit = money(row.get(credit_col)) if credit_col else MONEY_ZERO
        if amount_col and debit == MONEY_ZERO and credit == MONEY_ZERO:
            amount = money(row.get(amount_col))
            if amount < 0:
                debit = abs(amount)
            else:
                credit = amount
        signed = credit - debit
        balance_raw = row.get(balance_col) if balance_col else None
        balance = money(balance_raw) if balance_raw not in (None, "") else None
        row_currency = normalize_text(row.get(currency_col or "")) or currency

        if not any([line_date, description, debit, credit, balance]):
            continue

        parsed = ParsedBankLine(
            line_date=line_date,
            value_date=parse_date(row.get(value_date_col or "")),
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
            raw_text=normalize_text(row),
            raw_lines=[normalize_text(row)],
            source_row_index=row_index,
            extraction_confidence=0.98,
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

    header: dict[str, Any] = {
        "statement_period_from": next((line.line_date for line in lines if line.line_date), None),
        "statement_period_to": next((line.line_date for line in reversed(lines) if line.line_date), None),
        "opening_balance": None,
        "closing_balance": dec_to_float(lines[-1].balance_amount) if lines and lines[-1].balance_amount is not None else None,
        "currency": currency,
        "confidence_score": 0.95,
        "extractor": "bank_statement",
        "extractor_type": "bank_statement",
        "extractor_version": "v1",
        "source_format": "csv",
        "parser_strategy": "deterministic_csv",
        "extraction_warnings": [],
        "raw_extraction": extraction_metadata(
            extractor_type="bank_statement",
            extractor_version="v1",
            source_format="csv",
            parser_strategy="deterministic_csv",
            confidence_score=0.95,
            warnings=[],
            extra={"fieldnames": fieldnames, "line_count": len(lines)},
        ),
    }
    if lines and lines[0].balance_amount is not None:
        header["opening_balance"] = dec_to_float(lines[0].balance_amount - lines[0].signed_amount)
    return header, lines


def extract_pdf_text(file_bytes: bytes) -> str:
    if fitz is None:
        return ""
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        return "\n".join(page.get_text("text") for page in doc)
    except Exception:
        return ""


DATE_ANCHOR_RE = re.compile(r"^(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b")
MONEY_TOKEN_RE = re.compile(r"(?:[A-Z]{3}\s*)?-?\(?[A-Z$R]?\s?\d[\d\s,]*[.,]\d{2}\)?")


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


def parse_text_statement_from_text(text: str, *, bank_account_id: str, currency: Optional[str] = None) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    blocks = parse_transaction_blocks(text)
    warnings: list[dict[str, Any]] = []
    if not blocks:
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
            "extraction_warnings": [warning("no_transaction_blocks", "No transaction blocks could be detected in PDF text.")],
            "raw_extraction": extraction_metadata(
                extractor_type="bank_statement",
                extractor_version="v1",
                source_format="pdf",
                parser_strategy="pdf_text_blocks",
                confidence_score=0,
                warnings=[warning("no_transaction_blocks", "No transaction blocks could be detected in PDF text.")],
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
        if not continuation and re.search(r"\b(to|pmt|payment|transfer|trf|cr)\b", block["transaction_type"], re.IGNORECASE):
            block_warnings.append(warning("missing_continuation_detail", "Transaction appears to need beneficiary/reference continuation detail."))

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
        "closing_balance": dec_to_float(lines[-1].balance_amount) if lines and lines[-1].balance_amount is not None else None,
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


def parse_text_statement(file_bytes: bytes, *, bank_account_id: str, currency: Optional[str] = None) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    text = extract_pdf_text(file_bytes)
    return parse_text_statement_from_text(text, bank_account_id=bank_account_id, currency=currency)


def bank_statement_vlm_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "statement_period_from": {"type": ["string", "null"]},
            "statement_period_to": {"type": ["string", "null"]},
            "opening_balance": {"type": ["number", "null"]},
            "closing_balance": {"type": ["number", "null"]},
            "currency": {"type": ["string", "null"]},
            "confidence_score": {"type": "number"},
            "transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line_date": {"type": ["string", "null"]},
                        "value_date": {"type": ["string", "null"]},
                        "transaction_type": {"type": ["string", "null"]},
                        "description": {"type": "string"},
                        "reference": {"type": ["string", "null"]},
                        "bank_reference": {"type": ["string", "null"]},
                        "counterparty": {"type": ["string", "null"]},
                        "debit_amount": {"type": "number"},
                        "credit_amount": {"type": "number"},
                        "balance_amount": {"type": ["number", "null"]},
                        "raw_text": {"type": ["string", "null"]},
                        "extraction_warnings": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["description", "debit_amount", "credit_amount"],
                },
            },
        },
        "required": ["transactions", "confidence_score"],
    }


def parse_vlm_statement(file_bytes: bytes, *, mime_type: str, bank_account_id: str, currency: Optional[str] = None) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
        from app.services.invoice_extraction.vlm_parser import preprocess_for_vlm
    except Exception as exc:  # pragma: no cover - optional provider
        raise ValueError("VLM bank statement extraction is not available in this environment") from exc

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY is not configured for VLM bank statement extraction")

    page_parts = preprocess_for_vlm(file_bytes, mime_type)
    if not page_parts:
        raise ValueError("Could not prepare statement pages for VLM extraction")

    client = genai.Client(api_key=api_key)
    contents: list[Any] = [
        "Extract bank statement header fields and transaction rows. "
        "Return strict JSON only. Debits are money out; credits are money in. "
        "Preserve continuation lines, beneficiary names, transaction labels, bank references, and raw row text. "
        "Do not drop rows with fees, stop orders, transfers, card purchases, credits, interest, or charges. "
        f"Use this schema as the contract: {json.dumps(bank_statement_vlm_json_schema())}"
    ]
    for image_bytes, image_mime in page_parts:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))

    response = client.models.generate_content(
        model=os.getenv("GEMINI_VLM_MODEL") or "gemini-2.5-flash",
        contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    payload = json.loads(response.text or "{}")
    txns = payload.get("transactions") or []
    lines: list[ParsedBankLine] = []
    for txn in txns:
        debit = money(txn.get("debit_amount"))
        credit = money(txn.get("credit_amount"))
        parsed = ParsedBankLine(
            line_date=parse_date(txn.get("line_date")),
            value_date=parse_date(txn.get("value_date")),
            description=normalize_text(txn.get("description")),
            reference=normalize_text(txn.get("reference")) or None,
            counterparty=normalize_text(txn.get("counterparty")) or None,
            debit_amount=debit,
            credit_amount=credit,
            signed_amount=credit - debit,
            balance_amount=money(txn.get("balance_amount")) if txn.get("balance_amount") is not None else None,
            currency=payload.get("currency") or currency,
            transaction_type=normalize_text(txn.get("transaction_type")) or None,
            bank_reference=normalize_text(txn.get("bank_reference")) or extract_bank_reference(txn.get("reference"), txn.get("description"), txn.get("counterparty")),
            raw_text=normalize_text(txn.get("raw_text")) or None,
            raw_lines=[normalize_text(txn.get("raw_text"))] if normalize_text(txn.get("raw_text")) else [],
            source_row_index=len(lines),
            extraction_confidence=float(payload.get("confidence_score") or 0.5),
            extraction_warnings=[
                warning("vlm_line_warning", str(item))
                for item in (txn.get("extraction_warnings") or [])
            ],
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
    extraction_warnings = [
        item
        for line in lines
        for item in (line.extraction_warnings or [])
    ]
    header = {
        "statement_period_from": parse_date(payload.get("statement_period_from")),
        "statement_period_to": parse_date(payload.get("statement_period_to")),
        "opening_balance": dec_to_float(money(payload.get("opening_balance"))) if payload.get("opening_balance") is not None else None,
        "closing_balance": dec_to_float(money(payload.get("closing_balance"))) if payload.get("closing_balance") is not None else None,
        "currency": payload.get("currency") or currency,
        "confidence_score": payload.get("confidence_score") or 0.5,
        "extractor": "bank_statement",
        "extractor_type": "bank_statement",
        "extractor_version": "v1",
        "source_format": "vlm",
        "parser_strategy": "vlm",
        "extraction_warnings": extraction_warnings,
        "raw_extraction": extraction_metadata(
            extractor_type="bank_statement",
            extractor_version="v1",
            source_format="vlm",
            parser_strategy="vlm",
            confidence_score=payload.get("confidence_score") or 0.5,
            warnings=extraction_warnings,
            extra={"line_count": len(lines)},
        ),
    }
    return header, lines


def stamp_extractor_selection(header: dict[str, Any], *, extractor_type: str, extractor_version: str, source_format: str, parser_strategy: str) -> dict[str, Any]:
    warnings = list(header.get("extraction_warnings") or [])
    header["extractor"] = extractor_type
    header["extractor_type"] = extractor_type
    header["extractor_version"] = extractor_version
    header["source_format"] = source_format
    header["parser_strategy"] = header.get("parser_strategy") or parser_strategy
    header["extraction_warnings"] = warnings
    raw_extraction = header.get("raw_extraction")
    if not raw_extraction:
        raw_extraction = extraction_metadata(
            extractor_type=extractor_type,
            extractor_version=extractor_version,
            source_format=source_format,
            parser_strategy=header["parser_strategy"],
            confidence_score=header.get("confidence_score"),
            warnings=warnings,
        )
    raw_extraction["extractor_type"] = extractor_type
    raw_extraction["extractor_version"] = extractor_version
    raw_extraction["source_format"] = source_format
    raw_extraction["parser_strategy"] = header["parser_strategy"]
    header["raw_extraction"] = raw_extraction
    return header


def extract_statement(file_bytes: bytes, *, filename: str, mime_type: str, bank_account_id: str, currency: Optional[str] = None, account_type: Optional[str] = None) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    selection = select_bank_cash_extractor(account_type=account_type, filename=filename, mime_type=mime_type)
    if not selection.profile.implemented:
        raise ValueError(
            f"Extractor profile {selection.profile.key}_{selection.profile.version} is registered but not implemented yet"
        )
    lower = filename.lower()
    effective_mime = (mime_type or "").lower()
    if lower.endswith(".csv") or "csv" in effective_mime:
        header, lines = parse_csv_statement(file_bytes, bank_account_id=bank_account_id, currency=currency)
        return stamp_extractor_selection(
            header,
            extractor_type=selection.profile.key,
            extractor_version=selection.profile.version,
            source_format=selection.source_format,
            parser_strategy="deterministic_csv",
        ), lines
    if lower.endswith(".pdf") or effective_mime == "application/pdf":
        header, lines = parse_text_statement(file_bytes, bank_account_id=bank_account_id, currency=currency)
        if lines:
            return stamp_extractor_selection(
                header,
                extractor_type=selection.profile.key,
                extractor_version=selection.profile.version,
                source_format=selection.source_format,
                parser_strategy="pdf_text_blocks",
            ), lines
    header, lines = parse_vlm_statement(file_bytes, mime_type=mime_type or "application/pdf", bank_account_id=bank_account_id, currency=currency)
    return stamp_extractor_selection(
        header,
        extractor_type=selection.profile.key,
        extractor_version=selection.profile.version,
        source_format=selection.source_format,
        parser_strategy="vlm",
    ), lines


def detect_line_duplicates(
    *,
    db,
    organisation_id: str,
    bank_account_id: str,
    lines: list[ParsedBankLine],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    hashes = [line.transaction_hash for line in lines]
    existing_hashes: set[str] = set()
    if hashes:
        try:
            res = (
                db.table("bank_statement_lines")
                .select("transaction_hash")
                .eq("organisation_id", organisation_id)
                .eq("bank_account_id", bank_account_id)
                .in_("transaction_hash", hashes)
                .execute()
            )
            existing_hashes = {row["transaction_hash"] for row in (res.data or [])}
        except Exception:
            existing_hashes = set()

    seen: set[str] = set()
    payloads: list[dict[str, Any]] = []
    duplicate_count = 0
    for line in lines:
        status = "clear"
        if line.transaction_hash in seen or line.transaction_hash in existing_hashes:
            status = "possible_duplicate"
            duplicate_count += 1
        seen.add(line.transaction_hash)
        payloads.append({"line": line, "duplicate_status": status})

    return payloads, {
        "duplicate_line_count": duplicate_count,
        "duplicate_status": "possible_duplicates" if duplicate_count else "clear",
        "checked_hashes": len(hashes),
    }


def validate_balances(
    *,
    account_current_balance: Decimal,
    header: dict[str, Any],
    lines: list[ParsedBankLine],
) -> dict[str, Any]:
    opening = money(header.get("opening_balance")) if header.get("opening_balance") is not None else None
    closing = money(header.get("closing_balance")) if header.get("closing_balance") is not None else None
    if opening is None or closing is None:
        return {"balance_status": "missing_balance", "expected_closing": None, "difference": None}
    if abs(opening - account_current_balance) > Decimal("0.01"):
        return {
            "balance_status": "opening_mismatch",
            "expected_opening": dec_to_float(account_current_balance),
            "actual_opening": dec_to_float(opening),
            "difference": dec_to_float(opening - account_current_balance),
        }
    expected = opening + sum((line.signed_amount for line in lines), MONEY_ZERO)
    if abs(expected - closing) > Decimal("0.01"):
        return {
            "balance_status": "closing_mismatch",
            "expected_closing": dec_to_float(expected),
            "actual_closing": dec_to_float(closing),
            "difference": dec_to_float(closing - expected),
        }
    return {"balance_status": "balanced", "expected_closing": dec_to_float(expected), "difference": 0}


def line_to_insert(line: ParsedBankLine, *, organisation_id: str, bank_account_id: str, upload_id: str, duplicate_status: str) -> dict[str, Any]:
    return {
        "organisation_id": organisation_id,
        "bank_account_id": bank_account_id,
        "bank_statement_upload_id": upload_id,
        "line_date": line.line_date,
        "value_date": line.value_date,
        "description": line.description,
        "reference": line.reference,
        "counterparty": line.counterparty,
        "transaction_type": line.transaction_type,
        "bank_reference": line.bank_reference,
        "raw_text": line.raw_text,
        "raw_lines": line.raw_lines or [],
        "source_page": line.source_page,
        "source_row_index": line.source_row_index,
        "extraction_confidence": line.extraction_confidence,
        "extraction_warnings": line.extraction_warnings or [],
        "debit_amount": dec_to_float(line.debit_amount) or 0,
        "credit_amount": dec_to_float(line.credit_amount) or 0,
        "signed_amount": dec_to_float(line.signed_amount) or 0,
        "balance_amount": dec_to_float(line.balance_amount),
        "currency": line.currency,
        "transaction_hash": line.transaction_hash,
        "duplicate_status": duplicate_status,
        "match_status": "unmatched",
        "allocation_status": "unallocated",
        "posting_status": "unposted",
    }


def score_invoice_suggestions(db, *, organisation_id: str, line: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    amount = abs(money(line.get("signed_amount")))
    text = " ".join(
        normalize_text(line.get(key))
        for key in ["reference", "bank_reference", "counterparty", "description", "raw_text"]
    ).lower()
    try:
        invoices = (
            db.table("invoices_extracted")
            .select("id, invoice_number, supplier_name, supplier_id, total_amount, invoice_date, review_status, approval_status")
            .eq("organisation_id", organisation_id)
            .limit(500)
            .execute()
            .data
            or []
        )
    except Exception:
        return []

    suggestions: list[dict[str, Any]] = []
    for inv in invoices:
        inv_total = money(inv.get("total_amount"))
        diff = abs(inv_total - amount)
        ref = normalize_text(inv.get("invoice_number")).lower()
        supplier_name = normalize_text(inv.get("supplier_name")).lower()
        confidence = Decimal("0.00")
        reasons: list[str] = []
        if ref and ref in text:
            confidence += Decimal("0.60")
            reasons.append("reference matches invoice number")
        if diff <= Decimal("0.01"):
            confidence += Decimal("0.30")
            reasons.append("amount matches")
        elif diff <= Decimal("1.00"):
            confidence += Decimal("0.15")
            reasons.append("amount is within tolerance")
        if supplier_name and supplier_name in text:
            confidence += Decimal("0.10")
            reasons.append("counterparty resembles supplier")
        if confidence <= Decimal("0.20"):
            continue
        suggestions.append(
            {
                "suggestion_type": "supplier_invoice",
                "confidence_score": float(min(confidence, Decimal("0.99"))),
                "rationale": "; ".join(reasons) or "possible invoice match",
                "matched_invoice_id": inv.get("id"),
                "matched_invoice_number": inv.get("invoice_number"),
                "evidence": {
                    "amount_difference": float(diff),
                    "invoice_total": float(inv_total),
                    "line_amount": float(amount),
                },
            }
        )
    suggestions.sort(key=lambda s: s["confidence_score"], reverse=True)
    return suggestions[:limit]


RULE_FIELDS = {"description", "raw_text", "counterparty", "reference", "bank_reference"}


def bank_rule_search_fields(line: dict[str, Any]) -> dict[str, str]:
    return {
        "description": normalize_text(line.get("description")).lower(),
        "raw_text": normalize_text(line.get("raw_text")).lower(),
        "counterparty": normalize_text(line.get("counterparty")).lower(),
        "reference": normalize_text(line.get("reference")).lower(),
        "bank_reference": normalize_text(line.get("bank_reference")).lower(),
    }


def normalize_rule_criteria(criteria: Any) -> list[dict[str, str]]:
    if not isinstance(criteria, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in criteria:
        if not isinstance(item, dict):
            continue
        field = normalize_text(item.get("field")).lower()
        operator = normalize_text(item.get("operator") or "contains").lower()
        value = normalize_text(item.get("value"))
        if field not in RULE_FIELDS or operator != "contains" or not value:
            continue
        normalized.append({"field": field, "operator": "contains", "value": value})
    return normalized


def default_rule_criteria_from_line(line: dict[str, Any]) -> list[dict[str, str]]:
    candidates = [
        ("bank_reference", line.get("bank_reference")),
        ("counterparty", line.get("counterparty")),
        ("raw_text", line.get("raw_text")),
        ("description", line.get("description")),
        ("reference", line.get("reference")),
    ]
    criteria: list[dict[str, str]] = []
    for field, value in candidates:
        text = normalize_text(value)
        if not text:
            continue
        criteria.append({"field": field, "operator": "contains", "value": text[:80]})
        if len(criteria) >= 2:
            break
    return criteria


def rule_matches_criteria(rule: dict[str, Any], line: dict[str, Any]) -> bool:
    criteria = normalize_rule_criteria(rule.get("criteria"))
    if not criteria:
        return False
    mode = normalize_text(rule.get("criteria_mode") or "and").lower()
    fields = bank_rule_search_fields(line)
    matches = [
        normalize_text(item["value"]).lower() in fields.get(item["field"], "")
        for item in criteria
    ]
    if mode == "or":
        return any(matches)
    return all(matches)


def rule_criteria_rationale(rule: dict[str, Any]) -> str:
    criteria = normalize_rule_criteria(rule.get("criteria"))
    if not criteria:
        return f"matched rule: {rule.get('name')}"
    mode = normalize_text(rule.get("criteria_mode") or "and").upper()
    joined = f" {mode} ".join(f"{item['field']} contains '{item['value']}'" for item in criteria)
    return f"matched rule: {rule.get('name')} ({joined})"


def score_rule_suggestions(db, *, organisation_id: str, bank_account_id: str, line: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    try:
        rules = (
            db.table("bank_transaction_rules")
            .select("*")
            .eq("organisation_id", organisation_id)
            .eq("active", True)
            .order("priority", desc=False)
            .limit(200)
            .execute()
            .data
            or []
        )
    except Exception:
        return []

    fields = bank_rule_search_fields(line)
    amount = money(line.get("signed_amount"))
    direction = "money_in" if amount >= 0 else "money_out"
    suggestions: list[dict[str, Any]] = []
    for rule in rules:
        rule_account = rule.get("bank_account_id")
        if rule_account and str(rule_account) != bank_account_id:
            continue
        if rule.get("amount_direction") not in (None, "any", direction):
            continue
        min_amount = rule.get("min_amount")
        max_amount = rule.get("max_amount")
        abs_amount = abs(amount)
        if min_amount is not None and abs_amount < money(min_amount):
            continue
        if max_amount is not None and abs_amount > money(max_amount):
            continue
        matched = rule_matches_criteria(rule, line)
        if not matched:
            for field_value, pattern in [
                (fields["description"], rule.get("description_pattern")),
                (fields["reference"], rule.get("reference_pattern")),
                (fields["counterparty"], rule.get("counterparty_pattern")),
            ]:
                pattern_text = normalize_text(pattern).lower()
                if not pattern_text:
                    continue
                if rule.get("match_type") == "exact" and field_value == pattern_text:
                    matched = True
                elif rule.get("match_type") == "regex":
                    try:
                        matched = bool(re.search(pattern_text, field_value))
                    except re.error:
                        matched = False
                elif pattern_text in field_value:
                    matched = True
        if matched:
            suggestions.append(
                {
                    "suggestion_type": "rule",
                    "confidence_score": 0.85,
                    "rationale": rule_criteria_rationale(rule),
                    "suggested_account_id": rule.get("gl_account_id"),
                    "suggested_tracking": rule.get("tracking") or {},
                    "suggested_tax_treatment": rule.get("tax_treatment"),
                    "evidence": {"rule_id": rule.get("id"), "rule_name": rule.get("name"), "criteria": normalize_rule_criteria(rule.get("criteria"))},
                }
            )
    return suggestions[:limit]


def journal_lines_for_bank_transaction(
    *,
    organisation_id: str,
    bank_account_gl_id: str,
    allocation_account_id: str,
    amount: Decimal,
    description: str,
    tracking: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    absolute = abs(amount)
    if absolute == MONEY_ZERO:
        raise ValueError("Cannot create a journal for a zero-value transaction")
    if amount >= 0:
        return [
            {"organisation_id": organisation_id, "account_id": bank_account_gl_id, "description": description, "debit_amount": float(absolute), "credit_amount": 0, "tracking": {}, "sort_order": 0},
            {"organisation_id": organisation_id, "account_id": allocation_account_id, "description": description, "debit_amount": 0, "credit_amount": float(absolute), "tracking": tracking or {}, "sort_order": 1},
        ]
    return [
        {"organisation_id": organisation_id, "account_id": allocation_account_id, "description": description, "debit_amount": float(absolute), "credit_amount": 0, "tracking": tracking or {}, "sort_order": 0},
        {"organisation_id": organisation_id, "account_id": bank_account_gl_id, "description": description, "debit_amount": 0, "credit_amount": float(absolute), "tracking": {}, "sort_order": 1},
    ]


def reversal_lines_for_journal(lines: list[dict[str, Any]], *, description: str) -> list[dict[str, Any]]:
    reversed_lines: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        reversed_lines.append(
            {
                "organisation_id": line["organisation_id"],
                "account_id": line.get("account_id"),
                "description": description,
                "debit_amount": dec_to_float(money(line.get("credit_amount"))) or 0,
                "credit_amount": dec_to_float(money(line.get("debit_amount"))) or 0,
                "tracking": line.get("tracking") or {},
                "sort_order": index,
            }
        )
    return reversed_lines


def new_uuid() -> str:
    return str(uuid4())
