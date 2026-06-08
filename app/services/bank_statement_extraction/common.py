from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Optional

from app.services.extraction_foundation import warning


MONEY_ZERO = Decimal("0.00")


def money(value: Any) -> Decimal:
    if value is None:
        return MONEY_ZERO
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    raw = str(value).strip()
    if not raw:
        return MONEY_ZERO
    neg = raw.startswith("(") and raw.endswith(")")
    cleaned = re.sub(r"[^0-9,.\-]", "", raw)
    if cleaned.count(",") and cleaned.count("."):
        cleaned = cleaned.replace(",", "")
    elif cleaned.count(",") and not cleaned.count("."):
        cleaned = cleaned.replace(",", ".")
    if neg and not cleaned.startswith("-"):
        cleaned = f"-{cleaned}"
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return MONEY_ZERO


def dec_to_float(value: Optional[Decimal]) -> Optional[float]:
    return None if value is None else float(value)


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


_DESCRIPTION_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bC\*\d+\b", re.IGNORECASE),
    re.compile(r"#\d+\b"),
    re.compile(r"\b\d{6,}\b"),
)


def clean_description(value: Any) -> str:
    """Strip transaction-reference noise (e.g. ``C*4521``, ``#8821``, stray long digit runs)
    that clutters extracted descriptions/counterparties without describing the expense."""
    text = normalize_text(value)
    for pattern in _DESCRIPTION_NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    return normalize_text(text)


def parse_date(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    raw = normalize_text(value)
    if not raw:
        return None
    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%m/%d/%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return raw if re.match(r"^\d{4}-\d{2}-\d{2}$", raw) else None


def infer_column(fieldnames: Iterable[str], candidates: Iterable[str]) -> Optional[str]:
    normalized = {
        re.sub(r"[^a-z0-9]", "", str(field).lower()): str(field)
        for field in fieldnames
        if field is not None
    }
    for candidate in candidates:
        key = re.sub(r"[^a-z0-9]", "", candidate.lower())
        if key in normalized:
            return normalized[key]
    for normalized_name, original in normalized.items():
        for candidate in candidates:
            key = re.sub(r"[^a-z0-9]", "", candidate.lower())
            if key and key in normalized_name:
                return original
    return None


def extract_bank_reference(*values: Any) -> Optional[str]:
    combined = " ".join(normalize_text(value) for value in values if normalize_text(value))
    match = re.search(r"\b\d{7,}\b", combined)
    return match.group(0) if match else None


def split_transaction_type_and_reference(prefix: str) -> tuple[str, Optional[str]]:
    text = normalize_text(prefix)
    if not text:
        return "", None
    known_refs = {
        "settlement",
        "headoffice",
        "head office",
        "internet",
        "branch",
        "pos",
        "atm",
        "nk",
    }
    lowered = text.lower()
    for reference in sorted(known_refs, key=len, reverse=True):
        if lowered.endswith(f" {reference}"):
            return normalize_text(text[: -len(reference)]), text[-len(reference):].strip()
    return text, None


def infer_signed_amount(
    *,
    transaction_type: str,
    amount: Decimal,
    previous_balance: Optional[Decimal],
    balance: Optional[Decimal],
) -> tuple[Decimal, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    if previous_balance is not None and balance is not None:
        movement = balance - previous_balance
        if abs(abs(movement) - abs(amount)) <= Decimal("0.01"):
            return movement, warnings
        warnings.append(
            warning(
                "balance_movement_mismatch",
                "Transaction amount does not match movement between adjacent balances.",
                previous_balance=float(previous_balance),
                balance=float(balance),
                amount=float(amount),
                movement=float(movement),
            )
        )

    lowered = transaction_type.lower()
    credit_terms = (" credit", " cr", "deposit", "receipt", "received", "trf cr", "immediate trf cr")
    debit_terms = ("charge", "fee", "stop order", "payment", "debit", "purchase", "withdrawal", "paid")
    if any(term in lowered for term in credit_terms):
        return abs(amount), warnings
    if any(term in lowered for term in debit_terms):
        return -abs(amount), warnings

    warnings.append(warning("amount_direction_inferred", "Could not confidently infer debit/credit direction from statement text."))
    return abs(amount), warnings


def transaction_fingerprint(
    *,
    bank_account_id: str,
    line_date: Optional[str],
    amount: Decimal,
    reference: Optional[str],
    description: str,
    counterparty: Optional[str] = None,
    bank_reference: Optional[str] = None,
) -> str:
    basis = "|".join(
        [
            bank_account_id,
            line_date or "",
            str(amount.quantize(Decimal("0.01"))),
            normalize_text(reference).lower(),
            normalize_text(counterparty).lower(),
            normalize_text(bank_reference).lower(),
            normalize_text(description).lower(),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()
