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


PAGE_BREAK_MARKER = "__PAGE_BREAK__"


def extract_pdf_text(file_bytes: bytes) -> str:
    if fitz is None:
        return ""
    try:
        document = fitz.open(stream=file_bytes, filetype="pdf")
        return f"\n{PAGE_BREAK_MARKER}\n".join(page.get_text("text") for page in document)
    except Exception:
        return ""


def parse_transaction_blocks(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    current_page = 1

    for raw in text.splitlines():
        if raw.strip() == PAGE_BREAK_MARKER:
            current_page += 1
            continue
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
                "page": current_page,
            }
        elif current:
            current["raw_lines"].append(line)
            current["continuation_lines"].append(line)

    if current:
        blocks.append(current)
    return blocks


_HEADER_ANCHOR_LABELS = ("date", "transaction", "charge", "debit", "credit", "balance")
_FOOTER_TEXT_RE = re.compile(
    r"^(our privacy|page \d|absa bank limited|authorised financial|registration number|vat registration|csp\d)",
    re.IGNORECASE,
)


def extract_pdf_words_by_page(file_bytes: bytes) -> list[list[tuple[Any, ...]]]:
    if fitz is None:
        return []
    try:
        document = fitz.open(stream=file_bytes, filetype="pdf")
        return [page.get_text("words") for page in document]
    except Exception:
        return []


def _detect_columnar_header(words: list[tuple[Any, ...]]) -> Optional[tuple[dict[str, tuple[float, float]], float]]:
    # Labels like "balance", "credit" and "charge" also appear as words inside the
    # statement body (e.g. "Balance Brought Forward", "Acb Credit"), so we can't just
    # take the first occurrence of each label. Instead, group candidate words by row
    # (rounded y0) and find a row that contains every required column header.
    by_row: dict[int, dict[str, float]] = {}
    for word in words:
        x0, y0, _x1, _y1, text = word[0], word[1], word[2], word[3], word[4]
        lowered = text.strip().lower().rstrip(":")
        if lowered == "description":
            lowered = "transaction"
        if lowered not in _HEADER_ANCHOR_LABELS:
            continue
        bucket = by_row.setdefault(round(y0), {})
        bucket.setdefault(lowered, x0)

    anchors: Optional[dict[str, float]] = None
    header_y: Optional[float] = None
    for row_y, bucket in by_row.items():
        if all(label in bucket for label in _HEADER_ANCHOR_LABELS):
            anchors = bucket
            header_y = float(row_y)
            break
    if anchors is None or header_y is None:
        return None

    ordered = sorted(_HEADER_ANCHOR_LABELS, key=lambda label: anchors[label])
    boundaries: dict[str, tuple[float, float]] = {}
    for index, label in enumerate(ordered):
        x0 = anchors[label]
        left = 0.0 if index == 0 else (anchors[ordered[index - 1]] + x0) / 2
        right = float("inf") if index == len(ordered) - 1 else (x0 + anchors[ordered[index + 1]]) / 2
        boundaries[label] = (left, right)
    return boundaries, header_y


def _assign_column(x0: float, boundaries: dict[str, tuple[float, float]]) -> Optional[str]:
    for label, (left, right) in boundaries.items():
        if left <= x0 < right:
            return label
    return None


def parse_columnar_transaction_blocks(pages_words: list[list[tuple[Any, ...]]]) -> list[dict[str, Any]]:
    """Reconstruct transaction rows from word coordinates for "Print to PDF"
    statements where ``get_text("text")`` extracts dates, descriptions and
    amounts as separate column-major blocks rather than row-by-row."""
    blocks: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    boundaries: Optional[dict[str, tuple[float, float]]] = None
    header_y = -1.0

    for page_index, words in enumerate(pages_words, start=1):
        detected = _detect_columnar_header(words)
        if detected:
            boundaries, header_y = detected
        if boundaries is None:
            continue

        body_words = [word for word in words if word[1] > header_y + 1.0]
        rows: list[list[tuple[Any, ...]]] = []
        for word in sorted(body_words, key=lambda w: (w[1], w[0])):
            if rows and abs(word[1] - rows[-1][0][1]) <= 2.0:
                rows[-1].append(word)
            else:
                rows.append([word])

        for row in rows:
            columns: dict[str, list[tuple[float, str]]] = {label: [] for label in _HEADER_ANCHOR_LABELS}
            for word in row:
                x0, text = word[0], word[4]
                label = _assign_column(x0, boundaries)
                if label:
                    columns[label].append((x0, text))
            row_text = {
                label: normalize_text(" ".join(text for _, text in sorted(items, key=lambda item: item[0])))
                for label, items in columns.items()
            }

            date_match = DATE_ANCHOR_RE.match(row_text["date"])
            if date_match:
                if current:
                    blocks.append(current)
                prefix = normalize_text(f"{row_text['transaction']} {row_text['charge']}")
                transaction_type, reference = split_transaction_type_and_reference(prefix)
                raw_line = normalize_text(" ".join(text for text in row_text.values() if text))
                current = {
                    "date": date_match.group("date"),
                    "prefix": prefix,
                    "transaction_type": transaction_type or prefix,
                    "reference": reference,
                    "debit": money(row_text["debit"]),
                    "credit": money(row_text["credit"]),
                    "balance": money(row_text["balance"]),
                    "raw_lines": [raw_line],
                    "continuation_lines": [],
                    "page": page_index,
                }
            elif current:
                continuation_text = normalize_text(f"{row_text['transaction']} {row_text['charge']}")
                if not continuation_text or _FOOTER_TEXT_RE.match(continuation_text):
                    continue
                current["raw_lines"].append(continuation_text)
                current["continuation_lines"].append(continuation_text)

    if current:
        blocks.append(current)
    return blocks


def _build_statement_from_blocks(
    blocks: list[dict[str, Any]],
    parser_strategy: str,
    text: str,
    *,
    bank_account_id: str,
    currency: Optional[str] = None,
) -> tuple[dict[str, Any], list[ParsedBankLine]]:
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
        if "debit" in block and "credit" in block:
            signed = block["credit"] - block["debit"]
        else:
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

        if "debit" in block and "credit" in block:
            debit = block["debit"]
            credit = block["credit"]
        else:
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
            source_page=block.get("page"),
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
        "parser_strategy": parser_strategy,
        "extraction_warnings": warnings,
        "raw_extraction": extraction_metadata(
            extractor_type="bank_statement",
            extractor_version="v1",
            source_format="pdf",
            parser_strategy=parser_strategy,
            confidence_score=confidence_score,
            warnings=warnings,
            raw_preview=text,
            extra={"detected_transaction_blocks": len(blocks), "line_count": len(lines)},
        ),
    }
    return header, lines


def parse_text_statement_from_text(
    text: str,
    *,
    bank_account_id: str,
    currency: Optional[str] = None,
) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    blocks = parse_transaction_blocks(text)
    return _build_statement_from_blocks(blocks, "pdf_text_blocks", text, bank_account_id=bank_account_id, currency=currency)


def parse_text_statement(
    file_bytes: bytes,
    *,
    bank_account_id: str,
    currency: Optional[str] = None,
) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    text = extract_pdf_text(file_bytes)
    blocks = parse_transaction_blocks(text)
    parser_strategy = "pdf_text_blocks"
    if not blocks:
        columnar_blocks = parse_columnar_transaction_blocks(extract_pdf_words_by_page(file_bytes))
        if columnar_blocks:
            blocks = columnar_blocks
            parser_strategy = "pdf_columnar_blocks"
    return _build_statement_from_blocks(blocks, parser_strategy, text, bank_account_id=bank_account_id, currency=currency)
