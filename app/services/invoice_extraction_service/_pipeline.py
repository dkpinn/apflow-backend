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
    normalise_name,
)
from app.services.invoice_extraction.vlm_parser import VLM_MERGE_FIELDS, extract_with_gemini
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
    merge_supplier_recovery_fields,
    utc_now_iso,
)
from ._helpers import (
    get_organisation_extraction_settings,
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
            },
        )

        if job_id:
            mark_job_stage(supabase, job_id=job_id, stage="field_extraction")

        organisation_settings = get_organisation_extraction_settings(org_id)
        strategy = extraction_strategy or organisation_settings.get("extraction_strategy") or "auto_group"
        vlm_enabled = organisation_settings.get("vlm_enabled", False)

        parsed_data = parse_invoice_fields(text)

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

        force_vlm = strategy == "vlm" and vlm_enabled
        vlm_should_try = (
            force_vlm
            or parsed_data.get("confidence_score", 0) < 0.70
            or not parsed_data.get("invoice_number")
            or not parsed_data.get("total_amount")
            or not parsed_data.get("supplier_name_extracted")
        )

        if vlm_should_try:
            vlm_data = extract_with_gemini(file_bytes, raw.get("file_type"))
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
                    },
                    notes=f"Gemini VLM fallback merged. VLM confidence={vlm_confidence:.2f}, Tesseract confidence={tesseract_confidence:.2f}.",
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
                    },
                    notes="VLM fallback was needed (low confidence or missing fields) but extract_with_gemini returned None. Check GOOGLE_API_KEY and Gemini availability.",
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

        organisation = get_organisation(org_id)
        direction_result = classify_document_direction(text, organisation)

        parsed_data["issuer_name_extracted"] = direction_result.issuer_name
        parsed_data["recipient_name_extracted"] = direction_result.recipient_name
        parsed_data["document_direction"] = direction_result.document_direction
        parsed_data["organisation_match_status"] = direction_result.organisation_match_status
        parsed_data["validation_status"] = direction_result.validation_status
        parsed_data["validation_notes"] = direction_result.validation_notes

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

        if direction_result.confidence_adjustment:
            parsed_data["confidence_score"] = round(
                max(0.0, min(1.0, (parsed_data.get("confidence_score") or 0) + direction_result.confidence_adjustment)),
                2,
            )

        missing_supplier_failure = apply_missing_supplier_failure(parsed_data)

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
    }

    auto_linked_supplier_id = None

    # Auto-link supplier when not already linked and an exact identifier match exists.
    if not extracted_payload.get("supplier_id"):
        try:
            from app.services.supplier_matcher import attempt_supplier_auto_link  # noqa: PLC0415
            matched_id = attempt_supplier_auto_link(
                supabase,
                org_id=org_id,
                supplier_name_extracted=parsed_data.get("supplier_name_extracted"),
                vat_number_extracted=parsed_data.get("vat_number_extracted"),
                company_registration_number_extracted=parsed_data.get("company_registration_number_extracted"),
                cus_code_extracted=parsed_data.get("cus_code_extracted"),
                bank_account_number_extracted=parsed_data.get("bank_account_number_extracted"),
            )
            if matched_id:
                extracted_payload["supplier_id"] = matched_id
                auto_linked_supplier_id = matched_id
                try:
                    supabase.table("invoices_raw").update({
                        "supplier_id": matched_id,
                        "updated_at": utc_now_iso(),
                    }).eq("id", invoice_raw_id).execute()
                except Exception as raw_link_exc:
                    print(f"INVOICES_RAW AUTO-LINK UPDATE FAILED (non-fatal): {raw_link_exc}")
                print(f"AUTO-LINKED supplier {matched_id} via exact identifier match")
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
                "match_type": "exact_identifier",
            },
            notes="Supplier auto-linked from exact extracted identifier match.",
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
