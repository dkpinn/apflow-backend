from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_platform_owner
from app.services.invoice_extraction_benchmark import (
    build_invoice_benchmark_snapshot,
    build_pilot_accuracy_summary,
    validate_gold_snapshot,
)
from app.services.invoice_extraction_benchmark_suite import extractor_version, run_benchmark_case


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


class BenchmarkSuiteCreate(BaseModel):
    dataset_split: Literal["development", "validation", "locked"] = "locked"
    document_kind: Optional[Literal["invoice", "credit_note", "receipt"]] = None
    source_format: Optional[Literal["pdf", "image"]] = None


def _suite_payload(db, suite: dict[str, Any]) -> dict[str, Any]:
    items = (
        db.table("invoice_extraction_benchmark_suite_items")
        .select("*")
        .eq("suite_id", suite["id"])
        .order("created_at", desc=False)
        .execute()
        .data
        or []
    )
    gold_ids = [item.get("gold_document_id") for item in items if item.get("gold_document_id")]
    cases = (
        db.table("invoice_extraction_gold_documents")
        .select("id, source_file_name, document_kind, source_format, gold_json")
        .in_("id", gold_ids)
        .execute()
        .data
        or []
        if gold_ids else []
    )
    case_by_id = {str(case.get("id")): case for case in cases}
    for item in items:
        case = case_by_id.get(str(item.get("gold_document_id"))) or {}
        document = case.get("gold_json") if isinstance(case.get("gold_json"), dict) else {}
        document = document.get("document") if isinstance(document.get("document"), dict) else {}
        item["document_label"] = (
            document.get("supplier_name_extracted")
            or case.get("source_file_name")
            or "Benchmark document"
        )
        item["document_kind"] = case.get("document_kind")
        item["source_format"] = case.get("source_format")
    return {**suite, "items": items}


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
    if existing.get("dataset_split") == "locked" and any(
        field in updates for field in ("gold_json", "document_kind")
    ):
        raise HTTPException(
            status_code=409,
            detail="Locked gold truth is frozen. Move the document out of the locked split before changing it.",
        )
    if "gold_json" in updates:
        blockers = validate_gold_snapshot(updates["gold_json"])
        if blockers:
            raise HTTPException(status_code=400, detail={"message": "Gold document is incomplete", "blockers": blockers})
        updates["verified_by"] = user_id
        updates["verified_at"] = _now_iso()
    if updates.get("dataset_split") == "locked":
        prospective_gold = updates.get("gold_json", existing.get("gold_json") or {})
        blockers = validate_gold_snapshot(prospective_gold)
        if blockers:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Gold document cannot be locked until its truth is current and complete",
                    "blockers": blockers,
                },
            )
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
    if case.get("dataset_split") == "locked":
        raise HTTPException(
            status_code=409,
            detail="Locked gold truth is frozen. Move the document out of the locked split before recapturing it.",
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
    benchmark = run_benchmark_case(db, case=case, run_by=user_id, reextract=reextract)
    return {"success": True, "result": benchmark["result"], "run": benchmark["run"]}


@router.post("/suites")
def create_benchmark_suite(payload: BenchmarkSuiteCreate, auth: UserAuth):
    user_id, db = _platform_db(auth)
    active = (
        db.table("invoice_extraction_benchmark_suites")
        .select("id, status")
        .in_("status", ["queued", "running"])
        .limit(1)
        .execute()
        .data
        or []
    )
    if active:
        raise HTTPException(status_code=409, detail="A benchmark suite is already queued or running")

    query = (
        db.table("invoice_extraction_gold_documents")
        .select("*")
        .eq("dataset_split", payload.dataset_split)
        .order("created_at", desc=False)
    )
    if payload.document_kind:
        query = query.eq("document_kind", payload.document_kind)
    if payload.source_format:
        query = query.eq("source_format", payload.source_format)
    cases = query.execute().data or []
    if not cases:
        raise HTTPException(status_code=400, detail="No gold documents match this benchmark suite")
    ineligible = [case for case in cases if validate_gold_snapshot(case.get("gold_json") or {})]
    if ineligible:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(ineligible)} gold document(s) use incomplete or outdated truth. "
                "Unlock and recapture them before running the suite."
            ),
        )

    suite_row = {
        "dataset_split": payload.dataset_split,
        "document_kind": payload.document_kind,
        "source_format": payload.source_format,
        "status": "queued",
        "extractor_version": extractor_version(),
        "total_documents": len(cases),
        "requested_by": user_id,
    }
    created = db.table("invoice_extraction_benchmark_suites").insert(suite_row).execute().data or []
    suite = _one(created, "Unable to create benchmark suite")
    try:
        db.table("invoice_extraction_benchmark_suite_items").insert([
            {"suite_id": suite["id"], "gold_document_id": case["id"], "status": "queued"}
            for case in cases
        ]).execute()
    except Exception:
        db.table("invoice_extraction_benchmark_suites").delete().eq("id", suite["id"]).execute()
        raise
    refreshed = _one(
        db.table("invoice_extraction_benchmark_suites").select("*").eq("id", suite["id"]).limit(1).execute().data or [],
        "Benchmark suite was not found after creation",
    )
    return {"success": True, "suite": _suite_payload(db, refreshed)}


@router.get("/suites/latest")
def latest_benchmark_suite(auth: UserAuth):
    _user_id, db = _platform_db(auth)
    rows = (
        db.table("invoice_extraction_benchmark_suites")
        .select("*")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return {"success": True, "suite": _suite_payload(db, rows[0]) if rows else None}


@router.get("/suites/{suite_id}")
def get_benchmark_suite(suite_id: str, auth: UserAuth):
    _user_id, db = _platform_db(auth)
    suite = _one(
        db.table("invoice_extraction_benchmark_suites").select("*").eq("id", suite_id).limit(1).execute().data or [],
        "Benchmark suite not found",
    )
    return {"success": True, "suite": _suite_payload(db, suite)}


@router.post("/suites/{suite_id}/cancel")
def cancel_benchmark_suite(suite_id: str, auth: UserAuth):
    _user_id, db = _platform_db(auth)
    suite = _one(
        db.table("invoice_extraction_benchmark_suites").select("*").eq("id", suite_id).limit(1).execute().data or [],
        "Benchmark suite not found",
    )
    if suite.get("status") not in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Only a queued or running benchmark suite can be cancelled")
    db.table("invoice_extraction_benchmark_suites").update({
        "status": "cancelled",
        "completed_at": _now_iso(),
        "current_gold_document_id": None,
        "updated_at": _now_iso(),
    }).eq("id", suite_id).execute()
    db.table("invoice_extraction_benchmark_suite_items").update({
        "status": "skipped",
        "completed_at": _now_iso(),
        "claimed_by": None,
        "claimed_at": None,
        "lease_expires_at": None,
        "updated_at": _now_iso(),
    }).eq("suite_id", suite_id).in_("status", ["queued", "running"]).execute()
    refreshed = _one(
        db.table("invoice_extraction_benchmark_suites").select("*").eq("id", suite_id).limit(1).execute().data or [],
        "Benchmark suite not found",
    )
    return {"success": True, "suite": _suite_payload(db, refreshed)}


@router.post("/suites/{suite_id}/retry-failed")
def retry_failed_benchmark_suite_items(suite_id: str, auth: UserAuth):
    _user_id, db = _platform_db(auth)
    suite = _one(
        db.table("invoice_extraction_benchmark_suites").select("*").eq("id", suite_id).limit(1).execute().data or [],
        "Benchmark suite not found",
    )
    failed = (
        db.table("invoice_extraction_benchmark_suite_items")
        .select("id")
        .eq("suite_id", suite_id)
        .eq("status", "failed")
        .execute()
        .data
        or []
    )
    if not failed:
        raise HTTPException(status_code=409, detail="This benchmark suite has no failed items to retry")
    db.table("invoice_extraction_benchmark_suites").update({
        "status": "running",
        "completed_at": None,
        "updated_at": _now_iso(),
    }).eq("id", suite["id"]).execute()
    db.table("invoice_extraction_benchmark_suite_items").update({
        "status": "queued",
        "attempt_count": 0,
        "run_id": None,
        "accuracy": None,
        "correction_count": None,
        "critical_error_count": None,
        "within_two_corrections": None,
        "quality_passed": None,
        "error": None,
        "claimed_by": None,
        "claimed_at": None,
        "lease_expires_at": None,
        "started_at": None,
        "completed_at": None,
        "updated_at": _now_iso(),
    }).eq("suite_id", suite_id).eq("status", "failed").execute()
    refreshed = _one(
        db.table("invoice_extraction_benchmark_suites").select("*").eq("id", suite_id).limit(1).execute().data or [],
        "Benchmark suite not found",
    )
    return {"success": True, "suite": _suite_payload(db, refreshed)}


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
    return {"success": True, "summary": build_pilot_accuracy_summary(cases, all_runs)}
