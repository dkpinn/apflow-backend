from __future__ import annotations

import re
from datetime import date
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


DATE_ANCHOR_RE = re.compile(
    r"^(?P<date>"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}"
    r")\b",
    re.IGNORECASE,
)
MONEY_TOKEN_RE = re.compile(r"(?:[A-Z]{3}\s*)?-?\(?[A-Z$R]?\s?\d[\d\s,]*[.,]\d{2}\)?")

# Strict amount pattern (no spaces within numbers) used when searching for the
# debit/credit amount that precedes a date in description-first statement layouts.
_AMOUNT_RE = re.compile(r"(?:[A-Z]{3}\s*)?-?\(?[A-Z$R]?\s?\d[\d,]*[.,]\d{2}\)?")

# Matches [date] [balance] anchored at the end of a line.
# Balance uses the strict no-space pattern to avoid greedily absorbing a date
# fragment (e.g. "01" from "28/01") into the balance token.
# Used to detect description-first layouts: [description] [amount] [date] [balance].
_TAIL_RE = re.compile(
    r"\b(?P<date>"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}"
    r")\b"
    r"\s+"
    r"(?P<balance>(?:[A-Z]{3}\s*)?-?\(?[A-Z$R]?\s?\d[\d,]*[.,]\d{2}\)?)"
    r"\s*$",
    re.IGNORECASE,
)


PAGE_BREAK_MARKER = "__PAGE_BREAK__"

_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_STATEMENT_PERIOD_RE = re.compile(
    r"(?:statement\s+(?:period|date|from)|date\s+from)\s*:?\s*"
    r"(?P<date_from>\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})"
    r"\s*(?:to|-|–|—)\s*"
    r"(?P<date_to>\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    re.IGNORECASE,
)

# Detects "Cr" or "Dr" immediately after a money token — indicates FNB-style
# running balances where the suffix is printed directly after the number.
# Used to identify the extra "Accrued Bank Charges" column in FNB statements.
_CR_DR_RE = re.compile(r"^[CcDd][Rr]\b")


def _infer_statement_year(text: str) -> Optional[int]:
    for match in _YEAR_RE.finditer(text[:2000]):
        return int(match.group(1))
    return None


def _infer_statement_period(text: str) -> tuple[Optional[str], Optional[str]]:
    match = _STATEMENT_PERIOD_RE.search(text[:4000])
    if not match:
        return None, None
    return parse_date(match.group("date_from")), parse_date(match.group("date_to"))


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

        # Secondary: description-first layout — date+balance anchored at end of line.
        # [description] [amount] [date DD/MM or DD Mon] [balance]
        # Uses _TAIL_RE (strict no-space balance) to avoid MONEY_TOKEN_RE's greedy
        # [\d\s,]* consuming a date fragment like "01" from "28/01" into the token.
        tail_match = None
        embedded_amount_match = None
        if not starts_transaction:
            tail_match = _TAIL_RE.search(line)
            if tail_match:
                for m in _AMOUNT_RE.finditer(line[:tail_match.start("date")]):
                    embedded_amount_match = m  # keep last match before the date
                if embedded_amount_match:
                    starts_transaction = True

        if starts_transaction:
            if current:
                blocks.append(current)
            if date_match:
                # Date-at-start: [date] [description] [amount] [balance]
                # FNB Gold Business: when ≥3 tokens and the second-to-last is
                # immediately followed by "Cr"/"Dr", the last token is "Accrued
                # Bank Charges" — shift the assignment one position left.
                if (
                    len(money_matches) >= 3
                    and _CR_DR_RE.match(line[money_matches[-2].end():])
                ):
                    amount_match = money_matches[-3]
                    balance_match = money_matches[-2]
                else:
                    amount_match = money_matches[-2]
                    balance_match = money_matches[-1]
                detected_date = date_match.group("date")
                prefix = re.sub(r"\s+\*\s*$", "", normalize_text(line[date_match.end():amount_match.start()]))
                block_amount = money(amount_match.group(0))
                block_balance = money(balance_match.group(0))
            else:
                # Date-between-amounts: [description] [amount] [date] [balance]
                detected_date = tail_match.group("date")  # type: ignore[union-attr]
                prefix = re.sub(r"\s+\*\s*$", "", normalize_text(line[:embedded_amount_match.start()]))  # type: ignore[union-attr]
                block_amount = money(embedded_amount_match.group(0))  # type: ignore[union-attr]
                block_balance = money(tail_match.group("balance"))  # type: ignore[union-attr]
            transaction_type, reference = split_transaction_type_and_reference(prefix)
            current = {
                "date": detected_date,
                "prefix": prefix,
                "transaction_type": transaction_type or prefix,
                "reference": reference,
                "amount": block_amount,
                "balance": block_balance,
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
_COLUMNAR_OUTPUT_LABELS = ("transaction", "charge", "debit", "credit", "date", "balance")
_STANDARD_BANK_COMPACT_DATE_RE = re.compile(r"^(?P<month>\d{1,2})\s+(?P<day>\d{1,2})$")
_FOOTER_TEXT_RE = re.compile(
    r"^(our privacy|page \d|absa bank limited|authorised financial|registration number|vat registration|csp\d"
    r"|vat summary|total charge|total vat|##- these fees|these fees include)",
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


def _detect_absa_columnar_header(words: list[tuple[Any, ...]]) -> Optional[dict[str, Any]]:
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
    return {
        "name": "absa",
        "boundaries": boundaries,
        "header_y": header_y,
        "date_format": "default",
        "include_charge_in_text": True,
    }


def _detect_standard_bank_header(words: list[tuple[Any, ...]]) -> Optional[dict[str, Any]]:
    candidates: list[tuple[float, str, float]] = []
    wanted = {
        "details": "transaction",
        "service": "service",
        "fee": "fee",
        "debits": "debit",
        "credits": "credit",
        "date": "date",
        "balance": "balance",
    }
    for word in words:
        x0, y0, _x1, _y1, text = word[0], word[1], word[2], word[3], word[4]
        lowered = text.strip().lower().rstrip(":")
        if lowered in wanted:
            candidates.append((float(y0), wanted[lowered], float(x0)))

    for base_y, _label, _x0 in candidates:
        near: dict[str, float] = {}
        for y0, label, x0 in candidates:
            if abs(y0 - base_y) <= 12:
                near.setdefault(label, x0)
        if not all(label in near for label in ("transaction", "service", "fee", "debit", "credit", "date", "balance")):
            continue
        if not (near["transaction"] < near["service"] < near["debit"] < near["credit"] < near["date"] < near["balance"]):
            continue

        service_x = near["service"]
        debit_x = near["debit"]
        credit_x = near["credit"]
        date_x = near["date"]
        balance_x = near["balance"]
        return {
            "name": "standard_bank",
            "boundaries": {
                "transaction": (0.0, service_x - 10),
                "charge": (service_x - 10, debit_x - 15),
                "debit": (debit_x - 15, credit_x - 10),
                "credit": (credit_x - 10, date_x - 10),
                "date": (date_x - 10, balance_x - 15),
                "balance": (balance_x - 15, float("inf")),
            },
            "header_y": max(y0 for y0, _label, _x0 in candidates if abs(y0 - base_y) <= 12),
            "date_format": "month_day",
            "include_charge_in_text": False,
        }
    return None


def _detect_columnar_header(words: list[tuple[Any, ...]]) -> Optional[dict[str, Any]]:
    return _detect_absa_columnar_header(words) or _detect_standard_bank_header(words)


def _assign_column(x0: float, boundaries: dict[str, tuple[float, float]]) -> Optional[str]:
    for label, (left, right) in boundaries.items():
        if left <= x0 < right:
            return label
    return None


def _is_columnar_transaction_date(value: str, profile: dict[str, Any]) -> bool:
    if profile.get("date_format") == "month_day":
        return bool(_STANDARD_BANK_COMPACT_DATE_RE.match(value))
    return bool(DATE_ANCHOR_RE.match(value))


def _columnar_prefix(row_text: dict[str, str], profile: dict[str, Any]) -> str:
    if profile.get("include_charge_in_text", True):
        return normalize_text(f"{row_text['transaction']} {row_text['charge']}")
    return row_text["transaction"]


def _columnar_continuation_text(row_text: dict[str, str], profile: dict[str, Any]) -> str:
    text = _columnar_prefix(row_text, profile)
    if profile.get("name") == "standard_bank":
        lowered = text.lower()
        if row_text["balance"] or row_text["debit"] or row_text["credit"]:
            return ""
        if "balance brought forward" in lowered:
            return ""
    return text


def _parse_columnar_block_date(
    raw_date: str,
    *,
    date_format: Optional[str],
    statement_year: Optional[int],
    statement_period_from: Optional[str] = None,
    statement_period_to: Optional[str] = None,
    previous_line_date: Optional[str] = None,
) -> Optional[str]:
    if date_format == "month_day":
        match = _STANDARD_BANK_COMPACT_DATE_RE.match(raw_date)
        if not match:
            return None
        month = int(match.group("month"))
        day = int(match.group("day"))
        years: list[int] = []
        for period_date in (statement_period_from, statement_period_to):
            if period_date:
                year = int(period_date[:4])
                if year not in years:
                    years.append(year)
        if statement_year is not None and statement_year not in years:
            years.append(statement_year)
        if previous_line_date and int(previous_line_date[5:7]) == 12 and month == 1:
            rollover_year = int(previous_line_date[:4]) + 1
            if rollover_year not in years:
                years.insert(0, rollover_year)

        candidates: list[str] = []
        for year in years:
            try:
                candidate = date(year, month, day).isoformat()
            except ValueError:
                continue
            if candidate not in candidates:
                candidates.append(candidate)
        if not candidates:
            return None

        if statement_period_from and statement_period_to:
            in_period = [
                candidate for candidate in candidates
                if statement_period_from <= candidate <= statement_period_to
            ]
            if in_period:
                if previous_line_date:
                    forward = [candidate for candidate in in_period if candidate >= previous_line_date]
                    return min(forward) if forward else min(in_period)
                return min(in_period)

        return candidates[0]

    line_date = parse_date(raw_date, year=statement_year)
    if (
        statement_year is not None
        and line_date is not None
        and line_date[5:7] == "01"
        and previous_line_date is not None
        and previous_line_date[5:7] == "12"
    ):
        line_date = parse_date(raw_date, year=statement_year + 1)
    return line_date


def parse_columnar_transaction_blocks(pages_words: list[list[tuple[Any, ...]]]) -> list[dict[str, Any]]:
    """Reconstruct transaction rows from word coordinates for "Print to PDF"
    statements where ``get_text("text")`` extracts dates, descriptions and
    amounts as separate column-major blocks rather than row-by-row."""
    blocks: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    profile: Optional[dict[str, Any]] = None

    for page_index, words in enumerate(pages_words, start=1):
        detected = _detect_columnar_header(words)
        if detected:
            profile = detected
        else:
            continue

        boundaries = profile["boundaries"]
        body_words = [word for word in words if word[1] > profile["header_y"] + 1.0]
        rows: list[list[tuple[Any, ...]]] = []
        for word in sorted(body_words, key=lambda w: (w[1], w[0])):
            if rows and abs(word[1] - rows[-1][0][1]) <= 2.0:
                rows[-1].append(word)
            else:
                rows.append([word])

        for row in rows:
            columns: dict[str, list[tuple[float, str]]] = {label: [] for label in _COLUMNAR_OUTPUT_LABELS}
            for word in row:
                x0, text = word[0], word[4]
                label = _assign_column(x0, boundaries)
                if label:
                    columns[label].append((x0, text))
            row_text = {
                label: normalize_text(" ".join(text for _, text in sorted(items, key=lambda item: item[0])))
                for label, items in columns.items()
            }

            if _is_columnar_transaction_date(row_text["date"], profile):
                if current:
                    blocks.append(current)
                prefix = _columnar_prefix(row_text, profile)
                transaction_type, reference = split_transaction_type_and_reference(prefix)
                raw_line = normalize_text(" ".join(text for text in row_text.values() if text))
                current = {
                    "date": row_text["date"],
                    "date_format": profile.get("date_format"),
                    "prefix": prefix,
                    "transaction_type": transaction_type or prefix,
                    "reference": reference,
                    "debit": abs(money(row_text["debit"])),
                    "credit": abs(money(row_text["credit"])),
                    "balance": money(row_text["balance"]),
                    "raw_lines": [raw_line],
                    "continuation_lines": [],
                    "page": page_index,
                }
            elif current:
                continuation_text = _columnar_continuation_text(row_text, profile)
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
    statement_year: Optional[int] = None,
    statement_period_from: Optional[str] = None,
    statement_period_to: Optional[str] = None,
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

        line_date = _parse_columnar_block_date(
            block["date"],
            date_format=block.get("date_format"),
            statement_year=statement_year,
            statement_period_from=statement_period_from,
            statement_period_to=statement_period_to,
            previous_line_date=lines[-1].line_date if lines else None,
        )

        parsed = ParsedBankLine(
            line_date=line_date,
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
        "statement_period_from": statement_period_from or next((line.line_date for line in lines if line.line_date), None),
        "statement_period_to": statement_period_to or next((line.line_date for line in reversed(lines) if line.line_date), None),
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
    statement_year = _infer_statement_year(text)
    statement_period_from, statement_period_to = _infer_statement_period(text)
    blocks = parse_transaction_blocks(text)
    return _build_statement_from_blocks(
        blocks, "pdf_text_blocks", text,
        bank_account_id=bank_account_id,
        currency=currency,
        statement_year=statement_year,
        statement_period_from=statement_period_from,
        statement_period_to=statement_period_to,
    )


def parse_text_statement(
    file_bytes: bytes,
    *,
    bank_account_id: str,
    currency: Optional[str] = None,
) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    text = extract_pdf_text(file_bytes)
    statement_year = _infer_statement_year(text)
    statement_period_from, statement_period_to = _infer_statement_period(text)
    blocks = parse_transaction_blocks(text)
    parser_strategy = "pdf_text_blocks"
    columnar_blocks = parse_columnar_transaction_blocks(extract_pdf_words_by_page(file_bytes))
    if columnar_blocks and (
        not blocks
        or len(columnar_blocks) >= len(blocks) + 2
        or len(columnar_blocks) >= max(2, int(len(blocks) * 1.25))
    ):
        blocks = columnar_blocks
        parser_strategy = "pdf_columnar_blocks"
    return _build_statement_from_blocks(
        blocks, parser_strategy, text,
        bank_account_id=bank_account_id,
        currency=currency,
        statement_year=statement_year,
        statement_period_from=statement_period_from,
        statement_period_to=statement_period_to,
    )
