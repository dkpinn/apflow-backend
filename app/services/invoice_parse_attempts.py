from __future__ import annotations

from copy import deepcopy
from typing import Optional

from app.services.invoice_line_items import build_line_item_diagnostics
from app.services.invoice_ocr_pipeline import (
    DEEP_OCR_RENDER_DPI,
    OCR_RENDER_DPI,
    deep_extract_text_with_regions,
    parse_invoice_fields,
)


PARSE_ATTEMPT_SELECT = (
    "id, organisation_id, invoice_raw_id, invoice_extracted_id, attempt_number, "
    "strategy, dpi, ocr_variant, ocr_psm, ocr_used, ocr_confidence, "
    "image_quality_score, candidate_score, confidence_score, parsed_data, "
    "line_items, text_preview, selected, accepted_at, created_at"
)


def _copy_parsed(parsed: Optional[dict]) -> dict:
    copied = deepcopy(parsed or {})
    copied["line_items"] = copied.get("line_items") or []
    return copied


def _line_items(parsed: dict) -> list[dict]:
    value = parsed.get("line_items") or []
    return value if isinstance(value, list) else []


def _text_preview(text: Optional[str], limit: int = 4000) -> str:
    return (text or "")[:limit]


def _has_meaningful_text(text: Optional[str]) -> bool:
    return bool((text or "").strip())


def _attempt_score(attempt: dict) -> tuple:
    parsed = attempt.get("parsed_data") or {}
    line_items = attempt.get("line_items") or []
    confidence = float(attempt.get("confidence_score") or 0)
    candidate_score = float(attempt.get("candidate_score") or 0)
    has_total = 1 if parsed.get("total_amount") else 0
    has_supplier = 1 if parsed.get("supplier_name_extracted") else 0
    has_invoice_number = 1 if parsed.get("invoice_number") else 0
    text_length = len(attempt.get("text_preview") or "")
    return (
        confidence,
        candidate_score,
        min(len(line_items), 20),
        has_total,
        has_supplier,
        has_invoice_number,
        text_length,
    )


def select_best_parse_attempt(attempts: list[dict]) -> Optional[dict]:
    if not attempts:
        return None
    return max(attempts, key=_attempt_score)


def build_parse_attempt(
    *,
    strategy: str,
    text: str,
    parsed_data: Optional[dict] = None,
    dpi: Optional[int] = None,
    ocr_used: bool = False,
    ocr_confidence=None,
    image_quality_score=None,
    candidate_score=None,
    ocr_variant: Optional[str] = None,
    ocr_psm: Optional[str] = None,
) -> Optional[dict]:
    if not _has_meaningful_text(text) and not parsed_data:
        return None

    parsed = _copy_parsed(parsed_data or parse_invoice_fields(text or ""))
    line_items = _line_items(parsed)

    return {
        "strategy": strategy,
        "dpi": dpi,
        "ocr_variant": ocr_variant,
        "ocr_psm": ocr_psm,
        "ocr_used": bool(ocr_used),
        "ocr_confidence": ocr_confidence,
        "image_quality_score": image_quality_score,
        "candidate_score": candidate_score,
        "confidence_score": parsed.get("confidence_score"),
        "parsed_data": parsed,
        "line_items": line_items,
        "text_preview": _text_preview(text),
    }


def ensure_parsed_data_attempt(
    attempts: list[dict],
    *,
    parsed_data: dict,
    text: str = "",
    strategy: str = "final_extraction_snapshot",
) -> list[dict]:
    """
    Ensure the final raw extraction is persisted even when OCR text is empty.

    Supplier rules mutate the working line-item rows later. This snapshot keeps
    the pre-rule parsed data available so rules can be re-applied without
    running OCR/VLM again.
    """
    parsed = _copy_parsed(parsed_data)
    line_items = _line_items(parsed)
    has_extracted_values = any(
        parsed.get(key) not in (None, "", [])
        for key in (
            "supplier_name_extracted",
            "invoice_number",
            "invoice_date",
            "subtotal",
            "tax_amount",
            "total_amount",
            "currency",
        )
    )
    if not line_items and not has_extracted_values:
        return attempts

    updated_attempts = list(attempts or [])
    if updated_attempts:
        updated_attempts[0] = {
            **updated_attempts[0],
            "parsed_data": parsed,
            "line_items": line_items,
            "confidence_score": parsed.get("confidence_score"),
        }
        return updated_attempts

    attempt = build_parse_attempt(
        strategy=strategy,
        text=text or "",
        parsed_data=parsed,
        ocr_used=False,
    )
    if not attempt:
        return updated_attempts
    return [attempt]


def build_parse_attempts_from_text_result(text_result: dict) -> list[dict]:
    attempts: list[dict] = []
    text = text_result.get("text") or ""
    pages = text_result.get("pages") or []
    first_page = pages[0] if pages else {}
    method = text_result.get("method")

    main_attempt = build_parse_attempt(
        strategy="pdf_text" if method == "pdf_text" else "standard_ocr",
        text=text,
        dpi=None if method == "pdf_text" else OCR_RENDER_DPI,
        ocr_used=bool(text_result.get("ocr_used")),
        ocr_confidence=text_result.get("ocr_confidence"),
        image_quality_score=text_result.get("image_quality_score"),
        candidate_score=first_page.get("ocr_candidate_score"),
        ocr_variant=first_page.get("ocr_variant"),
        ocr_psm=first_page.get("ocr_psm"),
    )
    if main_attempt:
        attempts.append(main_attempt)

    receipt_region_ocr = first_page.get("receipt_region_ocr") or {}
    receipt_text = receipt_region_ocr.get("text") or ""
    if _has_meaningful_text(receipt_text) and receipt_text.strip() != text.strip():
        receipt_attempt = build_parse_attempt(
            strategy="receipt_region_ocr",
            text=receipt_text,
            dpi=OCR_RENDER_DPI,
            ocr_used=True,
            ocr_confidence=receipt_region_ocr.get("ocr_confidence"),
            image_quality_score=first_page.get("image_quality_score"),
            candidate_score=first_page.get("ocr_candidate_score"),
            ocr_variant="receipt_regions_combined",
            ocr_psm=None,
        )
        if receipt_attempt:
            attempts.append(receipt_attempt)

    return attempts


def build_deep_region_parse_attempt(file_bytes: bytes, file_type: Optional[str]) -> tuple[Optional[dict], Optional[dict], Optional[str]]:
    try:
        deep_result = deep_extract_text_with_regions(file_bytes, file_type)
        parsed_data = _copy_parsed(deep_result.get("parsed_data") or {})
        attempt = build_parse_attempt(
            strategy="deep_region_ocr",
            text=deep_result.get("text") or "",
            parsed_data=parsed_data,
            dpi=DEEP_OCR_RENDER_DPI,
            ocr_used=True,
            ocr_confidence=deep_result.get("ocr_confidence"),
            image_quality_score=None,
            candidate_score=deep_result.get("ocr_confidence"),
            ocr_variant="deep_regions_combined",
            ocr_psm=None,
        )
        return attempt, deep_result, None
    except Exception as exc:
        return None, None, str(exc)


def normalise_parse_attempts(attempts: list[dict], *, selected_attempt: Optional[dict] = None) -> list[dict]:
    selected = selected_attempt or select_best_parse_attempt(attempts)
    normalised: list[dict] = []
    for index, attempt in enumerate(attempts, start=1):
        attempt_copy = deepcopy(attempt)
        attempt_copy["attempt_number"] = index
        attempt_copy["selected"] = attempt is selected
        attempt_copy["diagnostics"] = build_line_item_diagnostics(
            line_items=attempt_copy.get("line_items") or [],
            invoice_total=(attempt_copy.get("parsed_data") or {}).get("total_amount"),
        )
        normalised.append(attempt_copy)
    return normalised


def persist_parse_attempts(
    supabase,
    *,
    organisation_id: str,
    invoice_raw_id: str,
    invoice_extracted_id: Optional[str],
    attempts: list[dict],
    selected_attempt: Optional[dict] = None,
) -> dict:
    normalised = normalise_parse_attempts(attempts, selected_attempt=selected_attempt)
    result = {
        "parse_attempts_found_count": len(normalised),
        "parse_attempts_inserted_count": 0,
        "parse_attempts_insert_error": None,
        "selected_parse_attempt_id": None,
    }

    try:
        supabase.table("invoice_parse_attempts").delete().eq(
            "invoice_raw_id",
            invoice_raw_id,
        ).execute()

        if not normalised:
            return result

        payload = []
        for attempt in normalised:
            parsed_data = _copy_parsed(attempt.get("parsed_data") or {})
            line_items = attempt.get("line_items") or _line_items(parsed_data)
            payload.append({
                "organisation_id": organisation_id,
                "invoice_raw_id": invoice_raw_id,
                "invoice_extracted_id": invoice_extracted_id,
                "attempt_number": attempt.get("attempt_number"),
                "strategy": attempt.get("strategy"),
                "dpi": attempt.get("dpi"),
                "ocr_variant": attempt.get("ocr_variant"),
                "ocr_psm": attempt.get("ocr_psm"),
                "ocr_used": attempt.get("ocr_used"),
                "ocr_confidence": attempt.get("ocr_confidence"),
                "image_quality_score": attempt.get("image_quality_score"),
                "candidate_score": attempt.get("candidate_score"),
                "confidence_score": attempt.get("confidence_score"),
                "parsed_data": parsed_data,
                "line_items": line_items,
                "text_preview": attempt.get("text_preview"),
                "selected": bool(attempt.get("selected")),
            })

        insert_res = supabase.table("invoice_parse_attempts").insert(payload).execute()
        inserted = insert_res.data or []
        result["parse_attempts_inserted_count"] = len(inserted or payload)
        selected_row = next((row for row in inserted if row.get("selected")), None)
        if selected_row:
            result["selected_parse_attempt_id"] = selected_row.get("id")
    except Exception as exc:
        result["parse_attempts_insert_error"] = str(exc)

    return result


def fetch_parse_attempts(supabase, *, invoice_raw_id: Optional[str]) -> tuple[list[dict], Optional[str]]:
    if not invoice_raw_id:
        return [], None

    attempts_res = (
        supabase
        .table("invoice_parse_attempts")
        .select(PARSE_ATTEMPT_SELECT)
        .eq("invoice_raw_id", invoice_raw_id)
        .order("attempt_number", desc=False)
        .order("created_at", desc=False)
        .execute()
    )
    attempts = attempts_res.data or []
    selected = next((attempt for attempt in attempts if attempt.get("selected")), None)
    return attempts, selected.get("id") if selected else None
