"""
_queue.py
----------
Invoice job queueing and worker drain helpers.

Group F from the original invoice_extraction_service.py:
  queue_invoice_job                 — create a document_processing_jobs row and enqueue
  process_next_queued_invoice_job   — dequeue one job and run extraction
  run_extract_worker_until_empty    — drain all queued jobs (single-threaded)
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from app.db.supabase_client import get_supabase_client
from app.services.audit_log import log_invoice_event
from app.services.document_jobs import (
    create_processing_job,
    get_next_queued_job,
    mark_job_completed,
    mark_job_failed,
    mark_job_processing,
    safe_update_invoice_raw_status,
)
from app.services.invoice_data_builders import utc_now_iso
from ._helpers import get_raw_invoice
from ._job_tracking import EXTRACT_WORKER_LOCK
from ._pipeline import run_invoice_extraction

try:
    supabase = get_supabase_client()
except Exception:
    supabase = None  # type: ignore[assignment]


def queue_invoice_job(
    *,
    invoice_raw_id: str,
    organisation_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    extraction_strategy: Optional[str] = None,
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
        extraction_strategy=extraction_strategy,
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

        strategy_override = job.get("extraction_strategy")
        result = run_invoice_extraction(
            invoice_raw_id=invoice_raw_id,
            organisation_id=organisation_id,
            job_id=job_id,
            extraction_strategy=strategy_override,
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
