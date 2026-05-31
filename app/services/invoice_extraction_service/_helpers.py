"""
_helpers.py
-----------
Shared utility helpers used by both the extraction and re-extraction pipelines.

Groups B + D + E from the original invoice_extraction_service.py:
  B — error/context helpers (_stringify_http_detail, _resolve_reextract_context, log_reextract_failure)
  D — raw DB lookups (get_raw_invoice, get_organisation)
  E — file & storage helpers (rename_invoice_file_after_extraction, store_basic_document_page_snapshot)
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from app.db.supabase_client import get_supabase_client
from app.services.audit_log import log_invoice_event
from app.services.invoice_extraction.file_naming import build_invoice_storage_filename
from app.services.invoice_data_builders import utc_now_iso

try:
    supabase = get_supabase_client()
except Exception:
    supabase = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Group B — Error & context helpers
# ---------------------------------------------------------------------------

def _stringify_http_detail(detail) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        return detail.get("message") or str(detail)
    return str(detail)


def _resolve_reextract_context(payload_data: dict) -> dict:
    invoice_raw_id = payload_data.get("invoice_raw_id")
    organisation_id = payload_data.get("organisation_id")
    raw = None
    extracted_invoice_id = None

    if invoice_raw_id:
        try:
            raw = get_raw_invoice(invoice_raw_id)
            organisation_id = organisation_id or raw.get("organisation_id")
        except Exception:
            raw = None

        try:
            existing_res = (
                supabase
                .table("invoices_extracted")
                .select("id")
                .eq("invoice_raw_id", invoice_raw_id)
                .limit(1)
                .execute()
            )
            if existing_res.data:
                extracted_invoice_id = existing_res.data[0].get("id")
        except Exception:
            extracted_invoice_id = None

    return {
        "invoice_raw_id": invoice_raw_id,
        "organisation_id": organisation_id,
        "raw": raw,
        "extracted_invoice_id": extracted_invoice_id,
    }


def log_reextract_failure(
    *,
    payload_data: dict,
    job_id: Optional[str],
    error: str,
    stage: str = "failed",
    extracted_invoice_id: Optional[str] = None,
) -> None:
    context = _resolve_reextract_context(payload_data)
    organisation_id = context.get("organisation_id")
    if not organisation_id:
        return

    log_invoice_event(
        supabase,
        organisation_id=organisation_id,
        invoice_raw_id=context.get("invoice_raw_id"),
        invoice_extracted_id=extracted_invoice_id or context.get("extracted_invoice_id"),
        job_id=job_id,
        event_type="re_extraction_failed",
        stage=stage,
        actor_type="api",
        new_value={
            "job_id": job_id,
            "error": error,
        },
        notes=error,
    )


# ---------------------------------------------------------------------------
# Group D — Raw invoice / organisation DB helpers
# ---------------------------------------------------------------------------

def get_raw_invoice(invoice_raw_id: str) -> dict:
    raw_res = (
        supabase
        .table("invoices_raw")
        .select("*")
        .eq("id", invoice_raw_id)
        .limit(1)
        .execute()
    )

    if not raw_res.data:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Raw invoice not found",
                "invoice_raw_id": invoice_raw_id,
            },
        )

    return raw_res.data[0]


def get_organisation(organisation_id: str) -> Optional[dict]:
    org_res = (
        supabase
        .table("organisations")
        .select("id, name, legal_name, trading_name, country, base_currency, currency, vat_number, tax_number")
        .eq("id", organisation_id)
        .limit(1)
        .execute()
    )

    return org_res.data[0] if org_res.data else None


def get_organisation_extraction_settings(organisation_id: str) -> dict:
    settings_res = (
        supabase
        .table("organisations")
        .select("extraction_strategy, ask_per_upload, vlm_enabled, supplier_auto_link_min_matches")
        .eq("id", organisation_id)
        .limit(1)
        .execute()
    )

    settings = settings_res.data[0] if settings_res.data else {}
    min_matches = settings.get("supplier_auto_link_min_matches")
    try:
        min_matches = int(min_matches)
    except (TypeError, ValueError):
        min_matches = 2
    return {
        "extraction_strategy": settings.get("extraction_strategy") or "auto_group",
        "ask_per_upload": bool(settings.get("ask_per_upload")),
        "vlm_enabled": bool(settings.get("vlm_enabled")),
        "supplier_auto_link_min_matches": min(4, max(1, min_matches)),
    }


def update_organisation_extraction_settings(organisation_id: str, updates: dict) -> dict:
    if not updates:
        raise ValueError("No settings provided to update")

    update_res = (
        supabase
        .table("organisations")
        .update(updates)
        .eq("id", organisation_id)
        .execute()
    )

    if not update_res.data:
        raise HTTPException(status_code=404, detail="Organisation not found")

    return get_organisation_extraction_settings(organisation_id)


def persist_invoice_page_group(
    invoice_raw_id: str,
    page_numbers: list[int],
    strategy: str,
    supplier_detected: Optional[str] = None,
    confidence: Optional[float] = None,
) -> dict:
    payload = {
        "invoice_raw_id": invoice_raw_id,
        "page_numbers": page_numbers,
        "strategy": strategy,
        "supplier_detected": supplier_detected,
        "confidence": confidence,
    }
    res = supabase.table("invoice_page_groups").insert(payload).execute()
    return res.data[0] if res.data else {}


def update_invoice_raw_grouping(
    invoice_raw_id: str,
    page_numbers: list[int],
    strategy: str,
    total_pages: Optional[int] = None,
) -> None:
    payload = {
        "grouped_from_pages": page_numbers,
        "page_grouping_strategy": strategy,
        "updated_at": utc_now_iso(),
    }
    if total_pages is not None:
        payload["total_pages_in_upload"] = total_pages

    supabase.table("invoices_raw").update(payload).eq("id", invoice_raw_id).execute()


# ---------------------------------------------------------------------------
# Group E — File & storage helpers
# ---------------------------------------------------------------------------

def rename_invoice_file_after_extraction(
    *,
    raw: dict,
    organisation_id: str,
    invoice_raw_id: str,
    parsed_data: dict,
) -> dict:
    """
    Rename/move uploaded invoice file in Supabase Storage after extraction.

    Keeps original upload if rename fails.
    Returns updated file_name and file_path.
    """
    old_file_path = raw.get("file_path")
    old_file_name = raw.get("file_name") or "invoice.pdf"

    if not old_file_path:
        return {
            "file_name": old_file_name,
            "file_path": old_file_path,
            "renamed": False,
            "reason": "missing_old_file_path",
        }

    new_file_name = build_invoice_storage_filename(
        original_filename=old_file_name,
        supplier_name=parsed_data.get("supplier_name_extracted"),
        invoice_number=parsed_data.get("invoice_number"),
        invoice_date=parsed_data.get("invoice_date"),
        total_amount=parsed_data.get("total_amount"),
        invoice_raw_id=invoice_raw_id,
    )

    new_file_path = f"{organisation_id}/invoices/processed/{new_file_name}"

    if new_file_path == old_file_path:
        return {
            "file_name": new_file_name,
            "file_path": new_file_path,
            "renamed": False,
            "reason": "same_path",
        }

    try:
        supabase.storage.from_("invoices").move(old_file_path, new_file_path)

        supabase.table("invoices_raw").update({
            "file_name": new_file_name,
            "file_path": new_file_path,
            "updated_at": utc_now_iso(),
        }).eq("id", invoice_raw_id).execute()

        return {
            "file_name": new_file_name,
            "file_path": new_file_path,
            "renamed": True,
            "reason": None,
        }
    except Exception as e:
        print("FILE RENAME FAILED:", str(e))
        return {
            "file_name": old_file_name,
            "file_path": old_file_path,
            "renamed": False,
            "reason": str(e),
        }


def _preprocessing_notes_text(value) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, list):
        return "; ".join(str(item) for item in value if item is not None) or None
    return str(value)


def store_basic_document_page_snapshot(
    *,
    organisation_id: str,
    invoice_raw_id: str,
    job_id: Optional[str],
    file_bytes: bytes,
    file_type: Optional[str],
    text_result: dict,
    parsed_data: Optional[dict] = None,
) -> None:
    """
    Phase B3 document_pages capture.

    Stores one row per detected page, including OCR confidence and image
    quality score where OCR was used. This is the foundation for later batch
    splitting and page-level review.
    """
    try:
        pages = text_result.get("pages") or []
        method = text_result.get("method")
        parsed = parsed_data or {}
        page_payloads: list[dict] = []

        if pages:
            for idx, page in enumerate(pages):
                page_text = page.get("text") or ""
                page_payloads.append({
                    "organisation_id": organisation_id,
                    "invoice_raw_id": invoice_raw_id,
                    "job_id": job_id,
                    "page_number": page.get("page_number") or idx + 1,
                    "page_count": page.get("page_count") or len(pages),
                    "extraction_method": page.get("method") or method,
                    "text_content": page_text or None,
                    "text_preview": page_text[:500] or None,
                    "image_quality_score": page.get("image_quality_score"),
                    "ocr_confidence": page.get("ocr_confidence"),
                    "layout_type": parsed.get("layout_type"),
                    "document_type": "invoice",
                    "supplier_guess": parsed.get("supplier_name_extracted"),
                    "issuer_guess": parsed.get("issuer_name_extracted"),
                    "recipient_guess": parsed.get("recipient_name_extracted"),
                    "document_direction": parsed.get("document_direction"),
                    "organisation_match_status": parsed.get("organisation_match_status"),
                    "validation_status": parsed.get("validation_status"),
                    "invoice_number_guess": parsed.get("invoice_number"),
                    "invoice_date_guess": parsed.get("invoice_date"),
                    "total_guess": parsed.get("total_amount"),
                    "is_continuation_page": idx > 0,
                    "document_group_key": parsed.get("invoice_number"),
                    "confidence_score": parsed.get("confidence_score"),
                    "original_preview_path": page.get("original_preview_path"),
                    "processed_preview_path": page.get("processed_preview_path"),
                    "preprocessing_notes": _preprocessing_notes_text(page.get("preprocessing_notes")),
                    "crop_applied": bool(page.get("crop_applied")),
                    "crop_box": page.get("crop_box"),
                    "crop_area_ratio": page.get("crop_area_ratio"),
                    "deskew_applied": bool(page.get("deskew_applied")),
                })

        if not page_payloads:
            text = text_result.get("text") or ""
            page_payloads.append({
                "organisation_id": organisation_id,
                "invoice_raw_id": invoice_raw_id,
                "job_id": job_id,
                "page_number": 1,
                "page_count": text_result.get("page_count") or 1,
                "extraction_method": method,
                "text_content": text or None,
                "text_preview": text[:500] or None,
                "image_quality_score": text_result.get("image_quality_score"),
                "ocr_confidence": text_result.get("ocr_confidence"),
                "layout_type": parsed.get("layout_type"),
                "document_type": "invoice",
                "supplier_guess": parsed.get("supplier_name_extracted"),
                "issuer_guess": parsed.get("issuer_name_extracted"),
                "recipient_guess": parsed.get("recipient_name_extracted"),
                "document_direction": parsed.get("document_direction"),
                "organisation_match_status": parsed.get("organisation_match_status"),
                "validation_status": parsed.get("validation_status"),
                "invoice_number_guess": parsed.get("invoice_number"),
                "invoice_date_guess": parsed.get("invoice_date"),
                "total_guess": parsed.get("total_amount"),
                "is_continuation_page": False,
                "document_group_key": parsed.get("invoice_number"),
                "confidence_score": parsed.get("confidence_score"),
                "original_preview_path": text_result.get("original_preview_path"),
                "processed_preview_path": text_result.get("processed_preview_path"),
                "preprocessing_notes": _preprocessing_notes_text(text_result.get("preprocessing_notes")),
                "crop_applied": bool(text_result.get("crop_applied")),
                "crop_box": text_result.get("crop_box"),
                "crop_area_ratio": text_result.get("crop_area_ratio"),
                "deskew_applied": bool(text_result.get("deskew_applied")),
            })

        supabase.table("document_pages").delete().eq("invoice_raw_id", invoice_raw_id).execute()
        supabase.table("document_pages").insert(page_payloads).execute()
    except Exception as exc:
        print("DOCUMENT PAGE SNAPSHOT FAILED:", str(exc))
