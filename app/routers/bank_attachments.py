"""bank_attachments.py
Attach multiple supporting documents to a single bank statement line.

The client uploads the file bytes directly to Supabase Storage (bucket
`statement-files`, org-first path), then registers the metadata here — the same
pattern as bank statement uploads (`bank_uploads.create_bank_upload`). Retrieval
returns short-lived signed URLs. This complements the single
`bank_statement_lines.receipt_document_id` link with an open-ended file list
(receipt + doctor's note + script + medicine receipt, etc.).
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.routers.bank import _auth, _one, log_bank_event
from app.routers.bank_uploads import _storage_signed_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bank", tags=["bank"])

_ALLOWED_DOC_KINDS = {"receipt", "note", "script", "invoice", "other"}


class BankAttachmentCreate(BaseModel):
    organisation_id: UUID
    storage_path: str
    storage_bucket: str = "statement-files"
    original_filename: Optional[str] = None
    mime_type: Optional[str] = None
    file_size_bytes: Optional[int] = None
    doc_kind: str = "other"


class BankAttachmentDelete(BaseModel):
    organisation_id: UUID


@router.post("/lines/{line_id}/attachments")
def create_bank_line_attachment(line_id: str, payload: BankAttachmentCreate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)

    line = _one(
        db.table("bank_statement_lines")
        .select("id, bank_account_id")
        .eq("id", line_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank statement line not found",
    )

    doc_kind = payload.doc_kind if payload.doc_kind in _ALLOWED_DOC_KINDS else "other"
    row = {
        "organisation_id": organisation_id,
        "bank_statement_line_id": line_id,
        "storage_bucket": payload.storage_bucket or "statement-files",
        "storage_path": payload.storage_path,
        "original_filename": payload.original_filename,
        "mime_type": payload.mime_type,
        "file_size_bytes": payload.file_size_bytes,
        "doc_kind": doc_kind,
        "uploaded_by": user_id,
    }
    created = _one(
        db.table("bank_line_attachments").insert(row).execute(),
        "Failed to save attachment",
    )
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_line_attachment_added",
        actor_user_id=user_id,
        bank_account_id=line.get("bank_account_id"),
        bank_statement_line_id=line_id,
        attachment_id=created.get("id"),
        doc_kind=doc_kind,
    )
    return {"success": True, "attachment": _with_signed_url(db, created)}


@router.get("/lines/{line_id}/attachments")
def list_bank_line_attachments(line_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    # Confirm the line is in this org before exposing its attachments.
    _one(
        db.table("bank_statement_lines")
        .select("id")
        .eq("id", line_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank statement line not found",
    )
    rows = (
        db.table("bank_line_attachments")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("bank_statement_line_id", line_id)
        .order("created_at")
        .execute()
        .data
        or []
    )
    return {"success": True, "attachments": [_with_signed_url(db, r) for r in rows]}


@router.delete("/attachments/{attachment_id}")
def delete_bank_line_attachment(attachment_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_write(user_id, organisation_id)
    attachment = _one(
        db.table("bank_line_attachments")
        .select("*")
        .eq("id", attachment_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Attachment not found",
    )
    bucket = attachment.get("storage_bucket") or "statement-files"
    path = attachment.get("storage_path")
    if path:
        try:
            db.storage.from_(bucket).remove([path])
        except Exception:
            # A missing/inaccessible storage object must not block removing the row.
            logger.warning("Failed to remove storage object %s/%s for attachment %s", bucket, path, attachment_id)
    db.table("bank_line_attachments").delete().eq("id", attachment_id).eq(
        "organisation_id", organisation_id
    ).execute()
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_line_attachment_removed",
        actor_user_id=user_id,
        bank_account_id=None,
        bank_statement_line_id=attachment.get("bank_statement_line_id"),
        attachment_id=attachment_id,
    )
    return {"success": True}


def _with_signed_url(db, attachment: dict) -> dict:
    bucket = attachment.get("storage_bucket") or "statement-files"
    path = attachment.get("storage_path")
    signed_url = access_error = None
    if path:
        signed_url, access_error = _storage_signed_url(db, bucket=bucket, path=path)
    return {**attachment, "signed_url": signed_url, "access_error": access_error}
