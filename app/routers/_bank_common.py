from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth

logger = logging.getLogger(__name__)


def svc():
    try:
        return get_supabase_client()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Supabase credentials missing") from exc


def _auth(auth: UserAuth) -> tuple[str, Any]:
    user_id, _user_db = auth
    return str(user_id), svc()


def _one(res, detail: str):
    if not res.data:
        raise HTTPException(status_code=404, detail=detail)
    return res.data[0] if isinstance(res.data, list) else res.data


def log_bank_event(db, *, organisation_id: str, event_type: str, actor_user_id: str, **details: Any) -> None:
    payload = {
        "organisation_id": organisation_id,
        "event_type": event_type,
        "actor_user_id": actor_user_id,
        "actor_type": "user",
        "bank_account_id": details.pop("bank_account_id", None),
        "bank_statement_upload_id": details.pop("bank_statement_upload_id", None),
        "bank_statement_line_id": details.pop("bank_statement_line_id", None),
        "gl_journal_id": details.pop("gl_journal_id", None),
        "details": details,
    }
    try:
        db.table("bank_audit_events").insert(payload).execute()
    except Exception:  # pragma: no cover
        logger.exception("bank audit event insert failed: %s", payload)


def _rpc_data(result: Any) -> dict[str, Any]:
    data = getattr(result, "data", result)
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def _database_error_parts(exc: Exception) -> tuple[str, Any]:
    message = str(exc)
    details: Any = None
    candidates = [getattr(exc, "message", None), getattr(exc, "details", None)]
    if exc.args and isinstance(exc.args[0], dict):
        payload = exc.args[0]
        candidates.extend([payload.get("message"), payload.get("details")])
    for candidate in candidates:
        if not candidate:
            continue
        if isinstance(candidate, str) and candidate.startswith("["):
            try:
                details = json.loads(candidate)
                continue
            except json.JSONDecodeError:
                pass
        if isinstance(candidate, str) and "blocked" in candidate.lower():
            message = candidate
        elif not isinstance(candidate, str):
            details = candidate
    return message, details


def _bank_delete_error(exc: Exception) -> HTTPException:
    message, blocked = _database_error_parts(exc)
    lowered = message.lower()
    if "blocked" in lowered or "posted or reversed" in lowered:
        return HTTPException(
            status_code=409,
            detail={
                "message": "Deletion blocked because selected bank data has posted or reversed journal history.",
                "blocked": blocked or [],
            },
        )
    if "not found" in lowered:
        return HTTPException(status_code=404, detail=message)
    return HTTPException(status_code=400, detail=message)


def _delete_bank_lines_rpc(
    db,
    *,
    organisation_id: str,
    line_ids: list[str],
    actor_user_id: str,
) -> dict[str, Any]:
    result = db.rpc(
        "delete_bank_statement_lines_atomic",
        {
            "p_org_id": organisation_id,
            "p_line_ids": line_ids,
            "p_actor_user_id": actor_user_id,
        },
    ).execute()
    return _rpc_data(result)


def _delete_bank_uploads_rpc(
    db,
    *,
    organisation_id: str,
    upload_ids: list[str],
    actor_user_id: str,
) -> dict[str, Any]:
    result = db.rpc(
        "delete_bank_statement_uploads_atomic",
        {
            "p_org_id": organisation_id,
            "p_upload_ids": upload_ids,
            "p_actor_user_id": actor_user_id,
        },
    ).execute()
    return _rpc_data(result)


def _remove_bank_upload_files(db, files: list[dict[str, Any]]) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for file in files:
        bucket = str(file.get("storage_bucket") or "statement-files")
        path = str(file.get("storage_path") or "")
        if not path:
            continue
        try:
            db.storage.from_(bucket).remove([path])
        except Exception as exc:
            failures.append({"storage_bucket": bucket, "storage_path": path, "error": str(exc)})
    return failures


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

