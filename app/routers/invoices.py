from __future__ import annotations

import io as _io
import re
from typing import Annotated, Optional

import fitz  # PyMuPDF
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.dependencies import authenticated_user
from app.routers.organisations import ExtractionStrategy
from app.db.supabase_client import get_supabase_client
from app.services.audit_log import log_invoice_event
from app.services.invoice_readiness import evaluate_invoice_readiness
from app.services.invoice_supplier_rules import (
    fetch_supplier_processing_settings,
    reapply_supplier_rules_to_invoice,
)
from app.services.invoice_parse_attempts import fetch_parse_attempts, persist_parse_attempts
from app.services.invoice_line_items import replace_invoice_line_items
from app.services.invoice_gl_posting import build_invoice_debit_lines
from app.services.organisation_module_settings import (
    required_tracking_dimensions,
    validate_supplier_allocations_tracking,
)
from app.services.invoice_review_agent import (
    agent_status_after_regeneration,
    filter_safe_apply_payload,
    generate_invoice_agent_suggestions,
)
from app.services.invoice_data_builders import (
    build_extracted_document_profile,
    build_extracted_supplier_profile,
    build_supplier_create_payload,
    utc_now_iso,
)
from app.services.invoice_extraction_service import (
    REEXTRACT_DEFAULT_DIAGNOSTIC,
    build_extract_job_status,
    create_reextract_job,
    get_processing_job,
    get_raw_invoice,
    get_reextract_job_status,
    log_reextract_failure,
    process_next_queued_invoice_job,
    queue_invoice_job,
    run_extract_worker_until_empty,
    run_invoice_extraction,
    run_invoice_re_extraction,
    run_reextract_job_background,
    _resolve_reextract_context,
)

router = APIRouter(prefix="/api/invoices", tags=["invoices"])
UserAuth = Annotated[tuple, Depends(authenticated_user)]
AGENT_WRITE_ROLES = {"owner", "admin", "accountant"}
try:
    supabase = get_supabase_client()
except Exception:
    supabase = None  # will fail on first API call; create .env with SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY


class ExtractInvoiceRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: Optional[str] = None
    batch_id: Optional[str] = None
    extraction_strategy: Optional[ExtractionStrategy] = Field(
        default=None,
        description="Optional override extraction strategy for this upload.",
    )
    process_mode: str = Field(
        default="queued",
        description="Default extraction requests are queued. Use the sync=true query flag for legacy synchronous extraction.",
    )


class QueueInvoiceRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: Optional[str] = None
    batch_id: Optional[str] = None
    extraction_strategy: Optional[ExtractionStrategy] = None
    priority: int = 100


class ProcessNextJobRequest(BaseModel):
    organisation_id: Optional[str] = None


class ReExtractInvoiceRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: Optional[str] = None
    force_update: bool = False


class SaveLineItemsRequest(BaseModel):
    invoice_extracted_id: str
    organisation_id: str
    supplier_id: Optional[str] = None
    line_items: list[dict]
    document_total: Optional[float] = None  # original VLM-extracted total; rounding reference


class GeneratePreviewRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: str


class AgentSuggestionActionRequest(BaseModel):
    note: Optional[str] = None


class SupplierComparisonIgnoreRequest(BaseModel):
    field_key: str
    reason: Optional[str] = None


IGNORABLE_SUPPLIER_COMPARISON_FIELDS = {
    "supplier_name_extracted",
    "supplier_telephone_extracted",
    "supplier_email_extracted",
    "supplier_website_extracted",
    "supplier_del_address_extracted",
    "company_registration_number_extracted",
}


@router.post("/extract")
def extract_invoice(
    payload: ExtractInvoiceRequest,
    background_tasks: BackgroundTasks,
    sync: bool = Query(False),
):
    """
    Legacy-compatible extraction endpoint.

    Default queues and starts the local single-worker processor so the browser
    does not wait on a long OCR request. Use ?sync=true for the old blocking
    behavior during debugging.
    """
    print("EXTRACT PAYLOAD:", payload.model_dump())

    if sync:
        return run_invoice_extraction(
            invoice_raw_id=payload.invoice_raw_id,
            organisation_id=payload.organisation_id,
            extraction_strategy=payload.extraction_strategy,
        )

    job = queue_invoice_job(
        invoice_raw_id=payload.invoice_raw_id,
        organisation_id=payload.organisation_id,
        batch_id=payload.batch_id,
        extraction_strategy=payload.extraction_strategy,
    )
    background_tasks.add_task(run_extract_worker_until_empty)
    return {
        "success": True,
        "status": "queued",
        "invoice_raw_id": payload.invoice_raw_id,
        "organisation_id": job["organisation_id"],
        "job_id": job["id"],
        "message": "Invoice queued for processing.",
    }


@router.get("/extract/{job_id}/status")
def get_extract_status(job_id: str):
    job = get_processing_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Extraction job not found")
    return build_extract_job_status(job)


@router.post("/re-extract")
def re_extract_invoice(
    payload: ReExtractInvoiceRequest,
    background_tasks: BackgroundTasks,
    sync: bool = Query(False),
):
    if sync:
        return run_invoice_re_extraction(
            invoice_raw_id=payload.invoice_raw_id,
            organisation_id=payload.organisation_id,
            force_update=payload.force_update,
        )

    raw = get_raw_invoice(payload.invoice_raw_id)
    org_id = payload.organisation_id or raw.get("organisation_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Missing organisation_id")
    if not raw.get("file_path"):
        log_reextract_failure(
            payload_data={**payload.model_dump(), "organisation_id": org_id},
            job_id=None,
            error="Missing file_path on invoices_raw row",
        )
        raise HTTPException(status_code=400, detail="Missing file_path on invoices_raw row")

    job = create_reextract_job(
        invoice_raw_id=payload.invoice_raw_id,
        organisation_id=org_id,
    )
    queued_context = _resolve_reextract_context({**payload.model_dump(), "organisation_id": org_id})
    log_invoice_event(
        supabase,
        organisation_id=org_id,
        invoice_raw_id=payload.invoice_raw_id,
        invoice_extracted_id=queued_context.get("extracted_invoice_id"),
        event_type="re_extraction_queued",
        stage="queued",
        actor_type="api",
        job_id=job["job_id"],
        new_value={
            "job_id": job["job_id"],
            "force_update": payload.force_update,
        },
        notes="Re-extraction queued.",
    )
    payload_data = payload.model_dump()
    payload_data["organisation_id"] = org_id
    background_tasks.add_task(run_reextract_job_background, job["job_id"], payload_data)
    return {
        "job_id": job["job_id"],
        "invoice_raw_id": job["invoice_raw_id"],
        "status": "queued",
    }


@router.get("/re-extract/{job_id}/status")
def get_re_extract_status(job_id: str):
    status = get_reextract_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Re-extract job not found")
    return status


@router.post("/queue")
def queue_invoice(payload: QueueInvoiceRequest):
    job = queue_invoice_job(
        invoice_raw_id=payload.invoice_raw_id,
        organisation_id=payload.organisation_id,
        batch_id=payload.batch_id,
        extraction_strategy=payload.extraction_strategy,
        priority=payload.priority,
    )
    return {
        "success": True,
        "status": "queued",
        "job_id": job["id"],
        "invoice_raw_id": job["invoice_raw_id"],
        "organisation_id": job["organisation_id"],
    }


@router.get("/raw/{invoice_raw_id}/audit-events")
def get_invoice_audit_events(invoice_raw_id: str):
    raw = get_raw_invoice(invoice_raw_id)

    events_res = (
        supabase
        .table("invoice_audit_events")
        .select("*")
        .eq("invoice_raw_id", invoice_raw_id)
        .order("created_at", desc=False)
        .execute()
    )

    events = events_res.data or []

    return {
        "success": True,
        "invoice_raw_id": invoice_raw_id,
        "organisation_id": raw.get("organisation_id"),
        "event_count": len(events),
        "events": events,
    }


@router.get("/{invoice_id}/review-data")
def get_invoice_review_data(invoice_id: str):
    """
    Return the complete invoice review payload for the frontend detail page.

    The current frontend route may hold either invoices_extracted.id or
    invoices_extracted.invoice_raw_id, so this endpoint resolves both forms.
    Optional child reads return empty data plus fetch_errors instead of making
    the whole review page fail.
    """
    fetch_errors: dict[str, str] = {}
    resolved_by = "invoice_extracted_id"

    invoice_res = (
        supabase
        .table("invoices_extracted")
        .select("*")
        .eq("id", invoice_id)
        .limit(1)
        .execute()
    )
    invoice = invoice_res.data[0] if invoice_res.data else None

    if not invoice:
        resolved_by = "invoice_raw_id"
        invoice_res = (
            supabase
            .table("invoices_extracted")
            .select("*")
            .eq("invoice_raw_id", invoice_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        invoice = invoice_res.data[0] if invoice_res.data else None

    if not invoice:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Extracted invoice not found",
                "invoice_id": invoice_id,
            },
        )

    extracted_invoice_id = invoice.get("id")
    invoice_raw_id = invoice.get("invoice_raw_id")
    organisation_id = invoice.get("organisation_id")

    raw = None
    if invoice_raw_id:
        try:
            raw_res = (
                supabase
                .table("invoices_raw")
                .select("*")
                .eq("id", invoice_raw_id)
                .limit(1)
                .execute()
            )
            raw = raw_res.data[0] if raw_res.data else None
        except Exception as exc:
            fetch_errors["raw"] = str(exc)

    supplier = None
    supplier_id = invoice.get("supplier_id")
    if supplier_id:
        try:
            supplier_res = (
                supabase
                .table("suppliers")
                .select("*")
                .eq("id", supplier_id)
                .limit(1)
                .execute()
            )
            supplier = supplier_res.data[0] if supplier_res.data else None
        except Exception as exc:
            fetch_errors["supplier"] = str(exc)

    supplier_branch = None
    supplier_branches: list[dict] = []
    if supplier_id:
        try:
            branches_res = (
                supabase
                .table("supplier_branches")
                .select("*")
                .eq("supplier_id", supplier_id)
                .eq("organisation_id", organisation_id)
                .eq("active", True)
                .order("branch_name", desc=False)
                .execute()
            )
            supplier_branches = branches_res.data or []
            supplier_branch_id = invoice.get("supplier_branch_id")
            if supplier_branch_id:
                supplier_branch = next(
                    (branch for branch in supplier_branches if branch.get("id") == supplier_branch_id),
                    None,
                )
                if supplier_branch is None:
                    branch_res = (
                        supabase
                        .table("supplier_branches")
                        .select("*")
                        .eq("id", supplier_branch_id)
                        .eq("organisation_id", organisation_id)
                        .eq("supplier_id", supplier_id)
                        .limit(1)
                        .execute()
                    )
                    supplier_branch = branch_res.data[0] if branch_res.data else None
        except Exception as exc:
            fetch_errors["supplier_branches"] = str(exc)

    document_pages: list[dict] = []
    if invoice_raw_id:
        try:
            pages_res = (
                supabase
                .table("document_pages")
                .select("*")
                .eq("invoice_raw_id", invoice_raw_id)
                .order("page_number", desc=False)
                .limit(100)
                .execute()
            )
            document_pages = pages_res.data or []
        except Exception as exc:
            fetch_errors["document_pages"] = str(exc)

    line_items: list[dict] = []
    if extracted_invoice_id:
        try:
            line_items_res = (
                supabase
                .table("invoice_line_items")
                .select("*")
                .eq("invoice_extracted_id", extracted_invoice_id)
                .order("created_at", desc=False)
                .order("id", desc=False)
                .execute()
            )
            line_items = line_items_res.data or []
            line_item_ids = [row.get("id") for row in line_items if row.get("id")]
            if line_item_ids:
                try:
                    allocations_res = (
                        supabase
                        .table("invoice_line_item_allocations")
                        .select("*")
                        .in_("invoice_line_item_id", line_item_ids)
                        .order("sort_order", desc=False)
                        .order("created_at", desc=False)
                        .execute()
                    )
                    allocations_by_line: dict[str, list[dict]] = {}
                    for allocation in allocations_res.data or []:
                        line_id = allocation.get("invoice_line_item_id")
                        if line_id:
                            allocations_by_line.setdefault(line_id, []).append(allocation)
                    for line_item in line_items:
                        line_item["allocations"] = allocations_by_line.get(line_item.get("id"), [])
                except Exception as exc:
                    fetch_errors["line_item_allocations"] = str(exc)
        except Exception as exc:
            fetch_errors["line_items"] = str(exc)

    parse_attempts: list[dict] = []
    selected_parse_attempt_id = None
    if invoice_raw_id:
        try:
            parse_attempts, selected_parse_attempt_id = fetch_parse_attempts(
                supabase,
                invoice_raw_id=invoice_raw_id,
            )
        except Exception as exc:
            fetch_errors["parse_attempts"] = str(exc)

    audit_events: list[dict] = []
    if invoice_raw_id:
        try:
            audit_res = (
                supabase
                .table("invoice_audit_events")
                .select("*")
                .eq("invoice_raw_id", invoice_raw_id)
                .order("created_at", desc=False)
                .execute()
            )
            audit_events = audit_res.data or []
        except Exception as exc:
            fetch_errors["audit_events"] = str(exc)

    document_profile = build_extracted_document_profile(invoice)
    document_profile["line_items"] = line_items or document_profile.get("line_items") or []
    supplier_profile = build_extracted_supplier_profile(invoice)

    supplier_create_payload = build_supplier_create_payload(
        organisation_id=organisation_id,
        invoice_raw_id=invoice_raw_id,
        invoice_extracted_id=extracted_invoice_id,
        parsed_data=invoice,
    )

    return {
        "success": True,
        "resolved_by": resolved_by,
        "invoice_extracted_id": extracted_invoice_id,
        "invoice_raw_id": invoice_raw_id,
        "organisation_id": organisation_id,
        "invoice": {
            **invoice,
            "supplier": supplier,
        },
        "supplier_branch": supplier_branch,
        "supplier_branches": supplier_branches,
        "raw": raw,
        "document_pages": document_pages,
        "line_items": line_items,
        "parse_attempts": parse_attempts,
        "selected_parse_attempt_id": selected_parse_attempt_id,
        "audit_events": audit_events,
        "extracted_supplier_profile": supplier_profile,
        "supplier_create_payload": supplier_create_payload,
        "extracted_document_profile": document_profile,
        "supplier_endpoints": {
            "create_from_invoice": "/api/suppliers/from-invoice",
            "create": "/api/suppliers",
            "link": "/api/suppliers/link",
            "profile_from_invoice": (
                f"/api/suppliers/from-invoice/{extracted_invoice_id}"
                if extracted_invoice_id
                else None
            ),
        },
        "counts": {
            "document_pages": len(document_pages),
            "line_items": len(line_items),
            "parse_attempts": len(parse_attempts),
            "audit_events": len(audit_events),
        },
        "fetch_errors": fetch_errors,
    }


def _fetch_agent_context(invoice_id: str) -> dict:
    review_data = get_invoice_review_data(invoice_id)
    organisation_id = review_data.get("organisation_id")
    invoice = review_data.get("invoice") or {}
    supplier = invoice.get("supplier") if isinstance(invoice.get("supplier"), dict) else None

    accounts: list[dict] = []
    tracking_dimensions: list[dict] = []
    tracking_values: list[dict] = []
    duplicate_count = 0

    if organisation_id:
        try:
            accounts_res = (
                supabase
                .table("accounts")
                .select("id, code, name, type, active, vat_treatment")
                .eq("organisation_id", organisation_id)
                .eq("active", True)
                .execute()
            )
            accounts = accounts_res.data or []
        except Exception:
            accounts = []

        try:
            tracking_dimensions = required_tracking_dimensions(
                supabase,
                organisation_id=str(organisation_id),
                module_key="supplier",
            )
            dimension_ids = [row.get("id") for row in tracking_dimensions if row.get("id")]
            if dimension_ids:
                values_res = (
                    supabase
                    .table("tracking_values")
                    .select("id, dimension_id, code, name, active, sort_order")
                    .in_("dimension_id", dimension_ids)
                    .eq("active", True)
                    .order("sort_order", desc=False)
                    .order("name", desc=False)
                    .execute()
                )
                tracking_values = values_res.data or []
        except Exception:
            tracking_dimensions = []
            tracking_values = []

    invoice_number = invoice.get("invoice_number")
    supplier_id = invoice.get("supplier_id")
    extracted_invoice_id = review_data.get("invoice_extracted_id")
    if organisation_id and invoice_number and supplier_id and extracted_invoice_id:
        try:
            duplicate_res = (
                supabase
                .table("invoices_extracted")
                .select("id")
                .eq("organisation_id", organisation_id)
                .eq("supplier_id", supplier_id)
                .eq("invoice_number", invoice_number)
                .neq("id", extracted_invoice_id)
                .limit(10)
                .execute()
            )
            duplicate_count = len(duplicate_res.data or [])
        except Exception:
            duplicate_count = 0

    return {
        **review_data,
        "supplier": supplier,
        "supplier_branch": review_data.get("supplier_branch"),
        "supplier_branches": review_data.get("supplier_branches") or [],
        "accounts": accounts,
        "tracking_dimensions": tracking_dimensions,
        "tracking_values": tracking_values,
        "duplicate_count": duplicate_count,
    }


def _fetch_agent_suggestions_for_invoice(invoice_extracted_id: str | None, invoice_raw_id: str | None) -> list[dict]:
    if invoice_extracted_id:
        res = (
            supabase
            .table("invoice_agent_suggestions")
            .select("*")
            .eq("invoice_extracted_id", invoice_extracted_id)
            .order("created_at", desc=False)
            .execute()
        )
        return res.data or []
    if invoice_raw_id:
        res = (
            supabase
            .table("invoice_agent_suggestions")
            .select("*")
            .eq("invoice_raw_id", invoice_raw_id)
            .order("created_at", desc=False)
            .execute()
        )
        return res.data or []
    return []


def _agent_summary(suggestions: list[dict]) -> dict:
    open_items = [item for item in suggestions if item.get("status") == "open"]
    return {
        "total": len(suggestions),
        "open": len(open_items),
        "critical": sum(1 for item in open_items if item.get("severity") == "critical"),
        "warning": sum(1 for item in open_items if item.get("severity") == "warning"),
        "info": sum(1 for item in open_items if item.get("severity") == "info"),
        "applied": sum(1 for item in suggestions if item.get("status") == "applied"),
        "dismissed": sum(1 for item in suggestions if item.get("status") == "dismissed"),
        "checked": sum(1 for item in suggestions if item.get("status") == "checked"),
    }


def _org_role_for_user(user_id: str, organisation_id: str | None) -> Optional[str]:
    if not user_id or not organisation_id:
        return None
    try:
        res = (
            supabase
            .table("organisation_users")
            .select("role, status")
            .eq("user_id", user_id)
            .eq("organisation_id", organisation_id)
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        row = res.data[0] if res.data else None
        return row.get("role") if row else None
    except Exception:
        return None


def _ensure_agent_read_access(user_id: str, organisation_id: str | None) -> None:
    if not _org_role_for_user(user_id, organisation_id):
        raise HTTPException(status_code=403, detail="You do not have access to this invoice organisation")


def _ensure_agent_write_access(user_id: str, organisation_id: str | None) -> None:
    role = _org_role_for_user(user_id, organisation_id)
    if role not in AGENT_WRITE_ROLES:
        raise HTTPException(status_code=403, detail="Only owners, admins, and accountants can update agent suggestions")


def _fetch_supplier_comparison_ignores(invoice_extracted_id: str | None) -> list[dict]:
    if not invoice_extracted_id:
        return []
    res = (
        supabase
        .table("invoice_supplier_comparison_ignores")
        .select("*")
        .eq("invoice_extracted_id", invoice_extracted_id)
        .order("created_at", desc=False)
        .execute()
    )
    return res.data or []


def _persist_agent_suggestions(context: dict, generated: list[dict]) -> list[dict]:
    organisation_id = context.get("organisation_id")
    invoice_raw_id = context.get("invoice_raw_id")
    invoice_extracted_id = context.get("invoice_extracted_id")
    if not organisation_id:
        raise HTTPException(status_code=400, detail="Missing organisation_id")

    existing = _fetch_agent_suggestions_for_invoice(invoice_extracted_id, invoice_raw_id)
    existing_by_fingerprint = {row.get("fingerprint"): row for row in existing if row.get("fingerprint")}

    for suggestion in generated:
        fingerprint = suggestion.get("fingerprint")
        if not fingerprint:
            continue
        payload = {
            "organisation_id": organisation_id,
            "invoice_raw_id": invoice_raw_id,
            "invoice_extracted_id": invoice_extracted_id,
            "category": suggestion.get("category"),
            "severity": suggestion.get("severity"),
            "message": suggestion.get("message"),
            "reason": suggestion.get("reason"),
            "confidence": suggestion.get("confidence"),
            "apply_payload": suggestion.get("apply_payload"),
            "target": suggestion.get("target"),
            "fingerprint": fingerprint,
        }
        prior = existing_by_fingerprint.get(fingerprint)
        if prior:
            next_status = agent_status_after_regeneration(prior.get("status"))
            if next_status == "open":
                supabase.table("invoice_agent_suggestions").update({
                    **payload,
                    "status": next_status,
                }).eq("id", prior["id"]).execute()
            continue
        supabase.table("invoice_agent_suggestions").insert({
            **payload,
            "status": "open",
        }).execute()

    return _fetch_agent_suggestions_for_invoice(invoice_extracted_id, invoice_raw_id)


@router.get("/{invoice_id}/agent-review")
def get_invoice_agent_review(invoice_id: str, auth: UserAuth):
    user_id, _db = auth
    context = _fetch_agent_context(invoice_id)
    _ensure_agent_read_access(user_id, context.get("organisation_id"))
    suggestions = _fetch_agent_suggestions_for_invoice(
        context.get("invoice_extracted_id"),
        context.get("invoice_raw_id"),
    )
    return {
        "success": True,
        "invoice_extracted_id": context.get("invoice_extracted_id"),
        "invoice_raw_id": context.get("invoice_raw_id"),
        "organisation_id": context.get("organisation_id"),
        "suggestions": suggestions,
        "summary": _agent_summary(suggestions),
    }


@router.post("/{invoice_id}/agent-review")
def run_invoice_agent_review(invoice_id: str, auth: UserAuth):
    user_id, _db = auth
    context = _fetch_agent_context(invoice_id)
    _ensure_agent_write_access(user_id, context.get("organisation_id"))
    invoice = context.get("invoice") or {}
    generated = generate_invoice_agent_suggestions(
        invoice=invoice,
        supplier=context.get("supplier"),
        supplier_branch=context.get("supplier_branch"),
        supplier_branches=context.get("supplier_branches") or [],
        line_items=context.get("line_items") or [],
        accounts=context.get("accounts") or [],
        tracking_dimensions=context.get("tracking_dimensions") or [],
        tracking_values=context.get("tracking_values") or [],
        audit_events=context.get("audit_events") or [],
        parse_attempts=context.get("parse_attempts") or [],
        duplicate_count=int(context.get("duplicate_count") or 0),
    )
    suggestions = _persist_agent_suggestions(context, generated)
    summary = _agent_summary(suggestions)

    log_invoice_event(
        supabase,
        organisation_id=context.get("organisation_id"),
        invoice_raw_id=context.get("invoice_raw_id"),
        invoice_extracted_id=context.get("invoice_extracted_id"),
        event_type="agent_review_generated",
        stage="completed",
        actor_type="agent",
        actor_user_id=user_id,
        new_value={
            "generated_count": len(generated),
            "summary": summary,
        },
        notes="Invoice review agent generated suggest-only recommendations.",
    )

    return {
        "success": True,
        "invoice_extracted_id": context.get("invoice_extracted_id"),
        "invoice_raw_id": context.get("invoice_raw_id"),
        "organisation_id": context.get("organisation_id"),
        "suggestions": suggestions,
        "summary": summary,
    }


def _get_agent_suggestion_or_404(suggestion_id: str) -> dict:
    res = (
        supabase
        .table("invoice_agent_suggestions")
        .select("*")
        .eq("id", suggestion_id)
        .limit(1)
        .execute()
    )
    row = res.data[0] if res.data else None
    if not row:
        raise HTTPException(status_code=404, detail="Agent suggestion not found")
    return row


@router.post("/agent-suggestions/{suggestion_id}/apply")
def apply_agent_suggestion(
    suggestion_id: str,
    auth: UserAuth,
    payload: AgentSuggestionActionRequest | None = None,
):
    user_id, _db = auth
    suggestion = _get_agent_suggestion_or_404(suggestion_id)
    _ensure_agent_write_access(user_id, suggestion.get("organisation_id"))
    if suggestion.get("status") != "open":
        raise HTTPException(status_code=409, detail="Only open suggestions can be applied")

    safe_payload = filter_safe_apply_payload(suggestion.get("apply_payload"))
    if not safe_payload:
        raise HTTPException(status_code=422, detail="Suggestion has no safe apply action")

    action_type = safe_payload["type"]
    fields = safe_payload["fields"]
    if action_type == "invoice_patch":
        target_id = suggestion.get("invoice_extracted_id")
        if not target_id:
            raise HTTPException(status_code=422, detail="Suggestion is not linked to an extracted invoice")
        supabase.table("invoices_extracted").update(fields).eq("id", target_id).execute()
    elif action_type == "line_item_patch":
        supabase.table("invoice_line_items").update(fields).eq(
            "id",
            safe_payload["line_item_id"],
        ).eq(
            "organisation_id",
            suggestion["organisation_id"],
        ).execute()
    elif action_type == "supplier_patch":
        supabase.table("suppliers").update(fields).eq(
            "id",
            safe_payload["supplier_id"],
        ).eq(
            "organisation_id",
            suggestion["organisation_id"],
        ).execute()
    else:
        raise HTTPException(status_code=422, detail="Unsupported suggestion action")

    update_res = (
        supabase
        .table("invoice_agent_suggestions")
        .update({"status": "applied"})
        .eq("id", suggestion_id)
        .execute()
    )
    updated = update_res.data[0] if update_res.data else {**suggestion, "status": "applied"}

    log_invoice_event(
        supabase,
        organisation_id=suggestion["organisation_id"],
        invoice_raw_id=suggestion.get("invoice_raw_id"),
        invoice_extracted_id=suggestion.get("invoice_extracted_id"),
        event_type="agent_suggestion_applied",
        stage="completed",
        actor_type="user",
        actor_user_id=user_id,
        field_name=action_type,
        new_value=safe_payload,
        notes=(payload.note if payload else None) or suggestion.get("message"),
    )

    readiness = None
    if suggestion.get("invoice_extracted_id"):
        readiness = evaluate_invoice_readiness(
            supabase,
            invoice_extracted_id=suggestion["invoice_extracted_id"],
            organisation_id=suggestion.get("organisation_id"),
            reason="Agent suggestion applied.",
            actor_type="user",
            actor_user_id=user_id,
        )

    return {"success": True, "suggestion": updated, "readiness": readiness}


@router.post("/agent-suggestions/{suggestion_id}/dismiss")
def dismiss_agent_suggestion(
    suggestion_id: str,
    auth: UserAuth,
    payload: AgentSuggestionActionRequest | None = None,
):
    user_id, _db = auth
    suggestion = _get_agent_suggestion_or_404(suggestion_id)
    _ensure_agent_write_access(user_id, suggestion.get("organisation_id"))
    update_res = (
        supabase
        .table("invoice_agent_suggestions")
        .update({"status": "dismissed"})
        .eq("id", suggestion_id)
        .execute()
    )
    updated = update_res.data[0] if update_res.data else {**suggestion, "status": "dismissed"}
    log_invoice_event(
        supabase,
        organisation_id=suggestion["organisation_id"],
        invoice_raw_id=suggestion.get("invoice_raw_id"),
        invoice_extracted_id=suggestion.get("invoice_extracted_id"),
        event_type="agent_suggestion_dismissed",
        stage="completed",
        actor_type="user",
        actor_user_id=user_id,
        new_value={"suggestion_id": suggestion_id, "message": suggestion.get("message")},
        notes=payload.note if payload else None,
    )
    return {"success": True, "suggestion": updated}


@router.post("/agent-suggestions/{suggestion_id}/checked")
def check_agent_suggestion(
    suggestion_id: str,
    auth: UserAuth,
    payload: AgentSuggestionActionRequest | None = None,
):
    user_id, _db = auth
    suggestion = _get_agent_suggestion_or_404(suggestion_id)
    _ensure_agent_write_access(user_id, suggestion.get("organisation_id"))
    update_res = (
        supabase
        .table("invoice_agent_suggestions")
        .update({"status": "checked"})
        .eq("id", suggestion_id)
        .execute()
    )
    updated = update_res.data[0] if update_res.data else {**suggestion, "status": "checked"}
    log_invoice_event(
        supabase,
        organisation_id=suggestion["organisation_id"],
        invoice_raw_id=suggestion.get("invoice_raw_id"),
        invoice_extracted_id=suggestion.get("invoice_extracted_id"),
        event_type="agent_suggestion_checked",
        stage="completed",
        actor_type="user",
        actor_user_id=user_id,
        new_value={
            "suggestion_id": suggestion_id,
            "message": suggestion.get("message"),
            "target": suggestion.get("target"),
        },
        notes=payload.note if payload else "Reviewer acknowledged the focused agent finding.",
    )
    return {"success": True, "suggestion": updated}


@router.post("/{invoice_id}/agent-review/checked")
def mark_agent_review_checked(
    invoice_id: str,
    auth: UserAuth,
    payload: AgentSuggestionActionRequest | None = None,
):
    user_id, _db = auth
    context = _fetch_agent_context(invoice_id)
    invoice_extracted_id = context.get("invoice_extracted_id")
    invoice_raw_id = context.get("invoice_raw_id")
    organisation_id = context.get("organisation_id")
    _ensure_agent_write_access(user_id, organisation_id)

    if invoice_extracted_id:
        supabase.table("invoice_agent_suggestions").update({"status": "checked"}).eq(
            "invoice_extracted_id",
            invoice_extracted_id,
        ).eq("status", "open").execute()
    elif invoice_raw_id:
        supabase.table("invoice_agent_suggestions").update({"status": "checked"}).eq(
            "invoice_raw_id",
            invoice_raw_id,
        ).eq("status", "open").execute()

    suggestions = _fetch_agent_suggestions_for_invoice(invoice_extracted_id, invoice_raw_id)
    log_invoice_event(
        supabase,
        organisation_id=organisation_id,
        invoice_raw_id=invoice_raw_id,
        invoice_extracted_id=invoice_extracted_id,
        event_type="agent_review_checked",
        stage="completed",
        actor_type="user",
        actor_user_id=user_id,
        new_value={"summary": _agent_summary(suggestions)},
        notes=payload.note if payload else "Reviewer marked the agent checklist as checked.",
    )
    return {
        "success": True,
        "suggestions": suggestions,
        "summary": _agent_summary(suggestions),
    }


@router.get("/{invoice_id}/supplier-comparison-ignores")
def get_supplier_comparison_ignores(invoice_id: str, auth: UserAuth):
    user_id, _db = auth
    context = _fetch_agent_context(invoice_id)
    _ensure_agent_read_access(user_id, context.get("organisation_id"))
    return {
        "success": True,
        "ignores": _fetch_supplier_comparison_ignores(context.get("invoice_extracted_id")),
    }


@router.post("/{invoice_id}/supplier-comparison-ignores")
def ignore_supplier_comparison_field(
    invoice_id: str,
    payload: SupplierComparisonIgnoreRequest,
    auth: UserAuth,
):
    user_id, _db = auth
    context = _fetch_agent_context(invoice_id)
    organisation_id = context.get("organisation_id")
    invoice_extracted_id = context.get("invoice_extracted_id")
    invoice_raw_id = context.get("invoice_raw_id")
    invoice = context.get("invoice") or {}
    field_key = (payload.field_key or "").strip()

    _ensure_agent_write_access(user_id, organisation_id)
    if not invoice_extracted_id:
        raise HTTPException(status_code=400, detail="Missing invoice_extracted_id")
    if field_key not in IGNORABLE_SUPPLIER_COMPARISON_FIELDS:
        raise HTTPException(status_code=422, detail="This supplier comparison field cannot be ignored")

    insert_payload = {
        "organisation_id": organisation_id,
        "invoice_extracted_id": invoice_extracted_id,
        "supplier_id": invoice.get("supplier_id"),
        "field_key": field_key,
        "reason": payload.reason,
        "created_by": user_id,
    }
    res = (
        supabase
        .table("invoice_supplier_comparison_ignores")
        .upsert(insert_payload, on_conflict="organisation_id,invoice_extracted_id,field_key")
        .execute()
    )
    ignored = res.data[0] if res.data else insert_payload

    log_invoice_event(
        supabase,
        organisation_id=organisation_id,
        invoice_raw_id=invoice_raw_id,
        invoice_extracted_id=invoice_extracted_id,
        event_type="supplier_comparison_ignored",
        stage="review",
        field_name=field_key,
        actor_type="user",
        actor_user_id=user_id,
        new_value=ignored,
        notes=payload.reason or "Reviewer ignored supplier comparison difference.",
    )
    return {"success": True, "ignore": ignored}


@router.delete("/{invoice_id}/supplier-comparison-ignores/{field_key}")
def undo_supplier_comparison_ignore(invoice_id: str, field_key: str, auth: UserAuth):
    user_id, _db = auth
    context = _fetch_agent_context(invoice_id)
    organisation_id = context.get("organisation_id")
    invoice_extracted_id = context.get("invoice_extracted_id")
    invoice_raw_id = context.get("invoice_raw_id")

    _ensure_agent_write_access(user_id, organisation_id)
    if not invoice_extracted_id:
        raise HTTPException(status_code=400, detail="Missing invoice_extracted_id")
    if field_key not in IGNORABLE_SUPPLIER_COMPARISON_FIELDS:
        raise HTTPException(status_code=422, detail="This supplier comparison field cannot be ignored")

    supabase.table("invoice_supplier_comparison_ignores").delete().eq(
        "invoice_extracted_id",
        invoice_extracted_id,
    ).eq("organisation_id", organisation_id).eq("field_key", field_key).execute()

    log_invoice_event(
        supabase,
        organisation_id=organisation_id,
        invoice_raw_id=invoice_raw_id,
        invoice_extracted_id=invoice_extracted_id,
        event_type="supplier_comparison_ignore_removed",
        stage="review",
        field_name=field_key,
        actor_type="user",
        actor_user_id=user_id,
        new_value={"field_key": field_key},
        notes="Reviewer removed supplier comparison ignore.",
    )
    return {"success": True}


@router.post("/jobs/process-next")
def process_next_invoice_job(payload: ProcessNextJobRequest):
    return process_next_queued_invoice_job(organisation_id=payload.organisation_id)


@router.get("/raw/{invoice_raw_id}/file")
def get_invoice_raw_file(invoice_raw_id: str):
    raw = get_raw_invoice(invoice_raw_id)
    file_path = raw.get("file_path")
    file_type = raw.get("file_type") or "application/pdf"

    if not file_path:
        raise HTTPException(status_code=400, detail="Missing file_path")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Storage download error: {str(e)}")

    return Response(
        content=file_bytes,
        media_type=file_type,
        headers={
            "Content-Disposition": f'inline; filename="{raw.get("file_name", "invoice.pdf")}"',
        },
    )


@router.get("/raw/{invoice_raw_id}/preview-image")
def get_invoice_preview_image(invoice_raw_id: str, page: int = 0):
    raw = get_raw_invoice(invoice_raw_id)
    file_path = raw.get("file_path")
    file_type = raw.get("file_type") or "application/pdf"

    if not file_path:
        raise HTTPException(status_code=400, detail="Missing file_path")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Storage download error: {str(e)}")

    try:
        if file_type == "application/pdf" or file_path.lower().endswith(".pdf"):
            doc = fitz.open(stream=file_bytes, filetype="pdf")

            if page < 0 or page >= len(doc):
                raise HTTPException(status_code=400, detail="Invalid page number")

            pdf_page = doc[page]
            matrix = fitz.Matrix(2, 2)
            pix = pdf_page.get_pixmap(matrix=matrix, alpha=False)
            image_bytes = pix.tobytes("png")

            return Response(
                content=image_bytes,
                media_type="image/png",
                headers={"Cache-Control": "no-store"},
            )

        return Response(
            content=file_bytes,
            media_type=file_type,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Preview rendering failed: {str(e)}")


@router.post("/save-line-items")
def save_invoice_line_items(req: SaveLineItemsRequest):
    """
    Persist user-edited line items and recompute invoices_extracted totals.
    Line items are the source of truth: subtotal = SUM(line_total), VAT = subtotal * rate,
    total = subtotal + VAT.  Rounding differences vs document_total are absorbed automatically.
    """
    # 1. Determine VAT rate — only if supplier is a VAT vendor (has vat_number)
    vat_rate = 0.0
    if req.supplier_id:
        settings = fetch_supplier_processing_settings(supabase, req.supplier_id)
        if settings.get("vat_number"):
            raw_rate = settings.get("default_vat_rate")
            vat_rate = float(raw_rate) / 100 if raw_rate else 0.15

    # 2. Subtotal from line items
    subtotal = round(sum(float(it.get("line_total") or 0) for it in req.line_items), 2)

    # 3. Fetch document-extracted tax_amount — this is the source-of-truth from VLM/OCR
    # and must NOT be overwritten by a re-calculation from line items.
    existing_inv = (
        supabase.table("invoices_extracted")
        .select("tax_amount")
        .eq("id", req.invoice_extracted_id)
        .single()
        .execute()
    )
    existing_tax = round(float((existing_inv.data or {}).get("tax_amount") or 0), 2)

    # computed_vat is used only for rounding/reconciliation logic below; it is
    # never written back to invoices_extracted.tax_amount.
    computed_vat = round(subtotal * vat_rate, 2)
    computed_total = round(subtotal + existing_tax, 2)

    # 4. Hybrid rounding adjustment vs original document total
    rounding_applied = None
    needs_review = False
    final_line_items = list(req.line_items)

    if req.document_total is not None:
        diff = round(req.document_total - computed_total, 2)
        abs_diff = abs(diff)
        if 0 < abs_diff <= 0.02:
            # Absorb silently — floating-point drift, total_amount already close enough
            rounding_applied = "vat_adjusted"
        elif 0 < abs_diff <= 0.50:
            # Named rounding line item for visible penny differences
            final_line_items.append({"description": "Rounding adjustment", "line_total": diff})
            subtotal = round(subtotal + diff, 2)
            computed_total = round(subtotal + existing_tax, 2)
            rounding_applied = "line_item_added"
        elif abs_diff > 0.50:
            # Too large to auto-fix — flag for human review
            needs_review = True
            rounding_applied = "needs_review"

    # 5. Persist line items (always delete-and-replace on explicit save)
    try:
        diagnostics = replace_invoice_line_items(
            supabase,
            invoice_extracted_id=req.invoice_extracted_id,
            organisation_id=req.organisation_id,
            line_items=final_line_items,
            invoice_total=computed_total,
            delete_when_empty=True,
            raise_on_error=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save line items: {exc}")

    # 6. Update invoices_extracted with derived totals.
    # total_amount and tax_amount are document-extracted values (set by VLM/OCR)
    # and must only change on re-extraction, not on line item saves.
    # Overwriting total_amount with a computed value causes double-counting when
    # prices include VAT (line totals are inclusive, so subtotal + existing_tax > document total).
    patch: dict = {
        "subtotal": subtotal,
    }
    if needs_review:
        patch["validation_status"] = "needs_review"

    supabase.table("invoices_extracted").update(patch).eq("id", req.invoice_extracted_id).execute()

    readiness = evaluate_invoice_readiness(
        supabase,
        invoice_extracted_id=req.invoice_extracted_id,
        organisation_id=req.organisation_id,
        reason="Line items saved.",
        actor_type="api",
    )

    return {
        "subtotal": subtotal,
        "tax_amount": existing_tax,
        "total_amount": computed_total,
        "rounding_applied": rounding_applied,
        "needs_review": needs_review,
        "diagnostics": diagnostics,
        "readiness": readiness,
    }


class ReapplyRulesRequest(BaseModel):
    invoice_extracted_id: str
    organisation_id: str


@router.post("/reapply-supplier-rules")
def reapply_supplier_rules_endpoint(req: ReapplyRulesRequest):
    result = (
        supabase.table("invoices_extracted")
        .select("*, supplier:suppliers(*)")
        .eq("id", req.invoice_extracted_id)
        .eq("organisation_id", req.organisation_id)
        .single()
        .execute()
    )
    invoice = result.data if result else None
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    supplier_id = invoice.get("supplier_id")
    if not supplier_id:
        raise HTTPException(status_code=422, detail="No supplier linked to this invoice")

    rules_applied = reapply_supplier_rules_to_invoice(
        supabase,
        invoice=invoice,
        supplier_id=supplier_id,
        actor_type="user",
        event_reason="Manual re-apply of supplier rules via UI.",
    )

    # Recompute and persist subtotal / tax / total from the fresh line items
    if not rules_applied.get("skipped"):
        _recompute_invoice_totals(supabase, req.invoice_extracted_id, invoice)

    readiness = evaluate_invoice_readiness(
        supabase,
        invoice_extracted_id=req.invoice_extracted_id,
        organisation_id=req.organisation_id,
        reason="Supplier rules re-applied.",
        actor_type="user",
    )

    return {"success": True, "rules_applied": rules_applied, "readiness": readiness}


def _recompute_invoice_totals(supabase_client, invoice_extracted_id: str, invoice: dict) -> None:
    """After line items are replaced, recompute subtotal and tax_amount on invoices_extracted.

    NOTE: total_amount is intentionally NOT updated here — it holds the value extracted
    from the source document (OCR/VLM) and must not be overwritten by a line-items
    recalculation (doing so would corrupt the 'Invoice total (from document)' display
    and break the reconciliation green/red dot logic).
    """
    try:
        rows = (
            supabase_client.table("invoice_line_items")
            .select("line_total")
            .eq("invoice_extracted_id", invoice_extracted_id)
            .execute()
        ).data or []

        subtotal = round(sum(float(r.get("line_total") or 0) for r in rows), 2)

        # Determine VAT rate from the linked supplier (already joined in the invoice dict)
        supplier = invoice.get("supplier") or {}
        vat_rate = 0.0
        if supplier.get("vat_number"):
            raw_rate = supplier.get("default_vat_rate")
            vat_rate = float(raw_rate) / 100 if raw_rate else 0.15

        tax_amount = round(subtotal * vat_rate, 2)

        supabase_client.table("invoices_extracted").update({
            "subtotal": subtotal,
            "tax_amount": tax_amount,
            # total_amount deliberately omitted — preserve the document-extracted value
        }).eq("id", invoice_extracted_id).execute()

        print(f"REAPPLY: recomputed totals for {invoice_extracted_id}: "
              f"subtotal={subtotal}, tax={tax_amount}")
    except Exception as exc:
        print(f"REAPPLY: failed to recompute totals for {invoice_extracted_id}: {exc}")


class MergeInvoicesPayload(BaseModel):
    invoice_raw_ids: list[str]
    organisation_id: str


@router.post("/merge")
def merge_invoices(payload: MergeInvoicesPayload, background_tasks: BackgroundTasks):
    """
    Merge two or more single-page invoice uploads into one multi-page document and
    trigger a fresh extraction.  Old raw records (and all dependent data) are deleted.
    """
    import time as _time

    if len(payload.invoice_raw_ids) < 2:
        raise HTTPException(status_code=400, detail="At least two invoice_raw_ids are required")

    # Step 1 — fetch raw records in caller-specified page order
    rows_result = (
        supabase.from_("invoices_raw")
        .select("id, file_path, file_name, file_type")
        .in_("id", payload.invoice_raw_ids)
        .eq("organisation_id", payload.organisation_id)
        .execute()
    )
    rows = rows_result.data or []
    if len(rows) != len(payload.invoice_raw_ids):
        raise HTTPException(status_code=404, detail="One or more invoices not found")

    id_to_row = {r["id"]: r for r in rows}
    ordered = [id_to_row[rid] for rid in payload.invoice_raw_ids]

    # Step 2 — download each file from Storage
    file_bytes_list: list[tuple[bytes, str]] = []
    for row in ordered:
        file_bytes = supabase.storage.from_("invoices").download(row["file_path"])
        file_bytes_list.append((file_bytes, row.get("file_type") or "application/pdf"))

    # Step 3 — merge into one PDF using PyMuPDF (fitz is already imported at module level)
    import io as _io
    from PIL import Image as _PILImage, ImageOps as _ImageOps

    merged_doc = fitz.open()
    for file_bytes, file_type in file_bytes_list:
        if file_type.startswith("image/"):
            # Apply EXIF orientation before embedding so the PDF is correctly oriented.
            pil_img = _ImageOps.exif_transpose(_PILImage.open(_io.BytesIO(file_bytes))).convert("RGB")
            corrected_buf = _io.BytesIO()
            pil_img.save(corrected_buf, format="PNG")
            img_doc = fitz.open(stream=corrected_buf.getvalue(), filetype="png")
            pdf_bytes = img_doc.convert_to_pdf()
            img_doc.close()
            src = fitz.open("pdf", pdf_bytes)
        else:
            src = fitz.open(stream=file_bytes, filetype="pdf")
        merged_doc.insert_pdf(src)
        src.close()
    merged_bytes = merged_doc.tobytes()
    merged_doc.close()

    # Step 4 — upload merged PDF to Storage
    base_name = re.sub(r"[^a-zA-Z0-9._-]", "_", ordered[0].get("file_name") or "merged")
    if not base_name.lower().endswith(".pdf"):
        base_name = base_name + ".pdf"
    new_file_name = f"{int(_time.time())}-merged-{base_name}"
    new_path = f"{payload.organisation_id}/invoices/{new_file_name}"

    supabase.storage.from_("invoices").upload(
        new_path,
        merged_bytes,
        {"content-type": "application/pdf"},
    )

    # Step 5 — create new invoices_raw record
    new_raw_result = (
        supabase.from_("invoices_raw")
        .insert({
            "organisation_id": payload.organisation_id,
            "file_path": new_path,
            "file_name": new_file_name,
            "file_type": "application/pdf",
            "parse_status": "pending",
            "upload_status": "uploaded",
        })
        .execute()
    )
    new_raw_id = new_raw_result.data[0]["id"]

    # Step 6 — delete old records (manual cascade: no FK cascade on invoices_raw)
    for old_id in payload.invoice_raw_ids:
        extracted_rows = (
            supabase.from_("invoices_extracted")
            .select("id")
            .eq("invoice_raw_id", old_id)
            .execute()
        ).data or []
        extracted_ids = [r["id"] for r in extracted_rows]

        if extracted_ids:
            supabase.from_("invoice_line_items").delete().in_("invoice_extracted_id", extracted_ids).execute()
            try:
                supabase.from_("invoice_extraction_feedback").delete().in_("invoice_extracted_id", extracted_ids).execute()
            except Exception:
                pass
            supabase.from_("invoices_extracted").delete().eq("invoice_raw_id", old_id).execute()

        supabase.from_("invoice_parse_attempts").delete().eq("invoice_raw_id", old_id).execute()
        supabase.from_("document_pages").delete().eq("invoice_raw_id", old_id).execute()
        supabase.from_("invoice_audit_events").delete().eq("invoice_raw_id", old_id).execute()

        try:
            supabase.storage.from_("invoices").remove([id_to_row[old_id]["file_path"]])
        except Exception:
            pass

        supabase.from_("invoices_raw").delete().eq("id", old_id).execute()

    # Step 7 — queue extraction on the merged record
    queue_invoice_job(invoice_raw_id=new_raw_id, organisation_id=payload.organisation_id)
    background_tasks.add_task(run_extract_worker_until_empty)

    return {"success": True, "new_invoice_raw_id": new_raw_id}


@router.post("/{raw_id}/split-into-pages")
def split_invoice_into_pages(raw_id: str, organisation_id: str, background_tasks: BackgroundTasks):
    """
    Split a multi-page PDF into individual single-page invoices, one per page.
    Each page is uploaded as a new invoices_raw record and queued for extraction.
    The original record and all dependent data are deleted.
    """
    import time as _time

    # Step 1 — fetch the raw record
    row_result = (
        supabase.from_("invoices_raw")
        .select("id, file_path, file_name, file_type")
        .eq("id", raw_id)
        .eq("organisation_id", organisation_id)
        .single()
        .execute()
    )
    row = row_result.data
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Step 2 — download and open with PyMuPDF
    file_bytes = supabase.storage.from_("invoices").download(row["file_path"])
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    page_count = len(doc)
    if page_count < 2:
        doc.close()
        raise HTTPException(status_code=422, detail="Document has only one page — use the crop tool for within-page splits")

    # Step 3 — split into N single-page PDFs and upload each
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", row.get("file_name") or "split")
    if safe_name.lower().endswith(".pdf"):
        safe_name = safe_name[:-4]

    new_raw_ids: list[str] = []
    for i in range(page_count):
        single_doc = fitz.open()
        single_doc.insert_pdf(doc, from_page=i, to_page=i)
        page_bytes = single_doc.tobytes()
        single_doc.close()

        new_file_name = f"{int(_time.time())}-p{i + 1}-{safe_name}.pdf"
        new_path = f"{organisation_id}/invoices/{new_file_name}"

        supabase.storage.from_("invoices").upload(
            new_path,
            page_bytes,
            {"content-type": "application/pdf"},
        )

        new_raw_result = (
            supabase.from_("invoices_raw")
            .insert({
                "organisation_id": organisation_id,
                "file_path": new_path,
                "file_name": new_file_name,
                "file_type": "application/pdf",
                "parse_status": "pending",
                "upload_status": "uploaded",
            })
            .execute()
        )
        new_raw_ids.append(new_raw_result.data[0]["id"])

    doc.close()

    # Step 4 — delete original record (same cascade order as merge)
    extracted_rows = (
        supabase.from_("invoices_extracted")
        .select("id")
        .eq("invoice_raw_id", raw_id)
        .execute()
    ).data or []
    extracted_ids = [r["id"] for r in extracted_rows]

    if extracted_ids:
        supabase.from_("invoice_line_items").delete().in_("invoice_extracted_id", extracted_ids).execute()
        try:
            supabase.from_("invoice_extraction_feedback").delete().in_("invoice_extracted_id", extracted_ids).execute()
        except Exception:
            pass
        supabase.from_("invoices_extracted").delete().eq("invoice_raw_id", raw_id).execute()

    supabase.from_("invoice_parse_attempts").delete().eq("invoice_raw_id", raw_id).execute()
    supabase.from_("document_pages").delete().eq("invoice_raw_id", raw_id).execute()
    supabase.from_("invoice_audit_events").delete().eq("invoice_raw_id", raw_id).execute()

    try:
        supabase.storage.from_("invoices").remove([row["file_path"]])
    except Exception:
        pass

    supabase.from_("invoices_raw").delete().eq("id", raw_id).execute()

    # Step 5 — queue extraction for all new records and drain the worker
    for new_id in new_raw_ids:
        queue_invoice_job(invoice_raw_id=new_id, organisation_id=organisation_id)
    background_tasks.add_task(run_extract_worker_until_empty)

    return {"success": True, "page_count": page_count, "new_raw_ids": new_raw_ids}


class _PageCropModel(BaseModel):
    x: float
    y: float
    w: float
    h: float


class _PageRefModel(BaseModel):
    kind: str               # "full" | "crop"
    page_number: int        # 1-indexed
    crop: _PageCropModel | None = None


class _PageGroupModel(BaseModel):
    pages: list[_PageRefModel]


class ProcessPageGroupsPayload(BaseModel):
    invoice_raw_id: str
    organisation_id: str
    groups: list[_PageGroupModel]


@router.post("/process-page-groups")
def process_page_groups(payload: ProcessPageGroupsPayload, background_tasks: BackgroundTasks):
    """
    Split a multi-page PDF into one output PDF per group, where each group is a user-defined
    set of full pages and/or cropped regions.  Supports both "each page is a doc" and
    "multiple docs on one page" scenarios.  Original record is deleted after splitting.
    """
    import time as _time

    if not payload.groups:
        raise HTTPException(status_code=400, detail="At least one group is required")

    # Step 1 — fetch original raw record
    row_result = (
        supabase.from_("invoices_raw")
        .select("id, file_path, file_name, file_type")
        .eq("id", payload.invoice_raw_id)
        .eq("organisation_id", payload.organisation_id)
        .limit(1)
        .execute()
    )
    if not row_result.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    row = row_result.data[0]

    # Step 2 — download and open original PDF
    file_bytes = supabase.storage.from_("invoices").download(row["file_path"])
    doc = fitz.open(stream=file_bytes, filetype="pdf")

    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", row.get("file_name") or "split")
    if safe_name.lower().endswith(".pdf"):
        safe_name = safe_name[:-4]

    # Step 3 — build one output PDF per group
    new_raw_ids: list[str] = []
    for group_idx, group in enumerate(payload.groups):
        out_doc = fitz.open()
        for ref in group.pages:
            page_index = ref.page_number - 1   # 1-indexed → 0-indexed
            if page_index < 0 or page_index >= len(doc):
                continue
            src_page = doc[page_index]
            if ref.kind == "full":
                tmp = fitz.open()
                tmp.insert_pdf(doc, from_page=page_index, to_page=page_index)
                out_doc.insert_pdf(tmp)
                tmp.close()
            elif ref.kind == "crop" and ref.crop:
                c = ref.crop
                rect = fitz.Rect(
                    src_page.rect.width  * c.x,
                    src_page.rect.height * c.y,
                    src_page.rect.width  * (c.x + c.w),
                    src_page.rect.height * (c.y + c.h),
                )
                pix = src_page.get_pixmap(clip=rect, dpi=200)
                img_doc = fitz.open()
                img_page = img_doc.new_page(width=pix.width, height=pix.height)
                img_page.insert_image(img_page.rect, pixmap=pix)
                out_doc.insert_pdf(img_doc)
                img_doc.close()

        if len(out_doc) == 0:
            out_doc.close()
            continue

        out_bytes = out_doc.tobytes()
        out_doc.close()

        new_file_name = f"{int(_time.time())}-g{group_idx + 1}-{safe_name}.pdf"
        new_path = f"{payload.organisation_id}/invoices/{new_file_name}"
        supabase.storage.from_("invoices").upload(
            new_path, out_bytes, {"content-type": "application/pdf"}
        )
        new_raw_result = (
            supabase.from_("invoices_raw")
            .insert({
                "organisation_id": payload.organisation_id,
                "file_path": new_path,
                "file_name": new_file_name,
                "file_type": "application/pdf",
                "parse_status": "pending",
                "upload_status": "uploaded",
            })
            .execute()
        )
        new_raw_ids.append(new_raw_result.data[0]["id"])

    doc.close()

    # Step 4 — queue extraction for all new records (before deleting original,
    # so a queue failure leaves the original intact and the user can retry)
    for new_id in new_raw_ids:
        queue_invoice_job(invoice_raw_id=new_id, organisation_id=payload.organisation_id)
    background_tasks.add_task(run_extract_worker_until_empty)

    # Step 5 — delete original record now that all new records are safely queued
    extracted_rows = (
        supabase.from_("invoices_extracted")
        .select("id")
        .eq("invoice_raw_id", payload.invoice_raw_id)
        .execute()
    ).data or []
    extracted_ids = [r["id"] for r in extracted_rows]

    if extracted_ids:
        supabase.from_("invoice_line_items").delete().in_("invoice_extracted_id", extracted_ids).execute()
        try:
            supabase.from_("invoice_extraction_feedback").delete().in_("invoice_extracted_id", extracted_ids).execute()
        except Exception:
            pass
        supabase.from_("invoices_extracted").delete().eq("invoice_raw_id", payload.invoice_raw_id).execute()

    supabase.from_("invoice_parse_attempts").delete().eq("invoice_raw_id", payload.invoice_raw_id).execute()
    supabase.from_("document_pages").delete().eq("invoice_raw_id", payload.invoice_raw_id).execute()
    supabase.from_("invoice_audit_events").delete().eq("invoice_raw_id", payload.invoice_raw_id).execute()

    try:
        supabase.storage.from_("invoices").remove([row["file_path"]])
    except Exception:
        pass

    supabase.from_("invoices_raw").delete().eq("id", payload.invoice_raw_id).execute()

    return {"success": True, "group_count": len(new_raw_ids), "new_raw_ids": new_raw_ids}


@router.post("/generate-preview")
def generate_invoice_preview(req: GeneratePreviewRequest):
    """
    Render preview images for an invoice without running VLM extraction (~1s).
    Saves images to Supabase Storage and upserts document_pages rows.
    Fixes missing previews for old invoices and PDFs processed via the selectable-text path.
    """
    import io as _io
    from PIL import Image as _Image
    from app.services.invoice_ocr_pipeline import pdf_to_images
    from app.services.invoice_extraction.receipt_preprocessing import generate_preview_images
    from app.services.invoice_previews import upload_invoice_preview_image

    raw_res = supabase.table("invoices_raw").select("file_path, file_type").eq("id", req.invoice_raw_id).single().execute()
    if not raw_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    raw = raw_res.data
    file_path = raw.get("file_path")
    if not file_path:
        raise HTTPException(status_code=400, detail="No file_path on invoices_raw record")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Storage download failed: {e}")

    file_type = raw.get("file_type") or "application/pdf"
    is_pdf = "pdf" in str(file_type).lower()

    try:
        if is_pdf:
            images = pdf_to_images(file_bytes)
        else:
            images = [_Image.open(_io.BytesIO(file_bytes)).convert("RGB")]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Page rendering failed: {e}")

    results = []
    for i, img in enumerate(images, 1):
        try:
            previews = generate_preview_images(img, img)
            orig_path = f"{req.organisation_id}/invoices/previews/{req.invoice_raw_id}/page-{i}-original.jpg"
            proc_path = f"{req.organisation_id}/invoices/previews/{req.invoice_raw_id}/page-{i}-processed.jpg"
            upload_invoice_preview_image(supabase, storage_path=orig_path, image=previews.original_preview)
            upload_invoice_preview_image(supabase, storage_path=proc_path, image=previews.processed_preview)
            results.append({
                "page_number": i,
                "original_preview_path": orig_path,
                "processed_preview_path": proc_path,
            })
        except Exception as e:
            print(f"[generate-preview] page {i} upload failed: {e}")

    # Upsert document_pages rows
    for page in results:
        existing = supabase.table("document_pages").select("id").eq("invoice_raw_id", req.invoice_raw_id).eq("page_number", page["page_number"]).execute().data
        if existing:
            supabase.table("document_pages").update({
                "original_preview_path": page["original_preview_path"],
                "processed_preview_path": page["processed_preview_path"],
            }).eq("invoice_raw_id", req.invoice_raw_id).eq("page_number", page["page_number"]).execute()
        else:
            supabase.table("document_pages").insert({
                "invoice_raw_id": req.invoice_raw_id,
                "organisation_id": req.organisation_id,
                "page_number": page["page_number"],
                "original_preview_path": page["original_preview_path"],
                "processed_preview_path": page["processed_preview_path"],
            }).execute()

    # Update invoices_raw with page-1 preview path
    if results:
        supabase.table("invoices_raw").update({
            "preview_path": results[0]["original_preview_path"],
            "processed_preview_path": results[0]["processed_preview_path"],
            "updated_at": utc_now_iso(),
        }).eq("id", req.invoice_raw_id).execute()

    return {"generated": len(results), "pages": results}


# ─────────────────────────────────────────────────────────────────────────────
# GL Posting
# ─────────────────────────────────────────────────────────────────────────────

class PostInvoiceToGLRequest(BaseModel):
    organisation_id: str


def _new_uuid() -> str:
    import uuid
    return str(uuid.uuid4())


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _fetch_org_role(user_id: str, organisation_id: str) -> str | None:
    res = (
        supabase.table("organisation_users")
        .select("role")
        .eq("organisation_id", organisation_id)
        .eq("user_id", user_id)
        .eq("status", "active")
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    return res.data[0].get("role")


def _approval_effective_user(organisation_id: str, workflow_type: str, approver_user_id: str | None) -> str | None:
    if not approver_user_id:
        return None
    try:
        res = supabase.rpc(
            "approval_effective_user",
            {
                "p_org_id": organisation_id,
                "p_workflow_type": workflow_type,
                "p_approver_user_id": approver_user_id,
            },
        ).execute()
        return res.data or approver_user_id
    except Exception:
        return approver_user_id


def _matches_tracking_limit(line_tracking: dict, limit: dict) -> bool:
    dimension_id = limit.get("tracking_dimension_id")
    if not dimension_id:
        return False

    value = None
    if isinstance(line_tracking, dict):
        value = line_tracking.get(dimension_id)
        if isinstance(value, dict):
            value = value.get("id") or value.get("value_id")

    limit_value = limit.get("tracking_value_id")
    if limit_value is None:
        return value is not None
    return str(value or "") == str(limit_value)


def _enforce_user_limits(
    *,
    user_id: str,
    organisation_id: str,
    journal_lines: list[dict],
    invoice_amount: float,
    action: str,
) -> None:
    try:
        account_limits_res = (
            supabase.table("organisation_user_account_limits")
            .select("*")
            .eq("organisation_id", organisation_id)
            .eq("user_id", user_id)
            .eq("active", True)
            .execute()
        )
        account_limits = account_limits_res.data or []
    except Exception:
        account_limits = []  # table not yet migrated — no limits configured

    try:
        tracking_limits_res = (
            supabase.table("organisation_user_tracking_limits")
            .select("*")
            .eq("organisation_id", organisation_id)
            .eq("user_id", user_id)
            .eq("active", True)
            .execute()
        )
        tracking_limits = tracking_limits_res.data or []
    except Exception:
        tracking_limits = []  # table not yet migrated — no limits configured

    for line in journal_lines:
        account_id = line.get("account_id")
        debit_amount = round(float(line.get("debit_amount") or 0), 2)
        amount = round(float(line.get("debit_amount") or line.get("credit_amount") or 0), 2)
        account_specific_limits = [limit for limit in account_limits if limit.get("account_id") is not None]
        if debit_amount > 0 and account_specific_limits and not any(
            str(limit.get("account_id")) == str(account_id) for limit in account_specific_limits
        ):
            raise HTTPException(
                status_code=403,
                detail="You are not allowed to use one of the selected invoice accounts",
            )

        matching_account_limits = [
            limit
            for limit in account_limits
            if limit.get("account_id") is None or str(limit.get("account_id")) == str(account_id)
        ]

        for limit in matching_account_limits:
            if action == "post":
                if not limit.get("can_post", True):
                    raise HTTPException(status_code=403, detail="You are not allowed to post to one of the selected accounts")
                max_amount = limit.get("max_post_amount")
                if max_amount is not None and amount > float(max_amount):
                    raise HTTPException(status_code=403, detail="Posting amount exceeds your account limit")
            if action == "approve":
                if not limit.get("can_approve", True):
                    raise HTTPException(status_code=403, detail="You are not allowed to approve one of the selected accounts")
                max_amount = limit.get("max_approval_amount")
                if max_amount is not None and invoice_amount > float(max_amount):
                    raise HTTPException(status_code=403, detail="Invoice amount exceeds your approval limit")

        tracking = line.get("tracking") or {}
        limited_dimensions = {
            str(limit.get("tracking_dimension_id"))
            for limit in tracking_limits
            if limit.get("tracking_dimension_id")
        }
        for dimension_id in limited_dimensions:
            value = tracking.get(dimension_id) if isinstance(tracking, dict) else None
            if isinstance(value, dict):
                value = value.get("id") or value.get("value_id")
            if value is None:
                continue

            dimension_limits = [
                limit
                for limit in tracking_limits
                if str(limit.get("tracking_dimension_id")) == dimension_id
            ]
            matching_dimension_limits = [
                limit
                for limit in dimension_limits
                if limit.get("tracking_value_id") is None or str(limit.get("tracking_value_id")) == str(value)
            ]
            if not matching_dimension_limits:
                raise HTTPException(
                    status_code=403,
                    detail="You are not allowed to use one of the selected tracking values",
                )
            for limit in matching_dimension_limits:
                if action == "post" and not limit.get("can_post", True):
                    raise HTTPException(status_code=403, detail="You are not allowed to post to one of the selected tracking values")
                if action == "approve" and not limit.get("can_approve", True):
                    raise HTTPException(status_code=403, detail="You are not allowed to approve one of the selected tracking values")


def _handle_invoice_approval_workflow(
    *,
    user_id: str,
    organisation_id: str,
    invoice_id: str,
    invoice_amount: float,
    journal_lines: list[dict],
) -> dict | None:
    try:
        workflow_res = (
            supabase.table("approval_workflows")
            .select("id")
            .eq("organisation_id", organisation_id)
            .eq("workflow_type", "invoice")
            .eq("active", True)
            .limit(1)
            .execute()
        )
    except Exception:
        return None  # approval_workflows table not present — skip workflow
    if not workflow_res.data:
        return None

    req_id = None
    try:
        req_res = supabase.rpc(
            "create_invoice_approval_request",
            {
                "p_org_id": organisation_id,
                "p_invoice_id": invoice_id,
                "p_amount": invoice_amount,
                "p_requested_by": user_id,
            },
        ).execute()
        req_id = req_res.data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Approval workflow setup failed: {exc}") from exc

    if not req_id:
        return None

    try:
        supabase.rpc("refresh_approval_request", {"p_request_id": req_id}).execute()
    except Exception:
        pass

    req_res = (
        supabase.table("approval_requests")
        .select("*")
        .eq("id", req_id)
        .limit(1)
        .execute()
    )
    request = req_res.data[0] if req_res.data else None
    if not request or request.get("status") == "approved":
        return None
    if request.get("status") in {"rejected", "cancelled"}:
        raise HTTPException(status_code=400, detail=f"Approval request is {request.get('status')}")

    steps_res = (
        supabase.table("approval_request_steps")
        .select("*")
        .eq("request_id", req_id)
        .order("step_order", desc=False)
        .execute()
    )
    steps = steps_res.data or []
    active_steps = [s for s in steps if s.get("status") in {"pending", "included"}]
    user_role = _fetch_org_role(user_id, organisation_id)

    approvable_steps = []
    for step in active_steps:
        approver_user = step.get("approver_user_id")
        effective_user = _approval_effective_user(organisation_id, "invoice", approver_user)
        if effective_user == user_id:
            approvable_steps.append(step)
            continue
        approver_role = step.get("approver_role")
        if approver_role and user_role == approver_role:
            approvable_steps.append(step)

    if not approvable_steps:
        return {
            "success": True,
            "status": "pending_approval",
            "approval_request_id": req_id,
            "message": "Invoice submitted for approval.",
        }

    _enforce_user_limits(
        user_id=user_id,
        organisation_id=organisation_id,
        journal_lines=journal_lines,
        invoice_amount=invoice_amount,
        action="approve",
    )

    now = _now_iso()
    current_step = approvable_steps[0]
    supabase.table("approval_request_steps").update({
        "status": "approved",
        "actioned_by": user_id,
        "actioned_at": now,
    }).eq("id", current_step["id"]).execute()

    remaining_active = [
        s
        for s in active_steps
        if s["id"] != current_step["id"] and s.get("status") in {"pending", "included"}
    ]
    if remaining_active:
        return {
            "success": True,
            "status": "pending_approval",
            "approval_request_id": req_id,
            "message": "Your approval was recorded. Other included approvers are still pending.",
        }

    current_is_final = False
    workflow_step_id = current_step.get("workflow_step_id")
    if workflow_step_id:
        final_res = (
            supabase.table("approval_steps")
            .select("is_final_step")
            .eq("id", workflow_step_id)
            .limit(1)
            .execute()
        )
        current_is_final = bool(final_res.data and final_res.data[0].get("is_final_step"))

    waiting = []
    if not current_is_final:
        waiting = [
            s
            for s in steps
            if s.get("status") == "waiting" and s.get("step_order", 0) > current_step.get("step_order", 0)
        ]
    if waiting:
        from datetime import datetime, timedelta, timezone

        next_step = sorted(waiting, key=lambda s: s.get("step_order", 0))[0]
        due_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        supabase.table("approval_request_steps").update({
            "status": "pending",
            "included_at": now,
            "due_at": due_at,
        }).eq("id", next_step["id"]).execute()
        supabase.table("approval_requests").update({
            "current_step_order": next_step.get("step_order"),
        }).eq("id", req_id).execute()
        return {
            "success": True,
            "status": "pending_approval",
            "approval_request_id": req_id,
            "message": "Your approval was recorded. The next approval step is now pending.",
        }

    supabase.table("approval_requests").update({
        "status": "approved",
        "completed_at": now,
    }).eq("id", req_id).execute()
    return None


@router.post("/{invoice_id}/post-to-gl")
def post_invoice_to_gl(invoice_id: str, payload: PostInvoiceToGLRequest, auth: UserAuth):
    """
    Create and post a double-entry GL journal for an approved invoice.

    Journal structure:
      Dr  [Expense account per line item]  — net amount (ex-VAT)
      Dr  [Expense account per line item]  — blocked/non-claimable VAT
      Dr  [VAT Control 8100]               — allowable input VAT
      Cr  [Trade Payables 2100]            — gross total (subtotal + VAT)
    """
    from decimal import Decimal

    user_id, _db = auth
    org_id = payload.organisation_id

    if supabase is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    # 1. Fetch invoice
    inv_res = (
        supabase.table("invoices_extracted")
        .select("*")
        .eq("id", invoice_id)
        .eq("organisation_id", org_id)
        .limit(1)
        .execute()
    )
    if not inv_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice = inv_res.data[0]

    if invoice.get("posting_status") == "posted":
        raise HTTPException(status_code=400, detail="Invoice has already been posted to GL")

    subtotal = float(invoice.get("subtotal") or 0)
    tax_amount = float(invoice.get("tax_amount") or 0)
    gross_total = round(subtotal + tax_amount, 2)

    if gross_total <= 0:
        raise HTTPException(status_code=400, detail="Invoice total is zero — nothing to post")

    # 2. Fetch line items
    li_res = (
        supabase.table("invoice_line_items")
        .select("*")
        .eq("invoice_extracted_id", invoice_id)
        .eq("organisation_id", org_id)
        .execute()
    )
    line_items = li_res.data or []

    supplier_vat_number = invoice.get("vat_number_extracted")
    if invoice.get("supplier_id"):
        supplier_res = (
            supabase.table("suppliers")
            .select("vat_number")
            .eq("id", invoice["supplier_id"])
            .eq("organisation_id", org_id)
            .limit(1)
            .execute()
        )
        if supplier_res.data:
            supplier_vat_number = supplier_res.data[0].get("vat_number") or supplier_vat_number

    # Fetch allocations for all line items in one query
    line_ids = [li["id"] for li in line_items if li.get("id")]
    allocations_by_line: dict[str, list] = {}
    if line_ids:
        alloc_res = (
            supabase.table("invoice_line_item_allocations")
            .select("*")
            .in_("invoice_line_item_id", line_ids)
            .eq("organisation_id", org_id)
            .order("sort_order")
            .execute()
        )
        for a in (alloc_res.data or []):
            lid = a.get("invoice_line_item_id")
            allocations_by_line.setdefault(str(lid), []).append(a)

    # 3. Fetch all accounts for this org (used for system account lookup + code/name resolution)
    all_accts_res = (
        supabase.table("accounts")
        .select("id, code, name, system_key")
        .eq("organisation_id", org_id)
        .execute()
    )
    all_accts = all_accts_res.data or []
    sys_accts = {row["system_key"]: row for row in all_accts if row.get("system_key")}
    trade_payables = sys_accts.get("trade_payables")
    vat_control = sys_accts.get("vat_control")

    if not trade_payables:
        raise HTTPException(
            status_code=400,
            detail="Trade Payables system account not found for this organisation. "
                   "Ensure the system accounts migration has been applied.",
        )

    # Resolve expense_account code/name → UUID (supports legacy line items that stored account codes)
    _acct_by_code = {a["code"]: a["id"] for a in all_accts if a.get("code")}
    _acct_by_name = {a["name"]: a["id"] for a in all_accts if a.get("name")}

    def _resolve_acct(val: str | None) -> str | None:
        if not val:
            return val
        if len(str(val)) == 36 and "-" in str(val):  # already a UUID
            return val
        return _acct_by_code.get(val) or _acct_by_name.get(val) or val

    line_items = [
        {**li, "expense_account": _resolve_acct(li.get("expense_account"))}
        for li in line_items
    ]
    allocations_by_line = {
        lid: [
            {**a, "expense_account": _resolve_acct(a.get("expense_account"))}
            for a in allocs
        ]
        for lid, allocs in allocations_by_line.items()
    }

    try:
        supplier_required_dimensions = required_tracking_dimensions(
            supabase,
            organisation_id=org_id,
            module_key="supplier",
        )
        validate_supplier_allocations_tracking(
            line_items=line_items,
            allocations_by_line=allocations_by_line,
            required_dimensions=supplier_required_dimensions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 4. Build journal lines (debit side)
    posting = build_invoice_debit_lines(
        organisation_id=org_id,
        invoice=invoice,
        line_items=line_items,
        allocations_by_line=allocations_by_line,
        supplier_has_vat_number=bool(str(supplier_vat_number or "").strip()),
        vat_control_account_id=str(vat_control["id"]) if vat_control else None,
    )
    journal_lines = posting["journal_lines"]
    missing_accounts = posting["missing_accounts"]
    claimable_tax = posting["claimable_tax"]
    description_base = posting["description_base"]

    if missing_accounts:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot post — the following lines have no expense account: {', '.join(missing_accounts[:5])}",
        )

    if claimable_tax > 0 and not vat_control:
        raise HTTPException(
            status_code=400,
            detail="VAT Control system account not found for this organisation.",
        )

    # Creditors credit line
    total_debit = round(sum(float(l["debit_amount"]) for l in journal_lines), 2)
    if abs(total_debit - gross_total) > 0.02:
        raise HTTPException(
            status_code=400,
            detail=(
                "VAT allocation did not reconcile to the invoice total "
                f"({total_debit:.2f} posted vs {gross_total:.2f} invoice)."
            ),
        )
    journal_lines.append({
        "organisation_id": org_id,
        "account_id": trade_payables["id"],
        "description": description_base,
        "debit_amount": 0.0,
        "credit_amount": total_debit,
        "tracking": {},
        "sort_order": len(journal_lines),
    })

    workflow_result = _handle_invoice_approval_workflow(
        user_id=user_id,
        organisation_id=org_id,
        invoice_id=invoice_id,
        invoice_amount=gross_total,
        journal_lines=journal_lines,
    )
    if workflow_result:
        return workflow_result

    _enforce_user_limits(
        user_id=user_id,
        organisation_id=org_id,
        journal_lines=journal_lines,
        invoice_amount=gross_total,
        action="post",
    )

    # 5. Create the posted journal
    journal_id = _new_uuid()
    now = _now_iso()
    journal_date = (invoice.get("invoice_date") or now[:10])

    supabase.table("gl_journals").insert({
        "id": journal_id,
        "organisation_id": org_id,
        "source_type": "invoice",
        "source_id": invoice_id,
        "journal_date": journal_date,
        "description": description_base,
        "status": "posted",
        "total_debit": total_debit,
        "total_credit": total_debit,
        "created_by": user_id,
        "posted_by": user_id,
        "posted_at": now,
    }).execute()

    supabase.table("gl_journal_lines").insert([
        {**line, "gl_journal_id": journal_id}
        for line in journal_lines
    ]).execute()

    # 6. Update invoice
    supabase.table("invoices_extracted").update({
        "gl_journal_id": journal_id,
        "posting_status": "posted",
        "posted_at": now,
        "posted_by": user_id,
        "approval_status": "approved",
        "review_status": "approved",
        "approved_at": now,
        "approved_by": user_id,
        "updated_at": utc_now_iso(),
    }).eq("id", invoice_id).execute()

    return {
        "success": True,
        "journal_id": journal_id,
        "total_debit": total_debit,
        "total_credit": total_debit,
        "lines": len(journal_lines),
        "trade_payables_account": trade_payables["code"],
        "vat_control_account": vat_control["code"] if vat_control else None,
    }
