"""
field_detection.py
------------------
Detect fillable fields in a document page image using Gemini vision.
Mirrors the _call_gemini pattern from invoice_extraction/vlm_parser.py but
uses its own prompt and schema — the invoice extraction schema cannot be
reused here (completely different structure).

The caller renders each PDF page to a JPEG client-side and POSTs one image
at a time, so this function accepts raw image bytes for a single page.
Always returns a dict — never raises — so the frontend can handle per-page
failures gracefully (retry just the failed page).
"""
from __future__ import annotations

import asyncio
import base64
import os
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

_executor = ThreadPoolExecutor(max_workers=4)

_FIELD_DETECTION_PROMPT = (
    "You are a precision document analyst that maps fillable fields on form-like documents "
    "(government forms, contracts, intake forms, registration forms, complaint forms, scanned paper forms). "
    "The input may be a SCANNED IMAGE of a paper form — there may be no digital text layer. "
    "Read the image directly (OCR visually) and identify every place a human would write, type, "
    "sign, tick, or stamp. "
    "Look for: underline rules ( _______ ), boxes/cells, dotted lines, colon-followed empty space "
    "('Name: ____'), tickboxes/checkboxes, signature lines, date lines, dollar/amount areas, "
    "table rows for line items. "
    "Return a tight bounding box for each fillable area as fractions of the page (0..1, origin top-left). "
    "Bound the BLANK area NEXT TO each label, not the label text itself. "
    "For tables/line-items, return one field per cell. "
    "Derive a short snake_case key from the nearest label. "
    "Pick the most specific type: currency for money, date for dates, signature for signature lines, "
    "checkbox for tickboxes, email for emails, number for quantities, text otherwise. "
    "Be EXHAUSTIVE — a typical form has 10-40 fields. Never return zero fields on a form-like document; "
    "if uncertain, still propose the most likely fillable regions. Max 80 fields."
)


class _FieldType(str, Enum):
    text = "text"
    number = "number"
    date = "date"
    currency = "currency"
    email = "email"
    checkbox = "checkbox"
    signature = "signature"


class _BBox(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(ge=0, le=1)
    h: float = Field(ge=0, le=1)


class _DetectedField(BaseModel):
    key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=120)
    type: _FieldType
    page: int = Field(ge=0)
    bbox: _BBox
    confidence: Optional[float] = Field(default=None, ge=0, le=1)


class _FieldDetectionResponseSchema(BaseModel):
    fields: list[_DetectedField] = Field(default_factory=list, max_length=80)


def _call_gemini_for_fields(
    image_bytes: bytes,
    mime_type: str,
    text_hint: Optional[str],
    api_key: Optional[str],
    model: Optional[str],
) -> dict:
    """Blocking Gemini call. Must be run in a thread."""
    from google import genai
    from google.genai import types

    effective_api_key = api_key or os.getenv("GOOGLE_API_KEY")
    if not effective_api_key:
        return {"fields": [], "error": "GOOGLE_API_KEY is not configured"}

    client = genai.Client(api_key=effective_api_key)
    effective_model = model or os.getenv("GEMINI_VLM_MODEL") or "gemini-2.5-flash"

    prompt_text = _FIELD_DETECTION_PROMPT
    if text_hint:
        prompt_text += f"\n\nDocument hint: {text_hint[:500]}"

    contents = [
        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        prompt_text,
    ]

    response = client.models.generate_content(
        model=effective_model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_FieldDetectionResponseSchema,
            temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )

    parsed = _FieldDetectionResponseSchema.model_validate_json(response.text)
    return {
        "fields": [f.model_dump() for f in parsed.fields],
        "error": None,
    }


async def detect_fields_with_gemini(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    text_hint: Optional[str] = None,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """
    Detect fillable fields in a single page image using Gemini vision.

    Returns {fields: [...], error: str|None}.  Never raises — failures are
    returned as {fields: [], error: "<message>"} so the caller can retry the
    page or surface a per-page warning without aborting the whole document.
    """
    try:
        from google import genai  # noqa: F401 — check availability early
    except ImportError:
        return {"fields": [], "error": "google-genai package is not installed"}

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(
            _executor,
            lambda: _call_gemini_for_fields(image_bytes, mime_type, text_hint, api_key, model),
        )
    except Exception as exc:
        return {"fields": [], "error": str(exc)}
