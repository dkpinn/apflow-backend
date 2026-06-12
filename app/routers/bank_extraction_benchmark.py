from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.routers.bank import _auth
from app.services.bank_extraction_validation import evaluate_extracted_against_gold

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
