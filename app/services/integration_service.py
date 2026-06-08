from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.services.integration_secrets import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    secret_fingerprint,
)

SYSTEM_INTEGRATIONS_TABLE = "system_integration_configs"
SYSTEM_POLICIES_TABLE = "system_integration_policies"
SYSTEM_CRITERIA_TABLE = "system_extraction_criteria_versions"
ORG_INTEGRATIONS_TABLE = "organisation_integration_configs"
AUDIT_TABLE = "integration_audit_events"

PLATFORM_AI_PROVIDERS = {"gemini", "openai", "anthropic", "openai_compatible"}
DEFAULT_AI_POLICY_TASK = "invoice_vlm_extraction"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitise_integration(row: dict, *, include_secret: bool = False) -> dict:
    clean = dict(row)
    encrypted = clean.pop("encrypted_api_key", None)
    mask_hint = clean.pop("api_key_mask_hint", None)
    if include_secret:
        clean["api_key"] = decrypt_secret(encrypted)
    else:
        clean["api_key"] = None
        clean["api_key_masked"] = mask_hint or ("configured" if encrypted else None)
    return clean


def _insert_audit(
    db,
    *,
    event_type: str,
    actor_user_id: Optional[str],
    integration_scope: str,
    integration_id: Optional[str] = None,
    organisation_id: Optional[str] = None,
    provider: Optional[str] = None,
    capability: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    payload = {
        "event_type": event_type,
        "actor_user_id": actor_user_id,
        "integration_scope": integration_scope,
        "integration_id": integration_id,
        "organisation_id": organisation_id,
        "provider": provider,
        "capability": capability,
        "details": details or {},
        "created_at": utc_now_iso(),
    }
    try:
        db.table(AUDIT_TABLE).insert(payload).execute()
    except Exception as exc:  # pragma: no cover - audit must not break config management
        print("INTEGRATION AUDIT INSERT FAILED:", str(exc), payload)


def _build_integration_payload(payload: dict, *, existing: Optional[dict] = None) -> dict:
    existing = existing or {}
    data = {
        "provider": payload.get("provider", existing.get("provider")),
        "capability": payload.get("capability", existing.get("capability")),
        "display_name": payload.get("display_name", existing.get("display_name")),
        "enabled": payload.get("enabled", existing.get("enabled", True)),
        "model": payload.get("model", existing.get("model")),
        "base_url": payload.get("base_url", existing.get("base_url")),
        "config": payload.get("config", existing.get("config") or {}),
        "updated_at": utc_now_iso(),
    }
    if "api_key" in payload and payload.get("api_key") is not None:
        api_key = payload.get("api_key")
        data["encrypted_api_key"] = encrypt_secret(api_key)
        data["api_key_fingerprint"] = secret_fingerprint(api_key)
        data["api_key_mask_hint"] = mask_secret(api_key)
    return {key: value for key, value in data.items() if value is not None}


def list_system_integrations(db) -> list[dict]:
    res = (
        db.table(SYSTEM_INTEGRATIONS_TABLE)
        .select("*")
        .order("created_at", desc=False)
        .execute()
    )
    return [sanitise_integration(row) for row in (res.data or [])]


def get_system_integration(db, integration_id: str, *, include_secret: bool = False) -> dict:
    res = (
        db.table(SYSTEM_INTEGRATIONS_TABLE)
        .select("*")
        .eq("id", integration_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="System integration not found")
    return sanitise_integration(res.data[0], include_secret=include_secret)


def create_system_integration(db, payload: dict, *, actor_user_id: Optional[str]) -> dict:
    data = _build_integration_payload(payload)
    data["created_at"] = utc_now_iso()
    res = db.table(SYSTEM_INTEGRATIONS_TABLE).insert(data).execute()
    row = res.data[0] if res.data else data
    _insert_audit(
        db,
        event_type="system_integration_created",
        actor_user_id=actor_user_id,
        integration_scope="system",
        integration_id=row.get("id"),
        provider=row.get("provider"),
        capability=row.get("capability"),
    )
    return sanitise_integration(row)


def update_system_integration(db, integration_id: str, payload: dict, *, actor_user_id: Optional[str]) -> dict:
    existing = get_system_integration(db, integration_id, include_secret=False)
    data = _build_integration_payload(payload, existing=existing)
    res = (
        db.table(SYSTEM_INTEGRATIONS_TABLE)
        .update(data)
        .eq("id", integration_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="System integration not found")
    row = res.data[0]
    _insert_audit(
        db,
        event_type="system_integration_updated",
        actor_user_id=actor_user_id,
        integration_scope="system",
        integration_id=integration_id,
        provider=row.get("provider"),
        capability=row.get("capability"),
    )
    return sanitise_integration(row)


def delete_system_integration(db, integration_id: str, *, actor_user_id: Optional[str]) -> dict:
    existing = get_system_integration(db, integration_id)
    res = db.table(SYSTEM_INTEGRATIONS_TABLE).delete().eq("id", integration_id).execute()
    _insert_audit(
        db,
        event_type="system_integration_deleted",
        actor_user_id=actor_user_id,
        integration_scope="system",
        integration_id=integration_id,
        provider=existing.get("provider"),
        capability=existing.get("capability"),
    )
    return {"deleted": bool(getattr(res, "data", None) is not None), "id": integration_id}


def test_system_integration(db, integration_id: str, *, actor_user_id: Optional[str]) -> dict:
    row = get_system_integration(db, integration_id, include_secret=True)
    provider = row.get("provider")
    api_key = row.get("api_key")
    status = "ok" if provider in PLATFORM_AI_PROVIDERS and api_key else "failed"
    error = None if status == "ok" else "Provider is unsupported or missing an API key"
    update_payload = {
        "last_test_status": status,
        "last_test_at": utc_now_iso(),
        "last_error": error,
        "updated_at": utc_now_iso(),
    }
    try:
        db.table(SYSTEM_INTEGRATIONS_TABLE).update(update_payload).eq("id", integration_id).execute()
    finally:
        _insert_audit(
            db,
            event_type="system_integration_tested",
            actor_user_id=actor_user_id,
            integration_scope="system",
            integration_id=integration_id,
            provider=provider,
            capability=row.get("capability"),
            details={"status": status, "error": error},
        )
    return {"id": integration_id, "status": status, "error": error, "provider": provider}


def get_system_policy(db, task: str) -> dict:
    res = (
        db.table(SYSTEM_POLICIES_TABLE)
        .select("*")
        .eq("task", task)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]
    return {
        "task": task,
        "enabled": True,
        "ordered_integration_ids": [],
        "config": {},
    }


def update_system_policy(db, task: str, payload: dict, *, actor_user_id: Optional[str]) -> dict:
    data = {
        "task": task,
        "enabled": payload.get("enabled", True),
        "ordered_integration_ids": payload.get("ordered_integration_ids") or [],
        "config": payload.get("config") or {},
        "updated_at": utc_now_iso(),
    }
    existing = get_system_policy(db, task)
    if existing.get("id"):
        res = db.table(SYSTEM_POLICIES_TABLE).update(data).eq("id", existing["id"]).execute()
    else:
        data["created_at"] = utc_now_iso()
        res = db.table(SYSTEM_POLICIES_TABLE).insert(data).execute()
    row = res.data[0] if res.data else data
    _insert_audit(
        db,
        event_type="system_integration_policy_updated",
        actor_user_id=actor_user_id,
        integration_scope="system",
        details={"task": task},
    )
    return row


def get_extraction_criteria(db, task: str) -> dict:
    rows = (
        db.table(SYSTEM_CRITERIA_TABLE)
        .select("*")
        .eq("task", task)
        .order("version", desc=True)
        .execute()
    ).data or []
    latest_published = next((row for row in rows if row.get("status") == "published"), None)
    latest_draft = next((row for row in rows if row.get("status") == "draft"), None)
    return {
        "task": task,
        "published": latest_published,
        "draft": latest_draft,
    }


def get_published_extraction_criteria(db, task: str) -> Optional[dict]:
    criteria = get_extraction_criteria(db, task)
    return criteria.get("published")


def upsert_extraction_criteria(db, task: str, payload: dict, *, actor_user_id: Optional[str]) -> dict:
    status = payload.get("status") or "draft"
    if status not in {"draft", "published", "archived"}:
        raise HTTPException(status_code=400, detail="Criteria status must be draft, published, or archived")

    existing_rows = (
        db.table(SYSTEM_CRITERIA_TABLE)
        .select("version")
        .eq("task", task)
        .order("version", desc=True)
        .limit(1)
        .execute()
    ).data or []
    next_version = int(existing_rows[0].get("version") or 0) + 1 if existing_rows else 1
    data = {
        "task": task,
        "version": next_version,
        "status": status,
        "prompt_template": payload.get("prompt_template"),
        "criteria": payload.get("criteria") or {},
        "notes": payload.get("notes"),
        "created_by": actor_user_id,
        "created_at": utc_now_iso(),
    }
    res = db.table(SYSTEM_CRITERIA_TABLE).insert(data).execute()
    row = res.data[0] if res.data else data
    _insert_audit(
        db,
        event_type="system_extraction_criteria_created",
        actor_user_id=actor_user_id,
        integration_scope="system",
        details={"task": task, "version": next_version, "status": status},
    )
    return row


def list_organisation_integrations(db, organisation_id: str) -> list[dict]:
    rows = (
        db.table(ORG_INTEGRATIONS_TABLE)
        .select("*")
        .eq("organisation_id", organisation_id)
        .order("created_at", desc=False)
        .execute()
    ).data or []
    return [sanitise_integration(row) for row in rows]


def get_organisation_integration(db, organisation_id: str, integration_id: str, *, include_secret: bool = False) -> dict:
    res = (
        db.table(ORG_INTEGRATIONS_TABLE)
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("id", integration_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Organisation integration not found")
    return sanitise_integration(res.data[0], include_secret=include_secret)


def create_organisation_integration(db, organisation_id: str, payload: dict, *, actor_user_id: Optional[str]) -> dict:
    data = _build_integration_payload(payload)
    data["organisation_id"] = organisation_id
    data["created_at"] = utc_now_iso()
    res = db.table(ORG_INTEGRATIONS_TABLE).insert(data).execute()
    row = res.data[0] if res.data else data
    _insert_audit(
        db,
        event_type="organisation_integration_created",
        actor_user_id=actor_user_id,
        integration_scope="organisation",
        integration_id=row.get("id"),
        organisation_id=organisation_id,
        provider=row.get("provider"),
        capability=row.get("capability"),
    )
    return sanitise_integration(row)


def update_organisation_integration(
    db,
    organisation_id: str,
    integration_id: str,
    payload: dict,
    *,
    actor_user_id: Optional[str],
) -> dict:
    existing = get_organisation_integration(db, organisation_id, integration_id)
    data = _build_integration_payload(payload, existing=existing)
    res = (
        db.table(ORG_INTEGRATIONS_TABLE)
        .update(data)
        .eq("organisation_id", organisation_id)
        .eq("id", integration_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Organisation integration not found")
    row = res.data[0]
    _insert_audit(
        db,
        event_type="organisation_integration_updated",
        actor_user_id=actor_user_id,
        integration_scope="organisation",
        integration_id=integration_id,
        organisation_id=organisation_id,
        provider=row.get("provider"),
        capability=row.get("capability"),
    )
    return sanitise_integration(row)


def delete_organisation_integration(db, organisation_id: str, integration_id: str, *, actor_user_id: Optional[str]) -> dict:
    existing = get_organisation_integration(db, organisation_id, integration_id)
    res = (
        db.table(ORG_INTEGRATIONS_TABLE)
        .delete()
        .eq("organisation_id", organisation_id)
        .eq("id", integration_id)
        .execute()
    )
    _insert_audit(
        db,
        event_type="organisation_integration_deleted",
        actor_user_id=actor_user_id,
        integration_scope="organisation",
        integration_id=integration_id,
        organisation_id=organisation_id,
        provider=existing.get("provider"),
        capability=existing.get("capability"),
    )
    return {"deleted": bool(getattr(res, "data", None) is not None), "id": integration_id}
