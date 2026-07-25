"""invoices_queue.py
Invoice extraction and queueing routes.

Routes: /extract, /extract/{job_id}/status, /re-extract, /re-extract/{job_id}/status,
        /queue, /raw/{invoice_raw_id}/audit-events

Extracted from invoices.py to keep router file sizes manageable.
invoices.py does NOT import this module — no circular import risk.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.routers.organisations import ExtractionStrategy
from app.services.audit_log import log_invoice_event
from app.services.invoice_extraction_service import (
    build_extract_job_status,
    create_reextract_job,
    get_processing_job,
    get_raw_invoice,
    get_reextract_job_status,
    log_reextract_failure,
    queue_invoice_job,
    run_invoice_extraction,
    run_invoice_re_extraction,
    run_reextract_job_background,
    _resolve_reextract_context,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/invoices", tags=["invoices"])

try:
    supabase = get_supabase_client()
except Exception:
    supabase = None


def _resolve_raw_invoice(
    invoice_raw_id: str,
    requested_org_id: Optional[str] = None,
) -> tuple[dict, str]:
    raw = get_raw_invoice(invoice_raw_id)
    organisation_id = raw.get("organisation_id")
    if not organisation_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if requested_org_id and str(requested_org_id) != str(organisation_id):
        raise HTTPException(status_code=400, detail="Invoice does not belong to organisation_id")
    return raw, str(organisation_id)


def _ensure_raw_write(auth: UserAuth, invoice_raw_id: str, requested_org_id: Optional[str] = None) -> tuple[dict, str]:
    raw, organisation_id = _resolve_raw_invoice(invoice_raw_id, requested_org_id)
    user_id, _db = auth
    ensure_org_write(user_id, organisation_id)
    return raw, organisation_id


def _ensure_job_read(auth: UserAuth, job: dict) -> None:
    organisation_id = job.get("organisation_id")
    if not organisation_id and job.get("invoice_raw_id"):
        _raw, organisation_id = _resolve_raw_invoice(str(job["invoice_raw_id"]))
    user_id, _db = auth
    ensure_org_read(user_id, organisation_id)


class ExtractInvoiceRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: Optional[str] = None
    batch_id: Optional[str] = None
    extraction_strategy: Optional[ExtractionStrategy] = Field(
        default=None,
        description="Optional override extraction strategy for this upload.",
    )
    process_mode: str = Field(
        default="queued",
        description="Default extraction requests are queued. Use the sync=true query flag for legacy synchronous extraction.",
    )


class QueueInvoiceRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: Optional[str] = None
    batch_id: Optional[str] = None
    extraction_strategy: Optional[ExtractionStrategy] = None
    priority: int = 100


class ReExtractInvoiceRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: Optional[str] = None
    force_update: bool = False


@router.post("/extract")
def extract_invoice(
    payload: ExtractInvoiceRequest,
    background_tasks: BackgroundTasks,
    auth: UserAuth,
    sync: bool = Query(False),
):
    """
    Legacy-compatible extraction endpoint.

    Default queues work for the dedicated invoice worker so the browser does
    not wait on a long OCR request. Use ?sync=true for blocking diagnostics.
    """
    _raw, organisation_id = _ensure_raw_write(auth, payload.invoice_raw_id, payload.organisation_id)
    if sync:
        return run_invoice_extraction(
            invoice_raw_id=payload.invoice_raw_id,
            organisation_id=organisation_id,
            extraction_strategy=payload.extraction_strategy,
        )

    job = queue_invoice_job(
        invoice_raw_id=payload.invoice_raw_id,
        organisation_id=organisation_id,
        batch_id=payload.batch_id,
        extraction_strategy=payload.extraction_strategy,
    )
    return {
        "success": True,
        "status": "queued",
        "invoice_raw_id": payload.invoice_raw_id,
        "organisation_id": job["organisation_id"],
        "job_id": job["id"],
        "message": "Invoice queued for processing.",
    }


@router.get("/extract/{job_id}/status")
def get_extract_status(job_id: str, auth: UserAuth):
    job = get_processing_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Extraction job not found")
    _ensure_job_read(auth, job)
    return build_extract_job_status(job)


@router.post("/re-extract")
def re_extract_invoice(
    payload: ReExtractInvoiceRequest,
    background_tasks: BackgroundTasks,
    auth: UserAuth,
    sync: bool = Query(False),
):
    raw, org_id = _ensure_raw_write(auth, payload.invoice_raw_id, payload.organisation_id)
    if sync:
        return run_invoice_re_extraction(
            invoice_raw_id=payload.invoice_raw_id,
            organisation_id=org_id,
            force_update=payload.force_update,
        )

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
def get_re_extract_status(job_id: str, auth: UserAuth):
    status = get_reextract_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Re-extract job not found")
    _ensure_job_read(auth, status)
    return status


@router.post("/queue")
def queue_invoice(payload: QueueInvoiceRequest, auth: UserAuth):
    _raw, organisation_id = _ensure_raw_write(auth, payload.invoice_raw_id, payload.organisation_id)
    job = queue_invoice_job(
        invoice_raw_id=payload.invoice_raw_id,
        organisation_id=organisation_id,
        batch_id=payload.batch_id,
        extraction_strategy=payload.extraction_strategy,
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
def get_invoice_audit_events(invoice_raw_id: str, auth: UserAuth):
    raw, organisation_id = _resolve_raw_invoice(invoice_raw_id)
    user_id, _db = auth
    ensure_org_read(user_id, organisation_id)

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
        "organisation_id": organisation_id,
        "event_count": len(events),
        "events": events,
    }
