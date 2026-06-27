"""Manages the active VLM core prompt for bank statement extraction.

The prompt stored here is the *instructions* section only — the currency hint
and JSON schema contract are always appended at extraction time so they cannot
be accidentally broken by a UI edit.

The active prompt is cached in-process for 60 seconds to avoid a DB round-trip
on every extraction.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_VLM_PROMPT: str = (
    "Extract bank statement header fields and all account activity rows. "
    "Return strict JSON only. Debits are money out; credits are money in. "
    "Every activity date must be returned as YYYY-MM-DD. If the statement prints only day and month "
    "on an activity line, take its year from the Statement Period. "
    "Include every activity row exactly once — fees, stop orders, interest, card purchases, "
    "internal transfers, and charges must all be present. Do not skip, merge, or deduplicate rows. "
    "Multi-line descriptions: an activity's description may wrap across 2–3 printed lines in the Details column. "
    "Only append a continuation line to the current activity if that line has NO value in ANY of the Debits, Credits, Date, or Balance columns — those cells must be completely blank. "
    "A printed line that has any value in Debits, Credits, or Balance is ALWAYS a new, independent activity — never append it to the previous description, even if it appears visually indented or continued. "
    "'FEE-ELECTRONIC ACCOUNT PAYMENT' rows with a debit amount (and often marked ## in a Service Fee column) are ALWAYS separate activities; emit them as their own row. "
    "A reference number line (e.g. '10193786875') below a fee row with no amounts belongs to that fee row's description, not the salary row above it. "
    "Do not emit any row where both debit_amount and credit_amount are 0. "
    "Preserve beneficiary names, activity labels, bank references, and raw row text. "
    "For each activity row, set page_number to the 1-based index of the page it appears on "
    "(matching the order the pages are provided in)."
)

_CACHE_TTL = 60.0  # seconds

_cached_text: Optional[str] = None
_cached_at: float = 0.0


def _invalidate_cache() -> None:
    global _cached_at
    _cached_at = 0.0


def get_active_vlm_prompt() -> str:
    """Return the active VLM instructions text.

    Loads from bank_vlm_prompt_config with a 60-second in-memory cache.
    Falls back to DEFAULT_VLM_PROMPT if no custom prompt is stored.
    """
    global _cached_text, _cached_at
    if time.monotonic() - _cached_at < _CACHE_TTL:
        return _cached_text if _cached_text is not None else DEFAULT_VLM_PROMPT
    try:
        from app.db.supabase_client import get_supabase_client
        res = (
            get_supabase_client()
            .table("bank_vlm_prompt_config")
            .select("prompt_text")
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )
        text: Optional[str] = res.data[0]["prompt_text"] if res.data else None
    except Exception:
        logger.exception("[VLM-PROMPT] Could not load custom prompt from DB, using default")
        text = None
    _cached_text = text
    _cached_at = time.monotonic()
    return text if text is not None else DEFAULT_VLM_PROMPT


def upsert_vlm_prompt(db, *, prompt_text: str, user_id: str) -> dict:
    """Save a custom prompt (UPSERT: delete-then-insert to keep single row)."""
    db.table("bank_vlm_prompt_config").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    res = db.table("bank_vlm_prompt_config").insert({
        "prompt_text": prompt_text,
        "updated_by": user_id,
    }).execute()
    _invalidate_cache()
    return res.data[0] if res.data else {}


def reset_vlm_prompt(db) -> None:
    """Delete any custom prompt — next extraction will use DEFAULT_VLM_PROMPT."""
    db.table("bank_vlm_prompt_config").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    _invalidate_cache()
