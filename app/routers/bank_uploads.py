"""bank_uploads.py
Bank statement upload routes: list, create, extract (OCR/VLM), delete, and the
per-upload line/audit-trail reads.

Shared helpers, models, and private utilities are imported from bank.py.
bank.py does NOT import this module — no circular import risk.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.db.supabase_client import get_fresh_supabase_client
from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.bank_extraction_validation import validate_extracted_statement_quality
from app.services.bank_statement_service import (
    correct_amounts_from_balance,
    detect_line_duplicates,
    extract_statement,
    line_to_insert,
    validate_balances,
    validate_running_balance,
)
from app.services.extraction_foundation import file_sha256

from app.routers.bank import (
    BankUploadCreate,
    BulkDeleteUploadsRequest,
    ExtractUploadRequest,
    _auth,
    _bank_delete_error,
    _calc_bank_cost,
    _delete_bank_uploads_rpc,
    _one,
    _remove_bank_upload_files,
    log_bank_event,
    lookup_parsing_hint,
    now_iso,
)
from app.services.bank_statement_extraction.vlm_bank_router import identify_bank

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bank", tags=["bank"])


def _line_signed_amount(line) -> float:
    if isinstance(line, dict):
        return float(line.get("signed_amount") or 0)
    return float(getattr(line, "signed_amount", 0) or 0)


@router.get("/uploads")
def list_bank_uploads(auth: UserAuth, organisation_id: str, bank_account_id: Optional[str] = None):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    q = db.table("bank_statement_uploads").select("*").eq("organisation_id", organisation_id)
    if bank_account_id:
        q = q.eq("bank_account_id", bank_account_id)
    res = q.order("uploaded_at", desc=True).limit(200).execute()
    return {"success": True, "uploads": res.data or []}


@router.post("/uploads")
def create_bank_upload(payload: BankUploadCreate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    account = _one(
        db.table("bank_accounts").select("*").eq("id", str(payload.bank_account_id)).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank account not found",
    )
    row = {
        "organisation_id": organisation_id,
        "bank_account_id": account["id"],
        "original_filename": payload.original_filename,
        "mime_type": payload.mime_type,
        "storage_bucket": payload.storage_bucket,
        "storage_path": payload.storage_path,
        "source_type": "upload",
        "extraction_status": "uploaded",
        "uploaded_by": user_id,
    }
    res = db.table("bank_statement_uploads").insert(row).execute()
    upload = _one(res, "Bank statement upload create failed")
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_statement_uploaded",
        actor_user_id=user_id,
        bank_account_id=account["id"],
        bank_statement_upload_id=upload["id"],
        original_filename=payload.original_filename,
    )
    return {"success": True, "upload": upload}


@router.post("/uploads/{upload_id}/extract")
def extract_bank_upload(upload_id: str, payload: ExtractUploadRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)

    upload = _one(
        db.table("bank_statement_uploads").select("*").eq("id", upload_id).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank statement upload not found",
    )
    account = _one(
        db.table("bank_accounts").select("*").eq("id", upload["bank_account_id"]).eq("organisation_id", organisation_id).limit(1).execute(),
        "Bank account not found",
    )
    db.table("bank_statement_uploads").update({"extraction_status": "processing"}).eq("id", upload_id).execute()
    try:
        file_bytes = db.storage.from_(upload.get("storage_bucket") or "statement-files").download(upload["storage_path"])
        file_hash = file_sha256(file_bytes)
        duplicate_file = (
            db.table("bank_statement_uploads")
            .select("id")
            .eq("organisation_id", organisation_id)
            .eq("bank_account_id", account["id"])
            .eq("file_sha256", file_hash)
            .neq("id", upload_id)
            .limit(1)
            .execute()
            .data
            or []
        )

        if not account.get("institution_name") or account.get("account_type") == "bank":
            _mime_for_router = upload.get("mime_type") or "application/octet-stream"
            _detected_name, _detected_type = identify_bank(file_bytes, _mime_for_router)
            _updates: dict = {}
            if _detected_name and not account.get("institution_name"):
                _updates["institution_name"] = _detected_name
                account["institution_name"] = _detected_name
            if _detected_type and account.get("account_type") == "bank":
                _updates["account_type"] = _detected_type
                account["account_type"] = _detected_type
            if _updates:
                db.table("bank_accounts").update(_updates).eq("id", account["id"]).execute()
                logger.info("[ROUTER] Auto-detected for account %s: %r", account["id"], _updates)

        parsing_hint = lookup_parsing_hint(
            db,
            organisation_id=organisation_id,
            institution_name=account.get("institution_name"),
            account_type=account.get("account_type"),
        )
        logger.info(
            "[EXTRACT] upload_id=%s institution=%r account_type=%r parsing_hint=%s",
            upload_id,
            account.get("institution_name"),
            account.get("account_type"),
            f"FOUND ({len(parsing_hint)} chars)" if parsing_hint else "NONE",
        )
        header, lines = extract_statement(
            file_bytes,
            filename=upload["original_filename"],
            mime_type=upload.get("mime_type") or "application/octet-stream",
            bank_account_id=account["id"],
            currency=account.get("currency"),
            account_type=account.get("account_type"),
            parsing_hint=parsing_hint,
        )
        correct_amounts_from_balance(lines, bank_account_id=account["id"])
        running_balance_result = validate_running_balance(lines, header)
        if running_balance_result["balance_walk_mismatches"]:
            logger.warning(
                "[BALANCE] Running balance walk failed for upload %s: %d mismatches",
                upload_id,
                running_balance_result["balance_walk_mismatches"],
            )
        # Refresh the DB connection after the long extraction call (Gemini VLM can take
        # 30-60s) — the persistent HTTP/2 connection may have gone stale while waiting.
        db = get_fresh_supabase_client()
        line_wrappers, duplicate_summary = detect_line_duplicates(
            db=db,
            organisation_id=organisation_id,
            bank_account_id=account["id"],
            lines=lines,
        )
        if duplicate_file:
            duplicate_summary["duplicate_status"] = "duplicate_file"
        balance_summary = validate_balances(
            account_current_balance=None,
            header=header,
            lines=lines,
        )
        validation_result = validate_extracted_statement_quality(
            extracted_lines=lines,
            header=header,
            duplicate_summary=duplicate_summary,
            balance_summary=balance_summary,
        )

        nil_line_count = sum(
            1 for w in line_wrappers
            if w["duplicate_status"] == "clear" and _line_signed_amount(w["line"]) == 0
        )
        clearable = [
            w for w in line_wrappers
            if w["duplicate_status"] == "clear" and _line_signed_amount(w["line"]) != 0
        ]
        duplicate_summary["nil_line_count"] = nil_line_count

        db.table("bank_statement_lines").delete().eq("bank_statement_upload_id", upload_id).execute()
        inserts = [
            line_to_insert(
                wrapper["line"],
                organisation_id=organisation_id,
                bank_account_id=account["id"],
                upload_id=upload_id,
                duplicate_status=wrapper["duplicate_status"],
            )
            for wrapper in clearable
        ]
        if inserts:
            db.table("bank_statement_lines").insert(inserts).execute()

        closing = header.get("closing_balance")
        upload_patch = {
            "file_sha256": file_hash,
            "statement_period_from": header.get("statement_period_from"),
            "statement_period_to": header.get("statement_period_to"),
            "opening_balance": header.get("opening_balance"),
            "closing_balance": closing,
            "extracted_line_count": len(inserts),
            "duplicate_line_count": duplicate_summary.get("duplicate_line_count", 0),
            "duplicate_status": duplicate_summary.get("duplicate_status", "clear"),
            "balance_status": balance_summary["balance_status"],
            "confidence_score": header.get("confidence_score"),
            "duplicate_summary": {**duplicate_summary, **balance_summary},
            "extractor_type": header.get("extractor_type") or header.get("extractor") or "bank_statement",
            "extractor_version": header.get("extractor_version") or "v1",
            "source_format": header.get("source_format"),
            "raw_extraction": header.get("raw_extraction") or {},
            "extraction_warnings": header.get("extraction_warnings") or [],
            "extraction_evidence": {
                "extractor": header.get("extractor"),
                "extractor_type": header.get("extractor_type"),
                "extractor_version": header.get("extractor_version"),
                "source_format": header.get("source_format"),
                "parser_strategy": header.get("parser_strategy"),
                "line_count": len(inserts),
                "warnings": header.get("extraction_warnings") or [],
                "validation": validation_result,
                "running_balance": running_balance_result,
            },
            "extraction_status": "extracted" if validation_result["can_allocate"] else "needs_review",
            "extracted_at": now_iso(),
            "extraction_input_tokens": header.get("extraction_input_tokens"),
            "extraction_output_tokens": header.get("extraction_output_tokens"),
            "extraction_model": header.get("extraction_model"),
            "extraction_cost_usd": _calc_bank_cost(
                header.get("extraction_model"),
                header.get("extraction_input_tokens"),
                header.get("extraction_output_tokens"),
            ),
        }
        db.table("bank_statement_uploads").update(upload_patch).eq("id", upload_id).execute()

        if validation_result["can_allocate"] and balance_summary["balance_status"] == "balanced" and closing is not None:
            db.table("bank_accounts").update({
                "current_reconciled_balance": closing,
                "last_statement_upload_id": upload_id,
            }).eq("id", account["id"]).execute()

        log_bank_event(
            db,
            organisation_id=organisation_id,
            event_type="bank_statement_extracted",
            actor_user_id=user_id,
            bank_account_id=account["id"],
            bank_statement_upload_id=upload_id,
            line_count=len(inserts),
            duplicate_summary=duplicate_summary,
            balance_summary=balance_summary,
        )
        return {
            "success": True,
            "upload_id": upload_id,
            "line_count": len(inserts),
            "duplicate_summary": duplicate_summary,
            "balance_summary": balance_summary,
        }
    except Exception as exc:
        logger.exception("[EXTRACT ERROR] upload_id=%s", upload_id)
        db.table("bank_statement_uploads").update({
            "extraction_status": "failed",
            "extraction_warnings": [str(exc)],
        }).eq("id", upload_id).execute()
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/uploads/{upload_id}")
def delete_bank_upload(upload_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_write(user_id, organisation_id)
    try:
        result = _delete_bank_uploads_rpc(
            db,
            organisation_id=organisation_id,
            upload_ids=[upload_id],
            actor_user_id=user_id,
        )
    except Exception as exc:
        raise _bank_delete_error(exc) from exc
    storage_failures = _remove_bank_upload_files(
        db,
        result.get("files") if isinstance(result.get("files"), list) else [],
    )
    return {
        "success": True,
        "deleted_count": int(result.get("deleted_count") or 0),
        "storage_cleanup_failures": storage_failures,
    }


@router.post("/uploads/bulk-delete")
def bulk_delete_bank_uploads(payload: BulkDeleteUploadsRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    upload_ids = [str(upload_id) for upload_id in payload.upload_ids]
    try:
        result = _delete_bank_uploads_rpc(
            db,
            organisation_id=organisation_id,
            upload_ids=upload_ids,
            actor_user_id=user_id,
        )
    except Exception as exc:
        raise _bank_delete_error(exc) from exc
    storage_failures = _remove_bank_upload_files(
        db,
        result.get("files") if isinstance(result.get("files"), list) else [],
    )
    return {
        "success": True,
        "deleted_count": int(result.get("deleted_count") or 0),
        "storage_cleanup_failures": storage_failures,
    }


@router.get("/uploads/{upload_id}/lines")
def list_bank_lines(upload_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    res = (
        db.table("bank_statement_lines")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("bank_statement_upload_id", upload_id)
        .order("line_date")
        .limit(2000)
        .execute()
    )
    return {"success": True, "lines": res.data or []}


@router.get("/uploads/{upload_id}/audit-trail")
def get_bank_upload_audit_trail(upload_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    upload = _one(
        db.table("bank_statement_uploads")
        .select("*")
        .eq("id", upload_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank statement upload not found",
    )
    events = (
        db.table("bank_audit_events")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("bank_statement_upload_id", upload_id)
        .order("created_at")
        .execute()
        .data
        or []
    )
    return {"success": True, "upload": upload, "events": events}
