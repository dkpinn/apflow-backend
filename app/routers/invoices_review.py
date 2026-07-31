"""invoices_review.py
Invoice review-data and agent-review routes.

Routes: /{id}/review-data, /{id}/agent-review (GET+POST),
        /agent-suggestions/{id}/apply|dismiss|checked,
        /{id}/agent-review/checked,
        /{id}/supplier-comparison-ignores (GET+POST+DELETE)

Extracted from invoices.py to keep router file sizes manageable.
invoices.py does NOT import this module — no circular import risk.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_org_read
from app.services.audit_log import log_invoice_event
from app.services.invoice_data_builders import (
    build_extracted_document_profile,
    build_extracted_supplier_profile,
    build_supplier_create_payload,
)
from app.services.invoice_parse_attempts import fetch_parse_attempts
from app.services.invoice_readiness import evaluate_invoice_readiness
from app.services.invoice_review_agent import (
    agent_status_after_regeneration,
    filter_safe_apply_payload,
    generate_invoice_agent_suggestions,
)
from app.services.organisation_module_settings import required_tracking_dimensions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/invoices", tags=["invoices"])

try:
    supabase = get_supabase_client()
except Exception:
    supabase = None

AGENT_WRITE_ROLES = {"owner", "admin", "accountant"}

IGNORABLE_SUPPLIER_COMPARISON_FIELDS = {
    "supplier_name_extracted",
    "supplier_telephone_extracted",
    "supplier_email_extracted",
    "supplier_website_extracted",
    "supplier_del_address_extracted",
    "company_registration_number_extracted",
}

EDITABLE_INVOICE_DOCUMENT_FIELDS = {
    "invoice_number",
    "document_reference",
    "supplier_name_extracted",
    "invoice_date",
    "due_date",
    "subtotal",
    "tax_amount",
    "total_amount",
    "currency",
    "supplier_email_extracted",
    "supplier_acc_email_extracted",
    "supplier_telephone_extracted",
    "supplier_fax_extracted",
    "supplier_cell_extracted",
    "supplier_website_extracted",
    "supplier_del_address_extracted",
    "supplier_pos_address_extracted",
    "vat_number_extracted",
    "cus_code_extracted",
    "company_registration_number_extracted",
    "bank_account_name_extracted",
    "bank_name_extracted",
    "bank_account_number_extracted",
    "bank_branch_code_extracted",
    "bank_swift_code_extracted",
    "issuer_name_extracted",
    "recipient_name_extracted",
    "expense_account",
}
NUMERIC_INVOICE_DOCUMENT_FIELDS = {"subtotal", "tax_amount", "total_amount"}


class AgentSuggestionActionRequest(BaseModel):
    note: Optional[str] = None


class SupplierComparisonIgnoreRequest(BaseModel):
    field_key: str
    reason: Optional[str] = None


class InvoiceDocumentFieldsUpdateRequest(BaseModel):
    organisation_id: str
    fields: dict[str, object]
    correction_type: str = "manual"


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/{invoice_id}/review-data")
def get_invoice_review_data(invoice_id: str, auth: UserAuth):
    review_data = _build_invoice_review_data(invoice_id)
    user_id, _db = auth
    ensure_org_read(user_id, review_data.get("organisation_id"))
    return review_data


@router.patch("/{invoice_id}/document-fields")
def update_invoice_document_fields(
    invoice_id: str,
    payload: InvoiceDocumentFieldsUpdateRequest,
    auth: UserAuth,
):
    """Persist reviewer corrections through the authorised service-role client."""
    user_id, _db = auth
    review_data = _build_invoice_review_data(invoice_id)
    organisation_id = str(review_data.get("organisation_id") or "")
    invoice_extracted_id = str(review_data.get("invoice_extracted_id") or "")
    invoice_raw_id = review_data.get("invoice_raw_id")
    before = dict(review_data.get("invoice") or {})

    if not organisation_id or not invoice_extracted_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if str(payload.organisation_id) != organisation_id:
        raise HTTPException(status_code=400, detail="Invoice does not belong to organisation_id")
    _ensure_agent_write_access(user_id, organisation_id)

    unsupported = sorted(set(payload.fields) - EDITABLE_INVOICE_DOCUMENT_FIELDS)
    if unsupported:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported invoice document fields: {', '.join(unsupported)}",
        )
    if not payload.fields:
        raise HTTPException(status_code=422, detail="At least one invoice document field is required")
    if payload.correction_type not in {"manual", "supplier_master"}:
        raise HTTPException(status_code=422, detail="Unsupported correction_type")

    fields: dict[str, object] = {}
    for field_name, raw_value in payload.fields.items():
        if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            fields[field_name] = None
        elif field_name in NUMERIC_INVOICE_DOCUMENT_FIELDS:
            try:
                fields[field_name] = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field_name} must be a number",
                ) from exc
        elif isinstance(raw_value, (str, int, float)):
            fields[field_name] = str(raw_value).strip()
        else:
            raise HTTPException(status_code=422, detail=f"{field_name} must be a scalar value")

    supabase.table("invoices_extracted").update(fields).eq(
        "id", invoice_extracted_id
    ).eq("organisation_id", organisation_id).execute()

    saved_res = (
        supabase.table("invoices_extracted")
        .select("*")
        .eq("id", invoice_extracted_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute()
    )
    saved = saved_res.data[0] if saved_res.data else None
    if not saved or any(saved.get(key) != value for key, value in fields.items()):
        raise HTTPException(status_code=500, detail="Invoice field update was not persisted")

    feedback_rows = []
    for field_name, corrected_value in fields.items():
        extracted_value = before.get(field_name)
        if extracted_value == corrected_value:
            continue
        feedback_rows.append({
            "organisation_id": organisation_id,
            "invoice_raw_id": invoice_raw_id,
            "invoice_extracted_id": invoice_extracted_id,
            "supplier_id": before.get("supplier_id"),
            "field_name": field_name,
            "extracted_value": None if extracted_value is None else str(extracted_value),
            "corrected_value": None if corrected_value is None else str(corrected_value),
            "source_text": None,
            "layout_type": before.get("layout_type"),
            "correction_type": payload.correction_type,
            "created_by": user_id,
        })
    feedback_recorded = True
    if feedback_rows:
        try:
            supabase.table("invoice_extraction_feedback").insert(feedback_rows).execute()
        except Exception:
            feedback_recorded = False
            logger.exception("Invoice fields saved but correction feedback insert failed")

    log_invoice_event(
        supabase,
        organisation_id=organisation_id,
        invoice_raw_id=invoice_raw_id,
        invoice_extracted_id=invoice_extracted_id,
        event_type="invoice_document_fields_updated",
        stage="review",
        actor_type="user",
        actor_user_id=user_id,
        old_value={key: before.get(key) for key in fields},
        new_value=fields,
        notes=(
            "Invoice document field updated from supplier master."
            if payload.correction_type == "supplier_master"
            else "Invoice document fields updated by reviewer."
        ),
    )
    return {
        "success": True,
        "invoice": saved,
        "updated_fields": fields,
        "feedback_recorded": feedback_recorded,
    }


def _build_invoice_review_data(invoice_id: str):
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
                .order("sort_order", desc=False)
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


# ── Private helpers ────────────────────────────────────────────────────────────

def _fetch_agent_context(invoice_id: str) -> dict:
    review_data = _build_invoice_review_data(invoice_id)
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


# ── Agent-review routes ────────────────────────────────────────────────────────

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


# ── Supplier-comparison-ignores routes ────────────────────────────────────────

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
