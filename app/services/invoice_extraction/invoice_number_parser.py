from __future__ import annotations

import re
from typing import Optional


def normalise_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def clean_invoice_number(value: str) -> str:
    value = value.strip()
    value = re.sub(r"\s+", "", value)
    value = value.replace(":", "").replace("#", "")
    return value


def is_valid_invoice_number(value: str) -> bool:
    value = clean_invoice_number(value)

    if not value:
        return False

    invalid_values = {
        "invoice",
        "number",
        "invoicenumber",
        "invoiceno",
        "date",
        "reference",
        "taxinvoice",
        "www",
    }

    if value.lower() in invalid_values:
        return False

    if value.lower().startswith("www"):
        return False

    # Must contain at least one digit.
    if not re.search(r"\d", value):
        return False

    # Avoid phone/VAT/company registration style numbers.
    if re.fullmatch(r"\d{8,15}", value):
        return False

    # Common invoice formats:
    # INV0007767, IN2549, INA14484, 0045417, INV-0101
    if re.fullmatch(r"[A-Z]{0,5}[-/]?\d{2,}[A-Z0-9\-\/]*", value, re.IGNORECASE):
        return True

    return False


def extract_invoice_number(text: str) -> Optional[str]:
    """
    Extract invoice number safely.

    Handles:
    Invoice:
    IN2549

    Invoice Number:
    INA14484

    Invoice No: INV0007767
    """

    lines = normalise_lines(text)

    invoice_labels = {
        "invoice",
        "invoice:",
        "invoice number",
        "invoice number:",
        "invoice no",
        "invoice no:",
        "invoice #",
        "invoice #:",
        "tax invoice no",
        "tax invoice no:",
        "tax invoice number",
        "tax invoice number:",
    }

    # Label on one line, value on next line
    for index, line in enumerate(lines):
        lower = line.lower().strip()

        if lower in invoice_labels:
            for candidate in lines[index + 1:index + 5]:
                candidate_clean = clean_invoice_number(candidate)

                if is_valid_invoice_number(candidate_clean):
                    return candidate_clean

    # Same-line labels
    label_patterns = [
        r"(?:Invoice\s*(?:Number|No\.?|#)?|Tax\s*Invoice\s*(?:Number|No\.?|#)?)\s*[:#\-]\s*([A-Z0-9\-\/]{2,40})",
        r"\b(INV[-\s]?\d{2,}[A-Z0-9\-\/]*)\b",
        r"\b(INA\d{2,}[A-Z0-9\-\/]*)\b",
        r"\b(IN\d{2,}[A-Z0-9\-\/]*)\b",
    ]

    for pattern in label_patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if not match:
            continue

        candidate = clean_invoice_number(match.group(1))

        if is_valid_invoice_number(candidate):
            return candidate

    return None