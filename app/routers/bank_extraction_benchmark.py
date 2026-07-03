from __future__ import annotations

import mimetypes
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.routers.bank import _auth
from app.services.bank_extraction_validation import build_extracted_document, evaluate_extracted_against_gold
from app.services.bank_statement_service import extract_statement

router = APIRouter(prefix="/api/bank-extraction", tags=["bank-extraction-benchmark"])


class CompareJsonRequest(BaseModel):
    extracted: dict[str, Any]
    gold: dict[str, Any]


class SaveTestRunRequest(BaseModel):
    organisation_id: Optional[UUID] = None
    bank_statement_upload_id: Optional[UUID] = None
    document_id: str
    bank: Optional[str] = None
    account_type: Optional[str] = None
    document_variant: Optional[str] = None
    extractor_name: Optional[str] = None
    validation_result: dict[str, Any]


class GoldFileCreate(BaseModel):
    organisation_id: Optional[UUID] = None
    document_id: str
    bank: str
    account_type: Optional[str] = None
    document_variant: str
    statement_start_date: Optional[str] = None
    statement_end_date: Optional[str] = None
    gold_json: Optional[dict[str, Any]] = None
    gold_csv_path: Optional[str] = None
    gold_pdf_storage_bucket: Optional[str] = None
    gold_pdf_storage_path: Optional[str] = None


def _gold_json_source_upload_id(gold_json: object) -> str | None:
    if not isinstance(gold_json, dict):
        return None
    source_upload_id = gold_json.get("_apflow_source_upload_id") or gold_json.get("source_upload_id")
    return str(source_upload_id) if source_upload_id else None


def _audit_source_upload_id_for_gold_file(db, *, organisation_id: str, gold_file_id: str) -> str | None:
    events = (
        db.table("bank_audit_events")
        .select("*")
        .eq("organisation_id", organisation_id)
        .eq("event_type", "bank_statement_gold_file_created")
        .execute()
        .data
        or []
    )
    for event in events:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        if str(details.get("gold_file_id") or "") == gold_file_id:
            upload_id = event.get("bank_statement_upload_id")
            return str(upload_id) if upload_id else None
    return None


@router.post("/compare-json")
def compare_extraction_to_gold_json(payload: CompareJsonRequest, auth: UserAuth):
    _auth(auth)
    return {"success": True, "validation_result": evaluate_extracted_against_gold(payload.extracted, payload.gold)}


@router.post("/save-test-run")
def save_extraction_test_run(payload: SaveTestRunRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id) if payload.organisation_id else None
    if organisation_id:
        ensure_org_write(user_id, organisation_id)

    result = payload.validation_result
    row = {
        "organisation_id": organisation_id,
        "bank_statement_upload_id": str(payload.bank_statement_upload_id) if payload.bank_statement_upload_id else None,
        "document_id": payload.document_id,
        "bank": payload.bank,
        "account_type": payload.account_type,
        "document_variant": payload.document_variant,
        "extractor_name": payload.extractor_name,
        "expected_transaction_count": result.get("expected_transaction_count"),
        "extracted_transaction_count": result.get("extracted_transaction_count"),
        "matched_transaction_count": result.get("matched_transaction_count"),
        "missing_transaction_count": result.get("missing_transaction_count"),
        "extra_transaction_count": result.get("extra_transaction_count"),
        "amount_accuracy": result.get("amount_accuracy"),
        "date_accuracy": result.get("date_accuracy"),
        "description_accuracy": result.get("description_accuracy"),
        "balance_accuracy": result.get("balance_accuracy"),
        "running_balance_passed": result.get("running_balance_passed"),
        "closing_balance_passed": result.get("closing_balance_passed"),
        "can_allocate": bool(result.get("can_allocate")),
        "overall_score": result.get("overall_score"),
        "critical_errors": result.get("critical_errors") or [],
        "warnings": result.get("warnings") or [],
    }
    res = db.table("bank_statement_extraction_runs").insert(row).execute()
    return {"success": True, "run": res.data[0] if res.data else None}


@router.get("/test-runs/{organisation_id}")
def list_extraction_test_runs(organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    res = (
        db.table("bank_statement_extraction_runs")
        .select("*")
        .eq("organisation_id", organisation_id)
        .order("created_at", desc=True)
        .limit(200)
        .execute()
    )
    return {"success": True, "runs": res.data or []}


@router.post("/gold-files")
def create_gold_file(payload: GoldFileCreate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id) if payload.organisation_id else None
    if organisation_id:
        ensure_org_write(user_id, organisation_id)

    row = {
        "organisation_id": organisation_id,
        "document_id": payload.document_id,
        "bank": payload.bank,
        "account_type": payload.account_type,
        "document_variant": payload.document_variant,
        "statement_start_date": payload.statement_start_date,
        "statement_end_date": payload.statement_end_date,
        "gold_json": payload.gold_json,
        "gold_csv_path": payload.gold_csv_path,
        "gold_pdf_storage_bucket": payload.gold_pdf_storage_bucket,
        "gold_pdf_storage_path": payload.gold_pdf_storage_path,
        "verified_by": user_id,
    }
    res = db.table("bank_statement_gold_files").insert(row).execute()
    return {"success": True, "gold_file": res.data[0] if res.data else None}


@router.get("/gold-files/{organisation_id}")
def list_gold_files(organisation_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    res = (
        db.table("bank_statement_gold_files")
        .select("*")
        .eq("organisation_id", organisation_id)
        .order("created_at", desc=True)
        .limit(200)
        .execute()
    )
    return {"success": True, "gold_files": res.data or []}


@router.post("/gold-files/{gold_file_id}/run")
def run_org_gold_file_benchmark(gold_file_id: str, auth: UserAuth):
    user_id, db = _auth(auth)
    gold_file_res = (
        db.table("bank_statement_gold_files")
        .select("*")
        .eq("id", gold_file_id)
        .limit(1)
        .execute()
    )
    if not gold_file_res.data:
        raise HTTPException(status_code=404, detail="Gold file not found")
    gold_file = gold_file_res.data[0]
    organisation_id = gold_file.get("organisation_id")
    if not organisation_id:
        raise HTTPException(status_code=404, detail="Gold file not found")
    ensure_org_write(user_id, str(organisation_id))
    storage_path = gold_file.get("gold_pdf_storage_path")
    if not storage_path:
        raise HTTPException(status_code=400, detail="Gold file has no associated source document")
    if not gold_file.get("gold_json"):
        raise HTTPException(status_code=400, detail="Gold file has no corrected gold JSON")

    bucket = gold_file.get("gold_pdf_storage_bucket") or "statement-files"
    file_bytes = db.storage.from_(bucket).download(storage_path)
    mime_type, _ = mimetypes.guess_type(storage_path)
    header, lines = extract_statement(
        file_bytes,
        filename=storage_path.rsplit("/", 1)[-1],
        mime_type=mime_type or "application/pdf",
        bank_account_id="00000000-0000-0000-0000-000000000000",
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
    source_upload_id = _gold_json_source_upload_id(gold_file.get("gold_json")) or _audit_source_upload_id_for_gold_file(
        db,
        organisation_id=str(organisation_id),
        gold_file_id=str(gold_file_id),
    )
    run_row = {
        "organisation_id": str(organisation_id),
        "bank_statement_upload_id": source_upload_id,
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
