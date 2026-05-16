from __future__ import annotations

import re
from typing import Optional


def normalise_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def clean_amount(value: str) -> Optional[float]:
    if value is None:
        return None

    try:
        cleaned = (
            str(value)
            .replace("R", "")
            .replace("ZAR", "")
            .replace("£", "")
            .replace("GBP", "")
            .replace("$", "")
            .replace("USD", "")
            .replace("€", "")
            .replace("EUR", "")
            .replace(" ", "")
            .strip()
        )

        # Handles South African format: 2 890,96 or 2890,96
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")

        # Handles normal thousands comma: 2,890.96
        elif "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")

        return float(cleaned)

    except Exception:
        return None

def extract_total_amount(text: str) -> Optional[float]:
    stacked = extract_stacked_totals(text)

    if stacked.get("total_amount") is not None:
        return stacked["total_amount"]

    receipt_total = extract_receipt_total_amount(text)
    if receipt_total is not None:
        return receipt_total

    lines = normalise_lines(text)

    priority_labels = [
        "amount due",
        "balance due",
        "grand total",
        "invoice total",
        "total due",
        "total amount",
    ]

    for label in priority_labels:
        pattern = rf"{label}\s*[:#\-]?\s*(?:ZAR|R|GBP|£|USD|\$|EUR|€)?\s*([0-9][0-9\s,]*[,.][0-9]{{2}})"
        matches = re.findall(pattern, text, re.IGNORECASE)

        if matches:
            amount = clean_amount(matches[-1])
            if amount is not None:
                return amount

    for index, line in enumerate(lines):
        lower = line.lower().strip()

        if lower in priority_labels:
            for candidate in lines[index + 1:index + 6]:
                amount = clean_amount(candidate)

                if amount is not None and amount > 0:
                    return amount

    return None


def _clean_amount_from_parts(parts: list[str]) -> Optional[float]:
    joined = "".join(str(part).strip() for part in parts if str(part).strip())
    return clean_amount(joined)


def extract_receipt_total_amount(text: str) -> Optional[float]:
    """
    Handles receipt OCR where TOTAL and amount pieces appear on separate lines:

    TOTAL
    695
    ,00
    """
    lines = normalise_lines(text)
    lower_lines = [line.lower().strip() for line in lines]
    total_candidates: list[float] = []

    for index, lower in enumerate(lower_lines):
        if lower not in {"total", "amount due", "total amount"}:
            continue

        window = lines[index + 1:index + 8]

        for candidate in window:
            amount = clean_amount(candidate)
            if amount is not None and amount > 0:
                total_candidates.append(amount)
                break

        for offset in range(0, max(0, len(window) - 1)):
            amount = _clean_amount_from_parts(window[offset:offset + 2])
            if amount is not None and amount > 0:
                total_candidates.append(amount)
                break

        for offset in range(0, max(0, len(window) - 2)):
            amount = _clean_amount_from_parts(window[offset:offset + 3])
            if amount is not None and amount > 0:
                total_candidates.append(amount)
                break

    if total_candidates:
        return total_candidates[-1]

    pattern = r"\bTOTAL\b\s*(?:ZAR|R)?\s*([0-9]{1,6})\s*(?:[,.]\s*|\n\s*[,.]\s*)?([0-9]{2})\b"
    matches = re.findall(pattern, text, re.IGNORECASE)
    if matches:
        amount = clean_amount(f"{matches[-1][0]},{matches[-1][1]}")
        if amount is not None and amount > 0:
            return amount

    return None

def extract_tax_amount(text: str) -> float:
    stacked = extract_stacked_totals(text)

    if stacked.get("tax_amount") is not None:
        return stacked["tax_amount"]

    patterns = [
        r"(?:VAT\s*Amount|Tax\s*Amount|VAT|Tax)\s*[:#\-]?\s*(?:ZAR|R|GBP|£|USD|\$|EUR|€)?\s*([0-9][0-9\s,]*[,.][0-9]{2})",
        r"(?:ZAR|R|GBP|£|USD|\$|EUR|€)\s*([0-9][0-9\s,]*[,.][0-9]{2})\s*(?:VAT|Tax)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            amount = clean_amount(match.group(1))
            return amount if amount is not None else 0.0

    return 0.0


def extract_subtotal(text: str, total_amount: Optional[float], tax_amount: float) -> Optional[float]:
    stacked = extract_stacked_totals(text)

    if stacked.get("subtotal") is not None:
        return stacked["subtotal"]

    patterns = [
        r"(?:Subtotal|Sub\s*Total|Amount\s*Excl|Net\s*Amount|Net\s*Total)\s*[:#\-]?\s*(?:ZAR|R|GBP|£|USD|\$|EUR|€)?\s*([0-9][0-9\s,]*[,.][0-9]{2})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return clean_amount(match.group(1))

    if total_amount is not None and total_amount > 0 and tax_amount is not None:
        return round(total_amount - tax_amount, 2)

    return None


def infer_total_if_missing_or_zero(
    total_amount: Optional[float],
    subtotal: Optional[float],
    tax_amount: Optional[float],
) -> Optional[float]:
    if (total_amount is None or total_amount == 0) and subtotal is not None and tax_amount is not None:
        calculated_total = round(subtotal + tax_amount, 2)
        if calculated_total > 0:
            return calculated_total

    return total_amount

def extract_stacked_totals(text: str) -> dict:
    """
    Handles PDFs where total values and labels are separated by text order.

    Example from Chimes:
    38,750.00
    38,750.00
    5,425.00
    44,175.00
    Date
    Page
    Document No
    ...
    Sub Total
    Amount Excl
    VAT
    Total
    """

    lines = normalise_lines(text)

    labels = ["sub total", "amount excl", "vat", "total"]

    for index in range(len(lines)):
        window = [line.lower().strip() for line in lines[index:index + 4]]

        if window != labels:
            continue

        # Case 1: labels first, values after
        after_lines = lines[index + 4:index + 14]
        after_amounts = [clean_amount(line) for line in after_lines]
        after_amounts = [amount for amount in after_amounts if amount is not None]

        if len(after_amounts) >= 4:
            return {
                "subtotal": after_amounts[0],
                "amount_excl": after_amounts[1],
                "tax_amount": after_amounts[2],
                "total_amount": after_amounts[3],
            }

        # Case 2: values appear before labels because of PDF text order
        before_lines = lines[max(0, index - 20):index]
        before_amounts = [clean_amount(line) for line in before_lines]
        before_amounts = [amount for amount in before_amounts if amount is not None]

        if len(before_amounts) >= 4:
            last_four = before_amounts[-4:]

            return {
                "subtotal": last_four[0],
                "amount_excl": last_four[1],
                "tax_amount": last_four[2],
                "total_amount": last_four[3],
            }

    return {
        "subtotal": None,
        "amount_excl": None,
        "tax_amount": None,
        "total_amount": None,
    }

def extract_chimes_totals(text: str) -> dict:
    """
    Chimes Crane Hire invoices often extract the totals as:

    38,750.00
    38,750.00
    5,425.00
    44,175.00
    ...
    Sub Total
    Amount Excl
    VAT
    Total
    """

    lines = normalise_lines(text)
    lower_lines = [line.lower().strip() for line in lines]

    # Find the label block
    for index in range(len(lower_lines) - 3):
        if (
            lower_lines[index] == "sub total"
            and lower_lines[index + 1] == "amount excl"
            and lower_lines[index + 2] == "vat"
            and lower_lines[index + 3] == "total"
        ):
            # Look backwards for the four totals immediately before the label section.
            previous_lines = lines[max(0, index - 60):index]

            amounts = []
            for line in previous_lines:
                amount = clean_amount(line)
                if amount is not None:
                    amounts.append(amount)

            if len(amounts) >= 4:
                subtotal, amount_excl, vat_amount, total_amount = amounts[-4:]

                return {
                    "subtotal": subtotal,
                    "amount_excl": amount_excl,
                    "tax_amount": vat_amount,
                    "total_amount": total_amount,
                }

    return {
        "subtotal": None,
        "amount_excl": None,
        "tax_amount": None,
        "total_amount": None,
    }
