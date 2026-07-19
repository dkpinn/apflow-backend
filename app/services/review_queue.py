from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

MONEY = Decimal("0.01")
ZERO = Decimal("0.00")

PENDING_STATUSES = {"pending", "needs_info", "in_review"}
ALL_STATUSES = {"pending", "needs_info", "in_review", "approved", "ignored"}
STATEMENT_DOCUMENT_TYPES = {"statement", "supplier_statement", "account_statement"}


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except Exception:
        return ZERO


def is_statement_document_type(value: Any) -> bool:
    normalised = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalised in STATEMENT_DOCUMENT_TYPES


def list_review_items(
    db,
    organisation_id: str,
    *,
    filter_status: str = "pending",
    limit: int = 200,
) -> list[dict]:
    """
    Return invoices_extracted needing accountant review.

    filter_status:
      "pending"  → review_status in (pending, needs_info, in_review) AND not posted
      "approved" → review_status = approved
      "ignored"  → review_status = ignored
      "all"      → all statuses, not posted
    """
    query = (
        db.table("invoices_extracted")
        .select(
            "id, invoice_raw_id, organisation_id, invoice_number, invoice_date, due_date,"
            " total_amount, currency, supplier_name_extracted, supplier_id, document_type,"
            " review_status, approval_status, posting_status, created_at"
        )
        .eq("organisation_id", organisation_id)
        .order("invoice_date", desc=True)
    )

    if filter_status == "pending":
        query = query.in_("review_status", list(PENDING_STATUSES)).neq("posting_status", "posted")
    elif filter_status == "approved":
        query = query.eq("review_status", "approved")
    elif filter_status == "ignored":
        query = query.eq("review_status", "ignored")
    else:
        query = query.neq("posting_status", "posted")

    rows = [
        row
        for row in (query.execute().data or [])
        if not is_statement_document_type(row.get("document_type"))
    ][:limit]

    out = []
    for row in rows:
        out.append({
            "id": row.get("id"),
            "invoice_raw_id": row.get("invoice_raw_id"),
            "invoice_number": row.get("invoice_number"),
            "invoice_date": str(row.get("invoice_date") or "")[:10] or None,
            "due_date": str(row.get("due_date") or "")[:10] or None,
            "total_amount": float(_d(row.get("total_amount"))),
            "currency": row.get("currency") or "ZAR",
            "supplier_name": row.get("supplier_name_extracted"),
            "supplier_id": row.get("supplier_id"),
            "document_type": row.get("document_type") or "invoice",
            "review_status": row.get("review_status") or "pending",
            "approval_status": row.get("approval_status"),
            "posting_status": row.get("posting_status") or "unposted",
            "created_at": str(row.get("created_at") or ""),
        })
    return out


def get_review_counts(db, organisation_id: str) -> dict:
    """Return counts per review_status bucket for the badge display."""
    counts = {"pending": 0, "needs_info": 0, "approved": 0, "ignored": 0}
    try:
        rows = (
            db.table("invoices_extracted")
            .select("review_status, document_type")
            .eq("organisation_id", organisation_id)
            .neq("posting_status", "posted")
            .execute()
            .data or []
        )
        for row in rows:
            if is_statement_document_type(row.get("document_type")):
                continue
            rs = (row.get("review_status") or "pending").lower()
            if rs in ("pending", "in_review"):
                counts["pending"] += 1
            elif rs == "needs_info":
                counts["needs_info"] += 1
            elif rs == "approved":
                counts["approved"] += 1
            elif rs == "ignored":
                counts["ignored"] += 1
    except Exception:
        pass
    return counts


def set_review_status(
    db,
    *,
    invoice_id: str,
    organisation_id: str,
    new_status: str,
    reviewed_by: str,
    note: str | None = None,
) -> dict:
    if new_status not in ALL_STATUSES:
        raise ValueError(f"Invalid review_status: {new_status}")

    payload: dict = {"review_status": new_status}

    result = (
        db.table("invoices_extracted")
        .update(payload)
        .eq("id", invoice_id)
        .eq("organisation_id", organisation_id)
        .execute()
    )
    updated = (result.data or [{}])[0]
    return {
        "id": invoice_id,
        "review_status": new_status,
        "updated": bool(updated),
    }
