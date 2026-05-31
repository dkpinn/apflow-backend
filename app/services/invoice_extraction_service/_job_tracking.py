"""
_job_tracking.py
-----------------
DB-backed re-extract job state and extract job status helpers.

Groups A + C from the original invoice_extraction_service.py:
  A — re-extract job lifecycle helpers (backed by document_processing_jobs)
  C — document_processing_jobs DB normalisation helpers
"""
from __future__ import annotations

import uuid
from threading import Lock
from typing import Optional

from app.db.supabase_client import get_supabase_client

try:
    supabase = get_supabase_client()
except Exception:
    supabase = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Stage progress / label constants
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Concurrency lock for the extraction worker (used by _queue.py)
# ---------------------------------------------------------------------------

EXTRACT_WORKER_LOCK = Lock()


# ---------------------------------------------------------------------------
# Group A — Re-extract job lifecycle helpers (document_processing_jobs table)
# ---------------------------------------------------------------------------

def _reextract_row_to_payload(row: dict) -> dict:
    stage = row.get("current_stage") or "queued"
    return {
        "job_id": row.get("id"),
        "status": row.get("status") or "queued",
        "stage": stage,
        "stage_label": REEXTRACT_STAGE_LABELS.get(stage, stage.replace("_", " ").title()),
        "progress": REEXTRACT_STAGE_PROGRESS.get(stage, 0),
        "invoice_raw_id": row.get("invoice_raw_id"),
        "extracted_invoice_id": row.get("extracted_invoice_id"),
        "error": row.get("last_error"),
        "diagnostic": {
            **REEXTRACT_DEFAULT_DIAGNOSTIC,
            **(row.get("diagnostic") or {}),
        },
    }


def create_reextract_job(*, invoice_raw_id: str, organisation_id: Optional[str] = None) -> dict:
    job_id = str(uuid.uuid4())
    supabase.table("document_processing_jobs").insert({
        "id": job_id,
        "invoice_raw_id": invoice_raw_id,
        "organisation_id": organisation_id,
        "status": "queued",
        "current_stage": "queued",
        "job_type": "re_extraction",
    }).execute()
    return {
        "job_id": job_id,
        "invoice_raw_id": invoice_raw_id,
        "status": "queued",
        "stage": "queued",
        "stage_label": REEXTRACT_STAGE_LABELS["queued"],
        "progress": REEXTRACT_STAGE_PROGRESS["queued"],
        "extracted_invoice_id": None,
        "error": None,
        "diagnostic": dict(REEXTRACT_DEFAULT_DIAGNOSTIC),
    }


def update_reextract_job(
    job_id: str,
    *,
    status: Optional[str] = None,
    stage: Optional[str] = None,
    extracted_invoice_id: Optional[str] = None,
    error: Optional[str] = None,
    diagnostic: Optional[dict] = None,
) -> None:
    patch: dict = {}
    if status is not None:
        patch["status"] = status
    if stage is not None:
        patch["current_stage"] = stage
    if extracted_invoice_id is not None:
        patch["extracted_invoice_id"] = extracted_invoice_id
    if error is not None:
        patch["last_error"] = error
    if diagnostic is not None:
        patch["diagnostic"] = {**REEXTRACT_DEFAULT_DIAGNOSTIC, **diagnostic}
    if patch:
        supabase.table("document_processing_jobs").update(patch).eq("id", job_id).execute()


def get_reextract_job_status(job_id: str) -> Optional[dict]:
    res = (
        supabase
        .table("document_processing_jobs")
        .select("*")
        .eq("id", job_id)
        .maybe_single()
        .execute()
    )
    if not res.data:
        return None
    return _reextract_row_to_payload(res.data)


# ---------------------------------------------------------------------------
# Group C — Extraction job status helpers (document_processing_jobs DB rows)
# ---------------------------------------------------------------------------

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
