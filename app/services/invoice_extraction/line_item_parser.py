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

        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        elif "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")

        return float(cleaned)
    except Exception:
        return None

def is_amount(value: str) -> bool:
    return clean_amount(value) is not None and bool(
        re.fullmatch(r"(?:[A-Z]{0,3}\s*)?[0-9][0-9,\s]*[,.][0-9]{2}", value.strip())
    )

def is_item_code(value: str) -> bool:
    value = value.strip()

    if not value:
        return False

    if is_amount(value):
        return False

    if len(value) > 40:
        return False

    # Common item-code style: CRANE, INSURANCE, SITE, MAN CAGE
    return bool(re.search(r"[A-Z]", value)) and not value.lower().startswith(
        (
            "sub total",
            "amount excl",
            "vat",
            "total",
            "bank details",
            "please note",
            "date",
            "page",
            "document no",
            "customer order",
            "tax exempt",
            "tax reference",
            "sales code",
            "delivery note",
            "tax registration",
            "code",
            "description",
            "quantity",
            "unit",
            "unit price",
            "disc",
            "nett price",
        )
    )

def extract_line_items_from_vertical_block(lines: list[str]) -> list[dict]:
    """
    Handles PDF text extraction where each column appears on a separate line.

    Example:
    CRANE
    100 TON CRANE
    10.00
    1,950.00
    2,730.00
    19,500.00
    """

    items: list[dict] = []
    index = 0

    while index < len(lines):
        code = lines[index].strip()

        if not is_item_code(code):
            index += 1
            continue

        # Need enough following lines for description + numeric fields
        lookahead = lines[index + 1:index + 8]

        if len(lookahead) < 4:
            index += 1
            continue

        description = lookahead[0].strip()

        if not description or is_amount(description):
            index += 1
            continue

        numeric_values: list[float] = []
        raw_numeric_lines: list[str] = []

        for candidate in lookahead[1:]:
            if is_amount(candidate):
                amount = clean_amount(candidate)
                if amount is not None:
                    numeric_values.append(amount)
                    raw_numeric_lines.append(candidate)

            if len(numeric_values) >= 4:
                break

        # Expected fields:
        # quantity, unit_price, tax_amount, line_total
        if len(numeric_values) >= 4:
            item = {
                "code": code,
                "description": f"{code} {description}".strip(),
                "quantity": numeric_values[0],
                "unit_price": numeric_values[1],
                "tax_amount": numeric_values[2],
                "line_total": numeric_values[3],
                "raw_line": " | ".join([code, description] + raw_numeric_lines[:4]),
            }

            items.append(item)

            # Move past this parsed block
            index += 2 + len(raw_numeric_lines[:4])
            continue

        index += 1

    return items

def extract_line_items_from_single_rows(lines: list[str]) -> list[dict]:
    """
    Handles rows where all item values are on one line.
    """

    items: list[dict] = []
    amount_pattern = r"[0-9][0-9,\s]*[,.][0-9]{2}"

    for line in lines:
        lower = line.lower()

        if any(
            term in lower
            for term in [
                "sub total",
                "amount excl",
                "vat",
                "total",
                "bank details",
                "please note",
            ]
        ):
            continue

        amounts = re.findall(amount_pattern, line)

        if len(amounts) < 3:
            continue

        first_amount_pos = line.find(amounts[0])
        description = line[:first_amount_pos].strip()

        if not description or len(description) < 3:
            continue

        numeric_values = [clean_amount(amount) for amount in amounts]
        numeric_values = [value for value in numeric_values if value is not None]

        if len(numeric_values) < 3:
            continue

        items.append(
            {
                "code": None,
                "description": description,
                "quantity": numeric_values[0],
                "unit_price": numeric_values[1] if len(numeric_values) >= 2 else None,
                "tax_amount": numeric_values[-2] if len(numeric_values) >= 4 else None,
                "line_total": numeric_values[-1],
                "raw_line": line,
            }
        )

    return items

def extract_chimes_line_items(text: str) -> list[dict]:
    """
    Supplier/template-specific parser for Chimes Crane Hire invoices.

    The PDF text is extracted in vertical blocks, for example:

    CRANE
    100 TON CRANE
    10.00
    1,950.00
    2,730.00
    19,500.00

    INSURANCE
    20 % INSURANCE
    0.20
    19,500.00
    546.00
    3,900.00
    """

    lines = normalise_lines(text)
    items: list[dict] = []

    known_codes = {
        "CRANE",
        "INSURANCE",
        "SITE",
        "MAN CAGE",
    }

    index = 0

    while index < len(lines):
        code = lines[index].strip()

        if code.upper() not in known_codes:
            index += 1
            continue

        if index + 5 >= len(lines):
            break

        description = lines[index + 1].strip()

        numeric_candidates = []
        raw_numeric_candidates = []

        lookahead = lines[index + 2:index + 10]

        for candidate in lookahead:
            if is_amount(candidate):
                amount = clean_amount(candidate)

                if amount is not None:
                    numeric_candidates.append(amount)
                    raw_numeric_candidates.append(candidate)

            if len(numeric_candidates) >= 4:
                break

        if len(numeric_candidates) >= 4:
            items.append({
                "code": code,
                "description": description,
                "quantity": numeric_candidates[0],
                "unit_price": numeric_candidates[1],
                "tax_amount": numeric_candidates[2],
                "line_total": numeric_candidates[3],
                "raw_line": " | ".join(
                    [code, description] + raw_numeric_candidates[:4]
                ),
            })

            index += 6
            continue

        index += 1

    return items

def extract_line_items(text: str, layout_type: str = "unknown") -> list[dict]:
    lines = normalise_lines(text)

    if layout_type == "chimes_column_table":
        return extract_chimes_line_items(text)

    if layout_type == "row_table":
        return extract_line_items_from_single_rows(lines)

    if layout_type == "vertical_column_table":
        return extract_line_items_from_vertical_block(lines)

    # Unknown fallback: try both strategies
    row_items = extract_line_items_from_single_rows(lines)

    if row_items:
        return row_items

    return extract_line_items_from_vertical_block(lines)