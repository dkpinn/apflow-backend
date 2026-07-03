"""bank_uploads.py
Bank statement upload routes: list, create, extract (OCR/VLM), delete, and the
per-upload line/audit-trail reads.

Shared helpers, models, and private utilities are imported from bank.py.
bank.py does NOT import this module — no circular import risk.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.db.supabase_client import get_fresh_supabase_client
from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.schemas.bank import ApproveExtractionRequest, CreateGoldFileFromUploadRequest
from app.services.bank_extraction_validation import validate_extracted_statement_quality
from app.services.bank.extraction_gate import corrected_fixture_benchmark_blockers
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


def _line_value(line, key: str, default=None):
    if isinstance(line, dict):
        return line.get(key, default)
    return getattr(line, key, default)


def _numeric_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _review_snapshot_line(wrapper: dict, row_number: int) -> dict:
    line = wrapper["line"]
    duplicate_status = wrapper.get("duplicate_status") or "clear"
    signed_amount = _line_signed_amount(line)
    if duplicate_status != "clear":
        import_status = "duplicate_filtered"
    elif signed_amount == 0:
        import_status = "dropped_zero_or_opening"
    else:
        import_status = "stored"
    return {
        "row_number": row_number,
        "import_status": import_status,
        "duplicate_status": duplicate_status,
        "line_date": _line_value(line, "line_date"),
        "value_date": _line_value(line, "value_date"),
        "description": _line_value(line, "description"),
        "reference": _line_value(line, "reference"),
        "counterparty": _line_value(line, "counterparty"),
        "debit_amount": _numeric_or_none(_line_value(line, "debit_amount")),
        "credit_amount": _numeric_or_none(_line_value(line, "credit_amount")),
        "signed_amount": signed_amount,
        "balance_amount": _numeric_or_none(_line_value(line, "balance_amount")),
        "currency": _line_value(line, "currency"),
        "source_page": _line_value(line, "source_page"),
        "source_row_index": _line_value(line, "source_row_index"),
        "extraction_confidence": _numeric_or_none(_line_value(line, "extraction_confidence")),
        "extraction_warnings": _line_value(line, "extraction_warnings", []) or [],
    }


def _storage_signed_url(db, *, bucket: str, path: str, expires_in: int = 3600) -> tuple[str | None, str | None]:
    try:
        storage_bucket = db.storage.from_(bucket)
        create_signed_url = getattr(storage_bucket, "create_signed_url", None)
        if not callable(create_signed_url):
            return None, "Storage client does not expose signed URL creation"
        result = create_signed_url(path, expires_in)
        if isinstance(result, dict):
            signed_url = (
                result.get("signedURL")
                or result.get("signed_url")
                or result.get("signedUrl")
                or result.get("url")
            )
            return signed_url, None if signed_url else "Storage client returned no signed URL"
        signed_url = getattr(result, "signed_url", None) or getattr(result, "signedURL", None)
        return signed_url, None if signed_url else "Storage client returned no signed URL"
    except Exception as exc:
        return None, str(exc)


def _gold_document_id(upload: dict) -> str:
    filename = str(upload.get("original_filename") or upload.get("id") or "bank-statement")
    stem = Path(filename).stem or "bank-statement"
    clean = "".join(ch.lower() if ch.isalnum() else "-" for ch in stem).strip("-")
    return clean or str(upload.get("id") or "bank-statement")


def _gold_transaction_from_review_line(line: dict, transaction_index: int) -> dict:
    return {
        "transaction_index": transaction_index,
        "date": line.get("line_date"),
        "description": line.get("description") or "",
        "amount": line.get("signed_amount"),
        "debit": line.get("debit_amount"),
        "credit": line.get("credit_amount"),
        "running_balance": line.get("balance_amount"),
        "page_number": line.get("source_page"),
        "source_reference": (
            f"row-{line.get('source_row_index')}"
            if line.get("source_row_index") not in (None, "")
            else f"extracted-row-{line.get('row_number') or transaction_index}"
        ),
    }


def _gold_draft_from_upload(upload: dict, account: dict) -> dict:
    evidence = upload.get("extraction_evidence") if isinstance(upload.get("extraction_evidence"), dict) else {}
    review_snapshot = evidence.get("review_snapshot") if isinstance(evidence.get("review_snapshot"), dict) else {}
    snapshot_lines = review_snapshot.get("lines") if isinstance(review_snapshot.get("lines"), list) else []
    transactions = []
    for line in snapshot_lines:
        if not isinstance(line, dict):
            continue
        if line.get("import_status") == "dropped_zero_or_opening":
            continue
        if _numeric_or_none(line.get("signed_amount")) == 0:
            continue
        transactions.append(_gold_transaction_from_review_line(line, len(transactions) + 1))
    return {
        "document_id": _gold_document_id(upload),
        "bank": account.get("institution_name"),
        "account_type": account.get("account_type"),
        "document_variant": upload.get("source_format") or "bank_upload",
        "statement_start_date": upload.get("statement_period_from"),
        "statement_end_date": upload.get("statement_period_to"),
        "opening_balance": upload.get("opening_balance"),
        "closing_balance": upload.get("closing_balance"),
        "transactions": transactions,
        "source_upload_id": upload.get("id"),
        "source_filename": upload.get("original_filename"),
        "needs_manual_correction": True,
    }


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


def _approval_blockers(upload: dict, db=None) -> list[str]:
    blockers: list[str] = []
    evidence = upload.get("extraction_evidence") if isinstance(upload.get("extraction_evidence"), dict) else {}
    validation = evidence.get("validation") if isinstance(evidence.get("validation"), dict) else {}
    running_balance = evidence.get("running_balance") if isinstance(evidence.get("running_balance"), dict) else {}
    duplicate_summary = upload.get("duplicate_summary") if isinstance(upload.get("duplicate_summary"), dict) else {}

    if upload.get("balance_status") != "balanced":
        blockers.append("Statement opening/closing balances are not reconciled")
    if validation.get("closing_balance_passed") is not True:
        blockers.append("Closing balance validation has not passed")
    if validation.get("running_balance_passed") is not True:
        blockers.append("Running balance validation has not passed")
    if running_balance.get("balance_walk_status") not in {None, "balanced"}:
        blockers.append("Running balance walk has not passed")
    if int(upload.get("extracted_line_count") or 0) <= 0:
        blockers.append("No importable transaction rows were stored")
    review_snapshot = evidence.get("review_snapshot") if isinstance(evidence.get("review_snapshot"), dict) else {}
    review_lines = review_snapshot.get("lines") if isinstance(review_snapshot.get("lines"), list) else []
    raw_count = int(evidence.get("raw_extracted_transaction_count") or 0)
    if raw_count and len(review_lines) != raw_count:
        blockers.append("Row-level extraction review snapshot is incomplete")
    duplicate_count = int(
        upload.get("duplicate_line_count")
        or duplicate_summary.get("duplicate_line_count")
        or 0
    )
    if duplicate_count:
        blockers.append("Duplicate transaction rows are still present")
    if db is not None:
        blockers.extend(
            corrected_fixture_benchmark_blockers(
                db,
                organisation_id=str(upload.get("organisation_id")),
                upload_id=str(upload.get("id")),
            )
        )
    return blockers


def _attestation_blockers(payload: ApproveExtractionRequest) -> list[str]:
    required_checks = [
        (payload.source_document_checked, "Reviewer must confirm the source document was opened and checked"),
        (payload.transaction_count_checked, "Reviewer must confirm the extracted transaction count matches the source"),
        (payload.amounts_and_dates_checked, "Reviewer must confirm extracted dates, descriptions, and amounts match the source"),
        (payload.balances_checked, "Reviewer must confirm opening, closing, and running balances reconcile"),
    ]
    return [message for passed, message in required_checks if not passed]


@router.post("/uploads/{upload_id}/approve-extraction")
def approve_bank_upload_extraction(upload_id: str, payload: ApproveExtractionRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    upload = _one(
        db.table("bank_statement_uploads")
        .select("*")
        .eq("id", upload_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank statement upload not found",
    )
    if upload.get("extraction_status") == "extracted":
        return {"success": True, "upload": upload, "already_approved": True}
    if upload.get("extraction_status") != "needs_review":
        raise HTTPException(status_code=400, detail="Only bank statement uploads needing review can be approved")

    evidence = upload.get("extraction_evidence") if isinstance(upload.get("extraction_evidence"), dict) else {}
    extracted_by = evidence.get("extracted_by")
    if user_id in {upload.get("uploaded_by"), extracted_by}:
        raise HTTPException(
            status_code=400,
            detail="Bank statement extraction must be approved by a different reviewer",
        )

    blockers = _approval_blockers(upload, db) + _attestation_blockers(payload)
    if blockers:
        raise HTTPException(status_code=400, detail={"message": "Bank statement extraction cannot be approved", "blockers": blockers})

    approved_at = now_iso()
    evidence = {
        **evidence,
        "manual_review": {
            "approved": True,
            "approved_by": user_id,
            "approved_at": approved_at,
            "source_document_checked": payload.source_document_checked,
            "transaction_count_checked": payload.transaction_count_checked,
            "amounts_and_dates_checked": payload.amounts_and_dates_checked,
            "balances_checked": payload.balances_checked,
            "reviewer_note": payload.reviewer_note,
            "reason": "Reviewer confirmed extracted bank statement lines against source document",
        },
    }
    res = (
        db.table("bank_statement_uploads")
        .update({
            "extraction_status": "extracted",
            "extraction_evidence": evidence,
        })
        .eq("id", upload_id)
        .eq("organisation_id", organisation_id)
        .execute()
    )
    updated = _one(res, "Bank statement upload approval failed")
    if upload.get("balance_status") == "balanced" and upload.get("closing_balance") is not None:
        db.table("bank_accounts").update({
            "current_reconciled_balance": upload.get("closing_balance"),
            "last_statement_upload_id": upload_id,
        }).eq("id", upload.get("bank_account_id")).eq("organisation_id", organisation_id).execute()
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_statement_extraction_approved",
        actor_user_id=user_id,
        bank_account_id=upload.get("bank_account_id"),
        bank_statement_upload_id=upload_id,
    )
    return {"success": True, "upload": updated, "already_approved": False}


@router.get("/uploads/{upload_id}/extraction-review")
def get_bank_upload_extraction_review(upload_id: str, organisation_id: str, auth: UserAuth):
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
    account = _one(
        db.table("bank_accounts")
        .select("*")
        .eq("id", upload["bank_account_id"])
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank account not found",
    )
    stored_lines = (
        db.table("bank_statement_lines")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("bank_statement_upload_id", upload_id)
        .order("line_date")
        .execute()
        .data
        or []
    )
    evidence = upload.get("extraction_evidence") if isinstance(upload.get("extraction_evidence"), dict) else {}
    review_snapshot = evidence.get("review_snapshot") if isinstance(evidence.get("review_snapshot"), dict) else {}
    bucket = upload.get("storage_bucket") or "statement-files"
    path = upload.get("storage_path")
    signed_url = access_error = None
    if path:
        signed_url, access_error = _storage_signed_url(db, bucket=bucket, path=path)
    return {
        "success": True,
        "upload": upload,
        "account": account,
        "source_file": {
            "bucket": bucket,
            "path": path,
            "filename": upload.get("original_filename"),
            "mime_type": upload.get("mime_type"),
            "signed_url": signed_url,
            "access_error": access_error,
        },
        "review_snapshot": review_snapshot,
        "stored_lines": stored_lines,
        "approval_blockers": _approval_blockers(upload, db),
    }


@router.get("/uploads/{upload_id}/gold-draft")
def get_bank_upload_gold_draft(upload_id: str, organisation_id: str, auth: UserAuth):
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
    account = _one(
        db.table("bank_accounts")
        .select("*")
        .eq("id", upload["bank_account_id"])
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank account not found",
    )
    draft = _gold_draft_from_upload(upload, account)
    if not draft["transactions"]:
        raise HTTPException(
            status_code=400,
            detail="No non-zero extracted transaction rows are available for a gold draft",
        )
    return {
        "success": True,
        "draft": draft,
        "review_snapshot": (
            upload.get("extraction_evidence", {})
            if isinstance(upload.get("extraction_evidence"), dict)
            else {}
        ).get("review_snapshot"),
    }


@router.post("/uploads/{upload_id}/gold-file")
def create_bank_upload_gold_file(
    upload_id: str,
    payload: CreateGoldFileFromUploadRequest,
    auth: UserAuth,
):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    upload = _one(
        db.table("bank_statement_uploads")
        .select("*")
        .eq("id", upload_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank statement upload not found",
    )
    account = _one(
        db.table("bank_accounts")
        .select("*")
        .eq("id", upload["bank_account_id"])
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank account not found",
    )
    gold_json = dict(payload.gold_json)
    transactions = gold_json.get("transactions") if isinstance(gold_json, dict) else None
    if not isinstance(transactions, list) or not transactions:
        raise HTTPException(status_code=400, detail="Corrected gold JSON must contain transactions")
    gold_json["_apflow_source_upload_id"] = upload_id
    gold_json["_apflow_source_bank_account_id"] = upload.get("bank_account_id")

    row = {
        "organisation_id": organisation_id,
        "document_id": payload.document_id or gold_json.get("document_id") or _gold_document_id(upload),
        "bank": payload.bank or gold_json.get("bank") or account.get("institution_name") or "Unknown bank",
        "account_type": payload.account_type or gold_json.get("account_type") or account.get("account_type"),
        "document_variant": (
            payload.document_variant
            or gold_json.get("document_variant")
            or upload.get("source_format")
            or "bank_upload"
        ),
        "statement_start_date": gold_json.get("statement_start_date") or upload.get("statement_period_from"),
        "statement_end_date": gold_json.get("statement_end_date") or upload.get("statement_period_to"),
        "gold_json": gold_json,
        "gold_pdf_storage_bucket": upload.get("storage_bucket") or "statement-files",
        "gold_pdf_storage_path": upload.get("storage_path"),
        "verified_by": user_id,
        "verified_at": now_iso(),
    }
    res = db.table("bank_statement_gold_files").insert(row).execute()
    gold_file = _one(res, "Gold file create failed")
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_statement_gold_file_created",
        actor_user_id=user_id,
        bank_account_id=upload.get("bank_account_id"),
        bank_statement_upload_id=upload_id,
        gold_file_id=gold_file.get("id"),
        document_id=row["document_id"],
    )
    return {"success": True, "gold_file": gold_file}


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
        correction_summary = correct_amounts_from_balance(lines, bank_account_id=account["id"])
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
        # Delete existing lines for this upload BEFORE duplicate detection so that
        # re-extracting the same file doesn't flag all its own lines as duplicates.
        db.table("bank_statement_lines").delete().eq("bank_statement_upload_id", upload_id).execute()
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
        duplicate_line_count = int(duplicate_summary.get("duplicate_line_count", 0) or 0)
        raw_extracted_transaction_count = len(lines)
        clearable = [
            w for w in line_wrappers
            if w["duplicate_status"] == "clear" and _line_signed_amount(w["line"]) != 0
        ]
        stored_line_count = len(clearable)
        duplicate_summary["nil_line_count"] = nil_line_count
        duplicate_summary["raw_extracted_transaction_count"] = raw_extracted_transaction_count
        duplicate_summary["stored_line_count"] = stored_line_count
        review_snapshot_lines = [
            _review_snapshot_line(wrapper, row_number)
            for row_number, wrapper in enumerate(line_wrappers, start=1)
        ]

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
            "extracted_line_count": stored_line_count,
            "duplicate_line_count": duplicate_line_count,
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
                "extracted_by": user_id,
                "source_format": header.get("source_format"),
                "parser_strategy": header.get("parser_strategy"),
                "line_count": stored_line_count,
                "raw_extracted_transaction_count": raw_extracted_transaction_count,
                "stored_line_count": stored_line_count,
                "nil_line_count": nil_line_count,
                "duplicate_line_count": duplicate_line_count,
                "dropped_line_count": raw_extracted_transaction_count - stored_line_count,
                "warnings": header.get("extraction_warnings") or [],
                "validation": validation_result,
                "running_balance": running_balance_result,
                "amount_correction": correction_summary,
                "pdf_rescue": header.get("pdf_rescue"),
                "review_snapshot": {
                    "lines": review_snapshot_lines,
                    "raw_extracted_transaction_count": raw_extracted_transaction_count,
                    "stored_line_count": stored_line_count,
                    "nil_line_count": nil_line_count,
                    "duplicate_line_count": duplicate_line_count,
                },
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
