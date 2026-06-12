"""Platform-wide admin endpoints for the bank statement extraction "gold library":
a cross-organisation set of anonymised PDF + gold-JSON pairs used to benchmark the
extraction pipeline. Restricted to platform owners.
"""

from __future__ import annotations

import mimetypes
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_platform_owner
from app.services.bank_extraction_validation import build_extracted_document, evaluate_extracted_against_gold
from app.services.bank_statement_service import extract_statement

router = APIRouter(prefix="/api/admin/bank-extraction", tags=["admin-bank-extraction"])

DUMMY_BANK_ACCOUNT_ID = "00000000-0000-0000-0000-000000000000"
GOLD_BUCKET = "bank-extraction-gold"


class GoldFileCreate(BaseModel):
    document_id: str
    bank: str
    account_type: Optional[str] = None
    document_variant: str
    statement_start_date: Optional[str] = None
    statement_end_date: Optional[str] = None
    gold_json: dict[str, Any]
    gold_pdf_storage_path: str


class GoldFileUpdate(BaseModel):
    gold_json: dict[str, Any]


def _platform_db(auth: UserAuth):
    user_id, _user_db = auth
    ensure_platform_owner(user_id)
    return user_id, get_supabase_client()


def _one(res, detail: str) -> dict[str, Any]:
    if not res.data:
        raise HTTPException(status_code=404, detail=detail)
    return res.data[0] if isinstance(res.data, list) else res.data


@router.post("/gold-files")
def create_gold_file(payload: GoldFileCreate, auth: UserAuth):
    user_id, db = _platform_db(auth)
    row = {
        "organisation_id": None,
        "document_id": payload.document_id,
        "bank": payload.bank,
        "account_type": payload.account_type,
        "document_variant": payload.document_variant,
        "statement_start_date": payload.statement_start_date,
        "statement_end_date": payload.statement_end_date,
        "gold_json": payload.gold_json,
        "gold_pdf_storage_bucket": GOLD_BUCKET,
        "gold_pdf_storage_path": payload.gold_pdf_storage_path,
        "verified_by": user_id,
    }
    res = db.table("bank_statement_gold_files").insert(row).execute()
    return {"success": True, "gold_file": res.data[0] if res.data else None}


@router.get("/gold-files")
def list_gold_files(auth: UserAuth):
    _user_id, db = _platform_db(auth)
    gold_files = (
        db.table("bank_statement_gold_files")
        .select("*")
        .is_("organisation_id", "null")
        .order("created_at", desc=True)
        .limit(200)
        .execute()
        .data
        or []
    )

    runs = (
        db.table("bank_statement_extraction_runs")
        .select("*")
        .is_("organisation_id", "null")
        .order("created_at", desc=True)
        .limit(500)
        .execute()
        .data
        or []
    )
    latest_run_by_document: dict[str, dict[str, Any]] = {}
    for run in runs:
        document_id = run.get("document_id")
        if document_id and document_id not in latest_run_by_document:
            latest_run_by_document[document_id] = run

    for gold_file in gold_files:
        gold_file["latest_run"] = latest_run_by_document.get(gold_file["document_id"])

    return {"success": True, "gold_files": gold_files}


@router.patch("/gold-files/{gold_file_id}")
def update_gold_file(gold_file_id: str, payload: GoldFileUpdate, auth: UserAuth):
    _user_id, db = _platform_db(auth)
    _one(
        db.table("bank_statement_gold_files")
        .select("id")
        .eq("id", gold_file_id)
        .is_("organisation_id", "null")
        .limit(1)
        .execute(),
        "Gold file not found",
    )

    res = (
        db.table("bank_statement_gold_files")
        .update({"gold_json": payload.gold_json})
        .eq("id", gold_file_id)
        .execute()
    )
    return {"success": True, "gold_file": res.data[0] if res.data else None}


@router.delete("/gold-files/{gold_file_id}")
def delete_gold_file(gold_file_id: str, auth: UserAuth):
    _user_id, db = _platform_db(auth)
    gold_file = _one(
        db.table("bank_statement_gold_files")
        .select("*")
        .eq("id", gold_file_id)
        .is_("organisation_id", "null")
        .limit(1)
        .execute(),
        "Gold file not found",
    )

    storage_path = gold_file.get("gold_pdf_storage_path")
    if storage_path:
        try:
            db.storage.from_(gold_file.get("gold_pdf_storage_bucket") or GOLD_BUCKET).remove([storage_path])
        except Exception:
            pass

    db.table("bank_statement_gold_files").delete().eq("id", gold_file_id).execute()
    return {"success": True}


@router.post("/draft")
def generate_draft(
    auth: UserAuth,
    file: UploadFile = File(...),
    document_id: Optional[str] = Form(None),
    bank: Optional[str] = Form(None),
    account_type: Optional[str] = Form(None),
    document_variant: Optional[str] = Form(None),
):
    _user_id, _db = _platform_db(auth)

    file_bytes = file.file.read()
    filename = file.filename or "statement.pdf"
    mime_type = file.content_type or mimetypes.guess_type(filename)[0] or "application/pdf"

    fallback_id = filename.rsplit(".", 1)[0]

    header, lines = extract_statement(
        file_bytes,
        filename=filename,
        mime_type=mime_type,
        bank_account_id=DUMMY_BANK_ACCOUNT_ID,
        currency=None,
        account_type=account_type,
        parsing_hint=None,
    )

    draft = build_extracted_document(
        document_id=document_id or fallback_id,
        bank=bank,
        account_type=account_type,
        document_variant=document_variant or "original_pdf",
        header=header,
        lines=lines,
    )

    return {
        "success": True,
        "draft": draft,
        "extractor": header.get("extractor"),
        "parser_strategy": header.get("parser_strategy"),
        "confidence_score": header.get("confidence_score"),
        "warnings": header.get("extraction_warnings") or [],
    }


@router.get("/test-runs")
def list_test_runs(auth: UserAuth):
    _user_id, db = _platform_db(auth)
    runs = (
        db.table("bank_statement_extraction_runs")
        .select("*")
        .is_("organisation_id", "null")
        .order("created_at", desc=True)
        .limit(200)
        .execute()
        .data
        or []
    )
    return {"success": True, "runs": runs}


@router.post("/gold-files/{gold_file_id}/run")
def run_gold_file_benchmark(gold_file_id: str, auth: UserAuth):
    _user_id, db = _platform_db(auth)
    gold_file = _one(
        db.table("bank_statement_gold_files")
        .select("*")
        .eq("id", gold_file_id)
        .is_("organisation_id", "null")
        .limit(1)
        .execute(),
        "Gold file not found",
    )

    storage_path = gold_file.get("gold_pdf_storage_path")
    if not storage_path:
        raise HTTPException(status_code=400, detail="Gold file has no associated PDF")

    bucket = gold_file.get("gold_pdf_storage_bucket") or GOLD_BUCKET
    file_bytes = db.storage.from_(bucket).download(storage_path)
    mime_type, _ = mimetypes.guess_type(storage_path)

    header, lines = extract_statement(
        file_bytes,
        filename=storage_path.rsplit("/", 1)[-1],
        mime_type=mime_type or "application/pdf",
        bank_account_id=DUMMY_BANK_ACCOUNT_ID,
        currency=None,
        account_type=gold_file.get("account_type"),
        parsing_hint=None,
    )

    extracted_doc = build_extracted_document(
        document_id=gold_file["document_id"],
        bank=gold_file.get("bank"),
        account_type=gold_file.get("account_type"),
        document_variant=gold_file.get("document_variant"),
        header=header,
        lines=lines,
    )

    validation_result = evaluate_extracted_against_gold(extracted_doc, gold_file["gold_json"])

    run_row = {
        "organisation_id": None,
        "bank_statement_upload_id": None,
        "document_id": gold_file["document_id"],
        "bank": gold_file.get("bank"),
        "account_type": gold_file.get("account_type"),
        "document_variant": gold_file.get("document_variant"),
        "extractor_name": header.get("extractor"),
        "expected_transaction_count": validation_result.get("expected_transaction_count"),
        "extracted_transaction_count": validation_result.get("extracted_transaction_count"),
        "matched_transaction_count": validation_result.get("matched_transaction_count"),
        "missing_transaction_count": validation_result.get("missing_transaction_count"),
        "extra_transaction_count": validation_result.get("extra_transaction_count"),
        "amount_accuracy": validation_result.get("amount_accuracy"),
        "date_accuracy": validation_result.get("date_accuracy"),
        "description_accuracy": validation_result.get("description_accuracy"),
        "balance_accuracy": validation_result.get("balance_accuracy"),
        "running_balance_passed": validation_result.get("running_balance_passed"),
        "closing_balance_passed": validation_result.get("closing_balance_passed"),
        "can_allocate": bool(validation_result.get("can_allocate")),
        "overall_score": validation_result.get("overall_score"),
        "critical_errors": validation_result.get("critical_errors") or [],
        "warnings": validation_result.get("warnings") or [],
    }
    res = db.table("bank_statement_extraction_runs").insert(run_row).execute()

    return {
        "success": True,
        "validation_result": validation_result,
        "run": res.data[0] if res.data else None,
    }
