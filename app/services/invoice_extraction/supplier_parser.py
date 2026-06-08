from __future__ import annotations

import re
from typing import Optional

from app.services.invoice_extraction.extraction_rules import (
    has_supplier_evidence,
    is_document_metadata_value,
    is_recipient_block_label,
    looks_like_address_value,
    looks_like_location_cluster,
)


MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "jan",
    "feb",
    "mar",
    "apr",
    "jun",
    "jul",
    "aug",
    "sep",
    "sept",
    "oct",
    "nov",
    "dec",
)


def normalise_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


_STANDALONE_BANK_BRANDS = {
    "standard", "nedbank", "absa", "capitec", "investec",
    "wesbank", "tymebank", "fnb",
}


def line_has_bank_context(line: str) -> bool:
    lower = line.lower()

    # Reject single-word lines that are a known bank brand name — handles the case
    # where OCR splits "Standard Bank" across two lines (line 1: "Standard", line 2: "Bank").
    if lower.strip() in _STANDALONE_BANK_BRANDS:
        return True

    bank_patterns = [
        r"\bbank\b",
        r"\bbranch\b",
        r"\bsort code\b",
        r"\brouting\b",
        r"\baccount number\b",
        r"\bacc no\b",
        r"\biban\b",
        r"\bswift\b",
        r"\bbic\b",
        r"\bnedbank\b",
        r"\bstandard bank\b",
        r"\babsa\b",
        r"\bfnb\b",
        r"\bfirst national bank\b",
        r"\bcapitec\b",
        r"\binvestec\b",
    ]

    return any(re.search(pattern, lower) for pattern in bank_patterns)


def is_date_like_supplier_candidate(line: str) -> bool:
    clean = re.sub(r"\s+", " ", (line or "").strip())
    lower = clean.lower().strip(" :;,.")
    if not lower:
        return False

    month_pattern = "|".join(MONTH_NAMES)
    if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", lower):
        return True
    if re.fullmatch(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}", lower):
        return True
    if re.fullmatch(rf"\d{{1,2}}(?:st|nd|rd|th)?\s+({month_pattern})\s+\d{{2,4}}", lower):
        return True
    if re.fullmatch(rf"({month_pattern})\s+\d{{1,2}}(?:st|nd|rd|th)?[,]?\s+\d{{2,4}}", lower):
        return True
    if re.fullmatch(rf"({month_pattern})\s+\d{{2,4}}", lower):
        return True

    words = re.findall(r"[A-Za-z]+|\d{2,4}", lower)
    if not words:
        return False
    month_hits = sum(1 for word in words if word in MONTH_NAMES)
    year_hits = sum(1 for word in words if re.fullmatch(r"20\d{2}|19\d{2}", word))
    return month_hits > 0 and year_hits > 0 and len(words) <= 5


def is_metadata_supplier_candidate(line: str) -> bool:
    lower = re.sub(r"\s+", " ", (line or "").lower()).strip(" :;,.")
    if not lower:
        return True

    metadata_exact = {
        "invoice",
        "tax invoice",
        "date",
        "invoice date",
        "not registered for vat",
        "registered for vat",
        "vat",
        "vat number",
        "description",
        "unit price",
        "total",
        "paid",
        "banking details",
    }
    if lower in metadata_exact:
        return True

    metadata_patterns = [
        r"\bnot\s+registered\s+for\s+vat\b",
        r"\bregistered\s+for\s+vat\b",
        r"\binvoice\s+(date|number|no)\b",
        r"\bvat\s*(number|no|registration)?\b",
        r"\b(qty|quantity|description|unit\s+price|subtotal|total)\b",
    ]
    return any(re.search(pattern, lower) for pattern in metadata_patterns)


def is_valid_supplier_candidate(line: str) -> bool:
    lower = line.lower().strip()
    clean = line.strip()

    ignored_terms = [
        "copy",
        "original",
        "copy of original",
        "customer copy",
        "invoice",
        "tax invoice",
        "tax invoice customer copy",
        "statement",
        "date",
        "invoice date",
        "invoice number",
        "document no",
        "customer",
        "client",
        "bill to",
        "ship to",
        "deliver to",
        "sales rep",
        "sales representative",
        "vat",
        "total",
        "subtotal",
        "amount",
        "description",
        "quantity",
        "unit price",
        "page",
        "www",
        "email",
        "tel",
        "fax",
        "address",
        "product code",
        "configuration",
        "please note",
        "welcome",
        "welcame",
        "welkom",
    ]

    if not clean:
        return False

    if (
        is_date_like_supplier_candidate(clean)
        or is_metadata_supplier_candidate(clean)
        or is_document_metadata_value(clean)
        or looks_like_address_value(clean)
        or is_recipient_block_label(clean)
    ):
        return False

    if lower in {"pty", "(pty)", "ltd", "limited", "(pty) ltd", "pty ltd"}:
        return False

    if lower in {"copy", "original", "customer copy", "copy of original"}:
        return False

    if "copy of original" in lower or "customer copy" in lower:
        return False

    if re.match(r"^[\W_]{2,}", clean):
        return False

    alpha_count = sum(char.isalpha() for char in clean)
    if alpha_count < 3:
        return False

    if len(clean) >= 6 and alpha_count / len(clean) < 0.45:
        return False

    words = re.findall(r"[A-Za-z]+", clean)
    if len(words) == 1 and len(words[0]) <= 4 and not re.search(r"\b(absa|fnb)\b", lower):
        return False

    if "@" in line:
        return False

    if any(term in lower for term in ignored_terms):
        return False

    if line_has_bank_context(line):
        return False

    if len(line) < 3 or len(line) > 100:
        return False

    # Avoid phone/VAT/company-number-only lines
    if re.fullmatch(r"[+0-9 ()\-]{7,25}", line):
        return False

    if re.search(r"^[\W_]+$", line):
        return False

    return True


def _phone_digits(line: str) -> str:
    return re.sub(r"\D+", "", line or "")


def line_has_phone_number(line: str) -> bool:
    digits = _phone_digits(line)
    if len(digits) < 10 or len(digits) > 13:
        return False
    return digits.startswith("0") or digits.startswith("27")


def looks_like_legal_entity(line: str) -> bool:
    if line.lower().strip() in {"pty", "(pty)", "ltd", "limited", "(pty) ltd", "pty ltd"}:
        return False
    return bool(
        re.search(
            r"\b(pty|ltd|limited|cc|inc|company|corporation|corp|co\.?)\b",
            line,
            re.IGNORECASE,
        )
    )


def _clean_receipt_supplier_candidate(value: str) -> str:
    candidate = re.sub(r"[_|]+", " ", value or "")
    candidate = re.sub(r"[^A-Za-z0-9 &().,/\-]+", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" -:,.")
    candidate = re.sub(r"\bT\s*/\s*A\b", "t/a", candidate, flags=re.IGNORECASE)
    return candidate


def extract_massmart_builders_supplier(lines: list[str]) -> Optional[str]:
    """
    Prefer the full legal/trading line on Massmart/Builders till receipts.

    OCR often exposes this as one line, but sometimes splits the legal name and
    trading-as brand across adjacent lines.
    """
    top_lines = lines[:80]
    for window_size in (1, 2, 3):
        for index in range(0, max(len(top_lines) - window_size + 1, 0)):
            candidate = _clean_receipt_supplier_candidate(
                " ".join(top_lines[index:index + window_size])
            )
            lower = candidate.lower()
            if "massmart" not in lower:
                continue
            if not re.search(r"\b(retailer|retail|builders|warehouse|t/a|pty|ltd)\b", lower):
                continue
            if re.search(r"\b(vat|invoice|receipt|tel|telephone|fax|email|total|subtotal)\b", lower):
                continue
            if len(candidate) <= 140:
                return candidate

    for line in top_lines:
        candidate = _clean_receipt_supplier_candidate(line)
        if re.search(r"\bbuilders\s+warehouse\b|\bbuilders\b", candidate, re.IGNORECASE):
            if not re.search(r"\b(vat|invoice|receipt|tel|total|subtotal)\b", candidate, re.IGNORECASE):
                return candidate

    return None


def extract_supplier_from_receipt_text(text: str) -> Optional[str]:
    lines = normalise_lines(text)
    joined = "\n".join(lines[:80])

    if re.search(r"\bwait[eo]ns\b|wait[eo]ns\.co\.za", text, re.IGNORECASE):
        return "Waitens"

    massmart_builders_supplier = extract_massmart_builders_supplier(lines)
    if massmart_builders_supplier:
        return massmart_builders_supplier

    build_it_candidates: list[str] = []
    for line in lines[:80]:
        if re.search(r"\b(?:build\s*it|bui[l1i]d\s*it|pinetown\s+bui|netown\s+bui)\b", line, re.IGNORECASE):
            candidate = re.sub(r"[_|]+", " ", line)
            candidate = re.sub(r"\s+", " ", candidate).strip(" -:")
            if is_valid_supplier_candidate(candidate):
                build_it_candidates.append(candidate)

    if build_it_candidates:
        for candidate in build_it_candidates:
            if re.search(r"\bpinetown\b|\bnewtown\b|\bbuild\s*it\b", candidate, re.IGNORECASE):
                return candidate.upper() if candidate.isupper() else candidate
        return build_it_candidates[0]

    if re.search(r"\bmassmart\b", joined, re.IGNORECASE):
        for line in lines[:80]:
            if re.search(r"\bmassmart\b", line, re.IGNORECASE):
                return _clean_receipt_supplier_candidate(line)

    if re.search(r"\bbuilders\b", joined, re.IGNORECASE):
        return "Builders"

    return None


def extract_supplier_from_evetech_layout(text: str) -> Optional[str]:
    """
    Evetech invoice-specific supplier extraction.

    The customer appears below 'Sales Rep', so generic candidate scanning can
    incorrectly pick the customer. The supplier is EVETECH / EVETECH (Pty) Ltd.
    """

    lines = normalise_lines(text)

    for line in lines[:30]:
        if re.search(r"\bEVETECH\s*\(Pty\)\s*Ltd\b", line, re.IGNORECASE):
            return "EVETECH (Pty) Ltd"

    for line in lines[:15]:
        if re.search(r"\bEVETECH\b", line, re.IGNORECASE):
            return "EVETECH (Pty) Ltd"

    return None


def extract_supplier_from_phone_header(lines: list[str]) -> Optional[str]:
    """
    Small service invoices often print a business logo/name block followed by a
    mobile number, with no explicit "Supplier" label.
    """

    for index, line in enumerate(lines[:35]):
        if not line_has_phone_number(line):
            continue
        if any(is_recipient_block_label(candidate) for candidate in lines[max(0, index - 8):index + 1]):
            continue

        candidate_lines: list[str] = []
        for candidate in reversed(lines[max(0, index - 5):index]):
            cleaned = _clean_receipt_supplier_candidate(candidate)
            lower = cleaned.lower().strip()
            if not cleaned:
                continue
            if is_metadata_supplier_candidate(cleaned) or is_date_like_supplier_candidate(cleaned):
                continue
            if (
                is_recipient_block_label(cleaned)
                or looks_like_address_value(cleaned)  # stop scanning — delivery/street address
                or re.search(r"\b(customer|client|bill to|invoice to|deliver to|delivery to|ship to|banking details|cash bank|paid)\b", lower)
            ):
                break
            if is_valid_supplier_candidate(cleaned):
                candidate_lines.insert(0, cleaned)
            if len(candidate_lines) >= 3:
                break

        if not candidate_lines:
            continue

        business_lines = [
            candidate
            for candidate in candidate_lines
            if looks_like_legal_entity(candidate)
            or candidate.isupper()
            or re.search(r"\b(plumbers?|plumbing|electric|electrical|repairs?|services?|construction|trading)\b", candidate, re.IGNORECASE)
        ]
        chosen = business_lines or candidate_lines
        combined = " ".join(chosen[-3:])
        combined = re.sub(r"\s+", " ", combined).strip(" -:,.")
        if is_valid_supplier_candidate(combined) and not looks_like_location_cluster(combined):
            return combined

    return None


def extract_supplier_from_from_to_block(lines: list[str]) -> Optional[str]:
    """
    Handles two-column invoice headers where text may be:
    FROM
    TO
    Supplier Name
    Customer Name
    """

    for index, line in enumerate(lines[:80]):
        normalised = line.strip().lower()

        if is_recipient_block_label(line):
            continue

        if normalised == "from":
            next_lines = [candidate.strip() for candidate in lines[index + 1:index + 12] if candidate.strip()]
        elif re.fullmatch(r"from\s+to", normalised, re.IGNORECASE):
            next_lines = [candidate.strip() for candidate in lines[index + 1:index + 12] if candidate.strip()]
        else:
            continue

        if next_lines and next_lines[0].lower() == "to":
            next_lines = next_lines[1:]

        valid_candidates = []
        for candidate in next_lines:
            if is_recipient_block_label(candidate):
                break
            if is_valid_supplier_candidate(candidate):
                valid_candidates.append(candidate)

        for candidate in valid_candidates:
            if looks_like_legal_entity(candidate):
                return candidate

        if valid_candidates:
            return valid_candidates[0]

    return None


def extract_supplier_after_label(lines: list[str]) -> Optional[str]:
    labels = {
        "from",
        "from:",
        "supplier",
        "supplier:",
        "vendor",
        "vendor:",
        "seller",
        "seller:",
    }

    for index, line in enumerate(lines[:80]):
        lower = line.lower().strip()
        if is_recipient_block_label(line):
            continue

        same_line_match = re.match(
            r"^(from|supplier|vendor|seller)\s*:\s*(.+)$",
            line,
            re.IGNORECASE,
        )

        if same_line_match:
            candidate = same_line_match.group(2).strip()
            if is_valid_supplier_candidate(candidate):
                return candidate

        if lower in labels:
            lookahead_candidates = []

            for candidate in lines[index + 1:index + 10]:
                candidate = candidate.strip()
                if is_recipient_block_label(candidate):
                    break

                if is_valid_supplier_candidate(candidate):
                    lookahead_candidates.append(candidate)

            for candidate in lookahead_candidates:
                if looks_like_legal_entity(candidate):
                    return candidate

            if lookahead_candidates:
                return lookahead_candidates[0]

    return None


def extract_supplier_from_top_header(lines: list[str]) -> Optional[str]:
    """
    Generic fallback:
    Prefer legal entities near top of document, but avoid customer/sales-rep blocks.
    """

    candidates: list[str] = []

    stop_customer_block_terms = [
        "sales rep",
        "bill to",
        "ship to",
        "deliver to",
        "delivery to",
        "customer",
        "client",
        "to",
    ]

    for index, line in enumerate(lines[:40]):
        lower = line.lower()

        # If the document enters a clear customer block, do not scan below it
        # unless no supplier was found above.
        if is_recipient_block_label(line) or any(term == lower or lower.startswith(term + ":") for term in stop_customer_block_terms):
            break
        if candidates and re.search(r"\b(tax invoice|invoice number|invoice date|date|page)\b", lower):
            break

        if not is_valid_supplier_candidate(line):
            continue

        if looks_like_legal_entity(line):
            candidates.insert(0, line)
        elif has_supplier_evidence("\n".join(lines[index:index + 5])):
            candidates.append(line)
        else:
            candidates.append(line)

    return candidates[0] if candidates else None


def extract_supplier_name(text: str, layout_type: str = "unknown") -> Optional[str]:
    lines = normalise_lines(text)

    # 1. Template-specific rules
    if layout_type == "evetech_image_invoice" or "evetech" in text.lower():
        supplier = extract_supplier_from_evetech_layout(text)
        if supplier:
            return supplier

    supplier = extract_supplier_from_receipt_text(text)
    if supplier:
        return supplier

    # 2. Phone-backed small service invoice header
    supplier = extract_supplier_from_phone_header(lines)
    if supplier:
        return supplier

    # 3. FROM / TO blocks
    supplier = extract_supplier_from_from_to_block(lines)
    if supplier:
        return supplier

    # 4. Explicit supplier/vendor labels
    supplier = extract_supplier_after_label(lines)
    if supplier:
        return supplier

    # 5. Top-header legal entity fallback
    supplier = extract_supplier_from_top_header(lines)
    if supplier:
        return supplier

    return None
