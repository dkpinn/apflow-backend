"""invoices_gl.py
GL posting route for supplier invoices, including the approval-workflow guard
and user-limit enforcement that runs before the journal is created.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth
from app.services.audit_log import log_invoice_event
from app.services.invoice_readiness import evaluate_invoice_readiness

router = APIRouter(prefix="/api/invoices", tags=["invoices"])

try:
    supabase = get_supabase_client()
except Exception:
    supabase = None


# ── Models ────────────────────────────────────────────────────────────────────

class PostInvoiceToGLRequest(BaseModel):
    organisation_id: str
    confirm_extraction_review: bool = False


# ── Private helpers ────────────────────────────────────────────────────────────

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _confirm_extraction_review_for_post(
    db,
    *,
    invoice_id: str,
    organisation_id: str,
    user_id: str,
) -> None:
    result = (
        db.table("invoices_extracted")
        .select(
            "id, invoice_raw_id, supplier_id, document_direction, "
            "organisation_match_status, validation_status, validation_notes"
        )
        .eq("id", invoice_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise ValueError("Invoice not found")
    invoice = result.data[0]
    if not invoice.get("supplier_id"):
        raise ValueError("Link a supplier before confirming this invoice for posting")

    old_value = {
        "document_direction": invoice.get("document_direction"),
        "organisation_match_status": invoice.get("organisation_match_status"),
        "validation_status": invoice.get("validation_status"),
        "validation_notes": invoice.get("validation_notes"),
    }
    new_value = {
        "document_direction": "supplier_invoice_payable",
        "organisation_match_status": "manually_confirmed_supplier_payable",
        "validation_status": "passed",
        "validation_notes": "Confirmed as supplier payable during invoice approval.",
    }
    (
        db.table("invoices_extracted")
        .update(new_value)
        .eq("id", invoice_id)
        .eq("organisation_id", organisation_id)
        .execute()
    )
    log_invoice_event(
        db,
        organisation_id=organisation_id,
        invoice_raw_id=invoice.get("invoice_raw_id"),
        invoice_extracted_id=invoice_id,
        event_type="supplier_payable_manually_confirmed",
        stage="approval",
        field_name="document_direction",
        old_value=old_value,
        new_value=new_value,
        actor_type="user",
        actor_user_id=user_id,
        notes="User explicitly approved the reviewed document as a supplier payable.",
    )


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


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post("/{invoice_id}/gl-preview")
def preview_invoice_gl(invoice_id: str, payload: PostInvoiceToGLRequest, auth: UserAuth):
    """Return the canonical balanced journal without creating or posting it."""
    from app.services.invoice_gl_posting import prepare_invoice_gl_posting

    user_id, _db = auth
    org_id = payload.organisation_id

    if supabase is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    if not _fetch_org_role(user_id, org_id):
        raise HTTPException(status_code=403, detail="You do not have access to this organisation")

    try:
        prepared = prepare_invoice_gl_posting(
            supabase,
            invoice_id=invoice_id,
            org_id=org_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "success": True,
        "journal_date": prepared.get("journal_date"),
        "description": prepared.get("description"),
        "gross_total": prepared["gross_total"],
        "total_debit": prepared["total_debit"],
        "total_credit": prepared["total_credit"],
        "journal_lines": prepared["journal_lines"],
    }


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
    from app.services.invoice_gl_posting import (
        post_invoice_to_gl_service,
        prepare_invoice_gl_posting,
    )

    user_id, db = auth
    org_id = payload.organisation_id

    if supabase is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    if payload.confirm_extraction_review:
        try:
            _confirm_extraction_review_for_post(
                supabase,
                invoice_id=invoice_id,
                organisation_id=org_id,
                user_id=user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    readiness = evaluate_invoice_readiness(
        supabase,
        invoice_extracted_id=invoice_id,
        organisation_id=org_id,
        reason="manual_gl_post_requested",
        actor_type="user",
        actor_user_id=user_id,
    )
    if not readiness.get("ready"):
        messages = [str(item.get("message")) for item in readiness.get("blockers") or [] if item.get("message")]
        detail = "; ".join(messages[:5]) or "Invoice readiness checks have not passed."
        raise HTTPException(status_code=400, detail=f"Cannot post invoice: {detail}")

    try:
        prepared = prepare_invoice_gl_posting(
            supabase,
            invoice_id=invoice_id,
            org_id=org_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    workflow_result = _handle_invoice_approval_workflow(
        user_id=user_id,
        organisation_id=org_id,
        invoice_id=invoice_id,
        invoice_amount=prepared["gross_total"],
        journal_lines=prepared["journal_lines"],
    )
    if workflow_result:
        return workflow_result

    _enforce_user_limits(
        user_id=user_id,
        organisation_id=org_id,
        journal_lines=prepared["journal_lines"],
        invoice_amount=prepared["gross_total"],
        action="post",
    )

    try:
        return post_invoice_to_gl_service(
            db,
            invoice_id=invoice_id,
            org_id=org_id,
            user_id=user_id,
            prepared=prepared,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
