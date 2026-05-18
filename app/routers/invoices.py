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
    fetch_parse_attempts,
    persist_parse_attempts,
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

router = APIRouter(prefix="/api/invoices", tags=["invoices"])
supabase = get_supabase_client()


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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def build_extracted_supplier_profile(parsed_data: dict) -> dict:
    return {
        "supplier_name": parsed_data.get("supplier_name_extracted") or parsed_data.get("issuer_name_extracted"),
        "supplier_code": parsed_data.get("cus_code_extracted"),
        "account_number": parsed_data.get("cus_code_extracted"),
        "currency": parsed_data.get("currency"),
        "default_email": parsed_data.get("supplier_email_extracted"),
        "accounting_email": parsed_data.get("supplier_acc_email_extracted"),
        "phone": parsed_data.get("supplier_telephone_extracted"),
        "telephone": parsed_data.get("supplier_telephone_extracted"),
        "fax": parsed_data.get("supplier_fax_extracted"),
        "cell": parsed_data.get("supplier_cell_extracted"),
        "website": parsed_data.get("supplier_website_extracted"),
        "delivery_address": parsed_data.get("supplier_del_address_extracted"),
        "postal_address": parsed_data.get("supplier_pos_address_extracted"),
        "vat_number": parsed_data.get("vat_number_extracted"),
        "tax_number": parsed_data.get("vat_number_extracted"),
        "company_registration_number": parsed_data.get("company_registration_number_extracted"),
        "registration_number": parsed_data.get("company_registration_number_extracted"),
        "bank_account_name": parsed_data.get("bank_account_name_extracted"),
        "bank_name": parsed_data.get("bank_name_extracted"),
        "bank_account_number": parsed_data.get("bank_account_number_extracted"),
        "bank_branch_code": parsed_data.get("bank_branch_code_extracted"),
        "bank_swift_code": parsed_data.get("bank_swift_code_extracted"),
        # Raw extraction aliases for frontends that render invoices_extracted names.
        "supplier_name_extracted": parsed_data.get("supplier_name_extracted"),
        "supplier_email_extracted": parsed_data.get("supplier_email_extracted"),
        "supplier_acc_email_extracted": parsed_data.get("supplier_acc_email_extracted"),
        "supplier_telephone_extracted": parsed_data.get("supplier_telephone_extracted"),
        "supplier_fax_extracted": parsed_data.get("supplier_fax_extracted"),
        "supplier_cell_extracted": parsed_data.get("supplier_cell_extracted"),
        "supplier_website_extracted": parsed_data.get("supplier_website_extracted"),
        "supplier_del_address_extracted": parsed_data.get("supplier_del_address_extracted"),
        "supplier_pos_address_extracted": parsed_data.get("supplier_pos_address_extracted"),
        "vat_number_extracted": parsed_data.get("vat_number_extracted"),
        "company_registration_number_extracted": parsed_data.get("company_registration_number_extracted"),
        "cus_code_extracted": parsed_data.get("cus_code_extracted"),
    }


def build_extracted_document_profile(parsed_data: dict) -> dict:
    return {
        "invoice_number": parsed_data.get("invoice_number"),
        "invoice_date": parsed_data.get("invoice_date"),
        "due_date": parsed_data.get("due_date"),
        "subtotal": parsed_data.get("subtotal"),
        "tax_amount": parsed_data.get("tax_amount"),
        "total_amount": parsed_data.get("total_amount"),
        "currency": parsed_data.get("currency"),
        "line_items": parsed_data.get("line_items") or [],
        "supplier": build_extracted_supplier_profile(parsed_data),
    }


def build_supplier_create_payload(
    *,
    organisation_id: str,
    invoice_raw_id: str,
    invoice_extracted_id: Optional[str],
    parsed_data: dict,
) -> dict:
    profile = build_extracted_supplier_profile(parsed_data)
    return {
        "organisation_id": organisation_id,
        "supplier_name": profile.get("supplier_name"),
        "supplier_code": profile.get("supplier_code"),
        "account_number": profile.get("supplier_code"),
        "currency": profile.get("currency"),
        "default_email": profile.get("accounting_email") or profile.get("default_email"),
        "phone": profile.get("phone") or profile.get("cell"),
        "vat_number": profile.get("vat_number"),
        "tax_number": profile.get("vat_number"),
        "registration_number": profile.get("company_registration_number"),
        "company_registration_number": profile.get("company_registration_number"),
        "bank_account_name": profile.get("bank_account_name"),
        "bank_name": profile.get("bank_name"),
        "bank_account_number": profile.get("bank_account_number"),
        "bank_branch_code": profile.get("bank_branch_code"),
        "bank_swift_code": profile.get("bank_swift_code"),
        "bank_country": "ZA" if (profile.get("currency") or "ZAR") == "ZAR" else None,
        "delivery_address": profile.get("delivery_address"),
        "postal_address": profile.get("postal_address"),
        "accounting_email": profile.get("accounting_email"),
        "fax": profile.get("fax"),
        "cell": profile.get("cell"),
        "website": profile.get("website"),
        "invoice_extracted_id": invoice_extracted_id,
        "invoice_raw_id": invoice_raw_id,
        "link_invoice": True,
    }


REEXTRACT_FIELD_MAP = {
    "supplier_name_extracted": "supplier_name_extracted",
    "invoice_number": "invoice_number",
    "invoice_date": "invoice_date",
    "due_date": "due_date",
    "subtotal": "subtotal",
    "tax_amount": "tax_amount",
    "total_amount": "total_amount",
    "currency": "currency",
    "supplier_del_address_extracted": "supplier_del_address_extracted",
    "supplier_pos_address_extracted": "supplier_pos_address_extracted",
    "supplier_email_extracted": "supplier_email_extracted",
    "supplier_acc_email_extracted": "supplier_acc_email_extracted",
    "supplier_telephone_extracted": "supplier_telephone_extracted",
    "supplier_fax_extracted": "supplier_fax_extracted",
    "supplier_cell_extracted": "supplier_cell_extracted",
    "supplier_website_extracted": "supplier_website_extracted",
    "vat_number_extracted": "vat_number_extracted",
    "cus_code_extracted": "cus_code_extracted",
    "company_registration_number_extracted": "company_registration_number_extracted",
    "bank_account_name_extracted": "bank_account_name_extracted",
    "bank_name_extracted": "bank_name_extracted",
    "bank_account_number_extracted": "bank_account_number_extracted",
    "bank_branch_code_extracted": "bank_branch_code_extracted",
    "bank_swift_code_extracted": "bank_swift_code_extracted",
    "issuer_name_extracted": "issuer_name_extracted",
    "recipient_name_extracted": "recipient_name_extracted",
    "document_direction": "document_direction",
    "organisation_match_status": "organisation_match_status",
    "validation_status": "validation_status",
    "validation_notes": "validation_notes",
}


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _looks_suspicious_value(field_name: str, value) -> bool:
    if not _has_value(value):
        return True
    if not isinstance(value, str):
        return False

    clean = value.strip()
    if field_name == "supplier_name_extracted":
        alpha_count = sum(char.isalpha() for char in clean)
        if clean.startswith("_") or alpha_count < 3:
            return True
        if len(clean) <= 4 and clean.lower() in {"pty", "ltd", "copy"}:
            return True
        if clean.lower() in {"original", "customer copy", "copy of original", "welcome", "welcame", "welkom"}:
            return True
    if field_name == "supplier_del_address_extracted":
        lower = clean.lower()
        if "scan to rate" in lower or "survey" in lower:
            return True
    return False


def _valid_reextract_value(field_name: str, value) -> bool:
    if not _has_value(value):
        return False

    if field_name in {"subtotal", "total_amount"}:
        try:
            return float(value) > 0
        except Exception:
            return False
    if field_name == "tax_amount":
        try:
            return float(value) >= 0
        except Exception:
            return False
    if field_name in {"vat_number_extracted", "bank_account_number_extracted"}:
        return len("".join(char for char in str(value) if char.isdigit())) >= 7
    if field_name in {"supplier_telephone_extracted", "supplier_fax_extracted", "supplier_cell_extracted"}:
        return len("".join(char for char in str(value) if char.isdigit())) >= 7
    if field_name == "supplier_name_extracted":
        clean = str(value).strip()
        lower = clean.lower()
        if lower in {"copy", "original", "customer copy", "copy of original", "tax invoice", "welcome", "welcame", "welkom"}:
            return False
        if "copy of original" in lower or "customer copy" in lower:
            return False
        if clean.startswith("_"):
            return False
        words = [word for word in re.findall(r"[A-Za-z]+", clean) if word]
        if not words:
            return False
        if len(words) == 1 and len(words[0]) <= 4:
            return False
        if len(words) == 1 and not re.search(r"\b(build|builders|makro|massmart|pinetown)\b", lower):
            return False
        return True
    if field_name == "invoice_number":
        return any(char.isdigit() for char in str(value))
    if field_name in {"invoice_date", "due_date"}:
        return bool(str(value).strip())
    return True


def build_reextract_update(
    *,
    existing: dict,
    parsed: dict,
    force_update: bool = False,
) -> tuple[dict, list[dict], list[str]]:
    update_payload: dict = {}
    improved_fields: list[dict] = []
    unchanged_fields: list[str] = []
    old_confidence = existing.get("confidence_score") or 0
    new_confidence = parsed.get("confidence_score") or 0
    confidence_materially_improved = new_confidence >= old_confidence + 0.15

    for target_field, parsed_key in REEXTRACT_FIELD_MAP.items():
        new_value = parsed.get(parsed_key)
        old_value = existing.get(target_field)

        if not _valid_reextract_value(target_field, new_value):
            if target_field == "supplier_name_extracted" and _looks_suspicious_value(target_field, old_value):
                update_payload[target_field] = None
                improved_fields.append({
                    "field": target_field,
                    "old_value": old_value,
                    "new_value": None,
                })
                continue
            unchanged_fields.append(target_field)
            continue

        should_update = (
            force_update
            or not _has_value(old_value)
            or _looks_suspicious_value(target_field, old_value)
            or confidence_materially_improved
        )

        if should_update and new_value != old_value:
            update_payload[target_field] = new_value
            improved_fields.append({
                "field": target_field,
                "old_value": old_value,
                "new_value": new_value,
            })
        else:
            unchanged_fields.append(target_field)

    if new_confidence is not None and (force_update or not old_confidence or new_confidence > old_confidence):
        update_payload["confidence_score"] = new_confidence

    if update_payload:
        update_payload["review_status"] = "needs_info"
        update_payload["updated_at"] = utc_now_iso()

    return update_payload, improved_fields, unchanged_fields


def _trim_region_text(region_ocr: dict, limit: int = 700) -> dict:
    region_text = (region_ocr.get("region_text_by_name") or {})
    return {
        name: (text[:limit] if text else "")
        for name, text in region_text.items()
    }


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

        parse_attempts = build_parse_attempts_from_text_result(text_result)
        if parse_attempts:
            parse_attempts[0]["parsed_data"] = dict(parsed_data)
            parse_attempts[0]["line_items"] = parsed_data.get("line_items") or []
            parse_attempts[0]["confidence_score"] = parsed_data.get("confidence_score")

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
        "updated_at": utc_now_iso(),
    }

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

    line_items = parsed_data.get("line_items", [])

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

    safe_update_invoice_raw_status(
        supabase,
        invoice_raw_id=invoice_raw_id,
        parse_status="completed",
        extra={"parse_completed_at": utc_now_iso()},
    )

    log_invoice_event(
        supabase,
        organisation_id=org_id,
        invoice_raw_id=invoice_raw_id,
        invoice_extracted_id=extracted_invoice_id,
        job_id=job_id,
        event_type="extraction_completed",
        stage="completed",
        actor_type="worker" if job_id else "api",
        confidence_after=parsed_data.get("confidence_score"),
        notes="Invoice extraction completed.",
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
    if job_id:
        update_reextract_job(job_id, status="running", stage="starting")

    raw = get_raw_invoice(payload.invoice_raw_id)
    org_id = payload.organisation_id or raw.get("organisation_id")

    if not org_id:
        raise HTTPException(status_code=400, detail="Missing organisation_id")

    file_path = raw.get("file_path")
    if not file_path:
        raise HTTPException(status_code=400, detail="Missing file_path on invoices_raw row")

    existing_res = (
        supabase
        .table("invoices_extracted")
        .select("*")
        .eq("invoice_raw_id", payload.invoice_raw_id)
        .limit(1)
        .execute()
    )
    if not existing_res.data:
        raise HTTPException(status_code=404, detail="No extracted invoice found to re-extract")

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
        new_value={"mode": "deep_region_ocr", "force_update": payload.force_update},
        notes="Deep region OCR re-extraction started.",
    )

    if job_id:
        update_reextract_job(job_id, status="running", stage="reading_document")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Storage download error: {str(exc)}")

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

        if deep_attempt:
            deep_attempt["parsed_data"] = dict(parsed_data)
            deep_attempt["line_items"] = parsed_data.get("line_items") or []
            deep_attempt["confidence_score"] = parsed_data.get("confidence_score")

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

        line_items = parsed_data.get("line_items") or []
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
        if deep_attempt:
            parse_attempts = [
                attempt
                for attempt in existing_parse_attempts
                if attempt.get("strategy") != "deep_region_ocr"
            ]
            parse_attempts.append(deep_attempt)
            parse_attempt_result = persist_parse_attempts(
                supabase,
                organisation_id=org_id,
                invoice_raw_id=payload.invoice_raw_id,
                invoice_extracted_id=extracted_invoice_id,
                attempts=parse_attempts,
                selected_attempt=deep_attempt,
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
        if job_id:
            update_reextract_job(
                job_id,
                status="failed",
                stage="failed",
                error=str(exc.detail) if exc.detail else "Re-extraction failed",
            )
        raise
    except Exception as exc:
        if job_id:
            update_reextract_job(job_id, status="failed", stage="failed", error=str(exc))
        log_invoice_event(
            supabase,
            organisation_id=org_id,
            invoice_raw_id=payload.invoice_raw_id,
            invoice_extracted_id=extracted_invoice_id,
            event_type="re_extraction_failed",
            stage="failed",
            actor_type="api",
            notes=str(exc),
        )
        raise HTTPException(status_code=400, detail=f"Deep re-extraction failed: {str(exc)}")


def run_reextract_job_background(job_id: str, payload_data: dict) -> None:
    try:
        payload = ReExtractInvoiceRequest(**payload_data)
        run_invoice_re_extraction(payload, job_id=job_id)
    except Exception as exc:
        update_reextract_job(job_id, status="failed", stage="failed", error=str(exc))


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
        raise HTTPException(status_code=400, detail="Missing file_path on invoices_raw row")

    job = create_reextract_job(
        invoice_raw_id=payload.invoice_raw_id,
        organisation_id=org_id,
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
