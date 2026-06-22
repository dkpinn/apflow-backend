from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

_MONEY = Decimal("0.01")
MONEY_ZERO = Decimal("0.00")


def money(value: Any) -> Decimal:
    """Coerce any DB/API value to a 2dp Decimal. Returns 0.00 for None, empty, or unparseable input."""
    if value in (None, ""):
        return MONEY_ZERO
    try:
        return Decimal(str(value)).quantize(_MONEY, rounding=ROUND_HALF_UP)
    except Exception:
        return MONEY_ZERO


def numeric_amount(value: Any) -> Optional[float]:
    """Parse a loosely-typed amount string (handles R/ZAR symbols, comma-as-decimal) to float.
    Returns None for None, empty, or unparseable input."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    clean = str(value).strip()
    if not clean:
        return None
    clean = clean.replace("R", "").replace("ZAR", "").replace(" ", "")
    if "," in clean and "." not in clean:
        clean = clean.replace(",", ".")
    else:
        clean = clean.replace(",", "")
    try:
        return round(float(clean), 2)
    except (ValueError, TypeError):
        return None
