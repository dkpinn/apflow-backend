from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


ADDRESS_TERMS_RE = re.compile(
    r"\b("
    r"south\s+africa|road|rd|street|st|avenue|ave|lane|ln|drive|dr|"
    r"crescent|cres|close|cl|park|industrial|germany|durban|johannesburg|"
    r"cape\s+town|pretoria|pinetown|germiston"
    r")\b",
    re.IGNORECASE,
)

RECIPIENT_BLOCK_LABEL_RE = re.compile(
    r"^\s*(deliver(?:y)?\s+to|ship\s+to|bill\s+to|invoice\s+to|customer|client|sold\s+to|to)\s*:?\s*$",
    re.IGNORECASE,
)

SUPPLIER_BLOCK_LABEL_RE = re.compile(
    r"^\s*(from|supplier|vendor|seller)\s*:?\s*$",
    re.IGNORECASE,
)

SUPPLIER_EVIDENCE_RE = re.compile(
    r"\b(vat|tax\s+registration|reg(?:istration)?\s+(?:number|no)|ck\s+no|"
    r"tel(?:ephone)?|fax|email|e-mail|www\.|@)\b",
    re.IGNORECASE,
)

DOCUMENT_METADATA_RE = re.compile(
    r"\b("
    r"tax\s+invoice|invoice\s+(?:number|no|date)|document\s+no|date|page|"
    r"customer\s+(?:no|number|code)|sales\s*person|sales\s*rep|terms|"
    r"po\s+number|reference|qty|quantity|item\s+number|description|"
    r"unit\s+price|disc\s+price|discount|extended\s+price|subtotal|total"
    r")\b",
    re.IGNORECASE,
)

ADDRESS_STOP_RE = re.compile(
    r"\b("
    r"p\s*\.?\s*o\s*\.?\s*box|po\s+box|postnet|private\s+bag|"
    r"tel(?:ephone)?|fax|e-?mail|email|website|www\.|@|"
    r"tax\s+registration|vat|reg\s+number|registration\s+number|ck\s+no|"
    r"tax\s+invoice|invoice\s+(?:number|no|date)|date|page|"
    r"customer\s+(?:no|number|code)|sales\s*person|sales\s*rep|terms|"
    r"po\s+number|reference|qty|quantity|item\s+number|description|"
    r"unit\s+price|disc\s+price|discount|extended\s+price|subtotal|total|"
    r"computer\s+generated"
    r")\b",
    re.IGNORECASE,
)

TABLE_CONTEXT_RE = re.compile(
    r"\b("
    r"customer\s+(?:no|number|code)|sales\s*person|sales\s*rep|terms|"
    r"po\s+number|reference|qty|quantity|item\s+number|description|"
    r"unit\s+price|disc\s+price|extended\s+price"
    r")\b",
    re.IGNORECASE,
)

VAT_LABEL_RE = re.compile(
    r"(?:Tax\s+Registration|VAT\s+Registration|VAT\s+Reg\.?|VAT\s+Reg\s+No\.?|"
    r"VAT\s+No\.?|VAT\s+Number|VAT)\s*(?:No\.?|Number)?\s*[:#\-]?",
    re.IGNORECASE,
)


def compact_line(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def is_recipient_block_label(line: str) -> bool:
    return bool(RECIPIENT_BLOCK_LABEL_RE.match(line or ""))


def is_supplier_block_label(line: str) -> bool:
    return bool(SUPPLIER_BLOCK_LABEL_RE.match(line or ""))


def has_supplier_evidence(text: str) -> bool:
    return bool(SUPPLIER_EVIDENCE_RE.search(text or ""))


def looks_like_address_value(value: str) -> bool:
    clean = compact_line(value).strip(" :;,.")
    lower = clean.lower()
    if not clean:
        return False

    if re.search(r"\bp\s*\.?\s*o\s*\.?\s*box\b|\bpo\s+box\b|\bpostnet\b|\bprivate\s+bag\b", lower):
        return True

    if lower in {"south africa"}:
        return True

    if re.fullmatch(r"(south\s+africa\s*)+", lower):
        return True

    if re.search(r"\b\d{1,5}\s+[A-Za-z][A-Za-z .'-]*(?:road|rd|street|st|avenue|ave|lane|ln|drive|dr)\b", clean, re.IGNORECASE):
        return True

    if re.search(r"\b(?:road|rd|street|st|avenue|ave|lane|ln|drive|dr)\b", clean, re.IGNORECASE):
        return True

    if ADDRESS_TERMS_RE.search(clean) and re.search(r"\b\d{4}\b|\b\d{1,5}\b", clean):
        return True

    words = re.findall(r"[A-Za-z]+", clean)
    if ADDRESS_TERMS_RE.search(clean) and len(words) >= 2 and not re.search(r"\b(pty|ltd|cc|inc|trading|services?)\b", lower):
        return True

    return False


def is_document_metadata_value(value: str) -> bool:
    return bool(DOCUMENT_METADATA_RE.search(value or ""))


def is_address_stop_line(line: str) -> bool:
    return bool(ADDRESS_STOP_RE.search(line or "") or is_recipient_block_label(line or ""))


def is_valid_supplier_address_line(line: str) -> bool:
    clean = compact_line(line)
    if not clean or len(clean) > 90:
        return False
    if is_address_stop_line(clean) or is_document_metadata_value(clean):
        return False
    if "@" in clean or "www." in clean.lower():
        return False
    if re.match(r"^[\W_]{2,}", clean):
        return False
    return True


def address_contains_metadata(value: Optional[str]) -> bool:
    if not value:
        return False
    return any(is_address_stop_line(line) or is_document_metadata_value(line) for line in str(value).splitlines())


def looks_like_location_cluster(name: str) -> bool:
    """Return True when a candidate looks like a suburb/area cluster rather than
    a business name: ALL CAPS, 2-4 short words, no legal entity terms, no
    industry keywords.

    Catches:  "COWIES HILL EURIKA", "FALCON PARK", "NEW GERMANY WEST"
    Excludes: "PRODEC PAINTS CC" (has CC), "SPAR" (single word),
              "MIKE'S PLUMBING" (industry term), names with > 4 words.
    """
    if not name:
        return False
    clean = re.sub(r"\s+", " ", name).strip()
    if clean != clean.upper():
        return False  # Mixed case → business name
    words = re.findall(r"[A-Za-z]+", clean)
    if len(words) < 2 or len(words) > 4:
        return False  # Single word (SPAR/SHELL) or too long
    if any(len(w) > 12 for w in words):
        return False  # Long word suggests company name
    if re.search(
        r"\b(pty|ltd|limited|cc|inc|company|corp|co)\b",
        clean,
        re.IGNORECASE,
    ):
        return False  # Legal entity term present
    if re.search(
        r"\b(trading|plumb(?:ers?|ing)?|electric(?:al|ians?)?|paint(?:s|ing)?|"
        r"hardware|services?|construction|tyres?|repairs?|auto|motor|store|"
        r"shop|supplies|wholesale|engineering|contractors?)\b",
        clean,
        re.IGNORECASE,
    ):
        return False  # Industry term present
    return True  # Looks like suburb/area names


@dataclass(frozen=True)
class VatCandidate:
    value: str
    score: int
    line_index: int


def _digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def _candidate_context(lines: list[str], index: int, *, before: int = 6, after: int = 6) -> str:
    start = max(0, index - before)
    end = min(len(lines), index + after + 1)
    return "\n".join(lines[start:end])


def _line_in_recipient_zone(lines: list[str], index: int) -> bool:
    start = max(0, index - 8)
    for line in reversed(lines[start:index + 1]):
        if is_supplier_block_label(line):
            return False
        if is_recipient_block_label(line):
            return True
    return False


def score_vat_candidate(lines: list[str], value: str, line_index: int) -> int:
    context = _candidate_context(lines, line_index)
    score = 0

    if line_index <= 20:
        score += 5
    elif line_index <= 45:
        score += 1
    else:
        score -= 2

    if has_supplier_evidence(context):
        score += 4
    if re.search(r"\b(reg(?:istration)?\s+(?:number|no)|ck\s+no|tel(?:ephone)?|fax|email|@|www\.)\b", context, re.IGNORECASE):
        score += 2
    if _line_in_recipient_zone(lines, line_index):
        score -= 8
    if TABLE_CONTEXT_RE.search(context):
        score -= 6
    if re.search(r"\b(invoice\s+number|invoice\s+date|page|date)\b", context, re.IGNORECASE):
        score -= 2

    digits = _digits(value)
    if len(digits) == 10:
        score += 1
    return score


def extract_vat_candidates(lines: list[str]) -> list[VatCandidate]:
    candidates: list[VatCandidate] = []
    seen: set[tuple[str, int]] = set()

    for index, line in enumerate(lines):
        if not VAT_LABEL_RE.search(line):
            continue

        window = lines[index:index + 5]
        for offset, candidate_line in enumerate(window):
            for match in re.finditer(r"\b([0-9][0-9\s\-]{6,18}[0-9])\b", candidate_line):
                digits = _digits(match.group(1))
                if not 7 <= len(digits) <= 15:
                    continue
                key = (digits, index + offset)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(VatCandidate(
                    value=digits,
                    score=score_vat_candidate(lines, digits, index + offset),
                    line_index=index + offset,
                ))

    candidates.sort(key=lambda item: (item.score, -item.line_index), reverse=True)
    return candidates
