from __future__ import annotations

import re
from typing import Optional

from app.services.invoice_extraction.extraction_rules import (
    address_contains_metadata,
    extract_vat_candidates,
    is_address_stop_line,
    is_recipient_block_label,
    is_valid_supplier_address_line,
)


def normalise_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def extract_first_match(text: str, patterns: list[str]) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip() if match.lastindex else match.group(0).strip()

    return None


def extract_all_emails(text: str) -> list[str]:
    emails = re.findall(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        text,
        re.IGNORECASE,
    )

    seen = set()
    result = []

    for email in emails:
        lowered = email.lower()
        if lowered not in seen:
            seen.add(lowered)
            result.append(email)

    return result


def extract_supplier_email(text: str) -> Optional[str]:
    emails = extract_all_emails(text)
    return emails[0] if emails else None


def extract_supplier_accounting_email(text: str) -> Optional[str]:
    """
    Prefer labelled accounting/accounts email if available.
    Fallback to the first supplier email.
    """

    patterns = [
        r"(?:Accounts\s*Email|Account\s*Email|Accounts|Remittance|Remit To)\s*[:#\-]?\s*([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
    ]

    labelled = extract_first_match(text, patterns)

    if labelled:
        return labelled

    return extract_supplier_email(text)


def extract_supplier_website(text: str) -> Optional[str]:
    patterns = [
        r"(?:Website|Web)\s*[:#\-]?\s*((?:https?://)?(?:www\.)?[A-Z0-9.-]+\.[A-Z]{2,})",
        r"((?:https?://)?www\.[A-Z0-9.-]+\.[A-Z]{2,})",
    ]

    return extract_first_match(text, patterns)


def extract_supplier_telephone(text: str) -> Optional[str]:
    patterns = [
        r"(?:Tel|Telephone|Phone)\s*[:#\-]?\s*([+0-9 ()\-]{7,25})",
        r"\b(0\d{2}\s*\d{3}\s*\d{4})\b",
    ]

    return extract_first_match(text, patterns)


def extract_supplier_fax(text: str) -> Optional[str]:
    patterns = [
        r"(?:Fax)\s*[:#\-]?\s*([+0-9 ()\-]{7,25})",
    ]

    return extract_first_match(text, patterns)


def extract_supplier_cell(text: str) -> Optional[str]:
    patterns = [
        r"(?:Cell|Mobile|Mob)\s*[:#\-]?\s*([+0-9 ()\-]{7,25})",
    ]

    return extract_first_match(text, patterns)


def extract_vat_number(text: str) -> Optional[str]:
    candidates = extract_vat_candidates(normalise_lines(text))
    if not candidates:
        return None

    best = candidates[0]
    if best.score < 0:
        return None
    return best.value


def extract_customer_code(text: str) -> Optional[str]:
    # High-specificity labels — unambiguously a customer/account code
    high_priority = [
        r"(?:Customer\s+Code|Customer\s+No\.?|Customer\s+Number)\s*[:#\-]?\s*([A-Z0-9\-\/]{1,30})",
    ]
    result = extract_first_match(text, high_priority)
    if result:
        return result

    # "Account No." / "Account Number" — lower specificity.
    # Only match when banking keywords are NOT present in the preceding ~300 chars,
    # so we don't accidentally capture a bank account number as a customer code.
    _BANKING_CTX = re.compile(
        r"\b(?:bank|fnb|absa|nedbank|standard\s+bank|capitec|investec|branch\s+code|swift|iban|bic)\b",
        re.IGNORECASE,
    )
    _ACCT_PATTERN = re.compile(
        r"(?:Account\s+No\.?|Account\s+Number)\s*[:#\-]?\s*([A-Z0-9\-\/]{1,30})",
        re.IGNORECASE | re.MULTILINE,
    )
    for match in _ACCT_PATTERN.finditer(text):
        context_before = text[max(0, match.start() - 300): match.start()]
        if not _BANKING_CTX.search(context_before):
            return match.group(1).strip()

    return None


def extract_company_registration_number(text: str) -> Optional[str]:
    patterns = [
        r"(?:Reg\s+Number|Registration\s+Number|Company\s+Registration|Company\s+Reg\.?|CK)\s*[:#\-]?\s*([0-9]{4}/[0-9]{6}/[0-9]{2})",
        r"\b([0-9]{4}/[0-9]{6}/[0-9]{2})\b",
        r"\b([0-9]{2}/[0-9]{6}/[0-9]{2})\b",
        r"\b(IT\s?[0-9]{2,6}/[0-9]{2,4})\b",
    ]

    return extract_first_match(text, patterns)


def extract_supplier_delivery_address(text: str) -> Optional[str]:
    """
    Extract the supplier's physical/delivery address from the top header area.

    Example:
    Chimes Crane Hire (Pty) Ltd
    Nasmith Ave
    Jupiter Industrial
    Germiston
    P O Box 40578
    Cleveland
    2022

    Delivery/physical address should become:
    Nasmith Ave
    Jupiter Industrial
    Germiston
    """

    lines = normalise_lines(text)

    if len(lines) < 2:
        return None

    top_text = re.sub(r"\s+", " ", "\n".join(lines[:20]).lower())
    receipt_text = re.sub(r"\s+", " ", "\n".join(lines[:80]).lower())
    if (
        "scan to rate" in top_text
        or ("survey" in top_text and ("tax invoice" in receipt_text or "receipt" in receipt_text))
    ):
        return None

    start_index = 1
    for index, line in enumerate(lines[:12]):
        if re.search(r"\b(build\s*it|builders|pinetown)\b", line, re.IGNORECASE):
            start_index = index + 1
            break

    address_lines: list[str] = []

    for line in lines[start_index:start_index + 8]:
        if is_address_stop_line(line) or is_recipient_block_label(line):
            break

        if not is_valid_supplier_address_line(line):
            continue

        address_lines.append(line)

    address = "\n".join(address_lines).strip()
    if not address or address_contains_metadata(address):
        return None
    return address


def extract_supplier_postal_address(text: str) -> Optional[str]:
    """
    Extract postal address block starting at PO Box / P O Box / P.O. Box.
    """

    lines = normalise_lines(text)

    start_index = None

    for index, line in enumerate(lines[:30]):
        lower = line.lower().replace(".", "")

        if (
            "po box" in lower
            or "p o box" in lower
            # Only match "PostNet" when used as a mailbox service (e.g. "PostNet Suite 123"),
            # not when "PostNet" is the company/supplier name on the document header.
            or re.search(r"\bpostnet\s+(?:suite|box|mailbox)\b", lower)
            or "private bag" in lower
        ):
            start_index = index
            break

    if start_index is None:
        return None

    stop_terms = [
        "tel:",
        "telephone:",
        "fax:",
        "e-mail:",
        "email:",
        "website:",
        "tax registration:",
        "reg number:",
        "tax invoice",
        "vat:",
        "computer generated",
    ]

    postal_lines: list[str] = []

    for line in lines[start_index:start_index + 8]:
        lower = line.lower()

        if any(term in lower for term in stop_terms):
            break

        if "@" in line or "www." in lower:
            break

        if len(line) <= 90:
            postal_lines.append(line)

    return "\n".join(postal_lines).strip() if postal_lines else None


# Backwards-compatible names, if old code still imports these.
def extract_email(text: str) -> Optional[str]:
    return extract_supplier_email(text)


def extract_website(text: str) -> Optional[str]:
    return extract_supplier_website(text)


def extract_telephone(text: str) -> Optional[str]:
    return extract_supplier_telephone(text)


def extract_fax(text: str) -> Optional[str]:
    return extract_supplier_fax(text)


def extract_tax_registration_number(text: str) -> Optional[str]:
    return extract_vat_number(text)


def extract_supplier_address(text: str) -> Optional[str]:
    return extract_supplier_delivery_address(text)
