from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_processing_job(
    supabase,
    *,
    organisation_id: str,
    invoice_raw_id: str,
    batch_id: Optional[str] = None,
    job_type: str = "invoice_extract",
    priority: int = 100,
    extraction_strategy: Optional[str] = None,
) -> dict:
    payload = {
        "organisation_id": organisation_id,
        "batch_id": batch_id,
        "invoice_raw_id": invoice_raw_id,
        "job_type": job_type,
        "status": "queued",
        "current_stage": "queued",
        "priority": priority,
        "extraction_strategy": extraction_strategy,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }

    res = supabase.table("document_processing_jobs").insert(payload).execute()
    if not res.data:
        raise RuntimeError("Failed to create document_processing_jobs row")
    return res.data[0]


def get_next_queued_job(supabase, *, organisation_id: Optional[str] = None) -> Optional[dict]:
    query = (
        supabase
        .table("document_processing_jobs")
        .select("*")
        .eq("job_type", "invoice_extract")
        .eq("status", "queued")
        .order("priority", desc=False)
        .order("created_at", desc=False)
        .limit(1)
    )

    if organisation_id:
        query = query.eq("organisation_id", organisation_id)

    res = query.execute()
    return res.data[0] if res.data else None


def mark_job_processing(supabase, *, job_id: str, stage: str = "processing") -> None:
    supabase.table("document_processing_jobs").update({
        "status": "processing",
        "current_stage": stage,
        "started_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }).eq("id", job_id).execute()


def mark_job_stage(supabase, *, job_id: str, stage: str) -> None:
    supabase.table("document_processing_jobs").update({
        "current_stage": stage,
        "updated_at": utc_now_iso(),
    }).eq("id", job_id).execute()


def mark_job_completed(supabase, *, job_id: str, stage: str = "completed") -> None:
    supabase.table("document_processing_jobs").update({
        "status": "completed",
        "current_stage": stage,
        "completed_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }).eq("id", job_id).execute()


def mark_job_failed(supabase, *, job_id: str, error: str, stage: Optional[str] = None) -> None:
    # Fetch retry_count defensively so we can increment it without relying on SQL RPC.
    retry_count = 0
    try:
        current = (
            supabase
            .table("document_processing_jobs")
            .select("retry_count")
            .eq("id", job_id)
            .limit(1)
            .execute()
        )
        if current.data:
            retry_count = int(current.data[0].get("retry_count") or 0)
    except Exception:
        retry_count = 0

    supabase.table("document_processing_jobs").update({
        "status": "failed",
        "current_stage": stage or "failed",
        "retry_count": retry_count + 1,
        "last_error": error[:4000],
        "failed_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }).eq("id", job_id).execute()


def safe_update_invoice_raw_status(
    supabase,
    *,
    invoice_raw_id: str,
    parse_status: str,
    extra: Optional[dict] = None,
) -> None:
    """
    Update invoices_raw status as best effort.

    Some environments may have stricter parse_status constraints. We do not want
    a status update to break job creation, so failures are logged but not raised.
    """
    payload = {
        "parse_status": parse_status,
        "updated_at": utc_now_iso(),
    }
    if extra:
        payload.update(extra)

    try:
        supabase.table("invoices_raw").update(payload).eq("id", invoice_raw_id).execute()
    except Exception as exc:
        print("INVOICE_RAW STATUS UPDATE FAILED:", str(exc), payload)
