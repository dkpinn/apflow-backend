from __future__ import annotations

import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

import fitz  # PyMuPDF
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.db.supabase_client import get_supabase_client
from app.services.audit_log import log_invoice_event
from app.services.document_jobs import (
    create_processing_job,
    get_next_queued_job,
    mark_job_completed,
    mark_job_failed,
    mark_job_processing,
    mark_job_stage,
    safe_update_invoice_raw_status,
)
from app.services.invoice_extraction.file_naming import build_invoice_storage_filename
from app.services.invoice_line_items import (
    build_line_item_diagnostics,
    replace_invoice_line_items,
)
from app.services.invoice_parse_attempts import (
    build_deep_region_parse_attempt,
    build_parse_attempts_from_text_result,
    ensure_parsed_data_attempt,
    fetch_parse_attempts,
    persist_parse_attempts,
)
from app.services.invoice_supplier_rules import (
    apply_supplier_processing_rules,
    fetch_supplier_processing_settings,
    reapply_supplier_rules_to_invoice,
)
from app.services.invoice_ocr_pipeline import (
    calculate_confidence,
    extract_text_with_fallback,
    parse_invoice_fields,
)
from app.services.invoice_extraction.vlm_parser import (
    VLM_MERGE_FIELDS,
    extract_with_gemini,
)
from app.services.invoice_previews import persist_preview_artifacts
from app.services.invoice_extraction.entity_detection import classify_document_direction, normalise_name
from app.services.invoice_data_builders import (
    apply_missing_supplier_failure,
    build_extracted_document_profile,
    build_extracted_supplier_profile,
    build_reextract_update,
    build_supplier_create_payload,
    merge_supplier_recovery_fields,
    utc_now_iso,
    MISSING_SUPPLIER_VALIDATION_STATUS,
    _trim_region_text,
)

router = APIRouter(prefix="/api/invoices", tags=["invoices"])
try:
    supabase = get_supabase_client()
except Exception:
    supabase = None  # will fail on first API call; create .env with SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY


REEXTRACT_STAGE_PROGRESS = {
    "queued": 0,
    "starting": 5,
    "reading_document": 15,
    "ocr": 35,
    "parsing_invoice_fields": 55,
    "extracting_line_items": 75,
    "saving_extracted_data": 90,
    "completed": 100,
    "failed": 100,
}

REEXTRACT_STAGE_LABELS = {
    "queued": "Queued",
    "starting": "Starting re-extraction",
    "reading_document": "Reading document",
    "ocr": "Running OCR",
    "parsing_invoice_fields": "Parsing invoice fields",
    "extracting_line_items": "Extracting line items",
    "saving_extracted_data": "Saving extracted data",
    "completed": "Completed",
    "failed": "Failed",
}

REEXTRACT_DEFAULT_DIAGNOSTIC = {
    "line_items_found_count": 0,
    "line_items_inserted_count": 0,
    "line_items_insert_error": None,
    "line_items_total": None,
    "invoice_total": None,
    "line_items_match_invoice_total": None,
}

REEXTRACT_JOBS: dict[str, dict] = {}
REEXTRACT_JOBS_LOCK = Lock()
EXTRACT_WORKER_LOCK = Lock()


EXTRACT_STAGE_PROGRESS = {
    "queued": 0,
    "starting": 5,
    "processing": 10,
    "reading_document": 15,
    "text_extraction": 35,
    "ocr": 35,
    "field_extraction": 55,
    "parsing_invoice_fields": 55,
    "save_line_items": 75,
    "extracting_line_items": 75,
    "save_extracted_invoice": 90,
    "save_parse_attempts": 92,
    "saving_extracted_data": 90,
    "completed": 100,
    "failed": 100,
}

EXTRACT_STAGE_LABELS = {
    "queued": "Queued",
    "starting": "Starting extraction",
    "processing": "Starting extraction",
    "reading_document": "Reading document",
    "text_extraction": "Running OCR",
    "ocr": "Running OCR",
    "field_extraction": "Parsing invoice fields",
    "parsing_invoice_fields": "Parsing invoice fields",
    "save_line_items": "Extracting line items",
    "extracting_line_items": "Extracting line items",
    "save_extracted_invoice": "Saving extracted data",
    "save_parse_attempts": "Saving parse attempts",
    "saving_extracted_data": "Saving extracted data",
    "completed": "Completed",
    "failed": "Failed",
}


class ExtractInvoiceRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: Optional[str] = None
    batch_id: Optional[str] = None
    process_mode: str = Field(
        default="queued",
        description="Default extraction requests are queued. Use the sync=true query flag for legacy synchronous extraction.",
    )


class QueueInvoiceRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: Optional[str] = None
    batch_id: Optional[str] = None
    priority: int = 100


class ProcessNextJobRequest(BaseModel):
    organisation_id: Optional[str] = None


class ReExtractInvoiceRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: Optional[str] = None
    force_update: bool = False


class SaveLineItemsRequest(BaseModel):
    invoice_extracted_id: str
    organisation_id: str
    supplier_id: Optional[str] = None
    line_items: list[dict]
    document_total: Optional[float] = None  # original VLM-extracted total; rounding reference


class GeneratePreviewRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: str


def _reextract_status_payload(job: dict) -> dict:
    stage = job.get("stage") or "queued"
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status") or "queued",
        "stage": stage,
        "stage_label": job.get("stage_label") or REEXTRACT_STAGE_LABELS.get(stage, stage.replace("_", " ").title()),
        "progress": int(job.get("progress") or REEXTRACT_STAGE_PROGRESS.get(stage, 0)),
        "invoice_raw_id": job.get("invoice_raw_id"),
        "extracted_invoice_id": job.get("extracted_invoice_id"),
        "error": job.get("error"),
        "diagnostic": {
            **REEXTRACT_DEFAULT_DIAGNOSTIC,
            **(job.get("diagnostic") or {}),
        },
    }


def create_reextract_job(*, invoice_raw_id: str, organisation_id: Optional[str] = None) -> dict:
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "status": "queued",
        "stage": "queued",
        "stage_label": REEXTRACT_STAGE_LABELS["queued"],
        "progress": REEXTRACT_STAGE_PROGRESS["queued"],
        "invoice_raw_id": invoice_raw_id,
        "organisation_id": organisation_id,
        "extracted_invoice_id": None,
        "error": None,
        "diagnostic": dict(REEXTRACT_DEFAULT_DIAGNOSTIC),
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    with REEXTRACT_JOBS_LOCK:
        REEXTRACT_JOBS[job_id] = job
    return _reextract_status_payload(job)


def update_reextract_job(
    job_id: str,
    *,
    status: Optional[str] = None,
    stage: Optional[str] = None,
    extracted_invoice_id: Optional[str] = None,
    error: Optional[str] = None,
    diagnostic: Optional[dict] = None,
) -> dict:
    with REEXTRACT_JOBS_LOCK:
        job = REEXTRACT_JOBS.get(job_id)
        if not job:
            job = {
                "job_id": job_id,
                "invoice_raw_id": None,
                "diagnostic": dict(REEXTRACT_DEFAULT_DIAGNOSTIC),
                "created_at": utc_now_iso(),
            }
            REEXTRACT_JOBS[job_id] = job

        if status:
            job["status"] = status
        if stage:
            job["stage"] = stage
            job["stage_label"] = REEXTRACT_STAGE_LABELS.get(stage, stage.replace("_", " ").title())
            job["progress"] = REEXTRACT_STAGE_PROGRESS.get(stage, job.get("progress") or 0)
        if extracted_invoice_id is not None:
            job["extracted_invoice_id"] = extracted_invoice_id
        if error is not None:
            job["error"] = error
        if diagnostic is not None:
            job["diagnostic"] = {
                **REEXTRACT_DEFAULT_DIAGNOSTIC,
                **diagnostic,
            }
        job["updated_at"] = utc_now_iso()
        return _reextract_status_payload(deepcopy(job))


def get_reextract_job_status(job_id: str) -> Optional[dict]:
    with REEXTRACT_JOBS_LOCK:
        job = REEXTRACT_JOBS.get(job_id)
        if not job:
            return None
        return _reextract_status_payload(deepcopy(job))


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


def _normalise_extract_status(status: Optional[str]) -> str:
    if status == "processing":
        return "running"
    if status in {"queued", "running", "completed", "failed"}:
        return status
    return status or "queued"


def _extract_stage_label(stage: str) -> str:
    return EXTRACT_STAGE_LABELS.get(stage, stage.replace("_", " ").title())


def _extract_progress(stage: str, status: str) -> int:
    if status == "completed":
        return 100
    if status == "failed":
        return 100
    return EXTRACT_STAGE_PROGRESS.get(stage, 0)


def get_extracted_invoice_id_for_raw(invoice_raw_id: Optional[str]) -> Optional[str]:
    if not invoice_raw_id:
        return None
    try:
        res = (
            supabase
            .table("invoices_extracted")
            .select("id")
            .eq("invoice_raw_id", invoice_raw_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0].get("id")
    except Exception as exc:
        print("EXTRACTED ID LOOKUP FAILED:", str(exc))
    return None


def get_processing_job(job_id: str) -> Optional[dict]:
    res = (
        supabase
        .table("document_processing_jobs")
        .select("*")
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def build_extract_job_status(job: dict) -> dict:
    raw_status = job.get("status")
    status = _normalise_extract_status(raw_status)
    stage = job.get("current_stage") or ("completed" if status == "completed" else "failed" if status == "failed" else "queued")
    if status == "completed":
        stage = "completed"
    if status == "failed":
        stage = "failed"
    invoice_raw_id = job.get("invoice_raw_id")
    extracted_invoice_id = get_extracted_invoice_id_for_raw(invoice_raw_id)
    return {
        "job_id": job.get("id"),
        "status": status,
        "stage": stage,
        "stage_label": _extract_stage_label(stage),
        "progress": _extract_progress(stage, status),
        "invoice_raw_id": invoice_raw_id,
        "extracted_invoice_id": extracted_invoice_id,
        "error": job.get("last_error"),
    }


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
        .select("id, name, legal_name, trading_name, country, base_currency, currency")
        .eq("id", organisation_id)
        .limit(1)
        .execute()
    )

    return org_res.data[0] if org_res.data else None


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


def queue_invoice_job(
    *,
    invoice_raw_id: str,
    organisation_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    priority: int = 100,
) -> dict:
    raw = get_raw_invoice(invoice_raw_id)
    org_id = organisation_id or raw.get("organisation_id")

    if not org_id:
        raise HTTPException(status_code=400, detail="Missing organisation_id")

    job = create_processing_job(
        supabase,
        organisation_id=org_id,
        invoice_raw_id=invoice_raw_id,
        batch_id=batch_id,
        priority=priority,
    )

    safe_update_invoice_raw_status(
        supabase,
        invoice_raw_id=invoice_raw_id,
        parse_status="queued",
        extra={"parse_started_at": None, "parse_completed_at": None},
    )

    log_invoice_event(
        supabase,
        organisation_id=org_id,
        invoice_raw_id=invoice_raw_id,
        job_id=job["id"],
        event_type="queued_for_processing",
        stage="queued",
        actor_type="api",
        notes="Invoice queued in APPayPal document processing holding pen.",
    )

    return job


def run_invoice_extraction(
    *,
    invoice_raw_id: str,
    organisation_id: Optional[str] = None,
    job_id: Optional[str] = None,
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

        parsed_data = parse_invoice_fields(text)

        vlm_should_try = (
            parsed_data.get("confidence_score", 0) < 0.70
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
            from app.services.supplier_matcher import attempt_supplier_auto_link
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


@router.post("/extract")
def extract_invoice(
    payload: ExtractInvoiceRequest,
    background_tasks: BackgroundTasks,
    sync: bool = Query(False),
):
    """
    Legacy-compatible extraction endpoint.

    Default queues and starts the local single-worker processor so the browser
    does not wait on a long OCR request. Use ?sync=true for the old blocking
    behavior during debugging.
    """
    print("EXTRACT PAYLOAD:", payload.model_dump())

    if sync:
        return run_invoice_extraction(
            invoice_raw_id=payload.invoice_raw_id,
            organisation_id=payload.organisation_id,
        )

    job = queue_invoice_job(
        invoice_raw_id=payload.invoice_raw_id,
        organisation_id=payload.organisation_id,
        batch_id=payload.batch_id,
    )
    background_tasks.add_task(run_extract_worker_until_empty)
    return {
        "success": True,
        "status": "queued",
        "invoice_raw_id": payload.invoice_raw_id,
        "organisation_id": job["organisation_id"],
        "job_id": job["id"],
        "message": "Invoice queued for processing.",
    }


@router.get("/extract/{job_id}/status")
def get_extract_status(job_id: str):
    job = get_processing_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Extraction job not found")
    return build_extract_job_status(job)


def run_invoice_re_extraction(
    payload: ReExtractInvoiceRequest,
    *,
    job_id: Optional[str] = None,
) -> dict:
    org_id = payload.organisation_id
    extracted_invoice_id: Optional[str] = None
    if job_id:
        update_reextract_job(job_id, status="running", stage="starting")

    raw = get_raw_invoice(payload.invoice_raw_id)
    org_id = payload.organisation_id or raw.get("organisation_id")

    if not org_id:
        raise HTTPException(status_code=400, detail="Missing organisation_id")

    file_path = raw.get("file_path")
    if not file_path:
        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=payload.invoice_raw_id,
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
        .eq("invoice_raw_id", payload.invoice_raw_id)
        .limit(1)
        .execute()
    )
    if not existing_res.data:
        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=payload.invoice_raw_id,
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
        invoice_raw_id=payload.invoice_raw_id,
        invoice_extracted_id=extracted_invoice_id,
        event_type="re_extraction_started",
        stage="text_extraction",
        actor_type="api",
        new_value={"mode": "deep_region_ocr", "force_update": payload.force_update, "job_id": job_id},
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
            invoice_raw_id=payload.invoice_raw_id,
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
                invoice_raw_id=payload.invoice_raw_id,
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
                    invoice_raw_id=payload.invoice_raw_id,
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
                invoice_raw_id=payload.invoice_raw_id,
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
            force_update=payload.force_update,
        )

        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=payload.invoice_raw_id,
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
                invoice_raw_id=payload.invoice_raw_id,
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
                invoice_raw_id=payload.invoice_raw_id,
                invoice_extracted_id=extracted_invoice_id,
                attempts=parse_attempts,
                selected_attempt=selected_raw_parse_attempt,
            )

            log_invoice_event(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=payload.invoice_raw_id,
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
                invoice_raw_id=payload.invoice_raw_id,
                invoice_extracted_id=extracted_invoice_id,
                event_type="field_values_improved",
                stage="field_extraction",
                actor_type="api",
                new_value={
                    "fields": improved_fields,
                    "force_update": payload.force_update,
                },
                notes=f"Deep re-extract improved {len(improved_fields)} field(s).",
            )

        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=payload.invoice_raw_id,
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
            "invoice_raw_id": payload.invoice_raw_id,
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
                invoice_raw_id=payload.invoice_raw_id,
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
            invoice_raw_id=payload.invoice_raw_id,
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
        payload = ReExtractInvoiceRequest(**payload_data)
        run_invoice_re_extraction(payload, job_id=job_id)
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


@router.post("/re-extract")
def re_extract_invoice(
    payload: ReExtractInvoiceRequest,
    background_tasks: BackgroundTasks,
    sync: bool = Query(False),
):
    if sync:
        return run_invoice_re_extraction(payload)

    raw = get_raw_invoice(payload.invoice_raw_id)
    org_id = payload.organisation_id or raw.get("organisation_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Missing organisation_id")
    if not raw.get("file_path"):
        log_reextract_failure(
            payload_data={**payload.model_dump(), "organisation_id": org_id},
            job_id=None,
            error="Missing file_path on invoices_raw row",
        )
        raise HTTPException(status_code=400, detail="Missing file_path on invoices_raw row")

    job = create_reextract_job(
        invoice_raw_id=payload.invoice_raw_id,
        organisation_id=org_id,
    )
    queued_context = _resolve_reextract_context({**payload.model_dump(), "organisation_id": org_id})
    log_invoice_event(
        supabase,
        organisation_id=org_id,
        invoice_raw_id=payload.invoice_raw_id,
        invoice_extracted_id=queued_context.get("extracted_invoice_id"),
        event_type="re_extraction_queued",
        stage="queued",
        actor_type="api",
        job_id=job["job_id"],
        new_value={
            "job_id": job["job_id"],
            "force_update": payload.force_update,
        },
        notes="Re-extraction queued.",
    )
    payload_data = payload.model_dump()
    payload_data["organisation_id"] = org_id
    background_tasks.add_task(run_reextract_job_background, job["job_id"], payload_data)
    return {
        "job_id": job["job_id"],
        "invoice_raw_id": job["invoice_raw_id"],
        "status": "queued",
    }


@router.get("/re-extract/{job_id}/status")
def get_re_extract_status(job_id: str):
    status = get_reextract_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Re-extract job not found")
    return status


@router.post("/queue")
def queue_invoice(payload: QueueInvoiceRequest):
    job = queue_invoice_job(
        invoice_raw_id=payload.invoice_raw_id,
        organisation_id=payload.organisation_id,
        batch_id=payload.batch_id,
        priority=payload.priority,
    )
    return {
        "success": True,
        "status": "queued",
        "job_id": job["id"],
        "invoice_raw_id": job["invoice_raw_id"],
        "organisation_id": job["organisation_id"],
    }


@router.get("/raw/{invoice_raw_id}/audit-events")
def get_invoice_audit_events(invoice_raw_id: str):
    raw = get_raw_invoice(invoice_raw_id)

    events_res = (
        supabase
        .table("invoice_audit_events")
        .select("*")
        .eq("invoice_raw_id", invoice_raw_id)
        .order("created_at", desc=False)
        .execute()
    )

    events = events_res.data or []

    return {
        "success": True,
        "invoice_raw_id": invoice_raw_id,
        "organisation_id": raw.get("organisation_id"),
        "event_count": len(events),
        "events": events,
    }


@router.get("/{invoice_id}/review-data")
def get_invoice_review_data(invoice_id: str):
    """
    Return the complete invoice review payload for the frontend detail page.

    The current frontend route may hold either invoices_extracted.id or
    invoices_extracted.invoice_raw_id, so this endpoint resolves both forms.
    Optional child reads return empty data plus fetch_errors instead of making
    the whole review page fail.
    """
    fetch_errors: dict[str, str] = {}
    resolved_by = "invoice_extracted_id"

    invoice_res = (
        supabase
        .table("invoices_extracted")
        .select("*")
        .eq("id", invoice_id)
        .limit(1)
        .execute()
    )
    invoice = invoice_res.data[0] if invoice_res.data else None

    if not invoice:
        resolved_by = "invoice_raw_id"
        invoice_res = (
            supabase
            .table("invoices_extracted")
            .select("*")
            .eq("invoice_raw_id", invoice_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        invoice = invoice_res.data[0] if invoice_res.data else None

    if not invoice:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Extracted invoice not found",
                "invoice_id": invoice_id,
            },
        )

    extracted_invoice_id = invoice.get("id")
    invoice_raw_id = invoice.get("invoice_raw_id")
    organisation_id = invoice.get("organisation_id")

    raw = None
    if invoice_raw_id:
        try:
            raw_res = (
                supabase
                .table("invoices_raw")
                .select("*")
                .eq("id", invoice_raw_id)
                .limit(1)
                .execute()
            )
            raw = raw_res.data[0] if raw_res.data else None
        except Exception as exc:
            fetch_errors["raw"] = str(exc)

    supplier = None
    supplier_id = invoice.get("supplier_id")
    if supplier_id:
        try:
            supplier_res = (
                supabase
                .table("suppliers")
                .select("*")
                .eq("id", supplier_id)
                .limit(1)
                .execute()
            )
            supplier = supplier_res.data[0] if supplier_res.data else None
        except Exception as exc:
            fetch_errors["supplier"] = str(exc)

    document_pages: list[dict] = []
    if invoice_raw_id:
        try:
            pages_res = (
                supabase
                .table("document_pages")
                .select("*")
                .eq("invoice_raw_id", invoice_raw_id)
                .order("page_number", desc=False)
                .limit(100)
                .execute()
            )
            document_pages = pages_res.data or []
        except Exception as exc:
            fetch_errors["document_pages"] = str(exc)

    line_items: list[dict] = []
    if extracted_invoice_id:
        try:
            line_items_res = (
                supabase
                .table("invoice_line_items")
                .select("*")
                .eq("invoice_extracted_id", extracted_invoice_id)
                .order("created_at", desc=False)
                .order("id", desc=False)
                .execute()
            )
            line_items = line_items_res.data or []
            line_item_ids = [row.get("id") for row in line_items if row.get("id")]
            if line_item_ids:
                try:
                    allocations_res = (
                        supabase
                        .table("invoice_line_item_allocations")
                        .select("*")
                        .in_("invoice_line_item_id", line_item_ids)
                        .order("sort_order", desc=False)
                        .order("created_at", desc=False)
                        .execute()
                    )
                    allocations_by_line: dict[str, list[dict]] = {}
                    for allocation in allocations_res.data or []:
                        line_id = allocation.get("invoice_line_item_id")
                        if line_id:
                            allocations_by_line.setdefault(line_id, []).append(allocation)
                    for line_item in line_items:
                        line_item["allocations"] = allocations_by_line.get(line_item.get("id"), [])
                except Exception as exc:
                    fetch_errors["line_item_allocations"] = str(exc)
        except Exception as exc:
            fetch_errors["line_items"] = str(exc)

    parse_attempts: list[dict] = []
    selected_parse_attempt_id = None
    if invoice_raw_id:
        try:
            parse_attempts, selected_parse_attempt_id = fetch_parse_attempts(
                supabase,
                invoice_raw_id=invoice_raw_id,
            )
        except Exception as exc:
            fetch_errors["parse_attempts"] = str(exc)

    audit_events: list[dict] = []
    if invoice_raw_id:
        try:
            audit_res = (
                supabase
                .table("invoice_audit_events")
                .select("*")
                .eq("invoice_raw_id", invoice_raw_id)
                .order("created_at", desc=False)
                .execute()
            )
            audit_events = audit_res.data or []
        except Exception as exc:
            fetch_errors["audit_events"] = str(exc)

    document_profile = build_extracted_document_profile(invoice)
    document_profile["line_items"] = line_items or document_profile.get("line_items") or []
    supplier_profile = build_extracted_supplier_profile(invoice)

    supplier_create_payload = build_supplier_create_payload(
        organisation_id=organisation_id,
        invoice_raw_id=invoice_raw_id,
        invoice_extracted_id=extracted_invoice_id,
        parsed_data=invoice,
    )

    return {
        "success": True,
        "resolved_by": resolved_by,
        "invoice_extracted_id": extracted_invoice_id,
        "invoice_raw_id": invoice_raw_id,
        "organisation_id": organisation_id,
        "invoice": {
            **invoice,
            "supplier": supplier,
        },
        "raw": raw,
        "document_pages": document_pages,
        "line_items": line_items,
        "parse_attempts": parse_attempts,
        "selected_parse_attempt_id": selected_parse_attempt_id,
        "audit_events": audit_events,
        "extracted_supplier_profile": supplier_profile,
        "supplier_create_payload": supplier_create_payload,
        "extracted_document_profile": document_profile,
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
        "counts": {
            "document_pages": len(document_pages),
            "line_items": len(line_items),
            "parse_attempts": len(parse_attempts),
            "audit_events": len(audit_events),
        },
        "fetch_errors": fetch_errors,
    }


def process_next_queued_invoice_job(*, organisation_id: Optional[str] = None) -> dict:
    job = get_next_queued_job(supabase, organisation_id=organisation_id)

    if not job:
        return {
            "success": True,
            "status": "empty",
            "message": "No queued invoice jobs found.",
        }

    job_id = job["id"]
    invoice_raw_id = job["invoice_raw_id"]
    organisation_id = job["organisation_id"]

    try:
        mark_job_processing(supabase, job_id=job_id, stage="starting")
        safe_update_invoice_raw_status(
            supabase,
            invoice_raw_id=invoice_raw_id,
            parse_status="processing",
            extra={"parse_started_at": utc_now_iso()},
        )

        log_invoice_event(
            supabase,
            organisation_id=organisation_id,
            invoice_raw_id=invoice_raw_id,
            job_id=job_id,
            event_type="job_processing_started",
            stage="processing",
            actor_type="worker",
        )

        result = run_invoice_extraction(
            invoice_raw_id=invoice_raw_id,
            organisation_id=organisation_id,
            job_id=job_id,
        )

        mark_job_completed(supabase, job_id=job_id, stage="completed")
        return {
            "success": True,
            "status": "completed",
            "job_id": job_id,
            "result": result,
        }
    except Exception as exc:
        error_message = str(exc)
        mark_job_failed(supabase, job_id=job_id, error=error_message, stage="failed")
        safe_update_invoice_raw_status(supabase, invoice_raw_id=invoice_raw_id, parse_status="failed")

        log_invoice_event(
            supabase,
            organisation_id=organisation_id,
            invoice_raw_id=invoice_raw_id,
            job_id=job_id,
            event_type="job_failed",
            stage="failed",
            actor_type="worker",
            notes=error_message,
        )

        return {
            "success": False,
            "status": "failed",
            "job_id": job_id,
            "invoice_raw_id": invoice_raw_id,
            "error": error_message,
        }


def run_extract_worker_until_empty(organisation_id: Optional[str] = None) -> None:
    acquired = EXTRACT_WORKER_LOCK.acquire(blocking=False)
    if not acquired:
        return

    try:
        for _ in range(100):
            result = process_next_queued_invoice_job(organisation_id=organisation_id)
            if result.get("status") == "empty":
                return
    finally:
        EXTRACT_WORKER_LOCK.release()


@router.post("/jobs/process-next")
def process_next_invoice_job(payload: ProcessNextJobRequest):
    return process_next_queued_invoice_job(organisation_id=payload.organisation_id)


@router.get("/raw/{invoice_raw_id}/file")
def get_invoice_raw_file(invoice_raw_id: str):
    raw = get_raw_invoice(invoice_raw_id)
    file_path = raw.get("file_path")
    file_type = raw.get("file_type") or "application/pdf"

    if not file_path:
        raise HTTPException(status_code=400, detail="Missing file_path")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Storage download error: {str(e)}")

    return Response(
        content=file_bytes,
        media_type=file_type,
        headers={
            "Content-Disposition": f'inline; filename="{raw.get("file_name", "invoice.pdf")}"',
        },
    )


@router.get("/raw/{invoice_raw_id}/preview-image")
def get_invoice_preview_image(invoice_raw_id: str, page: int = 0):
    raw = get_raw_invoice(invoice_raw_id)
    file_path = raw.get("file_path")
    file_type = raw.get("file_type") or "application/pdf"

    if not file_path:
        raise HTTPException(status_code=400, detail="Missing file_path")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Storage download error: {str(e)}")

    try:
        if file_type == "application/pdf" or file_path.lower().endswith(".pdf"):
            doc = fitz.open(stream=file_bytes, filetype="pdf")

            if page < 0 or page >= len(doc):
                raise HTTPException(status_code=400, detail="Invalid page number")

            pdf_page = doc[page]
            matrix = fitz.Matrix(2, 2)
            pix = pdf_page.get_pixmap(matrix=matrix, alpha=False)
            image_bytes = pix.tobytes("png")

            return Response(
                content=image_bytes,
                media_type="image/png",
                headers={"Cache-Control": "no-store"},
            )

        return Response(
            content=file_bytes,
            media_type=file_type,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Preview rendering failed: {str(e)}")


@router.post("/save-line-items")
def save_invoice_line_items(req: SaveLineItemsRequest):
    """
    Persist user-edited line items and recompute invoices_extracted totals.
    Line items are the source of truth: subtotal = SUM(line_total), VAT = subtotal * rate,
    total = subtotal + VAT.  Rounding differences vs document_total are absorbed automatically.
    """
    # 1. Determine VAT rate — only if supplier is a VAT vendor (has vat_number)
    vat_rate = 0.0
    if req.supplier_id:
        settings = fetch_supplier_processing_settings(supabase, req.supplier_id)
        if settings.get("vat_number"):
            raw_rate = settings.get("default_vat_rate")
            vat_rate = float(raw_rate) / 100 if raw_rate else 0.15

    # 2. Subtotal from line items
    subtotal = round(sum(float(it.get("line_total") or 0) for it in req.line_items), 2)

    # 3. Computed VAT and total
    computed_vat = round(subtotal * vat_rate, 2)
    computed_total = round(subtotal + computed_vat, 2)

    # 4. Hybrid rounding adjustment vs original document total
    rounding_applied = None
    needs_review = False
    final_line_items = list(req.line_items)

    if req.document_total is not None:
        diff = round(req.document_total - computed_total, 2)
        abs_diff = abs(diff)
        if 0 < abs_diff <= 0.02:
            # Absorb silently into VAT (floating-point drift)
            computed_vat = round(computed_vat + diff, 2)
            computed_total = round(subtotal + computed_vat, 2)
            rounding_applied = "vat_adjusted"
        elif abs_diff <= 0.50:
            # Named rounding line item for visible penny differences
            final_line_items.append({"description": "Rounding adjustment", "line_total": diff})
            subtotal = round(subtotal + diff, 2)
            computed_total = round(subtotal + computed_vat, 2)
            rounding_applied = "line_item_added"
        elif abs_diff > 0.50:
            # Too large to auto-fix — flag for human review
            needs_review = True
            rounding_applied = "needs_review"

    # 5. Persist line items (always delete-and-replace on explicit save)
    try:
        diagnostics = replace_invoice_line_items(
            supabase,
            invoice_extracted_id=req.invoice_extracted_id,
            organisation_id=req.organisation_id,
            line_items=final_line_items,
            invoice_total=computed_total,
            delete_when_empty=True,
            raise_on_error=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save line items: {exc}")

    # 6. Update invoices_extracted with derived totals
    patch: dict = {
        "subtotal": subtotal,
        "tax_amount": computed_vat,
        "total_amount": computed_total,
    }
    if needs_review:
        patch["validation_status"] = "needs_review"

    supabase.table("invoices_extracted").update(patch).eq("id", req.invoice_extracted_id).execute()

    return {
        "subtotal": subtotal,
        "tax_amount": computed_vat,
        "total_amount": computed_total,
        "rounding_applied": rounding_applied,
        "needs_review": needs_review,
        "diagnostics": diagnostics,
    }


class ReapplyRulesRequest(BaseModel):
    invoice_extracted_id: str
    organisation_id: str


@router.post("/reapply-supplier-rules")
def reapply_supplier_rules_endpoint(req: ReapplyRulesRequest):
    result = (
        supabase.table("invoices_extracted")
        .select("*, supplier:suppliers(*)")
        .eq("id", req.invoice_extracted_id)
        .eq("organisation_id", req.organisation_id)
        .single()
        .execute()
    )
    invoice = result.data if result else None
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    supplier_id = invoice.get("supplier_id")
    if not supplier_id:
        raise HTTPException(status_code=422, detail="No supplier linked to this invoice")

    rules_applied = reapply_supplier_rules_to_invoice(
        supabase,
        invoice=invoice,
        supplier_id=supplier_id,
        actor_type="user",
        event_reason="Manual re-apply of supplier rules via UI.",
    )

    # Recompute and persist subtotal / tax / total from the fresh line items
    if not rules_applied.get("skipped"):
        _recompute_invoice_totals(supabase, req.invoice_extracted_id, invoice)

    return {"success": True, "rules_applied": rules_applied}


def _recompute_invoice_totals(supabase_client, invoice_extracted_id: str, invoice: dict) -> None:
    """After line items are replaced, recompute subtotal and tax_amount on invoices_extracted.

    NOTE: total_amount is intentionally NOT updated here — it holds the value extracted
    from the source document (OCR/VLM) and must not be overwritten by a line-items
    recalculation (doing so would corrupt the 'Invoice total (from document)' display
    and break the reconciliation green/red dot logic).
    """
    try:
        rows = (
            supabase_client.table("invoice_line_items")
            .select("line_total")
            .eq("invoice_extracted_id", invoice_extracted_id)
            .execute()
        ).data or []

        subtotal = round(sum(float(r.get("line_total") or 0) for r in rows), 2)

        # Determine VAT rate from the linked supplier (already joined in the invoice dict)
        supplier = invoice.get("supplier") or {}
        vat_rate = 0.0
        if supplier.get("vat_number"):
            raw_rate = supplier.get("default_vat_rate")
            vat_rate = float(raw_rate) / 100 if raw_rate else 0.15

        tax_amount = round(subtotal * vat_rate, 2)

        supabase_client.table("invoices_extracted").update({
            "subtotal": subtotal,
            "tax_amount": tax_amount,
            # total_amount deliberately omitted — preserve the document-extracted value
        }).eq("id", invoice_extracted_id).execute()

        print(f"REAPPLY: recomputed totals for {invoice_extracted_id}: "
              f"subtotal={subtotal}, tax={tax_amount}")
    except Exception as exc:
        print(f"REAPPLY: failed to recompute totals for {invoice_extracted_id}: {exc}")


class MergeInvoicesPayload(BaseModel):
    invoice_raw_ids: list[str]
    organisation_id: str


@router.post("/merge")
def merge_invoices(payload: MergeInvoicesPayload, background_tasks: BackgroundTasks):
    """
    Merge two or more single-page invoice uploads into one multi-page document and
    trigger a fresh extraction.  Old raw records (and all dependent data) are deleted.
    """
    import time as _time

    if len(payload.invoice_raw_ids) < 2:
        raise HTTPException(status_code=400, detail="At least two invoice_raw_ids are required")

    # Step 1 — fetch raw records in caller-specified page order
    rows_result = (
        supabase.from_("invoices_raw")
        .select("id, file_path, file_name, file_type")
        .in_("id", payload.invoice_raw_ids)
        .eq("organisation_id", payload.organisation_id)
        .execute()
    )
    rows = rows_result.data or []
    if len(rows) != len(payload.invoice_raw_ids):
        raise HTTPException(status_code=404, detail="One or more invoices not found")

    id_to_row = {r["id"]: r for r in rows}
    ordered = [id_to_row[rid] for rid in payload.invoice_raw_ids]

    # Step 2 — download each file from Storage
    file_bytes_list: list[tuple[bytes, str]] = []
    for row in ordered:
        file_bytes = supabase.storage.from_("invoices").download(row["file_path"])
        file_bytes_list.append((file_bytes, row.get("file_type") or "application/pdf"))

    # Step 3 — merge into one PDF using PyMuPDF (fitz is already imported at module level)
    import io as _io
    from PIL import Image as _PILImage, ImageOps as _ImageOps

    merged_doc = fitz.open()
    for file_bytes, file_type in file_bytes_list:
        if file_type.startswith("image/"):
            # Apply EXIF orientation before embedding so the PDF is correctly oriented.
            pil_img = _ImageOps.exif_transpose(_PILImage.open(_io.BytesIO(file_bytes))).convert("RGB")
            corrected_buf = _io.BytesIO()
            pil_img.save(corrected_buf, format="PNG")
            img_doc = fitz.open(stream=corrected_buf.getvalue(), filetype="png")
            pdf_bytes = img_doc.convert_to_pdf()
            img_doc.close()
            src = fitz.open("pdf", pdf_bytes)
        else:
            src = fitz.open(stream=file_bytes, filetype="pdf")
        merged_doc.insert_pdf(src)
        src.close()
    merged_bytes = merged_doc.tobytes()
    merged_doc.close()

    # Step 4 — upload merged PDF to Storage
    base_name = re.sub(r"[^a-zA-Z0-9._-]", "_", ordered[0].get("file_name") or "merged")
    if not base_name.lower().endswith(".pdf"):
        base_name = base_name + ".pdf"
    new_file_name = f"{int(_time.time())}-merged-{base_name}"
    new_path = f"{payload.organisation_id}/invoices/{new_file_name}"

    supabase.storage.from_("invoices").upload(
        new_path,
        merged_bytes,
        {"content-type": "application/pdf"},
    )

    # Step 5 — create new invoices_raw record
    new_raw_result = (
        supabase.from_("invoices_raw")
        .insert({
            "organisation_id": payload.organisation_id,
            "file_path": new_path,
            "file_name": new_file_name,
            "file_type": "application/pdf",
            "parse_status": "pending",
            "upload_status": "uploaded",
        })
        .execute()
    )
    new_raw_id = new_raw_result.data[0]["id"]

    # Step 6 — delete old records (manual cascade: no FK cascade on invoices_raw)
    for old_id in payload.invoice_raw_ids:
        extracted_rows = (
            supabase.from_("invoices_extracted")
            .select("id")
            .eq("invoice_raw_id", old_id)
            .execute()
        ).data or []
        extracted_ids = [r["id"] for r in extracted_rows]

        if extracted_ids:
            supabase.from_("invoice_line_items").delete().in_("invoice_extracted_id", extracted_ids).execute()
            try:
                supabase.from_("invoice_extraction_feedback").delete().in_("invoice_extracted_id", extracted_ids).execute()
            except Exception:
                pass
            supabase.from_("invoices_extracted").delete().eq("invoice_raw_id", old_id).execute()

        supabase.from_("invoice_parse_attempts").delete().eq("invoice_raw_id", old_id).execute()
        supabase.from_("document_pages").delete().eq("invoice_raw_id", old_id).execute()
        supabase.from_("invoice_audit_events").delete().eq("invoice_raw_id", old_id).execute()

        try:
            supabase.storage.from_("invoices").remove([id_to_row[old_id]["file_path"]])
        except Exception:
            pass

        supabase.from_("invoices_raw").delete().eq("id", old_id).execute()

    # Step 7 — queue extraction on the merged record
    queue_invoice_job(invoice_raw_id=new_raw_id, organisation_id=payload.organisation_id)
    background_tasks.add_task(run_extract_worker_until_empty)

    return {"success": True, "new_invoice_raw_id": new_raw_id}


@router.post("/{raw_id}/split-into-pages")
def split_invoice_into_pages(raw_id: str, organisation_id: str, background_tasks: BackgroundTasks):
    """
    Split a multi-page PDF into individual single-page invoices, one per page.
    Each page is uploaded as a new invoices_raw record and queued for extraction.
    The original record and all dependent data are deleted.
    """
    import time as _time

    # Step 1 — fetch the raw record
    row_result = (
        supabase.from_("invoices_raw")
        .select("id, file_path, file_name, file_type")
        .eq("id", raw_id)
        .eq("organisation_id", organisation_id)
        .single()
        .execute()
    )
    row = row_result.data
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Step 2 — download and open with PyMuPDF
    file_bytes = supabase.storage.from_("invoices").download(row["file_path"])
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    page_count = len(doc)
    if page_count < 2:
        doc.close()
        raise HTTPException(status_code=422, detail="Document has only one page — use the crop tool for within-page splits")

    # Step 3 — split into N single-page PDFs and upload each
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", row.get("file_name") or "split")
    if safe_name.lower().endswith(".pdf"):
        safe_name = safe_name[:-4]

    new_raw_ids: list[str] = []
    for i in range(page_count):
        single_doc = fitz.open()
        single_doc.insert_pdf(doc, from_page=i, to_page=i)
        page_bytes = single_doc.tobytes()
        single_doc.close()

        new_file_name = f"{int(_time.time())}-p{i + 1}-{safe_name}.pdf"
        new_path = f"{organisation_id}/invoices/{new_file_name}"

        supabase.storage.from_("invoices").upload(
            new_path,
            page_bytes,
            {"content-type": "application/pdf"},
        )

        new_raw_result = (
            supabase.from_("invoices_raw")
            .insert({
                "organisation_id": organisation_id,
                "file_path": new_path,
                "file_name": new_file_name,
                "file_type": "application/pdf",
                "parse_status": "pending",
                "upload_status": "uploaded",
            })
            .execute()
        )
        new_raw_ids.append(new_raw_result.data[0]["id"])

    doc.close()

    # Step 4 — delete original record (same cascade order as merge)
    extracted_rows = (
        supabase.from_("invoices_extracted")
        .select("id")
        .eq("invoice_raw_id", raw_id)
        .execute()
    ).data or []
    extracted_ids = [r["id"] for r in extracted_rows]

    if extracted_ids:
        supabase.from_("invoice_line_items").delete().in_("invoice_extracted_id", extracted_ids).execute()
        try:
            supabase.from_("invoice_extraction_feedback").delete().in_("invoice_extracted_id", extracted_ids).execute()
        except Exception:
            pass
        supabase.from_("invoices_extracted").delete().eq("invoice_raw_id", raw_id).execute()

    supabase.from_("invoice_parse_attempts").delete().eq("invoice_raw_id", raw_id).execute()
    supabase.from_("document_pages").delete().eq("invoice_raw_id", raw_id).execute()
    supabase.from_("invoice_audit_events").delete().eq("invoice_raw_id", raw_id).execute()

    try:
        supabase.storage.from_("invoices").remove([row["file_path"]])
    except Exception:
        pass

    supabase.from_("invoices_raw").delete().eq("id", raw_id).execute()

    # Step 5 — queue extraction for all new records and drain the worker
    for new_id in new_raw_ids:
        queue_invoice_job(invoice_raw_id=new_id, organisation_id=organisation_id)
    background_tasks.add_task(run_extract_worker_until_empty)

    return {"success": True, "page_count": page_count, "new_raw_ids": new_raw_ids}


class _PageCropModel(BaseModel):
    x: float
    y: float
    w: float
    h: float


class _PageRefModel(BaseModel):
    kind: str               # "full" | "crop"
    page_number: int        # 1-indexed
    crop: _PageCropModel | None = None


class _PageGroupModel(BaseModel):
    pages: list[_PageRefModel]


class ProcessPageGroupsPayload(BaseModel):
    invoice_raw_id: str
    organisation_id: str
    groups: list[_PageGroupModel]


@router.post("/process-page-groups")
def process_page_groups(payload: ProcessPageGroupsPayload, background_tasks: BackgroundTasks):
    """
    Split a multi-page PDF into one output PDF per group, where each group is a user-defined
    set of full pages and/or cropped regions.  Supports both "each page is a doc" and
    "multiple docs on one page" scenarios.  Original record is deleted after splitting.
    """
    import time as _time

    if not payload.groups:
        raise HTTPException(status_code=400, detail="At least one group is required")

    # Step 1 — fetch original raw record
    row_result = (
        supabase.from_("invoices_raw")
        .select("id, file_path, file_name, file_type")
        .eq("id", payload.invoice_raw_id)
        .eq("organisation_id", payload.organisation_id)
        .single()
        .execute()
    )
    row = row_result.data
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Step 2 — download and open original PDF
    file_bytes = supabase.storage.from_("invoices").download(row["file_path"])
    doc = fitz.open(stream=file_bytes, filetype="pdf")

    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", row.get("file_name") or "split")
    if safe_name.lower().endswith(".pdf"):
        safe_name = safe_name[:-4]

    # Step 3 — build one output PDF per group
    new_raw_ids: list[str] = []
    for group_idx, group in enumerate(payload.groups):
        out_doc = fitz.open()
        for ref in group.pages:
            page_index = ref.page_number - 1   # 1-indexed → 0-indexed
            if page_index < 0 or page_index >= len(doc):
                continue
            src_page = doc[page_index]
            if ref.kind == "full":
                tmp = fitz.open()
                tmp.insert_pdf(doc, from_page=page_index, to_page=page_index)
                out_doc.insert_pdf(tmp)
                tmp.close()
            elif ref.kind == "crop" and ref.crop:
                c = ref.crop
                rect = fitz.Rect(
                    src_page.rect.width  * c.x,
                    src_page.rect.height * c.y,
                    src_page.rect.width  * (c.x + c.w),
                    src_page.rect.height * (c.y + c.h),
                )
                pix = src_page.get_pixmap(clip=rect, dpi=200)
                img_doc = fitz.open()
                img_page = img_doc.new_page(width=pix.width, height=pix.height)
                img_page.insert_image(img_page.rect, pixmap=pix)
                out_doc.insert_pdf(img_doc)
                img_doc.close()

        if len(out_doc) == 0:
            out_doc.close()
            continue

        out_bytes = out_doc.tobytes()
        out_doc.close()

        new_file_name = f"{int(_time.time())}-g{group_idx + 1}-{safe_name}.pdf"
        new_path = f"{payload.organisation_id}/invoices/{new_file_name}"
        supabase.storage.from_("invoices").upload(
            new_path, out_bytes, {"content-type": "application/pdf"}
        )
        new_raw_result = (
            supabase.from_("invoices_raw")
            .insert({
                "organisation_id": payload.organisation_id,
                "file_path": new_path,
                "file_name": new_file_name,
                "file_type": "application/pdf",
                "parse_status": "pending",
                "upload_status": "uploaded",
            })
            .execute()
        )
        new_raw_ids.append(new_raw_result.data[0]["id"])

    doc.close()

    # Step 4 — delete original record (same cascade as split/merge)
    extracted_rows = (
        supabase.from_("invoices_extracted")
        .select("id")
        .eq("invoice_raw_id", payload.invoice_raw_id)
        .execute()
    ).data or []
    extracted_ids = [r["id"] for r in extracted_rows]

    if extracted_ids:
        supabase.from_("invoice_line_items").delete().in_("invoice_extracted_id", extracted_ids).execute()
        try:
            supabase.from_("invoice_extraction_feedback").delete().in_("invoice_extracted_id", extracted_ids).execute()
        except Exception:
            pass
        supabase.from_("invoices_extracted").delete().eq("invoice_raw_id", payload.invoice_raw_id).execute()

    supabase.from_("invoice_parse_attempts").delete().eq("invoice_raw_id", payload.invoice_raw_id).execute()
    supabase.from_("document_pages").delete().eq("invoice_raw_id", payload.invoice_raw_id).execute()
    supabase.from_("invoice_audit_events").delete().eq("invoice_raw_id", payload.invoice_raw_id).execute()

    try:
        supabase.storage.from_("invoices").remove([row["file_path"]])
    except Exception:
        pass

    supabase.from_("invoices_raw").delete().eq("id", payload.invoice_raw_id).execute()

    # Step 5 — queue extraction for all new records
    for new_id in new_raw_ids:
        queue_invoice_job(invoice_raw_id=new_id, organisation_id=payload.organisation_id)
    background_tasks.add_task(run_extract_worker_until_empty)

    return {"success": True, "group_count": len(new_raw_ids), "new_raw_ids": new_raw_ids}


@router.post("/generate-preview")
def generate_invoice_preview(req: GeneratePreviewRequest):
    """
    Render preview images for an invoice without running VLM extraction (~1s).
    Saves images to Supabase Storage and upserts document_pages rows.
    Fixes missing previews for old invoices and PDFs processed via the selectable-text path.
    """
    import io as _io
    from PIL import Image as _Image
    from app.services.invoice_ocr_pipeline import pdf_to_images
    from app.services.invoice_extraction.receipt_preprocessing import generate_preview_images
    from app.services.invoice_previews import upload_invoice_preview_image

    raw_res = supabase.table("invoices_raw").select("file_path, file_type").eq("id", req.invoice_raw_id).single().execute()
    if not raw_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    raw = raw_res.data
    file_path = raw.get("file_path")
    if not file_path:
        raise HTTPException(status_code=400, detail="No file_path on invoices_raw record")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Storage download failed: {e}")

    file_type = raw.get("file_type") or "application/pdf"
    is_pdf = "pdf" in str(file_type).lower()

    try:
        if is_pdf:
            images = pdf_to_images(file_bytes)
        else:
            images = [_Image.open(_io.BytesIO(file_bytes)).convert("RGB")]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Page rendering failed: {e}")

    results = []
    for i, img in enumerate(images, 1):
        try:
            previews = generate_preview_images(img, img)
            orig_path = f"{req.organisation_id}/invoices/previews/{req.invoice_raw_id}/page-{i}-original.jpg"
            proc_path = f"{req.organisation_id}/invoices/previews/{req.invoice_raw_id}/page-{i}-processed.jpg"
            upload_invoice_preview_image(supabase, storage_path=orig_path, image=previews.original_preview)
            upload_invoice_preview_image(supabase, storage_path=proc_path, image=previews.processed_preview)
            results.append({
                "page_number": i,
                "original_preview_path": orig_path,
                "processed_preview_path": proc_path,
            })
        except Exception as e:
            print(f"[generate-preview] page {i} upload failed: {e}")

    # Upsert document_pages rows
    for page in results:
        existing = supabase.table("document_pages").select("id").eq("invoice_raw_id", req.invoice_raw_id).eq("page_number", page["page_number"]).execute().data
        if existing:
            supabase.table("document_pages").update({
                "original_preview_path": page["original_preview_path"],
                "processed_preview_path": page["processed_preview_path"],
            }).eq("invoice_raw_id", req.invoice_raw_id).eq("page_number", page["page_number"]).execute()
        else:
            supabase.table("document_pages").insert({
                "invoice_raw_id": req.invoice_raw_id,
                "organisation_id": req.organisation_id,
                "page_number": page["page_number"],
                "original_preview_path": page["original_preview_path"],
                "processed_preview_path": page["processed_preview_path"],
            }).execute()

    # Update invoices_raw with page-1 preview path
    if results:
        supabase.table("invoices_raw").update({
            "preview_path": results[0]["original_preview_path"],
            "processed_preview_path": results[0]["processed_preview_path"],
            "updated_at": utc_now_iso(),
        }).eq("id", req.invoice_raw_id).execute()

    return {"generated": len(results), "pages": results}
