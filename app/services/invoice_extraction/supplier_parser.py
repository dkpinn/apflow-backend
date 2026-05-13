from __future__ import annotations

import re
from typing import Optional


def normalise_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def line_has_bank_context(line: str) -> bool:
    lower = line.lower()

    bank_terms = [
        "bank",
        "branch",
        "sort code",
        "routing",
        "account number",
        "acc no",
        "iban",
        "swift",
        "bic",
        "nedbank",
        "standard bank",
        "absa",
        "fnb",
        "first national bank",
        "capitec",
        "investec",
    ]

    return any(term in lower for term in bank_terms)


def is_valid_supplier_candidate(line: str) -> bool:
    lower = line.lower().strip()

    ignored_terms = [
        "invoice",
        "tax invoice",
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
    ]

    if not line:
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


def looks_like_legal_entity(line: str) -> bool:
    return bool(
        re.search(
            r"\b(pty|ltd|limited|cc|inc|company|corporation|corp|co\.?)\b",
            line,
            re.IGNORECASE,
        )
    )


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

        if normalised == "from":
            next_lines = [candidate.strip() for candidate in lines[index + 1:index + 12] if candidate.strip()]
        elif re.fullmatch(r"from\s+to", normalised, re.IGNORECASE):
            next_lines = [candidate.strip() for candidate in lines[index + 1:index + 12] if candidate.strip()]
        else:
            continue

        if next_lines and next_lines[0].lower() == "to":
            next_lines = next_lines[1:]

        valid_candidates = [
            candidate
            for candidate in next_lines
            if is_valid_supplier_candidate(candidate)
        ]

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
        "customer",
        "client",
        "to",
    ]

    for index, line in enumerate(lines[:40]):
        lower = line.lower()

        # If the document enters a clear customer block, do not scan below it
        # unless no supplier was found above.
        if any(term == lower or lower.startswith(term + ":") for term in stop_customer_block_terms):
            break

        if not is_valid_supplier_candidate(line):
            continue

        if looks_like_legal_entity(line):
            candidates.insert(0, line)
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

    # 2. FROM / TO blocks
    supplier = extract_supplier_from_from_to_block(lines)
    if supplier:
        return supplier

    # 3. Explicit supplier/vendor labels
    supplier = extract_supplier_after_label(lines)
    if supplier:
        return supplier

    # 4. Top-header legal entity fallback
    supplier = extract_supplier_from_top_header(lines)
    if supplier:
        return supplier

    return None