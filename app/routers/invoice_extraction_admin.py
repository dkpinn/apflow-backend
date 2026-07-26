from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_platform_owner
from app.services.invoice_extraction_benchmark import (
    build_invoice_benchmark_snapshot,
    build_pilot_accuracy_summary,
    evaluate_invoice_against_gold,
    validate_gold_snapshot,
)
from app.services.invoice_extraction_service import run_invoice_re_extraction


router = APIRouter(
    prefix="/api/admin/invoice-extraction",
    tags=["admin-invoice-extraction"],
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _platform_db(auth: UserAuth):
    user_id, _user_db = auth
    ensure_platform_owner(user_id)
    return user_id, get_supabase_client()


def _one(rows: list[dict[str, Any]], detail: str) -> dict[str, Any]:
    if not rows:
        raise HTTPException(status_code=404, detail=detail)
    return rows[0]


def _invoice_snapshot(db, invoice_extracted_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    invoice = _one(
        db.table("invoices_extracted").select("*").eq("id", invoice_extracted_id).limit(1).execute().data or [],
        "Extracted invoice not found",
    )
    raw = _one(
        db.table("invoices_raw").select("*").eq("id", invoice.get("invoice_raw_id")).limit(1).execute().data or [],
        "Source invoice file not found",
    )
    lines = (
        db.table("invoice_line_items")
        .select("*")
        .eq("invoice_extracted_id", invoice_extracted_id)
        .order("sort_order", desc=False)
        .order("id", desc=False)
        .execute()
        .data
        or []
    )
    return invoice, raw, build_invoice_benchmark_snapshot(invoice=invoice, line_items=lines, raw=raw)


class GoldDocumentCreate(BaseModel):
    invoice_extracted_id: str
    document_kind: Literal["invoice", "credit_note", "receipt"]
    dataset_split: Literal["development", "validation", "locked"] = "development"
    gold_json: Optional[dict[str, Any]] = None
    notes: Optional[str] = None


class GoldDocumentUpdate(BaseModel):
    document_kind: Optional[Literal["invoice", "credit_note", "receipt"]] = None
    dataset_split: Optional[Literal["development", "validation", "locked"]] = None
    gold_json: Optional[dict[str, Any]] = None
    notes: Optional[str] = None


@router.post("/gold-documents")
def create_gold_document(payload: GoldDocumentCreate, auth: UserAuth):
    user_id, db = _platform_db(auth)
    invoice, raw, current_snapshot = _invoice_snapshot(db, payload.invoice_extracted_id)
    gold = payload.gold_json or current_snapshot
    blockers = validate_gold_snapshot(gold)
    if blockers:
        raise HTTPException(status_code=400, detail={"message": "Gold document is incomplete", "blockers": blockers})
    file_type = str(raw.get("file_type") or "").lower()
    source_format = "pdf" if "pdf" in file_type else "image"
    row = {
        "organisation_id": invoice["organisation_id"],
        "invoice_raw_id": invoice["invoice_raw_id"],
        "invoice_extracted_id": invoice["id"],
        "document_kind": payload.document_kind,
        "source_format": source_format,
        "dataset_split": payload.dataset_split,
        "source_file_name": raw.get("file_name"),
        "source_file_type": raw.get("file_type"),
        "source_sha256": raw.get("file_sha256") or raw.get("sha256"),
        "gold_json": gold,
        "notes": payload.notes,
        "verified_by": user_id,
        "verified_at": _now_iso(),
        "created_by": user_id,
        "updated_at": _now_iso(),
    }
    result = db.table("invoice_extraction_gold_documents").insert(row).execute()
    return {"success": True, "gold_document": result.data[0] if result.data else None}


@router.get("/gold-documents")
def list_gold_documents(auth: UserAuth):
    _user_id, db = _platform_db(auth)
    cases = (
        db.table("invoice_extraction_gold_documents")
        .select("*")
        .order("created_at", desc=True)
        .limit(1000)
        .execute()
        .data
        or []
    )
    runs = (
        db.table("invoice_extraction_benchmark_runs")
        .select("*")
        .order("created_at", desc=True)
        .limit(3000)
        .execute()
        .data
        or []
    )
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        key = str(run.get("gold_document_id"))
        if key not in latest:
            latest[key] = run
    for case in cases:
        case["latest_run"] = latest.get(str(case.get("id")))
    return {"success": True, "gold_documents": cases}


@router.get("/candidates")
def list_recent_candidates(auth: UserAuth, limit: int = Query(100, ge=1, le=500)):
    _user_id, db = _platform_db(auth)
    invoices = (
        db.table("invoices_extracted")
        .select(
            "id, organisation_id, invoice_raw_id, supplier_name_extracted, invoice_number, "
            "invoice_date, total_amount, currency, document_type, validation_status, created_at"
        )
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )
    raw_ids = [row.get("invoice_raw_id") for row in invoices if row.get("invoice_raw_id")]
    raw_rows = (
        db.table("invoices_raw")
        .select("id, file_name, file_type, preview_path")
        .in_("id", raw_ids)
        .execute()
        .data
        or []
        if raw_ids else []
    )
    raw_by_id = {str(row.get("id")): row for row in raw_rows}
    existing = db.table("invoice_extraction_gold_documents").select("id, invoice_raw_id").execute().data or []
    gold_by_raw = {str(row.get("invoice_raw_id")): row.get("id") for row in existing}
    for invoice in invoices:
        raw = raw_by_id.get(str(invoice.get("invoice_raw_id"))) or {}
        invoice["source_file_name"] = raw.get("file_name")
        invoice["source_file_type"] = raw.get("file_type")
        invoice["preview_path"] = raw.get("preview_path")
        invoice["gold_document_id"] = gold_by_raw.get(str(invoice.get("invoice_raw_id")))
    return {"success": True, "candidates": invoices}


@router.patch("/gold-documents/{gold_document_id}")
def update_gold_document(gold_document_id: str, payload: GoldDocumentUpdate, auth: UserAuth):
    user_id, db = _platform_db(auth)
    existing = _one(
        db.table("invoice_extraction_gold_documents").select("*").eq("id", gold_document_id).limit(1).execute().data or [],
        "Gold document not found",
    )
    updates = payload.model_dump(exclude_unset=True)
    if "gold_json" in updates:
        blockers = validate_gold_snapshot(updates["gold_json"])
        if blockers:
            raise HTTPException(status_code=400, detail={"message": "Gold document is incomplete", "blockers": blockers})
        updates["verified_by"] = user_id
        updates["verified_at"] = _now_iso()
    if updates:
        updates["updated_at"] = _now_iso()
    result = db.table("invoice_extraction_gold_documents").update(updates).eq("id", existing["id"]).execute()
    return {"success": True, "gold_document": result.data[0] if result.data else None}


@router.post("/gold-documents/{gold_document_id}/capture-current")
def capture_current_as_gold(gold_document_id: str, auth: UserAuth):
    user_id, db = _platform_db(auth)
    case = _one(
        db.table("invoice_extraction_gold_documents").select("*").eq("id", gold_document_id).limit(1).execute().data or [],
        "Gold document not found",
    )
    _invoice, _raw, snapshot = _invoice_snapshot(db, case["invoice_extracted_id"])
    blockers = validate_gold_snapshot(snapshot)
    if blockers:
        raise HTTPException(status_code=400, detail={"message": "Correct the invoice before capturing it", "blockers": blockers})
    result = (
        db.table("invoice_extraction_gold_documents")
        .update({"gold_json": snapshot, "verified_by": user_id, "verified_at": _now_iso(), "updated_at": _now_iso()})
        .eq("id", gold_document_id)
        .execute()
    )
    return {"success": True, "gold_document": result.data[0] if result.data else None}


@router.post("/gold-documents/{gold_document_id}/run")
def run_gold_document(
    gold_document_id: str,
    auth: UserAuth,
    reextract: bool = Query(False),
):
    user_id, db = _platform_db(auth)
    case = _one(
        db.table("invoice_extraction_gold_documents").select("*").eq("id", gold_document_id).limit(1).execute().data or [],
        "Gold document not found",
    )
    if reextract:
        run_invoice_re_extraction(
            invoice_raw_id=case["invoice_raw_id"],
            organisation_id=case["organisation_id"],
            force_update=True,
        )
    invoice, _raw, actual = _invoice_snapshot(db, case["invoice_extracted_id"])
    result = evaluate_invoice_against_gold(actual, case["gold_json"])
    row = {
        "gold_document_id": case["id"],
        "invoice_extracted_id": invoice["id"],
        "extractor_version": os.getenv("APP_VERSION") or os.getenv("GIT_COMMIT_SHA") or "development",
        "extracted_json": actual,
        "correct_values": result["correct_values"],
        "total_values": result["total_values"],
        "accuracy": result["accuracy"],
        "correction_count": result["correction_count"],
        "within_two_corrections": result["within_two_corrections"],
        "exact_document": result["exact_document"],
        "critical_error_count": result["critical_error_count"],
        "discrepancies": result["discrepancies"],
        "extraction_rerun": reextract,
        "run_by": user_id,
    }
    saved = db.table("invoice_extraction_benchmark_runs").insert(row).execute()
    return {"success": True, "result": result, "run": saved.data[0] if saved.data else None}


@router.get("/summary")
def get_accuracy_summary(auth: UserAuth):
    _user_id, db = _platform_db(auth)
    cases = db.table("invoice_extraction_gold_documents").select("*").execute().data or []
    all_runs = (
        db.table("invoice_extraction_benchmark_runs")
        .select("*")
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    latest: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run in all_runs:
        key = str(run.get("gold_document_id"))
        if key not in seen:
            seen.add(key)
            latest.append(run)
    return {"success": True, "summary": build_pilot_accuracy_summary(cases, latest)}
