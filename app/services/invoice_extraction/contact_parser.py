from __future__ import annotations

import re
from typing import Optional


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
        r"\b(0[6-8]\d\s*\d{3}\s*\d{4})\b",
    ]

    return extract_first_match(text, patterns)


def extract_vat_number(text: str) -> Optional[str]:
    patterns = [
        r"(?:Tax\s+Registration|VAT\s+Registration|VAT\s+Reg\.?|VAT\s+Reg\s+No\.?|VAT\s+No\.?|VAT\s+Number|VAT)\s*[:#\-]?\s*([0-9]{7,15})",
    ]

    return extract_first_match(text, patterns)


def extract_customer_code(text: str) -> Optional[str]:
    patterns = [
        r"(?:Customer\s+Code|Customer\s+No\.?|Customer\s+Number|Account\s+No\.?|Account\s+Number)\s*[:#\-]?\s*([A-Z0-9\-\/]{2,30})",
    ]

    return extract_first_match(text, patterns)


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

    stop_terms = [
        "p o box",
        "po box",
        "p.o box",
        "postnet",
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

    address_lines: list[str] = []

    # Assume line 0 is supplier name for now.
    for line in lines[1:15]:
        lower = line.lower()

        if any(term in lower for term in stop_terms):
            break

        if len(line) > 90:
            continue

        # Ignore obvious contact/metadata lines
        if "@" in line or "www." in lower:
            continue

        address_lines.append(line)

    return "\n".join(address_lines).strip() if address_lines else None


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
            or "postnet" in lower
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