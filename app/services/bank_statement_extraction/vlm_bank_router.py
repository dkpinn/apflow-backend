"""Lightweight bank-identification router.

Identifies which bank issued a statement and what account type it is before the
main VLM extraction runs, so that per-institution parsing hints can be looked up
automatically even when the bank account fields are blank.

Two-stage approach:
  Stage 1 — Free text scan: extract PDF text layer and match against known SA banks.
  Stage 2 — Cheap VLM call: send page 1 image to gemini-2.0-flash for identification.

Always returns (str | None, str | None). Never raises — failures are logged and silently skipped.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

_KNOWN_BANKS: list[str] = [
    "Standard Bank",
    "ABSA",
    "FNB",
    "First National Bank",
    "Nedbank",
    "Capitec",
    "Discovery Bank",
    "Investec",
    "African Bank",
    "TymeBank",
    "Bidvest Bank",
    "Mercantile Bank",
    "Grindrod Bank",
    "HBZ Bank",
    "Old Mutual",
    "Sasfin",
]

# Pre-compiled patterns for speed (longest/most-specific first within ties)
_BANK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(re.escape(name), re.IGNORECASE), name)
    for name in sorted(_KNOWN_BANKS, key=len, reverse=True)
]

# Maps free-text account type descriptions to DB enum values.
# Checked in order — first match wins.
_ACCOUNT_TYPE_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"credit\s*card", re.IGNORECASE), "credit_card"),
    (re.compile(r"home\s*loan|mortgage|bond\s*account", re.IGNORECASE), "mortgage"),
    (re.compile(r"vehicle\s*finance|car\s*(loan|finance)", re.IGNORECASE), "vehicle_finance"),
    (re.compile(r"investment|fixed\s*deposit|notice\s*account", re.IGNORECASE), "investment"),
    (re.compile(r"call\s*account", re.IGNORECASE), "call_account"),
    (re.compile(r"money\s*market", re.IGNORECASE), "money_market"),
]


def _map_account_type(raw: str) -> Optional[str]:
    if not raw:
        return None
    for pattern, db_value in _ACCOUNT_TYPE_MAP:
        if pattern.search(raw):
            return db_value
    return None


def _detect_from_text(text: str) -> Optional[str]:
    """Scan the first 2000 chars of text for a known bank name."""
    sample = text[:2000]
    for pattern, canonical_name in _BANK_PATTERNS:
        if pattern.search(sample):
            return canonical_name
    return None


def _detect_from_vlm(file_bytes: bytes, mime_type: str) -> tuple[Optional[str], Optional[str]]:
    """Send page 1 to gemini-2.0-flash and return (bank_name, account_type)."""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None, None
    try:
        from google import genai
        from google.genai import types
        from app.services.invoice_extraction.vlm_parser import preprocess_for_vlm

        page_parts = preprocess_for_vlm(file_bytes, mime_type)
        if not page_parts:
            return None, None

        first_page_bytes, first_page_mime = page_parts[0]

        client = genai.Client(api_key=api_key)
        prompt = (
            "This is a bank statement. What bank issued it and what type of account is it? "
            "Return ONLY a JSON object: "
            '{"bank_name": "<name exactly as printed>", "account_type": "<e.g. Current Account, Credit Card, Home Loan, Vehicle Finance, Investment, Call Account, Money Market>", "currency": "<ISO code>"}'
        )
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                prompt,
                types.Part.from_bytes(data=first_page_bytes, mime_type=first_page_mime),
            ],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        raw = (response.text or "").strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None, None
        data = json.loads(match.group())
        bank_name = (data.get("bank_name") or "").strip() or None
        account_type = _map_account_type(data.get("account_type") or "")
        return bank_name, account_type
    except Exception as exc:
        logger.debug("[ROUTER] VLM identification failed: %s", exc)
        return None, None


def identify_bank(file_bytes: bytes, mime_type: str) -> tuple[Optional[str], Optional[str]]:
    """Return (institution_name, account_type) detected from the statement.

    Either value may be None if not detected. Never raises.

    Stage 1: PDF text scan (free) — detects bank name only.
    Stage 2: VLM page-1 call (cheap) — detects bank name + account type.
    """
    # Stage 1 — text scan (free, fast); gives bank name but not account type
    if "pdf" in mime_type.lower():
        try:
            import fitz
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            first_page_text = doc[0].get_text() if doc.page_count > 0 else ""
            doc.close()
            if first_page_text and len(first_page_text) > 50:
                detected_name = _detect_from_text(first_page_text)
                if detected_name:
                    logger.info("[ROUTER] Text-scan identified bank: %r", detected_name)
                    # Still fall through to VLM to get account type
                    name_vlm, type_vlm = _detect_from_vlm(file_bytes, mime_type)
                    account_type = type_vlm  # text scan can't determine account type
                    return detected_name, account_type
        except Exception as exc:
            logger.debug("[ROUTER] Text extraction failed: %s", exc)

    # Stage 2 — VLM page-1 call (cheap, ~$0.001)
    name, account_type = _detect_from_vlm(file_bytes, mime_type)
    if name or account_type:
        logger.info("[ROUTER] VLM identified bank=%r account_type=%r", name, account_type)
    return name, account_type
