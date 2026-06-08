from __future__ import annotations

import json
import os
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
                        "raw_text": {"type": ["string", "null"]},
                        "extraction_warnings": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["description", "debit_amount", "credit_amount"],
                },
            },
        },
        "required": ["transactions", "confidence_score"],
    }


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

    client = genai.Client(api_key=api_key)
    prompt = (
        "Extract bank statement header fields and transaction rows. "
        "Return strict JSON only. Debits are money out; credits are money in. "
        "Preserve continuation lines, beneficiary names, transaction labels, bank references, and raw row text. "
        "Do not drop rows with fees, stop orders, transfers, card purchases, credits, interest, or charges. "
        f"Use this schema as the contract: {json.dumps(bank_statement_vlm_json_schema())}"
    )
    if parsing_hint:
        prompt += f" Bank-specific layout guidance for this statement: {parsing_hint.strip()}"
    contents: list[Any] = [prompt]
    for image_bytes, image_mime in page_parts:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))

    effective_model = os.getenv("GEMINI_VLM_MODEL") or "gemini-2.5-flash"
    response = client.models.generate_content(
        model=effective_model,
        contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    input_tokens: int | None = None
    output_tokens: int | None = None
    try:
        usage = getattr(response, "usage_metadata", None)
        if usage:
            input_tokens = getattr(usage, "prompt_token_count", None)
            output_tokens = getattr(usage, "candidates_token_count", None)
    except Exception:
        pass

    payload = json.loads(response.text or "{}")
    lines: list[ParsedBankLine] = []
    for transaction in payload.get("transactions") or []:
        debit = money(transaction.get("debit_amount"))
        credit = money(transaction.get("credit_amount"))
        raw_text = normalize_text(transaction.get("raw_text"))
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
        "extraction_model": effective_model if (input_tokens or output_tokens) else None,
    }
    return header, lines
