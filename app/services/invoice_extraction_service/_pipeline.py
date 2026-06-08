"""
_pipeline.py
-------------
Primary invoice extraction pipeline.

Group G from the original invoice_extraction_service.py:
  run_invoice_extraction — full OCR → parse → VLM fallback → DB persist pipeline
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from app.db.supabase_client import get_supabase_client
from app.services.audit_log import log_invoice_event
from app.services.document_jobs import (
    mark_job_completed,
    mark_job_failed,
    mark_job_stage,
    safe_update_invoice_raw_status,
)
from app.services.invoice_extraction.entity_detection import (
    classify_document_direction,
    name_matches_org,
    normalise_name,
)
from app.services.invoice_extraction.extraction_rules import looks_like_location_cluster
from app.services.invoice_extraction.supplier_parser import (
    extract_supplier_name,
    is_valid_supplier_candidate,
)
from app.services.ai_provider_fallback import extract_with_vlm_fallback
from app.services.invoice_extraction.vlm_parser import VLM_MERGE_FIELDS

# Published rates (USD per million tokens) — update when providers change pricing.
_COST_PER_MILLION: dict[str, dict[str, float]] = {
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.5-pro":   {"input": 1.25, "output": 10.00},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro":   {"input": 3.50, "output": 10.50},
    "gpt-4o":           {"input": 5.00, "output": 15.00},
    "gpt-4.1-mini":     {"input": 0.40, "output": 1.60},
    "claude-3-5-sonnet-latest": {"input": 3.00, "output": 15.00},
}

def _auto_reconcile_vat(parsed_data: dict, vat_rate: float = 0.15) -> None:
    """
    Detect whether extracted line item prices are VAT-inclusive or exclusive by
    Determine VAT treatment using the canonical decision tree:
      1. No VAT number on document → non-VAT supplier, no VAT claimed
      2. SUM(line_totals) ≈ doc_total → prices are VAT-INCLUSIVE → strip VAT from lines
      3. SUM × (1+rate) ≈ doc_total  → prices are EX-VAT → use as-is, derive VAT
      4. Neither matches → cannot determine, user sees Solve button

    VLM now returns prices EXACTLY as printed — this function normalises to ex-VAT.
    """
    from decimal import Decimal

    doc_total_raw = parsed_data.get("total_amount")
    line_items = parsed_data.get("line_items") or []
    vat_number = parsed_data.get("vat_number_extracted")

    if not doc_total_raw or not line_items:
        return

    try:
        doc_total = float(doc_total_raw)
    except (TypeError, ValueError):
        return

    line_sum = sum(float(it.get("line_total") or 0) for it in line_items)
    if line_sum <= 0 or doc_total <= 0:
        return

    # Case 1: No VAT number → non-VAT supplier, use line totals as-is
    if not vat_number:
        parsed_data["prices_include_vat_detected"] = None  # not applicable, not a DB enum value
        parsed_data["subtotal"] = round(line_sum, 2)
        parsed_data["tax_amount"] = 0.0
        return

    TOLERANCE = 0.03  # 3%

    # Case 2: Prices inclusive (SUM ≈ doc_total)
    diff_inclusive = abs(line_sum - doc_total) / doc_total

    # Case 3: Prices exclusive (SUM × (1+rate) ≈ doc_total)
    diff_exclusive = abs(line_sum * (1 + vat_rate) - doc_total) / doc_total

    if diff_inclusive <= diff_exclusive and diff_inclusive < TOLERANCE:
        # VAT-INCLUSIVE: strip VAT from printed prices → store ex-VAT
        parsed_data["prices_include_vat_detected"] = "inclusive"
        new_items = []
        scale = Decimal(str(1 + vat_rate))
        for it in line_items:
            raw_total = float(it.get("line_total") or 0)
            ex_total = round(float(Decimal(str(raw_total)) / scale), 2)
            raw_unit = float(it.get("unit_price") or 0)
            ex_unit = round(float(Decimal(str(raw_unit)) / scale), 4) if raw_unit else 0
            new_items.append({**it, "unit_price": ex_unit, "line_total": ex_total})
        parsed_data["line_items"] = new_items
        ex_sum = round(sum(it["line_total"] for it in new_items), 2)
        parsed_data["subtotal"] = ex_sum
        parsed_data["tax_amount"] = round(doc_total - ex_sum, 2)

    elif diff_exclusive < diff_inclusive and diff_exclusive < TOLERANCE:
        # EX-VAT: prices already ex-VAT → derive VAT from doc_total
        parsed_data["prices_include_vat_detected"] = "exclusive"
        parsed_data["subtotal"] = round(line_sum, 2)
        parsed_data["tax_amount"] = round(doc_total - line_sum, 2)

    # else: cannot determine — leave as-is, user sees Solve button


def _calc_cost_usd(model: str | None, input_tokens: int | None, output_tokens: int | None) -> float | None:
    if not model or input_tokens is None:
        return None
    # Match by prefix (e.g. "gemini-2.5-flash-001" → "gemini-2.5-flash")
    rates = None
    for key, r in _COST_PER_MILLION.items():
        if (model or "").startswith(key):
            rates = r
            break
    if not rates:
        return None
    cost = (input_tokens or 0) * rates["input"] / 1_000_000
    cost += (output_tokens or 0) * rates.get("output", 0) / 1_000_000
    return round(cost, 8)
from app.services.invoice_line_items import replace_invoice_line_items
from app.services.invoice_ocr_pipeline import (
    calculate_confidence,
    extract_text_with_fallback,
    parse_invoice_fields,
)
from app.services.invoice_parse_attempts import (
    build_parse_attempts_from_text_result,
    ensure_parsed_data_attempt,
    persist_parse_attempts,
)
from app.services.invoice_previews import persist_preview_artifacts
from app.services.invoice_readiness import evaluate_invoice_readiness
from app.services.invoice_supplier_rules import (
    apply_supplier_processing_rules,
    fetch_supplier_processing_settings,
)
from app.services.invoice_data_builders import (
    MISSING_SUPPLIER_NOTE,
    MISSING_SUPPLIER_VALIDATION_STATUS,
    apply_missing_supplier_failure,
    build_extracted_document_profile,
    build_extracted_supplier_profile,
    build_supplier_create_payload,
    clear_organisation_vat_from_supplier,
    merge_supplier_recovery_fields,
    utc_now_iso,
)
from app.services.organisation_extraction_settings import get_organisation_extraction_settings
from ._helpers import (
    get_raw_invoice,
    get_organisation,
    persist_invoice_page_group,
    rename_invoice_file_after_extraction,
    store_basic_document_page_snapshot,
    update_invoice_raw_grouping,
    _preprocessing_notes_text,
)

try:
    supabase = get_supabase_client()
except Exception:
    supabase = None  # type: ignore[assignment]


def _ocr_dependency_issue(text_result: dict) -> str | None:
    dependency = text_result.get("ocr_dependency") or {}
    if dependency.get("available") is False:
        message = dependency.get("message") or "Tesseract OCR is unavailable."
        command = dependency.get("command") or "tesseract"
        return f"OCR engine unavailable ({command}): {message}"

    errors = text_result.get("ocr_errors") or []
    if errors and not (text_result.get("text") or "").strip():
        first = errors[0]
        message = first.get("message") or "OCR failed before any text could be extracted."
        return f"OCR engine failed: {message}"

    return None


_IMAGE_MAGIC: list[bytes] = [
    b"\xff\xd8\xff",            # JPEG
    b"\x89PNG\r\n\x1a\n",      # PNG
    b"GIF87a", b"GIF89a",       # GIF
    b"II\x2a\x00", b"MM\x00\x2a",  # TIFF (little/big endian)
]


def _is_image_bytes(data: bytes) -> bool:
    """Detect common image formats by magic bytes — independent of the stored MIME type."""
    head = data[:12]
    if any(head.startswith(sig) for sig in _IMAGE_MAGIC):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":  # WEBP
        return True
    if head[4:8] == b"ftyp":  # HEIC / HEIF / AVIF
        return True
    return False


def _append_validation_note(existing: str | None, note: str) -> str:
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing} {note}"


def run_invoice_extraction(
    *,
    invoice_raw_id: str,
    organisation_id: Optional[str] = None,
    job_id: Optional[str] = None,
    extraction_strategy: Optional[str] = None,
) -> dict:
    print("RUN INVOICE EXTRACTION:", {"invoice_raw_id": invoice_raw_id, "organisation_id": organisation_id, "job_id": job_id})

    raw = get_raw_invoice(invoice_raw_id)
    org_id = organisation_id or raw.get("organisation_id")

    if not org_id:
        raise HTTPException(status_code=400, detail="Missing organisation_id")

    file_path = raw.get("file_path")
    if not file_path:
        safe_update_invoice_raw_status(supabase, invoice_raw_id=invoice_raw_id, parse_status="failed")
        raise HTTPException(status_code=400, detail="Missing file_path on invoices_raw row")

    log_invoice_event(
        supabase,
        organisation_id=org_id,
        invoice_raw_id=invoice_raw_id,
        job_id=job_id,
        event_type="extraction_started",
        stage="download",
        actor_type="worker" if job_id else "api",
        notes="Invoice extraction started.",
    )

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as e:
        safe_update_invoice_raw_status(supabase, invoice_raw_id=invoice_raw_id, parse_status="failed")
        raise HTTPException(status_code=400, detail=f"Storage download error: {str(e)}")

    preview_result: dict = {}
    parse_attempts: list[dict] = []
    parse_attempt_result: dict = {}

    try:
        if job_id:
            mark_job_stage(supabase, job_id=job_id, stage="text_extraction")

        text_result = extract_text_with_fallback(file_bytes, raw.get("file_type"))
        text = text_result["text"]
        preview_result = persist_preview_artifacts(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            text_result=text_result,
        )

        if preview_result and not preview_result.get("error"):
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                job_id=job_id,
                event_type="preview_generated",
                stage="text_extraction",
                actor_type="worker" if job_id else "api",
                new_value=preview_result,
                notes="Generated original and processed page preview artifacts.",
            )

        if text_result.get("ocr_used"):
            first_page = (text_result.get("pages") or [{}])[0]
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                job_id=job_id,
                event_type="preprocessing_completed",
                stage="text_extraction",
                actor_type="worker" if job_id else "api",
                new_value={
                    "crop_applied": first_page.get("crop_applied"),
                    "deskew_applied": first_page.get("deskew_applied"),
                    "preprocessing_notes": first_page.get("preprocessing_notes") or [],
                    "crop_box": first_page.get("crop_box"),
                    "crop_area_ratio": first_page.get("crop_area_ratio"),
                    "original_preview_path": first_page.get("original_preview_path"),
                    "processed_preview_path": first_page.get("processed_preview_path"),
                    "image_quality_score": first_page.get("image_quality_score"),
                },
                notes=_preprocessing_notes_text(first_page.get("preprocessing_notes")),
            )

            receipt_region_ocr = first_page.get("receipt_region_ocr") or {}
            if receipt_region_ocr:
                log_invoice_event(
                    supabase,
                    organisation_id=org_id,
                    invoice_raw_id=invoice_raw_id,
                    job_id=job_id,
                    event_type="receipt_region_ocr_completed",
                    stage="text_extraction",
                    actor_type="worker" if job_id else "api",
                    new_value={
                        "regions_attempted": receipt_region_ocr.get("regions_attempted") or [],
                        "confidence_by_region": receipt_region_ocr.get("confidence_by_region") or {},
                        "combined_text_length": receipt_region_ocr.get("text_length") or 0,
                        "selected_strategy": receipt_region_ocr.get("strategy"),
                        "ocr_errors": receipt_region_ocr.get("ocr_errors") or [],
                    },
                    notes="Receipt OCR completed using header, middle, bottom and full processed regions.",
                )

        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            job_id=job_id,
            event_type="ocr_completed" if text_result.get("ocr_used") else "pdf_text_extracted",
            stage="text_extraction",
            actor_type="worker" if job_id else "api",
            new_value={
                "method": text_result.get("method"),
                "ocr_used": text_result.get("ocr_used"),
                "text_length": len(text or ""),
                "page_count": text_result.get("page_count"),
                "ocr_confidence": text_result.get("ocr_confidence"),
                "image_quality_score": text_result.get("image_quality_score"),
                "quality_notes": text_result.get("quality_notes") or [],
                "ocr_dependency": text_result.get("ocr_dependency"),
                "ocr_errors": text_result.get("ocr_errors") or [],
            },
        )

        ocr_dependency_issue = _ocr_dependency_issue(text_result)
        if ocr_dependency_issue:
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                job_id=job_id,
                event_type="ocr_dependency_unavailable",
                stage="text_extraction",
                actor_type="worker" if job_id else "api",
                new_value={
                    "ocr_dependency": text_result.get("ocr_dependency"),
                    "ocr_errors": text_result.get("ocr_errors") or [],
                    "text_length": len(text or ""),
                },
                notes=ocr_dependency_issue,
            )

        if job_id:
            mark_job_stage(supabase, job_id=job_id, stage="field_extraction")

        organisation_settings = get_organisation_extraction_settings(org_id)
        strategy = extraction_strategy or organisation_settings.get("extraction_strategy") or "auto_group"
        vlm_enabled = organisation_settings.get("vlm_enabled", False)

        parsed_data = parse_invoice_fields(text)
        if ocr_dependency_issue:
            parsed_data["validation_status"] = "needs_review"
            parsed_data["validation_notes"] = _append_validation_note(
                parsed_data.get("validation_notes"),
                ocr_dependency_issue,
            )

        page_count = text_result.get("page_count") or 1
        page_numbers = list(range(1, page_count + 1))
        try:
            update_invoice_raw_grouping(
                invoice_raw_id=invoice_raw_id,
                page_numbers=page_numbers,
                strategy=strategy,
                total_pages=page_count,
            )
        except Exception as _grouping_exc:
            print(f"GROUPING METADATA UPDATE FAILED (non-fatal): {_grouping_exc}")

        if page_count > 1 or strategy != "auto_group":
            try:
                persist_invoice_page_group(
                    invoice_raw_id=invoice_raw_id,
                    page_numbers=page_numbers,
                    strategy=strategy,
                    supplier_detected=parsed_data.get("supplier_name_extracted"),
                    confidence=parsed_data.get("confidence_score"),
                )
            except Exception as _page_group_exc:
                print(f"PERSIST PAGE GROUP FAILED (non-fatal): {_page_group_exc}")

        # Fetch org early — needed to detect when Tesseract picked up the org's own name
        # as the supplier (a common AP error when the customer block appears before the issuer).
        organisation = get_organisation(org_id)

        # Routing strategy:
        #   Digital PDFs (ocr_used=False) → pdfplumber + regex pipeline; VLM only as fallback.
        #   Scanned PDFs (ocr_used=True, PDF MIME) → VLM always (Tesseract on scans is unreliable;
        #     the `vlm_enabled` gate previously blocked this — removed as OCR adds cost with no benefit).
        #   Images (JPEG/PNG/WEBP/HEIC) → VLM always, regardless of vlm_enabled.
        #     If no API key is configured, extract_with_vlm_fallback returns None gracefully.
        is_image_file = (
            (raw.get("file_type") or "").startswith("image/")
            or _is_image_bytes(file_bytes)
        )
        is_scanned_pdf = text_result.get("ocr_used", False) and not is_image_file
        force_vlm = (
            (strategy == "vlm" and vlm_enabled)
            or is_image_file   # always VLM for photos
            or is_scanned_pdf  # always VLM for scanned PDFs — OCR results on scans are unreliable
        )
        parsed_supplier_candidate = parsed_data.get("supplier_name_extracted")
        supplier_candidate_invalid = bool(
            parsed_supplier_candidate
            and not is_valid_supplier_candidate(str(parsed_supplier_candidate))
        )
        vlm_should_try = (
            force_vlm
            or parsed_data.get("confidence_score", 0) < 0.70
            or not parsed_data.get("invoice_number")
            or not parsed_data.get("total_amount")
            or not parsed_data.get("supplier_name_extracted")
            or supplier_candidate_invalid
            # If OCR extracted the org's own name as the supplier, force VLM —
            # the platform VLM fallback has image context to identify the issuer.
            or name_matches_org(parsed_data.get("supplier_name_extracted"), organisation)
            # If OCR extracted what looks like a suburb/area cluster instead of a
            # business name (e.g. "COWIES HILL EURIKA"), fall back to VLM which has
            # image context to find the real invoice issuer.
            or looks_like_location_cluster(parsed_data.get("supplier_name_extracted") or "")
            # No line items from OCR: VLM has image context to find line items the
            # regex pipeline missed (receipts, non-standard tables, etc.).
            or (
                not parsed_data.get("line_items")
                and parsed_data.get("total_amount")
            )
        )

        _extraction_input_tokens: int | None = None
        _extraction_output_tokens: int | None = None
        _extraction_model: str | None = None

        if vlm_should_try:
            vlm_result = extract_with_vlm_fallback(
                file_bytes,
                raw.get("file_type"),
                organisation_id=org_id,
            )
            # Capture token usage from VLM response
            _vlm_usage = vlm_result.get("usage") or {}
            _extraction_input_tokens = _vlm_usage.get("input_tokens")
            _extraction_output_tokens = _vlm_usage.get("output_tokens")
            _extraction_model = vlm_result.get("model")
            vlm_data = vlm_result.get("data")
            if vlm_data is not None:
                vlm_confidence = vlm_data.get("confidence_score", 0)
                tesseract_confidence = parsed_data.get("confidence_score", 0)

                for field in VLM_MERGE_FIELDS:
                    vlm_value = vlm_data.get(field)
                    if vlm_value is not None and vlm_value != [] and vlm_value != "":
                        if not parsed_data.get(field) or vlm_confidence > tesseract_confidence:
                            parsed_data[field] = vlm_value

                parsed_data["confidence_score"] = calculate_confidence(parsed_data)

                log_invoice_event(
                    supabase,
                    organisation_id=org_id,
                    invoice_raw_id=invoice_raw_id,
                    job_id=job_id,
                    event_type="vlm_extraction_completed",
                    stage="field_extraction",
                    actor_type="worker" if job_id else "api",
                    new_value={
                        "vlm_confidence": vlm_confidence,
                        "tesseract_confidence": tesseract_confidence,
                        "merged_confidence": parsed_data.get("confidence_score"),
                        "vlm_supplier": vlm_data.get("supplier_name_extracted"),
                        "vlm_invoice_number": vlm_data.get("invoice_number"),
                        "vlm_total": vlm_data.get("total_amount"),
                        "vlm_line_items_count": len(vlm_data.get("line_items") or []),
                        "vlm_provider": vlm_result.get("provider"),
                        "vlm_model": vlm_result.get("model"),
                        "vlm_attempts": vlm_result.get("attempts") or [],
                    },
                    notes=f"VLM fallback merged via {vlm_result.get('provider') or 'unknown provider'}. VLM confidence={vlm_confidence:.2f}, Tesseract confidence={tesseract_confidence:.2f}.",
                )
            else:
                # VLM was needed but returned None — API key missing, rate-limited, or an error
                # was silently swallowed inside extract_with_gemini. Log so it shows in the Audit
                # trail and operators can distinguish "VLM ran, found nothing" from "VLM never ran".
                log_invoice_event(
                    supabase,
                    organisation_id=org_id,
                    invoice_raw_id=invoice_raw_id,
                    job_id=job_id,
                    event_type="vlm_skipped",
                    stage="field_extraction",
                    actor_type="worker" if job_id else "api",
                    new_value={
                        "tesseract_confidence": parsed_data.get("confidence_score", 0),
                        "missing_supplier": not bool(parsed_data.get("supplier_name_extracted")),
                        "missing_invoice_number": not bool(parsed_data.get("invoice_number")),
                        "missing_total": not bool(parsed_data.get("total_amount")),
                        "vlm_failure_reason": vlm_result.get("reason"),
                        "vlm_error": vlm_result.get("error"),
                        "vlm_error_type": vlm_result.get("error_type"),
                        "mime_type": vlm_result.get("mime_type"),
                        "vlm_provider": vlm_result.get("provider"),
                        "vlm_model": vlm_result.get("model"),
                        "vlm_attempts": vlm_result.get("attempts") or [],
                    },
                    notes=f"VLM fallback was needed but could not complete: {vlm_result.get('reason') or 'unknown_error'}.",
                )

                # Image files have no text layer — Tesseract produces no usable data.
                # VLM is the only viable extractor for photos; fail hard instead of saving garbage.
                if is_image_file:
                    reason = vlm_result.get("reason") or "unknown_error"
                    raise ValueError(
                        f"Image files require VLM extraction, but VLM could not complete "
                        f"({reason}). Please check your VLM integration settings."
                    )

        supplier_recovery_result = {"applied": False, "fields": []}
        if not parsed_data.get("supplier_name_extracted"):
            supplier_recovery_result = merge_supplier_recovery_fields(parsed_data, text_result)
            if supplier_recovery_result.get("applied"):
                parsed_data["confidence_score"] = calculate_confidence(parsed_data)
                log_invoice_event(
                    supabase,
                    organisation_id=org_id,
                    invoice_raw_id=invoice_raw_id,
                    job_id=job_id,
                    event_type="supplier_recovery_ocr_applied",
                    stage="field_extraction",
                    actor_type="worker" if job_id else "api",
                    new_value=supplier_recovery_result,
                    notes="Missing supplier fields were recovered from full-page OCR without changing totals or line items.",
                )

        # organisation already fetched above (before VLM trigger) — reused here
        direction_result = classify_document_direction(text, organisation)

        parsed_data["issuer_name_extracted"] = direction_result.issuer_name
        parsed_data["recipient_name_extracted"] = direction_result.recipient_name
        parsed_data["document_direction"] = direction_result.document_direction
        parsed_data["organisation_match_status"] = direction_result.organisation_match_status
        parsed_data["validation_status"] = direction_result.validation_status
        parsed_data["validation_notes"] = direction_result.validation_notes
        if ocr_dependency_issue:
            parsed_data["validation_status"] = "needs_review"
            parsed_data["validation_notes"] = _append_validation_note(
                parsed_data.get("validation_notes"),
                ocr_dependency_issue,
            )

        original_supplier_name = parsed_data.get("supplier_name_extracted")
        supplier_correction_reason = None

        # Correct the common AP extraction error where the parser picks the
        # recipient/customer block as the supplier. In APPayPal, "supplier" means
        # the invoice issuer/vendor, not the recipient/customer.
        original_supplier_norm = normalise_name(original_supplier_name)
        issuer_norm = normalise_name(direction_result.issuer_name)
        recipient_norm = normalise_name(direction_result.recipient_name)

        if direction_result.issuer_name and original_supplier_norm == recipient_norm and recipient_norm:
            if direction_result.document_direction == "customer_sales_invoice":
                parsed_data["supplier_name_extracted"] = None
                supplier_correction_reason = (
                    "Original supplier candidate matched the invoice recipient. "
                    "Document appears to be a customer sales invoice, so supplier was cleared."
                )
            else:
                parsed_data["supplier_name_extracted"] = direction_result.issuer_name
                supplier_correction_reason = (
                    "Original supplier candidate matched the invoice recipient. "
                    "Supplier corrected to detected invoice issuer."
                )
        elif (
            direction_result.document_direction == "supplier_invoice_payable"
            and direction_result.issuer_name
            and not parsed_data.get("supplier_name_extracted")
        ):
            parsed_data["supplier_name_extracted"] = direction_result.issuer_name
            supplier_correction_reason = (
                "Supplier was missing. Supplier set to detected invoice issuer "
                "because selected organisation appears to be the recipient."
            )
        elif (
            direction_result.document_direction == "supplier_invoice_payable"
            and direction_result.issuer_name
            and issuer_norm
            and original_supplier_norm
            and original_supplier_norm != issuer_norm
        ):
            parsed_data["supplier_name_extracted"] = direction_result.issuer_name
            supplier_correction_reason = (
                "Supplier candidate differed from detected invoice issuer. "
                "Supplier corrected to issuer because selected organisation appears to be the recipient."
            )

        rejected_supplier_candidate = None
        current_supplier_name = parsed_data.get("supplier_name_extracted")
        if current_supplier_name and not is_valid_supplier_candidate(str(current_supplier_name)):
            rejected_supplier_candidate = current_supplier_name
            recovered_supplier_name = extract_supplier_name(text)
            if recovered_supplier_name and is_valid_supplier_candidate(recovered_supplier_name):
                parsed_data["supplier_name_extracted"] = recovered_supplier_name
                supplier_correction_reason = (
                    "Supplier candidate looked like a date or document metadata. "
                    "Supplier recovered from the document header."
                )
            else:
                parsed_data["supplier_name_extracted"] = None
                if parsed_data.get("validation_status") != MISSING_SUPPLIER_VALIDATION_STATUS:
                    parsed_data["validation_status"] = "needs_review"
                rejection_note = (
                    f"Rejected supplier candidate '{rejected_supplier_candidate}' because it looked like "
                    "a date or document metadata. Manual supplier review is required."
                )
                parsed_data["validation_notes"] = (
                    (parsed_data.get("validation_notes") + " " if parsed_data.get("validation_notes") else "")
                    + rejection_note
                )
                parsed_data["supplier_candidate_rejected"] = True

        if direction_result.confidence_adjustment:
            parsed_data["confidence_score"] = round(
                max(0.0, min(1.0, (parsed_data.get("confidence_score") or 0) + direction_result.confidence_adjustment)),
                2,
            )

        vat_guard_result = clear_organisation_vat_from_supplier(parsed_data, organisation)
        if vat_guard_result:
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                job_id=job_id,
                event_type="supplier_vat_cleared_organisation_match",
                stage="entity_validation",
                actor_type="worker" if job_id else "api",
                field_name="vat_number_extracted",
                old_value=vat_guard_result.get("cleared_vat_number"),
                new_value=None,
                notes=vat_guard_result.get("note"),
            )

        missing_supplier_failure = (
            False
            if parsed_data.get("supplier_candidate_rejected")
            else apply_missing_supplier_failure(parsed_data)
        )

        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            job_id=job_id,
            event_type="document_direction_classified",
            stage="entity_validation",
            actor_type="worker" if job_id else "api",
            new_value={
                "issuer_name": direction_result.issuer_name,
                "recipient_name": direction_result.recipient_name,
                "document_direction": direction_result.document_direction,
                "organisation_match_status": direction_result.organisation_match_status,
                "validation_status": direction_result.validation_status,
                "validation_notes": direction_result.validation_notes,
            },
            notes=direction_result.validation_notes,
        )

        if supplier_correction_reason:
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                job_id=job_id,
                event_type="supplier_role_corrected",
                stage="entity_validation",
                actor_type="worker" if job_id else "api",
                field_name="supplier_name_extracted",
                old_value={"supplier_name_extracted": original_supplier_name},
                new_value={
                    "supplier_name_extracted": parsed_data.get("supplier_name_extracted"),
                    "issuer_name": direction_result.issuer_name,
                    "recipient_name": direction_result.recipient_name,
                    "document_direction": direction_result.document_direction,
                },
                notes=supplier_correction_reason,
            )

        if rejected_supplier_candidate:
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                job_id=job_id,
                event_type="supplier_candidate_rejected",
                stage="entity_validation",
                actor_type="worker" if job_id else "api",
                field_name="supplier_name_extracted",
                old_value={"supplier_name_extracted": rejected_supplier_candidate},
                new_value={"supplier_name_extracted": parsed_data.get("supplier_name_extracted")},
                notes="Rejected supplier candidate because it looked like a date or document metadata.",
            )

        if missing_supplier_failure:
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                job_id=job_id,
                event_type="supplier_missing_failed",
                stage="entity_validation",
                actor_type="worker" if job_id else "api",
                new_value={
                    "validation_status": parsed_data.get("validation_status"),
                    "supplier_recovery": supplier_recovery_result,
                },
                notes=MISSING_SUPPLIER_NOTE,
            )

        ocr_quality_needs_review = False
        if text_result.get("ocr_used"):
            ocr_confidence = text_result.get("ocr_confidence")
            image_quality_score = text_result.get("image_quality_score")
            quality_notes = text_result.get("quality_notes") or []

            if ocr_confidence is not None and ocr_confidence < 0.55:
                ocr_quality_needs_review = True
            if image_quality_score is not None and image_quality_score < 0.45:
                ocr_quality_needs_review = True
            if len(text or "") < 80:
                ocr_quality_needs_review = True

            if ocr_quality_needs_review:
                quality_note = (
                    "OCR/image quality is low. Manual review is required. "
                    f"OCR confidence={ocr_confidence}; image quality={image_quality_score}; notes={quality_notes}."
                )
                if parsed_data.get("validation_status") != MISSING_SUPPLIER_VALIDATION_STATUS:
                    parsed_data["validation_status"] = "needs_review"
                parsed_data["validation_notes"] = (
                    (parsed_data.get("validation_notes") + " " if parsed_data.get("validation_notes") else "")
                    + quality_note
                )

                log_invoice_event(
                    supabase,
                    organisation_id=org_id,
                    invoice_raw_id=invoice_raw_id,
                    job_id=job_id,
                    event_type="ocr_quality_flagged",
                    stage="text_extraction",
                    actor_type="worker" if job_id else "api",
                    new_value={
                        "ocr_confidence": ocr_confidence,
                        "image_quality_score": image_quality_score,
                        "quality_notes": quality_notes,
                        "text_length": len(text or ""),
                    },
                    notes=quality_note,
                )

        extraction_needs_review = (
            parsed_data.get("confidence_score", 0) < 0.70
            or not parsed_data.get("invoice_number")
            or not parsed_data.get("total_amount")
            or not parsed_data.get("supplier_name_extracted")
            or parsed_data.get("validation_status") != "passed"
            or ocr_quality_needs_review
        )

        parse_attempts = ensure_parsed_data_attempt(
            build_parse_attempts_from_text_result(text_result),
            parsed_data=parsed_data,
            text=text,
        )

        store_basic_document_page_snapshot(
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            job_id=job_id,
            file_bytes=file_bytes,
            file_type=raw.get("file_type"),
            text_result=text_result,
            parsed_data=parsed_data,
        )
    except Exception as e:
        safe_update_invoice_raw_status(supabase, invoice_raw_id=invoice_raw_id, parse_status="failed")
        raise HTTPException(status_code=400, detail=f"Invoice extraction failed: {str(e)}")

    # ── Auto-reconcile VAT treatment ─────────────────────────────────────────
    # Compare SUM(line_totals) against document total to determine whether
    # prices are VAT-inclusive or exclusive, then correct accordingly.
    # This prevents the "Solve" prompt for systematic VAT differences.
    _auto_reconcile_vat(parsed_data, vat_rate=0.15)

    extracted_payload = {
        "organisation_id": org_id,
        "invoice_raw_id": invoice_raw_id,
        "supplier_id": raw.get("supplier_id"),
        "supplier_name_extracted": parsed_data.get("supplier_name_extracted"),
        "invoice_number": parsed_data.get("invoice_number"),
        "invoice_date": parsed_data.get("invoice_date"),
        "due_date": parsed_data.get("due_date"),
        "subtotal": parsed_data.get("subtotal"),
        "tax_amount": parsed_data.get("tax_amount"),
        "total_amount": parsed_data.get("total_amount"),
        "currency": parsed_data.get("currency"),
        "confidence_score": parsed_data.get("confidence_score"),
        "supplier_del_address_extracted": parsed_data.get("supplier_del_address_extracted"),
        "supplier_pos_address_extracted": parsed_data.get("supplier_pos_address_extracted"),
        "supplier_email_extracted": parsed_data.get("supplier_email_extracted"),
        "supplier_acc_email_extracted": parsed_data.get("supplier_acc_email_extracted"),
        "supplier_telephone_extracted": parsed_data.get("supplier_telephone_extracted"),
        "supplier_fax_extracted": parsed_data.get("supplier_fax_extracted"),
        "supplier_cell_extracted": parsed_data.get("supplier_cell_extracted"),
        "supplier_website_extracted": parsed_data.get("supplier_website_extracted"),
        "vat_number_extracted": parsed_data.get("vat_number_extracted"),
        "cus_code_extracted": parsed_data.get("cus_code_extracted"),
        "company_registration_number_extracted": parsed_data.get("company_registration_number_extracted"),
        "issuer_name_extracted": parsed_data.get("issuer_name_extracted"),
        "recipient_name_extracted": parsed_data.get("recipient_name_extracted"),
        "document_direction": parsed_data.get("document_direction"),
        "organisation_match_status": parsed_data.get("organisation_match_status"),
        "validation_status": parsed_data.get("validation_status"),
        "validation_notes": parsed_data.get("validation_notes"),
        "review_status": "needs_info" if extraction_needs_review else "pending",
        "notes": (
            parsed_data.get("validation_notes")
            if extraction_needs_review and parsed_data.get("validation_notes")
            else "Low-confidence extraction. Manual review required."
            if extraction_needs_review
            else "Extracted/re-extracted by FastAPI invoice parser."
        ),
        "bank_account_name_extracted": parsed_data.get("bank_account_name_extracted"),
        "bank_name_extracted": parsed_data.get("bank_name_extracted"),
        "bank_account_number_extracted": parsed_data.get("bank_account_number_extracted"),
        "bank_branch_code_extracted": parsed_data.get("bank_branch_code_extracted"),
        "bank_swift_code_extracted": parsed_data.get("bank_swift_code_extracted"),
        "document_type": parsed_data.get("document_type") or "tax_invoice",
        "document_count": parsed_data.get("document_count") or 1,
        "updated_at": utc_now_iso(),
        "extraction_input_tokens": _extraction_input_tokens,
        "extraction_output_tokens": _extraction_output_tokens,
        "extraction_model": _extraction_model,
        "extraction_cost_usd": _calc_cost_usd(_extraction_model, _extraction_input_tokens, _extraction_output_tokens),
        "prices_include_vat_detected": parsed_data.get("prices_include_vat_detected"),
    }

    auto_linked_supplier_id = None
    auto_link_match_result = None

    # Auto-link supplier when the organisation's configured identity threshold is met.
    if not extracted_payload.get("supplier_id"):
        try:
            from app.services.supplier_matcher import find_supplier_match_result  # noqa: PLC0415
            match_result = find_supplier_match_result(
                supabase,
                org_id=org_id,
                invoice_total=parsed_data.get("total_amount"),
                supplier_name_extracted=parsed_data.get("supplier_name_extracted"),
                vat_number_extracted=parsed_data.get("vat_number_extracted"),
                company_registration_number_extracted=parsed_data.get("company_registration_number_extracted"),
                cus_code_extracted=parsed_data.get("cus_code_extracted"),
                bank_account_number_extracted=parsed_data.get("bank_account_number_extracted"),
                supplier_telephone_extracted=parsed_data.get("supplier_telephone_extracted") or parsed_data.get("supplier_cell_extracted"),
                supplier_email_extracted=parsed_data.get("supplier_email_extracted"),
                supplier_acc_email_extracted=parsed_data.get("supplier_acc_email_extracted"),
            )
            if match_result and match_result.get("auto_link"):
                matched_id = str(match_result["supplier_id"])
                extracted_payload["supplier_id"] = matched_id
                auto_linked_supplier_id = matched_id
                auto_link_match_result = match_result
                try:
                    supabase.table("invoices_raw").update({
                        "supplier_id": matched_id,
                        "updated_at": utc_now_iso(),
                    }).eq("id", invoice_raw_id).execute()
                except Exception as raw_link_exc:
                    print(f"INVOICES_RAW AUTO-LINK UPDATE FAILED (non-fatal): {raw_link_exc}")
                print(f"AUTO-LINKED supplier {matched_id} via supplier identity threshold")
        except Exception as exc:
            print(f"SUPPLIER AUTO-MATCH FAILED (non-fatal): {exc}")

    supplier_settings = fetch_supplier_processing_settings(
        supabase,
        extracted_payload.get("supplier_id"),
    )
    supplier_rule_result = apply_supplier_processing_rules(parsed_data, supplier_settings)
    extracted_payload.update(supplier_rule_result["invoice_patch"])
    line_items = supplier_rule_result["line_items"]

    print("EXTRACTED PAYLOAD TO SAVE:", extracted_payload)

    if job_id:
        mark_job_stage(supabase, job_id=job_id, stage="save_extracted_invoice")

    existing_res = (
        supabase
        .table("invoices_extracted")
        .select("id, confidence_score")
        .eq("invoice_raw_id", invoice_raw_id)
        .limit(1)
        .execute()
    )

    if existing_res.data:
        extracted_invoice_id = existing_res.data[0]["id"]
        old_confidence = existing_res.data[0].get("confidence_score")

        update_res = (
            supabase
            .table("invoices_extracted")
            .update(extracted_payload)
            .eq("id", extracted_invoice_id)
            .execute()
        )
        print("UPDATED INVOICES_EXTRACTED:", update_res.data)

        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            invoice_extracted_id=extracted_invoice_id,
            job_id=job_id,
            event_type="invoice_extracted_updated",
            stage="save_extracted_invoice",
            actor_type="worker" if job_id else "api",
            confidence_before=old_confidence,
            confidence_after=parsed_data.get("confidence_score"),
            notes="Updated existing extracted invoice row.",
        )
    else:
        insert_res = supabase.table("invoices_extracted").insert(extracted_payload).execute()
        extracted_invoice_id = insert_res.data[0]["id"] if insert_res.data else None
        print("INSERTED INVOICES_EXTRACTED:", insert_res.data)

        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            invoice_extracted_id=extracted_invoice_id,
            job_id=job_id,
            event_type="invoice_extracted_created",
            stage="save_extracted_invoice",
            actor_type="worker" if job_id else "api",
            confidence_after=parsed_data.get("confidence_score"),
            notes="Created extracted invoice row.",
        )

    if auto_linked_supplier_id and extracted_invoice_id:
        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            invoice_extracted_id=extracted_invoice_id,
            job_id=job_id,
            event_type="supplier_auto_linked",
            stage="supplier_auto_link",
            actor_type="worker" if job_id else "api",
            new_value={
                "supplier_id": auto_linked_supplier_id,
                "match_type": "identity_threshold",
                "match_count": auto_link_match_result.get("match_count") if auto_link_match_result else None,
                "threshold": auto_link_match_result.get("threshold") if auto_link_match_result else None,
                "evidence": auto_link_match_result.get("evidence") if auto_link_match_result else [],
            },
            notes="Supplier auto-linked from extracted supplier identity evidence.",
        )

    if extracted_invoice_id:
        if job_id:
            mark_job_stage(supabase, job_id=job_id, stage="save_line_items")

        line_item_diagnostics = replace_invoice_line_items(
            supabase,
            invoice_extracted_id=extracted_invoice_id,
            organisation_id=org_id,
            line_items=line_items,
            invoice_total=parsed_data.get("total_amount"),
            delete_when_empty=True,
            raise_on_error=True,
        )

        if extracted_payload.get("supplier_id"):
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                invoice_extracted_id=extracted_invoice_id,
                job_id=job_id,
                event_type="supplier_rules_applied",
                stage="supplier_processing_rules",
                actor_type="worker" if job_id else "api",
                new_value={
                    "supplier_id": extracted_payload.get("supplier_id"),
                    "invoice_patch": supplier_rule_result["invoice_patch"],
                    **line_item_diagnostics,
                },
                notes="Supplier processing rules applied during extraction.",
            )

        if line_items:
            print("INSERTED LINE ITEMS:", line_item_diagnostics)

            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                invoice_extracted_id=extracted_invoice_id,
                job_id=job_id,
                event_type="line_items_extracted",
                stage="save_line_items",
                actor_type="worker" if job_id else "api",
                new_value=line_item_diagnostics,
            )
        else:
            print("NO LINE ITEMS EXTRACTED")
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                invoice_extracted_id=extracted_invoice_id,
                job_id=job_id,
                event_type="line_items_missing",
                stage="save_line_items",
                actor_type="worker" if job_id else "api",
                notes="No line items were extracted.",
            )

    file_rename_result = rename_invoice_file_after_extraction(
        raw=raw,
        organisation_id=org_id,
        invoice_raw_id=invoice_raw_id,
        parsed_data=parsed_data,
    )

    readiness_result = None
    if extracted_invoice_id:
        if job_id:
            mark_job_stage(supabase, job_id=job_id, stage="save_parse_attempts")

        selected_parse_attempt = parse_attempts[0] if parse_attempts else None
        parse_attempt_result = persist_parse_attempts(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            invoice_extracted_id=extracted_invoice_id,
            attempts=parse_attempts,
            selected_attempt=selected_parse_attempt,
        )

        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            invoice_extracted_id=extracted_invoice_id,
            job_id=job_id,
            event_type=(
                "parse_attempts_persist_failed"
                if parse_attempt_result.get("parse_attempts_insert_error")
                else "parse_attempts_recorded"
            ),
            stage="save_parse_attempts",
            actor_type="worker" if job_id else "api",
            new_value=parse_attempt_result,
            notes=parse_attempt_result.get("parse_attempts_insert_error"),
        )

        readiness_result = evaluate_invoice_readiness(
            supabase,
            invoice_extracted_id=extracted_invoice_id,
            organisation_id=org_id,
            reason="Extraction completed.",
            actor_type="worker" if job_id else "api",
            job_id=job_id,
        )

        # STP accepts any trusted supplier link and still requires full readiness.
        if (
            extracted_payload.get("supplier_id")
            and readiness_result
            and (parsed_data.get("document_count") or 1) == 1
        ):
            from app.services.invoice_stp import attempt_invoice_stp  # noqa: PLC0415

            stp_result = attempt_invoice_stp(
                supabase=supabase,
                org_id=org_id,
                invoice_id=extracted_invoice_id,
                readiness_result=readiness_result,
            )
            _stp_status = stp_result.get("status")
            if _stp_status in {"posted", "failed", "not_eligible"}:
                try:
                    _stp_event_map = {
                        "posted": "stp_auto_posted",
                        "failed": "stp_auto_post_failed",
                        "not_eligible": "stp_not_eligible",
                    }
                    log_invoice_event(
                        supabase,
                        organisation_id=org_id,
                        invoice_raw_id=invoice_raw_id,
                        invoice_extracted_id=extracted_invoice_id,
                        job_id=job_id,
                        event_type=_stp_event_map[_stp_status],
                        stage="stp_auto_post",
                        actor_type="system",
                        new_value=stp_result if _stp_status == "posted" else None,
                        notes=(
                            "Invoice auto-posted via Straight-Through Processing (STP)."
                            if _stp_status == "posted"
                            else f"STP skipped: {stp_result.get('reason')}"
                        ),
                    )
                except Exception as audit_exc:
                    print(f"STP AUDIT LOG FAILED (non-fatal): {audit_exc}")

    _doc_count = parsed_data.get("document_count") or 1
    _final_status = "needs_split" if _doc_count > 1 else "completed"

    safe_update_invoice_raw_status(
        supabase,
        invoice_raw_id=invoice_raw_id,
        parse_status=_final_status,
        extra={"parse_completed_at": utc_now_iso()},
    )

    log_invoice_event(
        supabase,
        organisation_id=org_id,
        invoice_raw_id=invoice_raw_id,
        invoice_extracted_id=extracted_invoice_id,
        job_id=job_id,
        event_type="extraction_completed",
        stage=_final_status,
        actor_type="worker" if job_id else "api",
        confidence_after=parsed_data.get("confidence_score"),
        notes=f"Invoice extraction completed. document_type={parsed_data.get('document_type')}, document_count={_doc_count}."
        if _doc_count > 1
        else "Invoice extraction completed.",
    )

    response = {
        "success": True,
        "status": "completed",
        "invoice_raw_id": invoice_raw_id,
        "extracted_invoice_id": extracted_invoice_id,
        "organisation_id": org_id,
        "job_id": job_id,
        "file_path": file_rename_result.get("file_path"),
        "file_name": file_rename_result.get("file_name"),
        "file_renamed": file_rename_result.get("renamed"),
        "file_rename_reason": file_rename_result.get("reason"),
        "preview_path": preview_result.get("preview_path"),
        "processed_preview_path": preview_result.get("processed_preview_path"),
        "parse_attempts": parse_attempts,
        **parse_attempt_result,
        "readiness": readiness_result,
        "text_preview": text[:2000],
        "supplier_name": parsed_data.get("supplier_name_extracted"),
        "extracted_supplier_profile": build_extracted_supplier_profile(parsed_data),
        "extracted_document_profile": build_extracted_document_profile(parsed_data),
        "supplier_create_payload": build_supplier_create_payload(
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            invoice_extracted_id=extracted_invoice_id,
            parsed_data=parsed_data,
        ),
        "supplier_endpoints": {
            "create_from_invoice": "/api/suppliers/from-invoice",
            "create": "/api/suppliers",
            "link": "/api/suppliers/link",
            "profile_from_invoice": (
                f"/api/suppliers/from-invoice/{extracted_invoice_id}"
                if extracted_invoice_id
                else None
            ),
        },
        "invoice_number": parsed_data.get("invoice_number"),
        "invoice_date": parsed_data.get("invoice_date"),
        "due_date": parsed_data.get("due_date"),
        "subtotal": parsed_data.get("subtotal"),
        "vat_amount": parsed_data.get("tax_amount"),
        "total_amount": parsed_data.get("total_amount"),
        "currency": parsed_data.get("currency"),
        "confidence_score": parsed_data.get("confidence_score"),
        "issuer_name": parsed_data.get("issuer_name_extracted"),
        "recipient_name": parsed_data.get("recipient_name_extracted"),
        "document_direction": parsed_data.get("document_direction"),
        "organisation_match_status": parsed_data.get("organisation_match_status"),
        "validation_status": parsed_data.get("validation_status"),
        "validation_notes": parsed_data.get("validation_notes"),
        "debug": {
            "ocr_method": text_result.get("method"),
            "ocr_used": text_result.get("ocr_used"),
            "text_preview": text[:2000],
        },
    }

    print("EXTRACT RESPONSE:", response)
    return response
