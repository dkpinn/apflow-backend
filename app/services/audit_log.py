from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (str, int, float, bool, list, dict)):
        return value
    return str(value)


def log_invoice_event(
    supabase,
    *,
    organisation_id: str,
    event_type: str,
    invoice_raw_id: Optional[str] = None,
    invoice_extracted_id: Optional[str] = None,
    job_id: Optional[str] = None,
    stage: Optional[str] = None,
    field_name: Optional[str] = None,
    old_value: Any = None,
    new_value: Any = None,
    actor_type: str = "system",
    actor_user_id: Optional[str] = None,
    source: Optional[str] = "fastapi",
    confidence_before: Optional[float] = None,
    confidence_after: Optional[float] = None,
    notes: Optional[str] = None,
) -> None:
    """
    Append-only invoice audit event helper.

    This deliberately never raises to the caller. Audit logging should improve
    traceability, not break invoice extraction if an audit insert fails.
    """
    if not organisation_id or not event_type:
        return

    payload = {
        "organisation_id": organisation_id,
        "invoice_raw_id": invoice_raw_id,
        "invoice_extracted_id": invoice_extracted_id,
        "job_id": job_id,
        "event_type": event_type,
        "stage": stage,
        "field_name": field_name,
        "old_value": _json_safe(old_value),
        "new_value": _json_safe(new_value),
        "actor_type": actor_type,
        "actor_user_id": actor_user_id,
        "source": source,
        "confidence_before": confidence_before,
        "confidence_after": confidence_after,
        "notes": notes,
        "created_at": utc_now_iso(),
    }

    try:
        supabase.table("invoice_audit_events").insert(payload).execute()
    except Exception as exc:  # pragma: no cover - best-effort logging
        print("AUDIT LOG INSERT FAILED:", str(exc), payload)
