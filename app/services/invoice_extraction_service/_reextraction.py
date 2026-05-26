"""
_reextraction.py
-----------------
Deep-region re-extraction pipeline and background task wrapper.

Group H from the original invoice_extraction_service.py:
  run_invoice_re_extraction       — full deep OCR re-extraction pipeline
  run_reextract_job_background    — background task wrapper
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from app.db.supabase_client import get_supabase_client
from app.services.audit_log import log_invoice_event
from app.services.invoice_extraction.entity_detection import classify_document_direction
from app.services.invoice_extraction.vlm_parser import VLM_MERGE_FIELDS, extract_with_gemini
from app.services.invoice_line_items import build_line_item_diagnostics, replace_invoice_line_items
from app.services.invoice_ocr_pipeline import calculate_confidence
from app.services.invoice_parse_attempts import (
    build_deep_region_parse_attempt,
    ensure_parsed_data_attempt,
    fetch_parse_attempts,
    persist_parse_attempts,
)
from app.services.invoice_supplier_rules import (
    apply_supplier_processing_rules,
    fetch_supplier_processing_settings,
)
from app.services.invoice_data_builders import (
    MISSING_SUPPLIER_NOTE,
    _trim_region_text,
    apply_missing_supplier_failure,
    build_extracted_document_profile,
    build_extracted_supplier_profile,
    build_reextract_update,
)
from ._helpers import (
    get_raw_invoice,
    get_organisation,
    _stringify_http_detail,
    log_reextract_failure,
)
from ._job_tracking import update_reextract_job

try:
    supabase = get_supabase_client()
except Exception:
    supabase = None  # type: ignore[assignment]


def run_invoice_re_extraction(
    *,
    invoice_raw_id: str,
    organisation_id: Optional[str] = None,
    force_update: bool = False,
    job_id: Optional[str] = None,
) -> dict:
    org_id = organisation_id
    extracted_invoice_id: Optional[str] = None
    if job_id:
        update_reextract_job(job_id, status="running", stage="starting")

    raw = get_raw_invoice(invoice_raw_id)
    org_id = organisation_id or raw.get("organisation_id")

    if not org_id:
        raise HTTPException(status_code=400, detail="Missing organisation_id")

    file_path = raw.get("file_path")
    if not file_path:
        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            job_id=job_id,
            event_type="re_extraction_failed",
            stage="failed",
            actor_type="api",
            new_value={"job_id": job_id, "error": "Missing file_path on invoices_raw row"},
            notes="Missing file_path on invoices_raw row",
        )
        exc = HTTPException(status_code=400, detail="Missing file_path on invoices_raw row")
        setattr(exc, "_audit_logged", True)
        raise exc

    existing_res = (
        supabase
        .table("invoices_extracted")
        .select("*")
        .eq("invoice_raw_id", invoice_raw_id)
        .limit(1)
        .execute()
    )
    if not existing_res.data:
        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            job_id=job_id,
            event_type="re_extraction_failed",
            stage="failed",
            actor_type="api",
            new_value={"job_id": job_id, "error": "No extracted invoice found to re-extract"},
            notes="No extracted invoice found to re-extract",
        )
        exc = HTTPException(status_code=404, detail="No extracted invoice found to re-extract")
        setattr(exc, "_audit_logged", True)
        raise exc

    existing = existing_res.data[0]
    extracted_invoice_id = existing["id"]

    log_invoice_event(
        supabase,
        organisation_id=org_id,
        invoice_raw_id=invoice_raw_id,
        invoice_extracted_id=extracted_invoice_id,
        event_type="re_extraction_started",
        stage="text_extraction",
        actor_type="api",
        new_value={"mode": "deep_region_ocr", "force_update": force_update, "job_id": job_id},
        notes="Deep region OCR re-extraction started.",
    )

    if job_id:
        update_reextract_job(job_id, status="running", stage="reading_document")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as exc:
        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            invoice_extracted_id=extracted_invoice_id,
            job_id=job_id,
            event_type="re_extraction_failed",
            stage="failed",
            actor_type="api",
            new_value={"job_id": job_id, "error": f"Storage download error: {str(exc)}"},
            notes=f"Storage download error: {str(exc)}",
        )
        http_exc = HTTPException(status_code=400, detail=f"Storage download error: {str(exc)}")
        setattr(http_exc, "_audit_logged", True)
        raise http_exc

    try:
        existing_parse_attempts: list[dict] = []
        parse_attempt_fetch_error = None
        try:
            existing_parse_attempts, _ = fetch_parse_attempts(
                supabase,
                invoice_raw_id=invoice_raw_id,
            )
        except Exception as exc:
            parse_attempt_fetch_error = str(exc)

        if job_id:
            update_reextract_job(job_id, status="running", stage="ocr")

        deep_attempt, deep_result, deep_error = build_deep_region_parse_attempt(
            file_bytes,
            raw.get("file_type"),
        )
        if deep_error or not deep_result:
            raise HTTPException(status_code=400, detail=f"Deep re-extraction failed: {deep_error}")

        if job_id:
            update_reextract_job(job_id, status="running", stage="parsing_invoice_fields")

        parsed_data = deep_result.get("parsed_data") or {}
        deep_text = deep_result.get("text") or ""

        vlm_should_try = (
            parsed_data.get("confidence_score", 0) < 0.70
            or not parsed_data.get("invoice_number")
            or not parsed_data.get("total_amount")
            or not parsed_data.get("supplier_name_extracted")
        )

        if vlm_should_try:
            vlm_data = extract_with_gemini(file_bytes, raw.get("file_type"))
            print("RE-EXTRACT VLM RAW RESULT:", vlm_data)
            if vlm_data is not None:
                vlm_confidence = vlm_data.get("confidence_score", 0)
                tesseract_confidence = parsed_data.get("confidence_score", 0)
                print(f"RE-EXTRACT VLM LINE ITEMS: {len(vlm_data.get('line_items') or [])} items — {vlm_data.get('line_items')}")

                for field in VLM_MERGE_FIELDS:
                    vlm_value = vlm_data.get(field)
                    if vlm_value is not None and vlm_value != [] and vlm_value != "":
                        if not parsed_data.get(field) or vlm_confidence > tesseract_confidence:
                            parsed_data[field] = vlm_value

                print(f"RE-EXTRACT MERGED LINE ITEMS: {len(parsed_data.get('line_items') or [])} items")
                parsed_data["confidence_score"] = calculate_confidence(parsed_data)

                log_invoice_event(
                    supabase,
                    organisation_id=org_id,
                    invoice_raw_id=invoice_raw_id,
                    invoice_extracted_id=extracted_invoice_id,
                    event_type="vlm_extraction_completed",
                    stage="field_extraction",
                    actor_type="api",
                    new_value={
                        "vlm_confidence": vlm_confidence,
                        "tesseract_confidence": tesseract_confidence,
                        "merged_confidence": parsed_data.get("confidence_score"),
                        "vlm_supplier": vlm_data.get("supplier_name_extracted"),
                        "vlm_invoice_number": vlm_data.get("invoice_number"),
                        "vlm_total": vlm_data.get("total_amount"),
                        "vlm_line_items_count": len(vlm_data.get("line_items") or []),
                    },
                    notes=f"Gemini VLM fallback merged during re-extract. VLM confidence={vlm_confidence:.2f}, deep OCR confidence={tesseract_confidence:.2f}.",
                )

        organisation = get_organisation(org_id)
        direction_result = classify_document_direction(deep_text, organisation)
        parsed_data["issuer_name_extracted"] = direction_result.issuer_name
        parsed_data["recipient_name_extracted"] = direction_result.recipient_name
        parsed_data["document_direction"] = direction_result.document_direction
        parsed_data["organisation_match_status"] = direction_result.organisation_match_status
        parsed_data["validation_status"] = direction_result.validation_status
        parsed_data["validation_notes"] = direction_result.validation_notes

        if (
            direction_result.document_direction == "supplier_invoice_payable"
            and direction_result.issuer_name
            and not parsed_data.get("supplier_name_extracted")
        ):
            parsed_data["supplier_name_extracted"] = direction_result.issuer_name

        missing_supplier_failure = apply_missing_supplier_failure(parsed_data)
        if missing_supplier_failure:
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                invoice_extracted_id=extracted_invoice_id,
                event_type="supplier_missing_failed",
                stage="entity_validation",
                actor_type="api",
                new_value={"validation_status": parsed_data.get("validation_status")},
                notes=MISSING_SUPPLIER_NOTE,
            )

        if deep_attempt:
            deep_attempt["parsed_data"] = dict(parsed_data)
            deep_attempt["line_items"] = parsed_data.get("line_items") or []
            deep_attempt["confidence_score"] = parsed_data.get("confidence_score")

        raw_parse_attempts = ensure_parsed_data_attempt(
            [deep_attempt] if deep_attempt else [],
            parsed_data=parsed_data,
            text=deep_text,
            strategy="deep_region_ocr",
        )
        selected_raw_parse_attempt = raw_parse_attempts[0] if raw_parse_attempts else None

        update_payload, improved_fields, unchanged_fields = build_reextract_update(
            existing=existing,
            parsed=parsed_data,
            force_update=force_update,
        )

        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            invoice_extracted_id=extracted_invoice_id,
            event_type="region_ocr_completed",
            stage="text_extraction",
            actor_type="api",
            new_value={
                "mode": deep_result.get("method"),
                "regions_attempted": deep_result.get("regions_attempted") or [],
                "confidence_by_region": deep_result.get("confidence_by_region") or {},
                "combined_text_length": len(deep_text),
                "ocr_confidence": deep_result.get("ocr_confidence"),
                "region_text_preview": _trim_region_text(deep_result.get("region_ocr") or {}, limit=350),
            },
            notes="Deep region OCR completed.",
        )

        if job_id:
            update_reextract_job(job_id, status="running", stage="extracting_line_items")

        supplier_settings = fetch_supplier_processing_settings(
            supabase,
            existing.get("supplier_id") or raw.get("supplier_id"),
        )
        supplier_rule_result = apply_supplier_processing_rules(parsed_data, supplier_settings)
        line_items = supplier_rule_result["line_items"]
        update_payload.update(supplier_rule_result["invoice_patch"])

        line_items_replaced = False
        if line_items:
            line_item_diagnostics = replace_invoice_line_items(
                supabase,
                invoice_extracted_id=extracted_invoice_id,
                organisation_id=org_id,
                line_items=line_items,
                invoice_total=parsed_data.get("total_amount"),
                delete_when_empty=False,
                raise_on_error=False,
            )
            if line_item_diagnostics.get("line_items_insert_error"):
                print("RE-EXTRACT LINE ITEM INSERT FAILED:", line_item_diagnostics["line_items_insert_error"])
            else:
                line_items_replaced = True
                improved_fields.append({
                    "field": "line_items",
                    "old_value": "existing_line_items",
                    "new_value": {"line_item_count": len(line_items)},
                })
        else:
            line_item_diagnostics = build_line_item_diagnostics(
                line_items=line_items,
                invoice_total=parsed_data.get("total_amount"),
            )

        if supplier_settings.get("supplier_id"):
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                invoice_extracted_id=extracted_invoice_id,
                job_id=job_id,
                event_type="supplier_rules_applied",
                stage="supplier_processing_rules",
                actor_type="api",
                new_value={
                    "supplier_id": supplier_settings.get("supplier_id"),
                    "invoice_patch": supplier_rule_result["invoice_patch"],
                    **line_item_diagnostics,
                },
                notes="Supplier processing rules applied during re-extraction.",
            )

        if job_id:
            update_reextract_job(
                job_id,
                status="running",
                stage="saving_extracted_data",
                diagnostic=line_item_diagnostics,
            )

        if update_payload:
            supabase.table("invoices_extracted").update(update_payload).eq("id", extracted_invoice_id).execute()

        parse_attempt_result: dict = {}
        if raw_parse_attempts:
            parse_attempts = [
                attempt
                for attempt in existing_parse_attempts
                if attempt.get("strategy") != "deep_region_ocr"
            ]
            parse_attempts.extend(raw_parse_attempts)
            parse_attempt_result = persist_parse_attempts(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                invoice_extracted_id=extracted_invoice_id,
                attempts=parse_attempts,
                selected_attempt=selected_raw_parse_attempt,
            )

            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                invoice_extracted_id=extracted_invoice_id,
                event_type=(
                    "parse_attempts_persist_failed"
                    if parse_attempt_result.get("parse_attempts_insert_error")
                    else "parse_attempts_recorded"
                ),
                stage="save_parse_attempts",
                actor_type="api",
                new_value={
                    **parse_attempt_result,
                    "parse_attempt_fetch_error": parse_attempt_fetch_error,
                },
                notes=parse_attempt_result.get("parse_attempts_insert_error") or parse_attempt_fetch_error,
            )

        if improved_fields:
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                invoice_extracted_id=extracted_invoice_id,
                event_type="field_values_improved",
                stage="field_extraction",
                actor_type="api",
                new_value={
                    "fields": improved_fields,
                    "force_update": force_update,
                },
                notes=f"Deep re-extract improved {len(improved_fields)} field(s).",
            )

        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            invoice_extracted_id=extracted_invoice_id,
            event_type="re_extraction_completed",
            stage="completed",
            actor_type="api",
            new_value={
                "fields_improved": [field["field"] for field in improved_fields],
                "fields_unchanged": unchanged_fields,
                "line_items_replaced": line_items_replaced,
                **line_item_diagnostics,
                "confidence_score": parsed_data.get("confidence_score"),
            },
            notes="Deep region OCR re-extraction completed.",
        )

        response = {
            "success": True,
            "mode": "deep_region_ocr",
            "invoice_raw_id": invoice_raw_id,
            "extracted_invoice_id": extracted_invoice_id,
            "fields_improved": [field["field"] for field in improved_fields],
            "field_changes": improved_fields,
            "fields_unchanged": unchanged_fields,
            "line_items_replaced": line_items_replaced,
            **line_item_diagnostics,
            "needs_review": True,
            "ocr_confidence": deep_result.get("ocr_confidence"),
            **parse_attempt_result,
            "regions_attempted": deep_result.get("regions_attempted") or [],
            "confidence_by_region": deep_result.get("confidence_by_region") or {},
            "region_text_preview": _trim_region_text(deep_result.get("region_ocr") or {}),
            "parsed_deep_fields": parsed_data,
            "text_preview": deep_text[:2000],
            "extracted_supplier_profile": build_extracted_supplier_profile(parsed_data),
            "extracted_document_profile": build_extracted_document_profile(parsed_data),
        }
        if job_id:
            update_reextract_job(
                job_id,
                status="completed",
                stage="completed",
                extracted_invoice_id=extracted_invoice_id,
                diagnostic=line_item_diagnostics,
            )
        return response
    except HTTPException as exc:
        error_message = _stringify_http_detail(exc.detail) if exc.detail else "Re-extraction failed"
        if job_id:
            update_reextract_job(
                job_id,
                status="failed",
                stage="failed",
                error=error_message,
            )
        if org_id:
            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=invoice_raw_id,
                invoice_extracted_id=extracted_invoice_id,
                job_id=job_id,
                event_type="re_extraction_failed",
                stage="failed",
                actor_type="api",
                new_value={"job_id": job_id, "status_code": exc.status_code, "error": error_message},
                notes=error_message,
            )
            setattr(exc, "_audit_logged", True)
        raise
    except Exception as exc:
        if job_id:
            update_reextract_job(job_id, status="failed", stage="failed", error=str(exc))
        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=invoice_raw_id,
            invoice_extracted_id=extracted_invoice_id,
            job_id=job_id,
            event_type="re_extraction_failed",
            stage="failed",
            actor_type="api",
            new_value={"job_id": job_id, "error": str(exc)},
            notes=str(exc),
        )
        raise HTTPException(status_code=400, detail=f"Deep re-extraction failed: {str(exc)}")


def run_reextract_job_background(job_id: str, payload_data: dict) -> None:
    try:
        run_invoice_re_extraction(
            invoice_raw_id=payload_data["invoice_raw_id"],
            organisation_id=payload_data.get("organisation_id"),
            force_update=payload_data.get("force_update", False),
            job_id=job_id,
        )
    except HTTPException as exc:
        error_message = _stringify_http_detail(exc.detail) if exc.detail else "Re-extraction failed"
        update_reextract_job(job_id, status="failed", stage="failed", error=error_message)
        if not getattr(exc, "_audit_logged", False):
            log_reextract_failure(
                payload_data=payload_data,
                job_id=job_id,
                error=error_message,
            )
    except Exception as exc:
        update_reextract_job(job_id, status="failed", stage="failed", error=str(exc))
        log_reextract_failure(
            payload_data=payload_data,
            job_id=job_id,
            error=str(exc),
        )
