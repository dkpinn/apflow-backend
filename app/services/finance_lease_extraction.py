"""Finance lease document extraction using the same Gemini → Anthropic →
OpenRouter VLM cascade as bank statement extraction.

Extracts key lease terms from a scanned or digital finance lease / instalment
sale agreement PDF.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from app.services.lm_studio_vlm import (
    lm_studio_enabled as shared_lm_studio_enabled,
    lm_studio_vision_text,
)

logger = logging.getLogger(__name__)

# ── JSON schema for the extracted payload ─────────────────────────────────────

LEASE_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lessor_name":          {"type": ["string", "null"]},
        "asset_description":    {"type": ["string", "null"]},
        "reference_number":     {"type": ["string", "null"]},
        "commencement_date":    {"type": ["string", "null"]},   # ISO YYYY-MM-DD
        "end_date":             {"type": ["string", "null"]},   # ISO YYYY-MM-DD
        "payment_amount":       {"type": ["number", "null"]},
        "payment_frequency":    {"type": ["string", "null"]},   # monthly/quarterly/semi_annual/annual
        "annual_interest_rate": {"type": ["number", "null"]},   # e.g. 12.5
        "documentation_fees":   {"type": ["number", "null"]},
        "confidence_score":     {"type": "number"},
        "extraction_notes":     {"type": ["string", "null"]},
    },
    "required": ["confidence_score"],
}

LEASE_EXTRACTION_PROMPT = (
    "You are extracting finance lease information from a lease agreement, "
    "instalment sale agreement, or asset finance contract. "
    "Extract the following fields and return ONLY valid JSON matching the schema provided — "
    "no markdown, no explanation, just the JSON object.\n\n"
    "Fields to extract:\n"
    "• lessor_name: the full legal name of the financing company, bank, or lessor.\n"
    "• asset_description: a concise description of the leased/financed asset "
    "  (make, model, registration, or property address as applicable).\n"
    "• reference_number: contract number, agreement number, or account number.\n"
    "• commencement_date: the date the lease or finance agreement starts (ISO YYYY-MM-DD).\n"
    "• end_date: the date the lease or agreement ends / last payment date (ISO YYYY-MM-DD).\n"
    "• payment_amount: the regular instalment or rental amount as a plain number (no currency symbols).\n"
    "• payment_frequency: one of exactly: monthly, quarterly, semi_annual, annual.\n"
    "• annual_interest_rate: the annual interest or finance charge rate as a percentage "
    "  number — e.g. 12.5 means 12.5%, not 0.125.\n"
    "• documentation_fees: any initiation fee, documentation fee, or admin fee charged "
    "  at inception as a plain number. Use 0 if not stated.\n"
    "• confidence_score: your confidence in the extraction from 0.0 (no data found) "
    "  to 1.0 (all fields extracted with certainty).\n"
    "• extraction_notes: any important caveats, ambiguities, or fields you could not extract.\n\n"
    "Use null for any field that cannot be found in the document. "
    f"Schema: {json.dumps(LEASE_EXTRACTION_SCHEMA)}"
)

_RETRYABLE = ("503", "500", "unavailable", "429", "resource_exhausted", "quota", "overloaded")


def _is_retryable(exc: Exception) -> bool:
    return any(p in str(exc).lower() for p in _RETRYABLE)


def _parse_json(text: str | None, *, provider: str) -> dict[str, Any]:
    text = (text or "{}").strip()
    decoder = json.JSONDecoder()
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[i:])
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            continue
    raise ValueError(f"{provider} returned unparseable JSON for lease extraction. Preview: {text[:200]!r}")


def _call_openrouter(
    page_parts: list[tuple[bytes, str]],
    *,
    pdf_text_block: str | None,
    model: str,
    api_key: str,
    timeout: int = 60,
) -> str:
    import base64
    import httpx as _httpx

    content: list[dict] = [{"type": "text", "text": LEASE_EXTRACTION_PROMPT}]
    if pdf_text_block:
        content.append({"type": "text", "text": f"PDF TEXT:\n\n{pdf_text_block}"})
    for img_bytes, img_mime in page_parts:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{img_mime};base64,{base64.b64encode(img_bytes).decode()}"},
        })
    resp = _httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"] or "{}"


def _call_anthropic(
    page_parts: list[tuple[bytes, str]],
    *,
    pdf_text_block: str | None,
    model: str,
    api_key: str,
    timeout: int = 90,
) -> str:
    import base64
    import httpx as _httpx

    content: list[dict] = [{"type": "text", "text": LEASE_EXTRACTION_PROMPT}]
    if pdf_text_block:
        content.append({"type": "text", "text": f"PDF TEXT:\n\n{pdf_text_block}"})
    for img_bytes, img_mime in page_parts:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": img_mime, "data": base64.b64encode(img_bytes).decode()},
        })
    resp = _httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": content}],
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"] or "{}"


def _lm_studio_enabled() -> bool:
    return shared_lm_studio_enabled()


def _resolve_lm_studio_model(base_url: str, timeout: int) -> str:
    configured_model = os.getenv("LM_STUDIO_VLM_MODEL")
    if configured_model:
        return configured_model
    try:
        import httpx as _httpx

        resp = _httpx.get(f"{base_url}/models", timeout=min(timeout, 5))
        resp.raise_for_status()
        models = resp.json().get("data") or []
        first_model = next((row.get("id") for row in models if row.get("id")), None)
        if first_model:
            return str(first_model)
    except Exception:
        pass
    return "local-model"


def _call_lm_studio(
    page_parts: list[tuple[bytes, str]],
    *,
    pdf_text_block: str | None,
) -> str:
    response = lm_studio_vision_text(
        prompt=LEASE_EXTRACTION_PROMPT,
        page_parts=page_parts,
        pdf_text_block=pdf_text_block,
    )
    return response["text"]

    import base64
    import httpx as _httpx

    base_url = os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")
    timeout = int(os.getenv("LM_STUDIO_TIMEOUT_SECONDS", "120"))
    model = _resolve_lm_studio_model(base_url, timeout)
    api_key = os.getenv("LM_STUDIO_API_KEY", "lm-studio")
    content: list[dict] = [{"type": "text", "text": LEASE_EXTRACTION_PROMPT}]
    if pdf_text_block:
        content.append({"type": "text", "text": f"PDF TEXT:\n\n{pdf_text_block}"})
    for img_bytes, img_mime in page_parts:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{img_mime};base64,{base64.b64encode(img_bytes).decode()}"},
        })
    resp = _httpx.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": int(os.getenv("LM_STUDIO_MAX_TOKENS", "8192")),
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"] or "{}"


def extract_lease_document(
    file_bytes: bytes,
    *,
    mime_type: str,
) -> dict[str, Any]:
    """Extract finance lease fields from a PDF or image file.

    Tries providers in order: Gemini → Anthropic → OpenRouter.
    Returns a dict with all schema fields; missing fields are None.
    Raises ValueError if all providers fail.
    """
    try:
        from app.services.invoice_extraction.vlm_parser import preprocess_for_vlm
    except Exception as exc:
        raise ValueError("VLM lease extraction is not available in this environment") from exc

    # Prepare image page parts
    page_parts = preprocess_for_vlm(file_bytes, mime_type)
    if not page_parts:
        raise ValueError("Could not prepare document pages for VLM extraction")

    # For digital PDFs also pass the text layer
    pdf_text_block: str | None = None
    if "pdf" in mime_type.lower():
        try:
            import fitz
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            raw = "\n".join(doc[i].get_text() for i in range(doc.page_count)).strip()
            if len(raw) > 100:
                pdf_text_block = raw[:12_000]  # cap to avoid token overflow
        except Exception:
            pass

    # ── Provider 1: Gemini ────────────────────────────────────────────────────
    if _lm_studio_enabled():
        try:
            text = _call_lm_studio(page_parts, pdf_text_block=pdf_text_block)
            payload = _parse_json(text, provider="LM Studio")
            payload["_provider"] = f"lm_studio/{os.getenv('LM_STUDIO_VLM_MODEL') or 'local-model'}"
            logger.info("[LeaseExtract] LM Studio succeeded (conf=%.2f)", payload.get("confidence_score", 0))
            return payload
        except Exception as exc:
            logger.warning("[LeaseExtract] LM Studio failed, falling back to backup providers: %s", exc)

    google_api_key = os.getenv("GOOGLE_API_KEY")
    if google_api_key:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore

        primary_model   = os.getenv("GEMINI_VLM_MODEL", "gemini-2.5-flash")
        secondary_model = os.getenv("GEMINI_VLM_SECONDARY_MODEL", "gemini-2.0-flash")
        client = genai.Client(api_key=google_api_key)

        for model in (primary_model, secondary_model):
            contents: list[Any] = [LEASE_EXTRACTION_PROMPT]
            if pdf_text_block:
                contents.append(f"PDF TEXT:\n\n{pdf_text_block}")
            for img_bytes, img_mime in page_parts:
                contents.append(types.Part.from_bytes(data=img_bytes, mime_type=img_mime))

            for attempt in range(3):
                try:
                    response = client.models.generate_content(
                        model=model,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0,
                        ),
                    )
                    text = response.text or "{}"
                    payload = _parse_json(text, provider=f"Gemini/{model}")
                    payload["_provider"] = f"gemini/{model}"
                    logger.info("[LeaseExtract] Gemini/%s succeeded (conf=%.2f)",
                                model, payload.get("confidence_score", 0))
                    return payload
                except Exception as exc:
                    if _is_retryable(exc) and attempt < 2:
                        time.sleep(4 * (attempt + 1))
                        continue
                    logger.warning("[LeaseExtract] Gemini/%s failed: %s", model, exc)
                    break

    # ── Provider 2: Anthropic ─────────────────────────────────────────────────
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if anthropic_key:
        anthropic_model = os.getenv("ANTHROPIC_VLM_MODEL", "claude-sonnet-4-6")
        try:
            text = _call_anthropic(
                page_parts,
                pdf_text_block=pdf_text_block,
                model=anthropic_model,
                api_key=anthropic_key,
            )
            payload = _parse_json(text, provider="Anthropic")
            payload["_provider"] = f"anthropic/{anthropic_model}"
            logger.info("[LeaseExtract] Anthropic/%s succeeded (conf=%.2f)",
                        anthropic_model, payload.get("confidence_score", 0))
            return payload
        except Exception as exc:
            logger.warning("[LeaseExtract] Anthropic/%s failed: %s", anthropic_model, exc)

    # ── Provider 3: OpenRouter ────────────────────────────────────────────────
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if openrouter_key:
        or_model = os.getenv("OPENROUTER_VLM_MODEL", "google/gemini-2.0-flash-001")
        try:
            text = _call_openrouter(
                page_parts,
                pdf_text_block=pdf_text_block,
                model=or_model,
                api_key=openrouter_key,
            )
            payload = _parse_json(text, provider="OpenRouter")
            payload["_provider"] = f"openrouter/{or_model}"
            logger.info("[LeaseExtract] OpenRouter/%s succeeded (conf=%.2f)",
                        or_model, payload.get("confidence_score", 0))
            return payload
        except Exception as exc:
            logger.warning("[LeaseExtract] OpenRouter/%s failed: %s", or_model, exc)

    raise ValueError(
        "All VLM providers failed for lease document extraction. "
        "Check GOOGLE_API_KEY, ANTHROPIC_API_KEY, or OPENROUTER_API_KEY environment variables."
    )
