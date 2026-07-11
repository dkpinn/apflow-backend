"""AI-based bank line allocation suggestions via Gemini.

Given a bank statement line and the organisation's active chart of accounts,
ask Gemini to pick the most likely GL account for the transaction. The result
is emitted as a suggestion dict shaped exactly like the rule/invoice scorers in
``bank_statement_service`` (``suggestion_type="ai"``) so it flows through the
existing ``bank_transaction_suggestions`` pipeline and reconciliation UI with no
further wiring.

Safe to call without ``GOOGLE_API_KEY`` or the ``google-genai`` package — it
logs and returns ``[]`` rather than raising, mirroring
``invoice_extraction.vlm_parser``.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_MODEL_ENV = "GEMINI_VLM_MODEL"
_DEFAULT_MODEL = "gemini-2.5-flash"


class _AiAccountSuggestion(BaseModel):
    account_code: str = Field(
        default="",
        description="The chosen account code from the provided chart, or empty string if none fit.",
    )
    confidence: float = Field(
        default=0.0,
        description="Confidence that this account is correct, from 0 to 1.",
    )
    reason: str = Field(default="", description="A short (one sentence) reason for the choice.")


def _line_text(line: dict[str, Any]) -> str:
    parts = [
        line.get("description"),
        line.get("counterparty"),
        line.get("reference"),
        line.get("raw_text"),
    ]
    return " ".join(str(p) for p in parts if p).strip()


def _fetch_candidate_accounts(db, organisation_id: str) -> list[dict[str, Any]]:
    try:
        return (
            db.table("accounts")
            .select("id, code, name, type")
            .eq("organisation_id", organisation_id)
            .eq("active", True)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.exception("AI suggestion: failed to load accounts for org=%s", organisation_id)
        return []


def score_ai_suggestions(
    db,
    *,
    organisation_id: str,
    line: dict[str, Any],
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return at most one ``suggestion_type="ai"`` suggestion for ``line``.

    Returns ``[]`` when the AI is unavailable (no key / package / candidates),
    when the transaction has no usable text, or when the model declines to pick.
    """
    effective_api_key = api_key or os.getenv("GOOGLE_API_KEY")
    if not effective_api_key:
        logger.info("AI suggestion skipped: GOOGLE_API_KEY not set")
        return []

    text = _line_text(line)
    if not text:
        return []

    accounts = _fetch_candidate_accounts(db, organisation_id)
    catalogue = [
        {"code": a.get("code"), "name": a.get("name"), "type": a.get("type")}
        for a in accounts
        if a.get("code")
    ]
    if not catalogue:
        return []

    signed = line.get("signed_amount")
    try:
        signed_num = float(signed) if signed is not None else 0.0
    except (TypeError, ValueError):
        signed_num = 0.0
    direction = "money in (received)" if signed_num >= 0 else "money out (paid)"

    prompt = (
        "You are a bookkeeper categorising a single bank transaction. Choose the "
        "one most appropriate general-ledger account from the provided chart of "
        "accounts, using the transaction description and whether money came in or "
        "went out.\n\n"
        f"Transaction description: {text}\n"
        f"Amount: {signed_num} ({direction})\n\n"
        "Chart of accounts (JSON list of {code, name, type}):\n"
        f"{json.dumps(catalogue, ensure_ascii=False)}\n\n"
        "Respond with the account code that best matches, a confidence between 0 "
        "and 1, and a short reason. If nothing fits well, return an empty code and "
        "confidence 0."
    )

    try:
        from google import genai
        from google.genai import types
    except Exception:
        logger.info("AI suggestion skipped: google-genai not installed")
        return []

    effective_model = model or os.getenv(_MODEL_ENV) or _DEFAULT_MODEL
    try:
        client = genai.Client(api_key=effective_api_key)
        response = client.models.generate_content(
            model=effective_model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_AiAccountSuggestion,
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        parsed = _AiAccountSuggestion.model_validate_json(response.text)
    except Exception:
        logger.exception("AI suggestion Gemini call failed for org=%s", organisation_id)
        return []

    code = (parsed.account_code or "").strip()
    if not code or parsed.confidence <= 0:
        return []

    match = next((a for a in accounts if str(a.get("code")) == code), None)
    if not match:
        logger.info("AI suggestion returned unknown account code %r for org=%s", code, organisation_id)
        return []

    confidence = max(0.0, min(1.0, float(parsed.confidence)))
    return [
        {
            "suggestion_type": "ai",
            "confidence_score": round(confidence, 2),
            "rationale": parsed.reason or "AI-suggested allocation",
            "suggested_account_id": match.get("id"),
            "suggested_tracking": {},
            "suggested_tax_treatment": None,
            "evidence": {
                "model": effective_model,
                "account_code": code,
                "account_name": match.get("name"),
            },
        }
    ]
