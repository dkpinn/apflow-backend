from __future__ import annotations

from typing import Any, Optional

from app.services.audit_log import log_invoice_event
from app.services.invoice_data_builders import utc_now_iso
from app.services.invoice_review_agent import generate_invoice_agent_suggestions
from app.services.organisation_module_settings import required_tracking_dimensions


READY_REVIEW_STATUS = "in_review"
BLOCKED_REVIEW_STATUS = "needs_info"
MIN_READY_CONFIDENCE = 0.70


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _reason(category: str, severity: str, message: str, reason: str) -> dict:
    return {
        "category": category,
        "severity": severity,
        "message": message,
        "reason": reason,
    }


def _current_extraction_blockers(invoice: dict) -> list[dict]:
    blockers: list[dict] = []

    validation_status = invoice.get("validation_status")
    validation_notes = str(invoice.get("validation_notes") or invoice.get("notes") or "").strip()
    if validation_status == "failed_missing_supplier":
        blockers.append(_reason(
            "extraction_validation",
            "critical",
            "Supplier extraction failed.",
            validation_notes or "The invoice still needs a linked supplier before it can be approved.",
        ))
    elif validation_status and validation_status not in {"passed", "needs_review"}:
        blockers.append(_reason(
            "extraction_validation",
            "warning",
            "Extraction validation still needs review.",
            validation_notes or f"validation_status is {validation_status}.",
        ))

    confidence = invoice.get("confidence_score")
    try:
        confidence_value = float(confidence) if confidence is not None else None
    except Exception:
        confidence_value = None
    if confidence_value is None or confidence_value < MIN_READY_CONFIDENCE:
        blockers.append(_reason(
            "extraction_confidence",
            "warning",
            "Extraction confidence is below the auto-approval threshold.",
            f"confidence_score is {confidence_value if confidence_value is not None else 'missing'}.",
        ))

    notes_text = f"{invoice.get('validation_notes') or ''} {invoice.get('notes') or ''}".lower()
    if "ocr/image quality is low" in notes_text or "ocr quality" in notes_text:
        blockers.append(_reason(
            "ocr_quality",
            "warning",
            "OCR/image quality was flagged.",
            "The current invoice validation notes contain an OCR quality warning.",
        ))

    direction = invoice.get("document_direction")
    if direction != "supplier_invoice_payable":
        severity = "critical" if direction in {"customer_sales_invoice", "wrong_organisation"} else "warning"
        blockers.append(_reason(
            "document_direction",
            severity,
            "Document direction is not confirmed as supplier payable.",
            f"document_direction is {direction or 'missing'}.",
        ))

    try:
        document_count = int(invoice.get("document_count") or 1)
    except Exception:
        document_count = 1
    if document_count > 1:
        blockers.append(_reason(
            "document_split",
            "critical",
            "Multiple documents were detected in one file.",
            "Split the file before approving individual supplier documents.",
        ))

    required_fields = [
        ("supplier_id", "Linked supplier is missing."),
        ("supplier_name_extracted", "Supplier name is missing."),
        ("invoice_number", "Document number is missing."),
        ("invoice_date", "Document date is missing."),
        ("total_amount", "Document total is missing."),
    ]
    for field, message in required_fields:
        if not _has_value(invoice.get(field)):
            blockers.append(_reason(
                "required_fields",
                "warning",
                message,
                f"{field} must be present before APPayPal can move the document to To Approve.",
            ))

    return blockers


def build_invoice_readiness_decision(
    *,
    invoice: dict,
    supplier: Optional[dict] = None,
    supplier_branch: Optional[dict] = None,
    supplier_branches: Optional[list[dict]] = None,
    line_items: Optional[list[dict]] = None,
    tracking_dimensions: Optional[list[dict]] = None,
    tracking_values: Optional[list[dict]] = None,
    audit_events: Optional[list[dict]] = None,
    parse_attempts: Optional[list[dict]] = None,
    duplicate_count: int = 0,
) -> dict:
    source_line_items = line_items or []
    suggestions = generate_invoice_agent_suggestions(
        invoice=invoice,
        supplier=supplier,
        supplier_branch=supplier_branch,
        supplier_branches=supplier_branches or [],
        line_items=source_line_items,
        accounts=[],
        tracking_dimensions=tracking_dimensions or [],
        tracking_values=tracking_values or [],
        audit_events=audit_events or [],
        parse_attempts=parse_attempts or [],
        duplicate_count=duplicate_count,
    )

    reasons = _current_extraction_blockers(invoice)
    for suggestion in suggestions:
        payload = suggestion.as_dict() if hasattr(suggestion, "as_dict") else dict(suggestion)
        if payload.get("severity") in {"critical", "warning"}:
            reasons.append({
                "category": payload.get("category"),
                "severity": payload.get("severity"),
                "message": payload.get("message"),
                "reason": payload.get("reason"),
                "target": payload.get("target"),
            })

    blockers = [item for item in reasons if item.get("severity") in {"critical", "warning"}]
    ready = not blockers
    return {
        "ready": ready,
        "review_status": READY_REVIEW_STATUS if ready else BLOCKED_REVIEW_STATUS,
        "blockers": blockers,
        "warnings": [item for item in reasons if item.get("severity") == "warning"],
        "suggestion_count": len(suggestions),
    }


def _fetch_rows(query) -> list[dict]:
    try:
        return query.execute().data or []
    except Exception:
        return []


def _fetch_invoice_context(supabase, invoice_extracted_id: str, organisation_id: Optional[str]) -> Optional[dict]:
    query = supabase.table("invoices_extracted").select("*").eq("id", invoice_extracted_id)
    if organisation_id:
        query = query.eq("organisation_id", organisation_id)
    rows = _fetch_rows(query.limit(1))
    invoice = rows[0] if rows else None
    if not invoice:
        return None

    org_id = invoice.get("organisation_id") or organisation_id
    supplier = None
    supplier_branches: list[dict] = []
    supplier_branch = None
    supplier_id = invoice.get("supplier_id")
    if supplier_id:
        supplier_rows = _fetch_rows(
            supabase.table("suppliers").select("*").eq("id", supplier_id).limit(1)
        )
        supplier = supplier_rows[0] if supplier_rows else None
        supplier_branches = _fetch_rows(
            supabase.table("supplier_branches")
            .select("*")
            .eq("supplier_id", supplier_id)
            .order("created_at", desc=False)
        )
        branch_id = invoice.get("supplier_branch_id")
        if branch_id:
            supplier_branch = next((row for row in supplier_branches if row.get("id") == branch_id), None)

    line_items = _fetch_rows(
        supabase.table("invoice_line_items")
        .select("*")
        .eq("invoice_extracted_id", invoice_extracted_id)
        .order("created_at", desc=False)
        .order("id", desc=False)
    )
    line_ids = [row.get("id") for row in line_items if row.get("id")]
    if line_ids:
        allocations = _fetch_rows(
            supabase.table("invoice_line_item_allocations")
            .select("*")
            .in_("invoice_line_item_id", line_ids)
            .order("sort_order", desc=False)
        )
        allocations_by_line: dict[str, list[dict]] = {}
        for allocation in allocations:
            line_id = allocation.get("invoice_line_item_id")
            if line_id:
                allocations_by_line.setdefault(line_id, []).append(allocation)
        for item in line_items:
            item["allocations"] = allocations_by_line.get(item.get("id"), [])

    tracking_dimensions = []
    tracking_values = []
    if org_id:
        try:
            tracking_dimensions = required_tracking_dimensions(
                supabase,
                organisation_id=str(org_id),
                module_key="supplier",
            )
        except Exception:
            tracking_dimensions = []
        dimension_ids = [row.get("id") for row in tracking_dimensions if row.get("id")]
        if dimension_ids:
            tracking_values = _fetch_rows(
                supabase.table("tracking_values")
                .select("id, dimension_id, code, name, active, sort_order")
                .in_("dimension_id", dimension_ids)
                .eq("active", True)
                .order("sort_order", desc=False)
                .order("name", desc=False)
            )

    duplicate_count = 0
    if org_id and invoice.get("invoice_number") and supplier_id:
        duplicate_rows = _fetch_rows(
            supabase.table("invoices_extracted")
            .select("id")
            .eq("organisation_id", org_id)
            .eq("supplier_id", supplier_id)
            .eq("invoice_number", invoice.get("invoice_number"))
            .neq("id", invoice_extracted_id)
            .limit(10)
        )
        duplicate_count = len(duplicate_rows)

    return {
        "invoice": invoice,
        "supplier": supplier,
        "supplier_branch": supplier_branch,
        "supplier_branches": supplier_branches,
        "line_items": line_items,
        "tracking_dimensions": tracking_dimensions,
        "tracking_values": tracking_values,
        "duplicate_count": duplicate_count,
    }


def evaluate_invoice_readiness(
    supabase,
    *,
    invoice_extracted_id: str,
    organisation_id: Optional[str] = None,
    reason: str = "invoice_readiness_evaluated",
    actor_type: str = "system",
    actor_user_id: Optional[str] = None,
    job_id: Optional[str] = None,
) -> dict:
    context = _fetch_invoice_context(supabase, invoice_extracted_id, organisation_id)
    if not context:
        return {
            "ready": False,
            "review_status": BLOCKED_REVIEW_STATUS,
            "blockers": [_reason(
                "invoice_lookup",
                "critical",
                "Extracted invoice could not be loaded.",
                "Readiness cannot be evaluated without the extracted invoice row.",
            )],
            "warnings": [],
            "updated": False,
        }

    invoice = context["invoice"]
    org_id = invoice.get("organisation_id") or organisation_id
    current_review_status = invoice.get("review_status")
    current_approval_status = invoice.get("approval_status")
    decision = build_invoice_readiness_decision(
        invoice=invoice,
        supplier=context.get("supplier"),
        supplier_branch=context.get("supplier_branch"),
        supplier_branches=context.get("supplier_branches") or [],
        line_items=context.get("line_items") or [],
        tracking_dimensions=context.get("tracking_dimensions") or [],
        tracking_values=context.get("tracking_values") or [],
        duplicate_count=int(context.get("duplicate_count") or 0),
    )

    updated = False
    if current_review_status != "approved" and current_approval_status != "approved":
        supabase.table("invoices_extracted").update({
            "review_status": decision["review_status"],
            "updated_at": utc_now_iso(),
        }).eq("id", invoice_extracted_id).execute()
        updated = True

    log_invoice_event(
        supabase,
        organisation_id=org_id,
        invoice_raw_id=invoice.get("invoice_raw_id"),
        invoice_extracted_id=invoice_extracted_id,
        job_id=job_id,
        event_type="invoice_readiness_evaluated",
        stage="review_readiness",
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        old_value={"review_status": current_review_status},
        new_value={
            "ready": decision["ready"],
            "review_status": decision["review_status"],
            "blockers": decision["blockers"],
            "warnings": decision["warnings"],
            "reason": reason,
        },
        notes=(
            "Invoice is ready for approval."
            if decision["ready"]
            else f"Invoice needs review: {len(decision['blockers'])} blocker(s)."
        ),
    )

    return {**decision, "updated": updated}
