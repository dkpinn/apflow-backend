"""
_job_tracking.py
-----------------
In-memory re-extract job state and DB-backed extract job status helpers.

Groups A + C from the original invoice_extraction_service.py:
  A — REEXTRACT_JOBS dict, locks, and job lifecycle helpers
  C — document_processing_jobs DB normalisation helpers
"""
from __future__ import annotations

import uuid
from copy import deepcopy
from threading import Lock
from typing import Optional

from app.db.supabase_client import get_supabase_client
from app.services.invoice_data_builders import utc_now_iso

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
# In-memory re-extract job state
# ---------------------------------------------------------------------------

REEXTRACT_JOBS: dict[str, dict] = {}
REEXTRACT_JOBS_LOCK = Lock()
EXTRACT_WORKER_LOCK = Lock()


# ---------------------------------------------------------------------------
# Group A — Re-extract job lifecycle helpers
# ---------------------------------------------------------------------------

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
