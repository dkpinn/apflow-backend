from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

from app.services.extraction_foundation import extraction_metadata, warning
from app.services.lm_studio_vlm import (
    lm_studio_enabled as shared_lm_studio_enabled,
    lm_studio_vision_text,
)

from .common import (
    clean_description,
    dec_to_float,
    extract_bank_reference,
    money,
    normalize_text,
    parse_date,
    transaction_fingerprint,
)
from .models import ParsedBankLine


def bank_statement_vlm_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "statement_period_from": {"type": ["string", "null"]},
            "statement_period_to": {"type": ["string", "null"]},
            "opening_balance": {"type": ["number", "null"]},
            "closing_balance": {"type": ["number", "null"]},
            "currency": {"type": ["string", "null"]},
            "confidence_score": {"type": "number"},
            "transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line_date": {"type": ["string", "null"]},
                        "value_date": {"type": ["string", "null"]},
                        "transaction_type": {"type": ["string", "null"]},
                        "description": {"type": "string"},
                        "reference": {"type": ["string", "null"]},
                        "bank_reference": {"type": ["string", "null"]},
                        "counterparty": {"type": ["string", "null"]},
                        "debit_amount": {"type": "number"},
                        "credit_amount": {"type": "number"},
                        "balance_amount": {"type": ["number", "null"]},
                        "page_number": {"type": ["integer", "null"]},
                        "raw_text": {"type": ["string", "null"]},
                        "extraction_warnings": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["description", "debit_amount", "credit_amount"],
                },
            },
        },
        "required": ["transactions", "confidence_score"],
    }


def _response_preview(text: str, limit: int = 300) -> str:
    compact = " ".join((text or "").split())
    return compact[:limit] + ("..." if len(compact) > limit else "")


def _parse_vlm_json_payload(response_text: str | None, *, provider: str) -> dict[str, Any]:
    text = (response_text or "{}").strip()
    decoder = json.JSONDecoder()

    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
        raise ValueError(f"{provider} returned JSON {type(payload).__name__}, expected object")
    except json.JSONDecodeError as direct_exc:
        last_exc: json.JSONDecodeError = direct_exc


    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError as exc:
            last_exc = exc
            continue
        if isinstance(payload, dict):
            return payload
        raise ValueError(f"{provider} returned JSON {type(payload).__name__}, expected object")

    raise ValueError(
        f"{provider} returned invalid JSON for bank statement extraction: "
        f"{last_exc.msg}. Response preview: {_response_preview(text)!r}"
    ) from last_exc


_RETRYABLE_PATTERNS = ("503", "500", "unavailable", "429", "resource_exhausted", "quota", "overloaded")


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _RETRYABLE_PATTERNS)


_STATEMENT_PERIOD_RE = re.compile(
    r"statement\s+period\s*:\s*"
    r"(?P<date_from>\d{1,2}\s+[A-Za-z]+\s+\d{4})\s+to\s+"
    r"(?P<date_to>\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    re.IGNORECASE,
)


def _vlm_statement_period_dates(
    payload: dict[str, Any],
    pdf_text: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    date_from = parse_date(payload.get("statement_period_from"))
    date_to = parse_date(payload.get("statement_period_to"))
    if date_from and date_to:
        return date_from, date_to

    match = _STATEMENT_PERIOD_RE.search(pdf_text or "")
    if match:
        date_from = date_from or parse_date(match.group("date_from"))
        date_to = date_to or parse_date(match.group("date_to"))
    return date_from, date_to


def _parse_vlm_transaction_date(
    value: Any,
    *,
    statement_period_from: Optional[str],
    statement_period_to: Optional[str],
) -> Optional[str]:
    parsed = parse_date(value)
    if parsed or not statement_period_from:
        return parsed

    start_year = int(statement_period_from[:4])
    parsed = parse_date(value, year=start_year)
    if not parsed:
        return None

    if statement_period_to and statement_period_to[:4] != statement_period_from[:4]:
        end_year = int(statement_period_to[:4])
        end_year_candidate = parse_date(value, year=end_year)
        if end_year_candidate and statement_period_from <= end_year_candidate <= statement_period_to:
            return end_year_candidate
    return parsed


def _call_openrouter_bank_vlm(
    page_parts: list[tuple[bytes, str]],
    *,
    prompt: str,
    pdf_text_block: "str | None",
    model: str,
    api_key: str,
    timeout: int = 60,
) -> str:
    """Call OpenRouter chat completions API and return raw JSON text."""
    import base64
    import httpx as _httpx

    content: list[dict] = [{"type": "text", "text": prompt}]
    if pdf_text_block:
        content.append({
            "type": "text",
            "text": (
                "EXACT TEXT EXTRACTED FROM PDF — use this for accuracy when reading dates, "
                "descriptions, amounts and references. Do not guess from the image where the "
                f"text below is available:\n\n{pdf_text_block}"
            ),
        })
    for image_bytes, image_mime in page_parts:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{encoded}"},
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


def _call_lm_studio_bank_vlm(
    page_parts: list[tuple[bytes, str]],
    *,
    prompt: str,
    pdf_text_block: "str | None",
) -> str:
    response = lm_studio_vision_text(
        prompt=prompt,
        page_parts=page_parts,
        pdf_text_block=pdf_text_block,
        pdf_text_intro=(
            "EXACT TEXT EXTRACTED FROM PDF - use this for accuracy when reading dates, "
            "descriptions, amounts and references. Do not guess from the image where the text below is available:"
        ),
    )
    return response["text"]

    import base64
    import httpx as _httpx

    base_url = os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")
    timeout = int(os.getenv("LM_STUDIO_TIMEOUT_SECONDS", "120"))
    model = _resolve_lm_studio_model(base_url, timeout)
    api_key = os.getenv("LM_STUDIO_API_KEY", "lm-studio")
    content: list[dict] = [{"type": "text", "text": prompt}]
    if pdf_text_block:
        content.append({
            "type": "text",
            "text": (
                "EXACT TEXT EXTRACTED FROM PDF — use this for accuracy when reading dates, "
                "descriptions, amounts and references. Do not guess from the image where the "
                f"text below is available:\n\n{pdf_text_block}"
            ),
        })
    for image_bytes, image_mime in page_parts:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{encoded}"},
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


def _call_anthropic_bank_vlm(
    page_parts: list[tuple[bytes, str]],
    *,
    prompt: str,
    pdf_text_block: "str | None",
    model: str,
    api_key: str,
    timeout: int = 90,
) -> str:
    """Call Anthropic Messages API and return raw JSON text."""
    import base64
    import httpx as _httpx

    content: list[dict] = [{"type": "text", "text": prompt}]
    if pdf_text_block:
        content.append({
            "type": "text",
            "text": (
                "SUPPLEMENTARY TEXT extracted from this PDF — use to verify descriptions, "
                f"dates, and reference numbers only:\n\n{pdf_text_block}"
            ),
        })
    for image_bytes, image_mime in page_parts:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image_mime, "data": encoded},
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
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": content}],
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"] or "{}"


def parse_vlm_statement(
    file_bytes: bytes,
    *,
    mime_type: str,
    bank_account_id: str,
    currency: Optional[str] = None,
    parsing_hint: Optional[str] = None,
) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    try:
        from app.services.invoice_extraction.vlm_parser import preprocess_for_vlm
    except Exception as exc:  # pragma: no cover - optional provider
        raise ValueError("VLM bank statement extraction is not available in this environment") from exc

    page_parts = preprocess_for_vlm(file_bytes, mime_type)
    if not page_parts:
        raise ValueError("Could not prepare statement pages for VLM extraction")

    # For digital PDFs, extract the text layer and pass it to Gemini alongside the
    # images. This eliminates visual OCR errors (e.g. "To" read as "10") that occur
    # when the model reads rendered page images. Scanned PDFs (no text layer) will
    # produce < 200 chars of extracted text and fall through to image-only mode.
    pdf_text_block: str | None = None
    if "pdf" in mime_type.lower():
        try:
            import fitz  # PyMuPDF — already used by preprocess_for_vlm
            _doc = fitz.open(stream=file_bytes, filetype="pdf")
            _pages_text = [_doc[i].get_text() for i in range(_doc.page_count)]
            _raw = "\n--- PAGE BREAK ---\n".join(_pages_text).strip()
            if len(_raw) > 200:
                pdf_text_block = _raw
        except Exception:
            pass  # scanned / locked PDF — fall back to image-only

    _currency_hint = currency or "ZAR"
    from app.services.bank_extraction_prompt import get_active_vlm_prompt as _get_prompt
    _instructions = parsing_hint.strip() if parsing_hint else _get_prompt()
    prompt = (
        _instructions
        + f" If currency is not shown on the statement, use: {_currency_hint}. "
        + f"Use this schema as the contract: {json.dumps(bank_statement_vlm_json_schema())}"
    )
    logger.info(
        "[VLM] Prompt assembled: source=%s (%d chars), supplementary_text=%s",
        "bank_hint" if parsing_hint else "core_default",
        len(_instructions),
        "YES" if pdf_text_block else "NO (image-only)",
    )
    _primary_model = os.getenv("GEMINI_VLM_MODEL") or "gemini-2.5-flash"
    _secondary_model = os.getenv("GEMINI_VLM_SECONDARY_MODEL") or "gemini-2.0-flash"
    _lite_model = "gemini-2.5-flash-lite"

    response = None
    payload = None
    _final_exc: Exception | None = None
    _model = _primary_model
    contents: list[Any] = []

    class _DeferredPart:
        @staticmethod
        def from_bytes(**_kwargs):
            return None

    class _DeferredTypes:
        Part = _DeferredPart

    types = _DeferredTypes()

    if pdf_text_block:
        contents.append(
            "SUPPLEMENTARY TEXT extracted from this PDF. Column alignment in this text is unreliable "
            "for multi-column tabular statements — do NOT use it for transaction descriptions or "
            "counterparty/beneficiary names. Use this text ONLY to verify exact reference numbers, "
            "sort codes, or account number digits that are ambiguous in the images. "
            "All descriptions, names, amounts, dates, debits, credits, and balances MUST be read "
            f"from the page images:\n\n{pdf_text_block}"
        )
    for image_bytes, image_mime in page_parts:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))

    if _lm_studio_enabled():
        try:
            logger.info("[VLM] Trying LM Studio local model first")
            _text = _call_lm_studio_bank_vlm(
                page_parts,
                prompt=prompt,
                pdf_text_block=pdf_text_block,
            )
            payload = _parse_vlm_json_payload(_text, provider="LM Studio VLM")
            _model = os.getenv("LM_STUDIO_VLM_MODEL") or "local-model"
        except Exception as _step_exc:
            logger.warning("[VLM] LM Studio failed, falling back to backup providers: %s", _step_exc)
            _final_exc = _step_exc

    if payload is None:
        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore
        except Exception as exc:  # pragma: no cover - optional provider
            if _final_exc:
                raise _final_exc
            raise ValueError("Gemini VLM bank statement extraction is not available in this environment") from exc

        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            if _final_exc:
                raise _final_exc
            raise ValueError("GOOGLE_API_KEY is not configured for VLM bank statement extraction")

        client = genai.Client(api_key=api_key)
        contents = [prompt]
        if pdf_text_block:
            contents.append(
                "SUPPLEMENTARY TEXT extracted from this PDF. Column alignment in this text is unreliable "
                "for multi-column tabular statements — do NOT use it for transaction descriptions or "
                "counterparty/beneficiary names. Use this text ONLY to verify exact reference numbers, "
                "sort codes, or account number digits that are ambiguous in the images. "
                "All descriptions, names, amounts, dates, debits, credits, and balances MUST be read "
                f"from the page images:\n\n{pdf_text_block}"
            )
        for image_bytes, image_mime in page_parts:
            contents.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))

    def _run_gemini_step(model_name: str) -> Any:
        """Run one Gemini model with 3 retries. Returns response or raises."""
        if payload is not None:
            return None
        _resp = None
        for _attempt in range(3):
            try:
                _resp = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
                return _resp
            except Exception as _exc:
                if _is_retryable(_exc):
                    if _attempt < 2:
                        _wait = 5 * (_attempt + 1)
                        logger.warning("[VLM] %s attempt %d/3 failed, retrying in %ds", model_name, _attempt + 1, _wait)
                        time.sleep(_wait)
                    else:
                        logger.warning("[VLM] %s exhausted all 3 attempts", model_name)
                        raise
                else:
                    raise
        return _resp

    # Step 1 — primary Gemini model
    try:
        response = _run_gemini_step(_model)
    except Exception as _step_exc:
        logger.warning("[VLM] %s failed, trying next model: %s", _model, _step_exc)
        _final_exc = _step_exc
        response = None

    # Step 2 — secondary Gemini model (free fallback, same API key)
    if payload is None and response is None and _secondary_model != _primary_model:
        _model = _secondary_model
        logger.info("[VLM] Falling back from primary to secondary model %r", _model)
        try:
            response = _run_gemini_step(_model)
        except Exception as _step_exc:
            logger.warning("[VLM] %s failed, trying lite model: %s", _model, _step_exc)
            _final_exc = _step_exc
            response = None

    # Step 3 — lite Gemini (last resort, image-only)
    if response is None and payload is None and _lite_model != _primary_model:
        _model = _lite_model
        logger.info("[VLM] Falling back to lite model %r", _model)
        try:
            response = _run_gemini_step(_model)
        except Exception as _step_exc:
            logger.warning("[VLM] %s failed: %s", _model, _step_exc)
            _final_exc = _step_exc
            response = None

    # Step 4 — Anthropic Claude (opt-in: only if ANTHROPIC_API_KEY is set)
    _anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    _anthropic_model = os.getenv("ANTHROPIC_VLM_MODEL") or "claude-3-5-haiku-20241022"
    if response is None and payload is None and _anthropic_key:
        logger.info("[VLM] Falling back to Anthropic model %r", _anthropic_model)
        try:
            _text = _call_anthropic_bank_vlm(
                page_parts,
                prompt=prompt,
                pdf_text_block=pdf_text_block,
                model=_anthropic_model,
                api_key=_anthropic_key,
            )
            payload = _parse_vlm_json_payload(_text, provider="Anthropic VLM")
            _model = _anthropic_model
        except Exception as _step_exc:
            logger.warning("[VLM] Anthropic %s failed: %s", _anthropic_model, _step_exc)
            _final_exc = _step_exc

    # Step 5 — OpenRouter (opt-in: only if OPENROUTER_API_KEY is set)
    _or_key = os.getenv("OPENROUTER_API_KEY")
    _or_model = os.getenv("OPENROUTER_VLM_MODEL") or "meta-llama/llama-3.2-11b-vision-instruct:free"
    if response is None and payload is None and _or_key:
        logger.info("[VLM] Falling back to OpenRouter model %r", _or_model)
        try:
            _text = _call_openrouter_bank_vlm(
                page_parts,
                prompt=prompt,
                pdf_text_block=pdf_text_block,
                model=_or_model,
                api_key=_or_key,
            )
            payload = _parse_vlm_json_payload(_text, provider="OpenRouter VLM")
            _model = _or_model
        except Exception as _step_exc:
            logger.warning("[VLM] OpenRouter %s failed: %s", _or_model, _step_exc)
            _final_exc = _step_exc

    if response is None and payload is None:
        raise _final_exc or RuntimeError("All VLM providers exhausted")

    input_tokens: int | None = None
    output_tokens: int | None = None

    if response is not None:
        try:
            usage = getattr(response, "usage_metadata", None)
            if usage:
                input_tokens = getattr(usage, "prompt_token_count", None)
                output_tokens = getattr(usage, "candidates_token_count", None)
        except Exception:
            pass
        payload = _parse_vlm_json_payload(response.text, provider=f"Gemini VLM {_model}")

    logger.info("[VLM] Completed with model=%r, hint=%s", _model, "yes" if parsing_hint else "no")
    statement_period_from, statement_period_to = _vlm_statement_period_dates(
        payload,
        pdf_text_block,
    )
    lines: list[ParsedBankLine] = []
    for transaction in payload.get("transactions") or []:
        debit = money(transaction.get("debit_amount"))
        credit = money(transaction.get("credit_amount"))
        raw_text = normalize_text(transaction.get("raw_text"))
        page_number = transaction.get("page_number")
        try:
            page_number = int(page_number) if page_number is not None else None
        except (TypeError, ValueError):
            page_number = None
        parsed = ParsedBankLine(
            line_date=_parse_vlm_transaction_date(
                transaction.get("line_date"),
                statement_period_from=statement_period_from,
                statement_period_to=statement_period_to,
            ),
            value_date=_parse_vlm_transaction_date(
                transaction.get("value_date"),
                statement_period_from=statement_period_from,
                statement_period_to=statement_period_to,
            ),
            description=clean_description(transaction.get("description")),
            reference=normalize_text(transaction.get("reference")) or None,
            counterparty=clean_description(transaction.get("counterparty")) or None,
            debit_amount=debit,
            credit_amount=credit,
            signed_amount=credit - debit,
            balance_amount=money(transaction.get("balance_amount")) if transaction.get("balance_amount") is not None else None,
            currency=payload.get("currency") or currency,
            transaction_type=normalize_text(transaction.get("transaction_type")) or None,
            bank_reference=normalize_text(transaction.get("bank_reference")) or extract_bank_reference(
                transaction.get("reference"),
                transaction.get("description"),
                transaction.get("counterparty"),
            ),
            raw_text=raw_text or None,
            raw_lines=[raw_text] if raw_text else [],
            source_page=page_number,
            source_row_index=len(lines),
            extraction_confidence=float(payload.get("confidence_score") or 0.5),
            extraction_warnings=[
                warning("vlm_line_warning", str(item))
                for item in (transaction.get("extraction_warnings") or [])
            ],
        )
        parsed.transaction_hash = transaction_fingerprint(
            bank_account_id=bank_account_id,
            line_date=parsed.line_date,
            amount=parsed.signed_amount,
            reference=parsed.reference,
            counterparty=parsed.counterparty,
            bank_reference=parsed.bank_reference,
            description=parsed.description,
        )
        lines.append(parsed)

    # Drop zero-amount placeholder rows (VLM sometimes returns blank continuation lines)
    _before = len(lines)
    lines = [ln for ln in lines if ln.debit_amount or ln.credit_amount or len(ln.description or "") >= 3]
    if len(lines) < _before:
        logger.debug("[VLM] Dropped %d zero-amount placeholder rows from %s", _before - len(lines), _model)

    extraction_warnings = [
        item
        for line in lines
        for item in (line.extraction_warnings or [])
    ]
    confidence_score = payload.get("confidence_score") or 0.5
    header = {
        "statement_period_from": statement_period_from,
        "statement_period_to": statement_period_to,
        "opening_balance": dec_to_float(money(payload.get("opening_balance"))) if payload.get("opening_balance") is not None else None,
        "closing_balance": dec_to_float(money(payload.get("closing_balance"))) if payload.get("closing_balance") is not None else None,
        "currency": payload.get("currency") or currency,
        "confidence_score": confidence_score,
        "extractor": "bank_statement",
        "extractor_type": "bank_statement",
        "extractor_version": "v1",
        "source_format": "vlm",
        "parser_strategy": "vlm",
        "extraction_warnings": extraction_warnings,
        "raw_extraction": extraction_metadata(
            extractor_type="bank_statement",
            extractor_version="v1",
            source_format="vlm",
            parser_strategy="vlm",
            confidence_score=confidence_score,
            warnings=extraction_warnings,
            extra={"line_count": len(lines)},
        ),
        "extraction_input_tokens": input_tokens,
        "extraction_output_tokens": output_tokens,
        "extraction_model": _model if (input_tokens or output_tokens) else None,
    }
    return header, lines
