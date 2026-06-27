from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional
from uuid import uuid4

from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)

VALID_TYPES = {
    "supplier_invoice",
    "sales_invoice",
    "journal_entry",
    "bank_standing_order",
    "finance_repayment",
    "operational_repayment",
}
VALID_SCHEDULES = {"weekly", "monthly", "quarterly", "annually"}


def _compute_next_due(start: date, schedule_type: str, schedule_day: Optional[int]) -> date:
    today = date.today()
    if schedule_type == "weekly":
        candidate = start
        while candidate <= today:
            candidate += timedelta(weeks=1)
        return candidate
    if schedule_type in ("monthly", "quarterly", "annually"):
        day = schedule_day or start.day
        if schedule_type == "monthly":
            delta = relativedelta(months=1)
        elif schedule_type == "quarterly":
            delta = relativedelta(months=3)
        else:
            delta = relativedelta(years=1)
        candidate = start
        while candidate <= today:
            try:
                candidate = (candidate + delta).replace(day=day)
            except ValueError:
                candidate = candidate + delta
        return candidate
    return start


def _advance_due_date(current: date, schedule_type: str, schedule_day: Optional[int]) -> date:
    day = schedule_day or current.day
    if schedule_type == "weekly":
        return current + timedelta(weeks=1)
    if schedule_type == "monthly":
        try:
            return (current + relativedelta(months=1)).replace(day=day)
        except ValueError:
            return current + relativedelta(months=1)
    if schedule_type == "quarterly":
        try:
            return (current + relativedelta(months=3)).replace(day=day)
        except ValueError:
            return current + relativedelta(months=3)
    if schedule_type == "annually":
        try:
            return (current + relativedelta(years=1)).replace(day=day)
        except ValueError:
            return current + relativedelta(years=1)
    return current


# ─── Templates ────────────────────────────────────────────────────────────────

def list_templates(db, organisation_id: str) -> list[dict]:
    return (
        db.table("recurring_transaction_templates")
        .select("*")
        .eq("organisation_id", organisation_id)
        .neq("status", "completed")
        .order("next_due_date")
        .execute()
        .data or []
    )


def get_template(db, organisation_id: str, template_id: str) -> dict:
    row = (
        db.table("recurring_transaction_templates")
        .select("*")
        .eq("id", template_id)
        .eq("organisation_id", organisation_id)
        .maybe_single()
        .execute()
        .data
    )
    if not row:
        raise ValueError("Recurring template not found")
    return row


def create_template(db, organisation_id: str, user_id: str, payload: dict) -> dict:
    transaction_type = payload.get("transaction_type", "")
    if transaction_type not in VALID_TYPES:
        raise ValueError(f"Invalid transaction_type: {transaction_type}")
    schedule_type = payload.get("schedule_type", "")
    if schedule_type not in VALID_SCHEDULES:
        raise ValueError(f"Invalid schedule_type: {schedule_type}")

    start_date = date.fromisoformat(str(payload["start_date"]))
    schedule_day = payload.get("schedule_day") or start_date.day
    next_due = _compute_next_due(start_date, schedule_type, schedule_day)

    row = {
        "id": str(uuid4()),
        "organisation_id": organisation_id,
        "name": str(payload["name"]).strip(),
        "transaction_type": transaction_type,
        "schedule_type": schedule_type,
        "schedule_day": schedule_day,
        "amount": float(payload["amount"]),
        "currency": payload.get("currency", "ZAR"),
        "supplier_id": payload.get("supplier_id") or None,
        "customer_id": payload.get("customer_id") or None,
        "description": payload.get("description") or None,
        "reference": payload.get("reference") or None,
        "template_data": payload.get("template_data") or {},
        "status": "active",
        "start_date": start_date.isoformat(),
        "end_date": payload["end_date"] if payload.get("end_date") else None,
        "next_due_date": next_due.isoformat(),
        "created_by": user_id,
    }
    db.table("recurring_transaction_templates").insert(row).execute()
    return row


def update_template(db, organisation_id: str, template_id: str, payload: dict) -> dict:
    get_template(db, organisation_id, template_id)  # raises if not found
    allowed = {
        "name", "amount", "currency", "supplier_id", "customer_id",
        "description", "reference", "template_data", "status",
        "end_date", "schedule_type", "schedule_day",
    }
    update = {k: v for k, v in payload.items() if k in allowed}
    if not update:
        raise ValueError("No updatable fields provided")
    db.table("recurring_transaction_templates").update(update).eq("id", template_id).execute()
    return get_template(db, organisation_id, template_id)


def delete_template(db, organisation_id: str, template_id: str) -> None:
    get_template(db, organisation_id, template_id)  # raises if not found
    db.table("recurring_transaction_templates").update(
        {"status": "completed"}
    ).eq("id", template_id).execute()


# ─── Drafts ───────────────────────────────────────────────────────────────────

def list_drafts(db, organisation_id: str, status: str = "pending") -> list[dict]:
    q = (
        db.table("recurring_transaction_drafts")
        .select("*, recurring_transaction_templates(name, schedule_type)")
        .eq("organisation_id", organisation_id)
        .order("due_date")
    )
    if status != "all":
        q = q.eq("status", status)
    return q.execute().data or []


def approve_draft(db, organisation_id: str, draft_id: str, user_id: str) -> dict:
    draft = (
        db.table("recurring_transaction_drafts")
        .select("*")
        .eq("id", draft_id)
        .eq("organisation_id", organisation_id)
        .maybe_single()
        .execute()
        .data
    )
    if not draft:
        raise ValueError("Draft not found")
    if draft.get("status") != "pending":
        raise ValueError(f"Draft is already {draft.get('status')}")

    posted_record_id: Optional[str] = None

    # For supplier_invoice: create a minimal invoices_extracted record
    if draft["transaction_type"] == "supplier_invoice":
        record_id = str(uuid4())
        try:
            db.table("invoices_extracted").insert({
                "id": record_id,
                "organisation_id": organisation_id,
                "supplier_id": draft["draft_data"].get("supplier_id"),
                "supplier_name": draft["draft_data"].get("supplier_name") or draft.get("description"),
                "total_amount": float(draft["amount"]),
                "currency": draft.get("currency", "ZAR"),
                "invoice_date": draft["due_date"],
                "description": draft.get("description"),
                "reference": draft.get("reference"),
                "document_type": "supplier_invoice",
                "parse_status": "completed",
                "source": "recurring",
            }).execute()
            posted_record_id = record_id
        except Exception:
            logger.exception("Failed to post recurring supplier_invoice draft %s", draft_id)

    # For sales_invoice: create a minimal sales_invoices record
    elif draft["transaction_type"] == "sales_invoice":
        record_id = str(uuid4())
        try:
            db.table("sales_invoices").insert({
                "id": record_id,
                "organisation_id": organisation_id,
                "customer_id": draft["draft_data"].get("customer_id"),
                "total_amount": float(draft["amount"]),
                "currency": draft.get("currency", "ZAR"),
                "invoice_date": draft["due_date"],
                "description": draft.get("description"),
                "reference": draft.get("reference"),
                "status": "draft",
                "source": "recurring",
            }).execute()
            posted_record_id = record_id
        except Exception:
            logger.exception("Failed to post recurring sales_invoice draft %s", draft_id)

    now_iso = _now_iso()
    db.table("recurring_transaction_drafts").update({
        "status": "approved",
        "posted_record_id": posted_record_id,
        "reviewed_by": user_id,
        "reviewed_at": now_iso,
    }).eq("id", draft_id).execute()

    return {
        "id": draft_id,
        "status": "approved",
        "posted_record_id": posted_record_id,
    }


def skip_draft(db, organisation_id: str, draft_id: str, user_id: str) -> dict:
    draft = (
        db.table("recurring_transaction_drafts")
        .select("id, status")
        .eq("id", draft_id)
        .eq("organisation_id", organisation_id)
        .maybe_single()
        .execute()
        .data
    )
    if not draft:
        raise ValueError("Draft not found")
    if draft.get("status") != "pending":
        raise ValueError(f"Draft is already {draft.get('status')}")

    db.table("recurring_transaction_drafts").update({
        "status": "skipped",
        "reviewed_by": user_id,
        "reviewed_at": _now_iso(),
    }).eq("id", draft_id).execute()

    return {"id": draft_id, "status": "skipped"}


# ─── Background generation ────────────────────────────────────────────────────

def generate_due_drafts(db) -> int:
    today = date.today().isoformat()
    templates = (
        db.table("recurring_transaction_templates")
        .select("*")
        .eq("status", "active")
        .lte("next_due_date", today)
        .execute()
        .data or []
    )
    generated = 0
    for tpl in templates:
        tpl_id = tpl["id"]
        due_date = tpl["next_due_date"]
        org_id = tpl["organisation_id"]

        # Check for end_date expiry
        if tpl.get("end_date") and tpl["end_date"] < today:
            try:
                db.table("recurring_transaction_templates").update(
                    {"status": "completed"}
                ).eq("id", tpl_id).execute()
            except Exception:
                logger.exception("Failed to mark template %s completed", tpl_id)
            continue

        # Build draft_data from template
        draft_data: dict[str, Any] = dict(tpl.get("template_data") or {})
        if tpl.get("supplier_id"):
            draft_data["supplier_id"] = tpl["supplier_id"]
        if tpl.get("customer_id"):
            draft_data["customer_id"] = tpl["customer_id"]

        try:
            db.table("recurring_transaction_drafts").insert({
                "id": str(uuid4()),
                "organisation_id": org_id,
                "template_id": tpl_id,
                "due_date": due_date,
                "transaction_type": tpl["transaction_type"],
                "amount": float(tpl["amount"]),
                "currency": tpl.get("currency", "ZAR"),
                "description": tpl.get("description"),
                "reference": tpl.get("reference"),
                "draft_data": draft_data,
                "status": "pending",
            }).execute()
            generated += 1
        except Exception:
            # Likely unique constraint violation (already generated) — safe to skip
            logger.debug("Draft already exists for template %s due %s", tpl_id, due_date)
            continue

        # Advance next_due_date
        try:
            next_due = _advance_due_date(
                date.fromisoformat(str(due_date)),
                tpl["schedule_type"],
                tpl.get("schedule_day"),
            )
            db.table("recurring_transaction_templates").update({
                "next_due_date": next_due.isoformat(),
                "last_generated_at": _now_iso(),
            }).eq("id", tpl_id).execute()
        except Exception:
            logger.exception("Failed to advance next_due_date for template %s", tpl_id)

    if generated:
        logger.info("Generated %d recurring transaction draft(s)", generated)
    return generated


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
