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


def _round_money(value: float | int | None) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 2)


def _discount_column_mode(text: str) -> Optional[str]:
    lower = (text or "").lower()
    if re.search(r"\b(?:disc(?:ount)?|nett?|net)\s*(?:unit\s*)?price\b", lower):
        return "discounted_unit_price"
    if re.search(r"\b(?:disc(?:ount)?)\s*%\b|\b%\s*(?:disc(?:ount)?)\b", lower):
        return "discount_percent"
    if re.search(r"\b(?:disc(?:ount)?|less)\b", lower):
        return "discount_amount"
    return None


def _with_discount_pricing(item: dict, *, mode: Optional[str] = None) -> dict:
    quantity = clean_amount(item.get("quantity"))
    unit_price = clean_amount(item.get("unit_price"))
    line_total = clean_amount(item.get("line_total"))
    discount_amount = clean_amount(item.get("discount_amount") or item.get("discount"))
    discount_percent = clean_amount(item.get("discount_percent"))
    discounted_unit_price = clean_amount(item.get("discounted_unit_price"))

    if quantity is None or quantity <= 0:
        return item

    gross_total = _round_money(quantity * unit_price) if unit_price is not None else None

    if discounted_unit_price is not None:
        discount_amount = (
            _round_money((unit_price - discounted_unit_price) * quantity)
            if unit_price is not None
            else discount_amount
        )
        if line_total is None:
            line_total = _round_money(quantity * discounted_unit_price)
        item["pricing_basis"] = item.get("pricing_basis") or "discounted_unit_price"
    elif discount_percent is not None and unit_price is not None:
        discount_amount = _round_money(quantity * unit_price * (discount_percent / 100))
        if line_total is None:
            line_total = _round_money((quantity * unit_price) - (discount_amount or 0))
        item["pricing_basis"] = item.get("pricing_basis") or "discount_percent"
    elif discount_amount is not None and unit_price is not None:
        if line_total is None:
            line_total = _round_money((quantity * unit_price) - discount_amount)
        item["pricing_basis"] = item.get("pricing_basis") or "discount_amount"
    elif gross_total is not None and line_total is not None and abs(gross_total - line_total) > 0.02:
        discount_amount = _round_money(gross_total - line_total)
        if 0 < (discount_amount or 0) < gross_total:
            discounted_unit_price = _round_money(line_total / quantity)
            item["pricing_basis"] = item.get("pricing_basis") or "extended_price_inferred_discount"

    if discount_amount is not None and discount_amount > 0:
        item["discount_amount"] = discount_amount
    if discount_percent is not None and discount_percent > 0:
        item["discount_percent"] = discount_percent
    if discounted_unit_price is not None and discounted_unit_price > 0:
        item["discounted_unit_price"] = discounted_unit_price
    if line_total is not None:
        item["line_total"] = line_total
    if mode:
        item["pricing_notes"] = {
            **(item.get("pricing_notes") or {}),
            "discount_column_mode": mode,
        }

    return item

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
    discount_mode = _discount_column_mode("\n".join(lines))
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
                "line_total": numeric_values[3],
                "raw_line": " | ".join([code, description] + raw_numeric_lines[:4]),
            }
            if discount_mode == "discounted_unit_price":
                item["discounted_unit_price"] = numeric_values[2]
                item["pricing_basis"] = "discounted_unit_price"
            elif discount_mode == "discount_percent":
                item["discount_percent"] = numeric_values[2]
                item["pricing_basis"] = "discount_percent"
            elif discount_mode == "discount_amount":
                item["discount_amount"] = numeric_values[2]
                item["pricing_basis"] = "discount_amount"
            else:
                item["tax_amount"] = numeric_values[2]

            items.append(_with_discount_pricing(item, mode=discount_mode))

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
    discount_mode = _discount_column_mode("\n".join(lines))
    amount_pattern = r"(?:\d{1,3}(?:[\s,]\d{3})+|\d{1,6})[,.]\d{2}"

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

        item = {
            "code": None,
            "description": description,
            "quantity": numeric_values[0],
            "unit_price": numeric_values[1] if len(numeric_values) >= 2 else None,
            "line_total": numeric_values[-1],
            "raw_line": line,
        }
        if len(numeric_values) >= 4 and discount_mode == "discounted_unit_price":
            item["discounted_unit_price"] = numeric_values[2]
            item["pricing_basis"] = "discounted_unit_price"
        elif len(numeric_values) >= 4 and discount_mode == "discount_percent":
            item["discount_percent"] = numeric_values[2]
            item["pricing_basis"] = "discount_percent"
        elif len(numeric_values) >= 4 and discount_mode == "discount_amount":
            item["discount_amount"] = numeric_values[2]
            item["pricing_basis"] = "discount_amount"
        elif len(numeric_values) >= 4:
            item["tax_amount"] = numeric_values[-2]

        items.append(_with_discount_pricing(item, mode=discount_mode))

    return items


def _normalise_receipt_ocr_text(value: str) -> str:
    return (
        (value or "")
        .replace("O", "0")
        .replace("o", "0")
        .replace("I", "1")
        .replace("l", "1")
        .replace("|", "1")
    )


def _looks_like_receipt_item_description(value: str) -> bool:
    lower = value.lower().strip()
    if not lower:
        return False
    if len(re.findall(r"[a-zA-Z]", value)) < 4:
        return False
    if any(
        term in lower
        for term in [
            "itemname",
            "item name",
            "description",
            "subtotal",
            "sub total",
            "vat",
            "total",
            "card",
            "cash",
            "payment",
            "receipt",
            "cashier",
            "till",
            "refund",
            "exchange",
            "terms",
            "condition",
            "welcome",
            "www.",
        ]
    ):
        return False
    return True


def _receipt_table_bounds(lines: list[str]) -> tuple[int, int]:
    start = 0
    end = len(lines)

    for index, line in enumerate(lines):
        lower = line.lower()
        if re.search(r"item\s*(?:name|mame|nane|nan[e3])", lower):
            start = index + 1
            break
        if re.search(r"\b[qo]ty\b", lower) and "price" in lower and "total" in lower:
            start = index + 1
            break

    for index in range(start, len(lines)):
        lower = lines[index].lower().strip(" :-")
        if re.match(r"^(total|subtotal|sub total|vat|card|cash|payment|tender|items?\s+rounding)\b", lower):
            end = index
            break

    return start, end


def _extract_receipt_amounts(value: str) -> list[float]:
    value = _normalise_receipt_ocr_text(value)
    matches = re.findall(r"\b\d{1,5}[,.]\d{2}\b", value)
    amounts = [clean_amount(match) for match in matches]
    return [amount for amount in amounts if amount is not None]


def _description_before_first_amount(value: str) -> str:
    match = re.search(r"\b\d{1,5}[,.]\d{2}\b", _normalise_receipt_ocr_text(value))
    head = value[:match.start()] if match else value
    head = re.sub(r"\b\d{6,}\b", "", head)
    head = re.sub(r"\s+", " ", head)
    return head.strip(" :-")


def extract_narrow_receipt_line_items(text: str) -> list[dict]:
    """
    Handles photographed till slips with ITEMNAME/QTY/PRICE/TOTAL style rows.

    OCR often splits each receipt row into a description line plus a following
    barcode/quantity/amount line, so this parser examines the current line
    together with a short lookahead window.
    """
    lines = normalise_lines(text)
    if not lines:
        return []

    lower_text = text.lower()
    has_receipt_shape = (
        re.search(r"item\s*(?:name|mame|nane|nan[e3])", lower_text) is not None
        or (re.search(r"\b[qo]ty\b", lower_text) and ("price" in lower_text or "total" in lower_text))
        or ("receipt no" in lower_text and "cashier" in lower_text)
    )
    if not has_receipt_shape:
        return []

    start, end = _receipt_table_bounds(lines)
    item_lines = lines[start:end]
    items: list[dict] = []
    index = 0

    while index < len(item_lines):
        line = item_lines[index].strip()
        if not _looks_like_receipt_item_description(line):
            index += 1
            continue

        window_lines = [line]
        for candidate in item_lines[index + 1:index + 4]:
            candidate_amounts = _extract_receipt_amounts(candidate)
            current_amounts = _extract_receipt_amounts(" ".join(window_lines))
            if current_amounts and not candidate_amounts and _looks_like_receipt_item_description(candidate):
                break
            window_lines.append(candidate)
            if len(_extract_receipt_amounts(" ".join(window_lines))) >= 2:
                break
        combined = " ".join(window_lines)
        amounts = _extract_receipt_amounts(combined)

        if len(amounts) < 2:
            index += 1
            continue

        description = _description_before_first_amount(line)
        if not description or len(description) < 4:
            description = _description_before_first_amount(combined)

        if not _looks_like_receipt_item_description(description):
            index += 1
            continue

        quantity = amounts[0]
        line_total = amounts[-1]
        unit_price = amounts[-2] if len(amounts) >= 3 else None
        if unit_price is None and quantity and quantity > 0:
            unit_price = round(line_total / quantity, 2)

        items.append(_with_discount_pricing({
            "code": None,
            "description": description,
            "quantity": quantity,
            "unit_price": unit_price,
            "tax_amount": None,
            "line_total": line_total,
            "raw_line": combined,
        }))

        index += max(1, len(window_lines))

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
            items.append(_with_discount_pricing({
                "code": code,
                "description": description,
                "quantity": numeric_candidates[0],
                "unit_price": numeric_candidates[1],
                "tax_amount": numeric_candidates[2],
                "line_total": numeric_candidates[3],
                "raw_line": " | ".join(
                    [code, description] + raw_numeric_candidates[:4]
                ),
            }))

            index += 6
            continue

        index += 1

    return items

_CODE_DESC_QTY_TOTAL_RE = re.compile(
    r"^(\d{1,6})\s+([A-Za-z].+?)\s+(\d{1,4})\s+(\d{1,5}[.,]\d{2})\s*$"
)


def extract_code_qty_total_rows(text: str) -> list[dict]:
    """
    Handles structured receipts/invoices with CODE | DESCRIPTION | QTY | TOTAL rows.
    Example: "75 B/W COPIES 3 6.00"
    """
    items = []
    for line in normalise_lines(text):
        m = _CODE_DESC_QTY_TOTAL_RE.match(line.strip())
        if not m:
            continue
        code, description, qty_str, total_str = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
        total = clean_amount(total_str)
        qty = int(qty_str)
        if total is None or qty <= 0:
            continue
        unit_price = round(total / qty, 4) if qty else None
        items.append({
            "code": code,
            "description": description,
            "quantity": float(qty),
            "unit_price": unit_price,
            "tax_amount": None,
            "line_total": total,
            "raw_line": line,
        })
    return items


def extract_line_items(text: str, layout_type: str = "unknown") -> list[dict]:
    lines = normalise_lines(text)

    receipt_items = extract_narrow_receipt_line_items(text)
    if receipt_items:
        return receipt_items

    code_qty_items = extract_code_qty_total_rows(text)
    if code_qty_items:
        return code_qty_items

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
