from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from app.services.extraction_foundation import extraction_metadata, warning

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


def parse_vlm_statement(
    file_bytes: bytes,
    *,
    mime_type: str,
    bank_account_id: str,
    currency: Optional[str] = None,
    parsing_hint: Optional[str] = None,
) -> tuple[dict[str, Any], list[ParsedBankLine]]:
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
        from app.services.invoice_extraction.vlm_parser import preprocess_for_vlm
    except Exception as exc:  # pragma: no cover - optional provider
        raise ValueError("VLM bank statement extraction is not available in this environment") from exc

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY is not configured for VLM bank statement extraction")

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

    client = genai.Client(api_key=api_key)
    prompt = (
        "Extract bank statement header fields and transaction rows. "
        "Return strict JSON only. Debits are money out; credits are money in. "
        "Preserve continuation lines, beneficiary names, transaction labels, bank references, and raw row text. "
        "Do not drop rows with fees, stop orders, transfers, card purchases, credits, interest, or charges. "
        "For each transaction, set page_number to the 1-based index of the page image it appears on "
        "(matching the order the page images are provided in). "
        f"Use this schema as the contract: {json.dumps(bank_statement_vlm_json_schema())}"
    )
    if parsing_hint:
        prompt += f" Bank-specific layout guidance for this statement: {parsing_hint.strip()}"
    contents: list[Any] = [prompt]
    if pdf_text_block:
        contents.append(
            "EXACT TEXT EXTRACTED FROM PDF — use this for accuracy when reading dates, "
            "descriptions, amounts and references. Do not guess from the image where the "
            f"text below is available:\n\n{pdf_text_block}"
        )
    for image_bytes, image_mime in page_parts:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))

    _primary_model = os.getenv("GEMINI_VLM_MODEL") or "gemini-2.5-flash"
    _lite_model = "gemini-2.5-flash-lite"
    _or_key = os.getenv("OPENROUTER_API_KEY")
    _or_model_name = os.getenv("OPENROUTER_VLM_MODEL") or "google/gemini-2.5-flash"

    response = None
    payload = None
    _final_exc: Exception | None = None
    _model = _primary_model

    # Step 1 — primary Gemini model
    for _attempt in range(3):
        try:
            response = client.models.generate_content(
                model=_model,
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            break
        except Exception as _exc:
            _retryable = "503" in str(_exc) or "UNAVAILABLE" in str(_exc) or "429" in str(_exc)
            if _retryable:
                _final_exc = _exc
                if _attempt < 2:
                    _wait = 5 * (_attempt + 1)
                    print(f"[VLM] {_model} attempt {_attempt + 1}/3 failed, retrying in {_wait}s: {_exc}")
                    time.sleep(_wait)
                else:
                    print(f"[VLM] {_model} exhausted all 3 attempts")
            else:
                raise

    # Step 2 — OpenRouter (if primary Gemini failed and key is available)
    if response is None and _or_key:
        print(f"[VLM] Falling back from {_model} to OpenRouter model={_or_model_name!r}")
        try:
            _or_text = _call_openrouter_bank_vlm(
                page_parts,
                prompt=prompt,
                pdf_text_block=pdf_text_block,
                model=_or_model_name,
                api_key=_or_key,
            )
            payload = json.loads(_or_text)
            _model = _or_model_name
        except Exception as _or_exc:
            print(f"[VLM] OpenRouter failed: {_or_exc}")
            _final_exc = _or_exc

    # Step 3 — lite Gemini (if still no result and model differs from primary)
    if response is None and payload is None and _lite_model != _primary_model:
        print(f"[VLM] Falling back to {_lite_model!r}")
        _model = _lite_model
        for _attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=_model,
                    contents=contents,
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
                break
            except Exception as _exc:
                _retryable = "503" in str(_exc) or "UNAVAILABLE" in str(_exc) or "429" in str(_exc)
                if _retryable:
                    _final_exc = _exc
                    if _attempt < 2:
                        _wait = 5 * (_attempt + 1)
                        print(f"[VLM] {_model} attempt {_attempt + 1}/3 failed, retrying in {_wait}s: {_exc}")
                        time.sleep(_wait)
                    else:
                        print(f"[VLM] {_model} exhausted all 3 attempts")
                else:
                    raise

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
        payload = json.loads(response.text or "{}")

    print(f"[VLM] Completed with model={_model!r}, hint={'yes' if parsing_hint else 'no'}")
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
            line_date=parse_date(transaction.get("line_date")),
            value_date=parse_date(transaction.get("value_date")),
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

    extraction_warnings = [
        item
        for line in lines
        for item in (line.extraction_warnings or [])
    ]
    confidence_score = payload.get("confidence_score") or 0.5
    header = {
        "statement_period_from": parse_date(payload.get("statement_period_from")),
        "statement_period_to": parse_date(payload.get("statement_period_to")),
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
