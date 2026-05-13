from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional


def slugify(value: Optional[str], fallback: str = "unknown") -> str:
    if not value:
        return fallback

    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")

    return value or fallback


def format_amount(value: Optional[float]) -> str:
    if value is None:
        return "no-amount"

    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
        return str(amount).replace(".", "-")
    except (InvalidOperation, ValueError):
        return "no-amount"


def build_invoice_storage_filename(
    original_filename: str,
    supplier_name: Optional[str],
    invoice_number: Optional[str],
    invoice_date: Optional[str],
    total_amount: Optional[float],
    invoice_raw_id: str,
) -> str:
    extension = Path(original_filename).suffix.lower() or ".pdf"

    supplier_slug = slugify(supplier_name, "unknown-supplier")
    invoice_slug = slugify(invoice_number, f"raw-{invoice_raw_id[:8]}")
    date_slug = invoice_date or "no-date"
    amount_slug = format_amount(total_amount)

    return f"{supplier_slug}_{invoice_slug}_{date_slug}_{amount_slug}{extension}"