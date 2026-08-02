from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from app.db.supabase_client import get_supabase_client
from app.services.invoice_extraction_benchmark import (
    PILOT_TARGET_ACCURACY,
    build_invoice_benchmark_snapshot,
    evaluate_invoice_against_gold,
    validate_gold_snapshot,
)
from app.services.invoice_extraction_service import run_invoice_re_extraction


logger = logging.getLogger(__name__)

try:
    supabase = get_supabase_client()
except Exception:
    supabase = None  # type: ignore[assignment]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extractor_version() -> str:
    return os.getenv("APP_VERSION") or os.getenv("GIT_COMMIT_SHA") or "development"


def _one(rows: list[dict[str, Any]], message: str) -> dict[str, Any]:
    if not rows:
        raise RuntimeError(message)
    return rows[0]


def load_benchmark_snapshot(db, case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    invoice_rows = (
        db.table("invoices_extracted")
        .select("*")
        .eq("id", case.get("invoice_extracted_id"))
        .limit(1)
        .execute()
        .data
        or []
    )
    if not invoice_rows:
        invoice_rows = (
            db.table("invoices_extracted")
            .select("*")
            .eq("invoice_raw_id", case.get("invoice_raw_id"))
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
    invoice = _one(invoice_rows, "Extracted invoice not found after benchmark extraction")
    raw = _one(
        db.table("invoices_raw")
        .select("*")
        .eq("id", case.get("invoice_raw_id"))
        .limit(1)
        .execute()
        .data
        or [],
        "Source invoice file not found",
    )
    lines = (
        db.table("invoice_line_items")
        .select("*")
        .eq("invoice_extracted_id", invoice["id"])
        .order("sort_order", desc=False)
        .order("id", desc=False)
        .execute()
        .data
        or []
    )
    return invoice, build_invoice_benchmark_snapshot(invoice=invoice, line_items=lines, raw=raw)


def run_benchmark_case(
    db,
    *,
    case: dict[str, Any],
    run_by: Optional[str],
    reextract: bool,
) -> dict[str, Any]:
    blockers = validate_gold_snapshot(case.get("gold_json") or {})
    if blockers:
        raise ValueError(f"Gold benchmark document is not eligible: {'; '.join(blockers)}")
    if reextract:
        run_invoice_re_extraction(
            invoice_raw_id=case["invoice_raw_id"],
            organisation_id=case["organisation_id"],
            force_update=True,
        )

    invoice, actual = load_benchmark_snapshot(db, case)
    result = evaluate_invoice_against_gold(actual, case["gold_json"])
    row = {
        "gold_document_id": case["id"],
        "invoice_extracted_id": invoice["id"],
        "extractor_version": extractor_version(),
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
        "run_by": run_by,
    }
    saved = db.table("invoice_extraction_benchmark_runs").insert(row).execute()
    return {
        "result": result,
        "run": saved.data[0] if saved.data else None,
        "invoice": invoice,
    }


def claim_next_benchmark_item(db, *, worker_id: str, lease_seconds: int = 7200) -> Optional[dict[str, Any]]:
    response = db.rpc(
        "claim_next_invoice_extraction_benchmark_item",
        {"p_worker_id": worker_id, "p_lease_seconds": lease_seconds},
    ).execute()
    if isinstance(response.data, list):
        return response.data[0] if response.data else None
    return response.data or None


def _release_item(db, item_id: str, *, worker_id: str, patch: dict[str, Any]) -> bool:
    result = (
        db.table("invoice_extraction_benchmark_suite_items")
        .update({
            **patch,
            "claimed_by": None,
            "claimed_at": None,
            "lease_expires_at": None,
            "updated_at": _now_iso(),
        })
        .eq("id", item_id)
        .eq("status", "running")
        .eq("claimed_by", worker_id)
        .execute()
    )
    return bool(result.data)


def process_next_benchmark_item(*, worker_id: str, db=None) -> dict[str, Any]:
    database = db or supabase
    if database is None:
        raise RuntimeError("Supabase service client is unavailable")
    item = claim_next_benchmark_item(database, worker_id=worker_id)
    if not item:
        return {"success": True, "status": "empty"}

    item_id = str(item["id"])
    try:
        suite = _one(
            database.table("invoice_extraction_benchmark_suites")
            .select("*")
            .eq("id", item["suite_id"])
            .limit(1)
            .execute()
            .data
            or [],
            "Benchmark suite not found",
        )
        if suite.get("status") == "cancelled":
            _release_item(
                database,
                item_id,
                worker_id=worker_id,
                patch={"status": "skipped", "completed_at": _now_iso()},
            )
            return {"success": True, "status": "skipped", "item_id": item_id}

        case = _one(
            database.table("invoice_extraction_gold_documents")
            .select("*")
            .eq("id", item["gold_document_id"])
            .limit(1)
            .execute()
            .data
            or [],
            "Gold benchmark document not found",
        )
        benchmark = run_benchmark_case(
            database,
            case=case,
            run_by=suite.get("requested_by"),
            reextract=True,
        )
        result = benchmark["result"]
        run = benchmark.get("run") or {}
        quality_passed = bool(
            float(result.get("accuracy") or 0) >= PILOT_TARGET_ACCURACY
            and result.get("within_two_corrections") is True
            and int(result.get("critical_error_count") or 0) == 0
        )
        released = _release_item(database, item_id, worker_id=worker_id, patch={
            "status": "completed",
            "run_id": run.get("id"),
            "accuracy": result.get("accuracy"),
            "correction_count": result.get("correction_count"),
            "critical_error_count": result.get("critical_error_count"),
            "within_two_corrections": result.get("within_two_corrections"),
            "quality_passed": quality_passed,
            "error": None,
            "completed_at": _now_iso(),
        })
        if not released:
            logger.warning("Ignored stale benchmark completion for item %s from worker %s", item_id, worker_id)
            return {
                "success": False,
                "status": "stale_claim",
                "suite_id": suite["id"],
                "item_id": item_id,
            }
        database.table("invoice_extraction_benchmark_suites").update({
            "extractor_version": extractor_version(),
            "updated_at": _now_iso(),
        }).eq("id", suite["id"]).execute()
        return {
            "success": True,
            "status": "completed",
            "suite_id": suite["id"],
            "item_id": item_id,
            "quality_passed": quality_passed,
            "result": result,
        }
    except Exception as exc:
        attempts = int(item.get("attempt_count") or 0)
        max_attempts = int(item.get("max_attempts") or 2)
        will_retry = attempts < max_attempts
        released = _release_item(database, item_id, worker_id=worker_id, patch={
            "status": "queued" if will_retry else "failed",
            "error": str(exc)[:4000],
            "completed_at": None if will_retry else _now_iso(),
        })
        if not released:
            logger.warning("Ignored stale benchmark failure for item %s from worker %s", item_id, worker_id)
            return {
                "success": False,
                "status": "stale_claim",
                "item_id": item_id,
                "error": str(exc),
            }
        logger.exception("Invoice extraction benchmark suite item %s failed", item_id)
        return {
            "success": False,
            "status": "retrying" if will_retry else "failed",
            "item_id": item_id,
            "error": str(exc),
        }
