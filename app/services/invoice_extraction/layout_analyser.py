from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InvoiceLayout:
    layout_type: str
    confidence: float
    reasons: list[str]


def normalise_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def analyse_invoice_layout(text: str) -> InvoiceLayout:
    """
    Detect the likely invoice text layout.

    layout_type options:
    - chimes_column_table
    - row_table
    - vertical_column_table
    - receipt
    - unknown
    """

    lines = normalise_lines(text)
    lower_text = text.lower()
    reasons: list[str] = []

    # Supplier-specific/template-specific detection
    if "chimes crane hire" in lower_text and "nett price" in lower_text:
        reasons.append("Detected Chimes Crane Hire template with Nett Price table.")
        return InvoiceLayout(
            layout_type="chimes_column_table",
            confidence=0.95,
            reasons=reasons,
        )

    if "evetech" in lower_text and "tax invoice" in lower_text:
        return InvoiceLayout(
            layout_type="evetech_image_invoice",
            confidence=0.90,
            reasons=["Detected Evetech Tax Invoice layout."]
        )

    # Generic row table detection
    row_table_terms = [
        "description",
        "quantity",
        "unit price",
        "tax",
        "nett price",
    ]

    if all(term in lower_text for term in row_table_terms):
        reasons.append("Detected table headers: Description, Quantity, Unit Price, Tax, Nett Price.")

        # If many lines contain multiple amounts, it is row style.
        multi_amount_lines = 0
        for line in lines:
            amount_count = sum(char.isdigit() for char in line)
            if amount_count >= 8 and "," in line:
                multi_amount_lines += 1

        if multi_amount_lines >= 3:
            reasons.append("Multiple table rows appear to contain amounts on the same line.")
            return InvoiceLayout(
                layout_type="row_table",
                confidence=0.80,
                reasons=reasons,
            )

        reasons.append("Headers found, but values appear split over multiple lines.")
        return InvoiceLayout(
            layout_type="vertical_column_table",
            confidence=0.75,
            reasons=reasons,
        )

    # Receipt-style detection
    receipt_terms = [
        "cashier",
        "subtotal",
        "all cards",
        "till",
        "tax invoice",
    ]

    receipt_hits = sum(1 for term in receipt_terms if term in lower_text)

    if receipt_hits >= 3:
        reasons.append("Detected receipt-like document terms.")
        return InvoiceLayout(
            layout_type="receipt",
            confidence=0.70,
            reasons=reasons,
        )

    reasons.append("No known layout confidently detected.")
    return InvoiceLayout(
        layout_type="unknown",
        confidence=0.30,
        reasons=reasons,
    )