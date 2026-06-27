from __future__ import annotations

from datetime import date


def _today() -> str:
    return date.today().isoformat()


def _upsert_notification(
    db,
    *,
    organisation_id: str,
    type: str,
    title: str,
    body: str | None,
    link: str | None,
    source_key: str,
) -> None:
    """Insert or update a notification by source_key (dedup handle)."""
    existing = (
        db.table("notifications")
        .select("id, read")
        .eq("organisation_id", organisation_id)
        .eq("source_key", source_key)
        .limit(1)
        .execute()
        .data or []
    )
    if existing:
        # Update title/body in case the count changed, but keep read state
        db.table("notifications").update({
            "title": title,
            "body": body,
        }).eq("id", existing[0]["id"]).execute()
    else:
        db.table("notifications").insert({
            "organisation_id": organisation_id,
            "type": type,
            "title": title,
            "body": body,
            "link": link,
            "source_key": source_key,
            "read": False,
        }).execute()


def refresh_notifications(db, organisation_id: str) -> None:
    """
    Generate today's digest notifications for the org based on current state.
    Called as a side-effect of listing notifications — idempotent per day.
    """
    today = _today()

    # ── Pending review queue ──────────────────────────────────────────────────
    try:
        pending_review = (
            db.table("invoices_extracted")
            .select("id", count="exact")
            .eq("organisation_id", organisation_id)
            .in_("review_status", ["pending", "needs_info", "in_review"])
            .neq("posting_status", "posted")
            .execute()
        )
        count = pending_review.count or 0
        if count > 0:
            _upsert_notification(
                db,
                organisation_id=organisation_id,
                type="review_queue",
                title=f"{count} invoice{'s' if count != 1 else ''} awaiting review",
                body="Open the review queue to approve or flag invoices.",
                link="/approvals/review-queue",
                source_key=f"review_queue_pending_{today}",
            )
    except Exception:
        pass

    # ── Pending recurring drafts ──────────────────────────────────────────────
    try:
        pending_drafts = (
            db.table("recurring_transaction_drafts")
            .select("id", count="exact")
            .eq("organisation_id", organisation_id)
            .eq("status", "pending")
            .execute()
        )
        count = pending_drafts.count or 0
        if count > 0:
            _upsert_notification(
                db,
                organisation_id=organisation_id,
                type="recurring_draft",
                title=f"{count} recurring draft{'s' if count != 1 else ''} need approval",
                body="Review and approve or skip the generated recurring transactions.",
                link="/tools/recurring/drafts",
                source_key=f"recurring_drafts_pending_{today}",
            )
    except Exception:
        pass

    # ── Invoices due in the next 7 days ───────────────────────────────────────
    try:
        from datetime import timedelta
        due_soon_end = (date.today() + timedelta(days=7)).isoformat()
        due_soon = (
            db.table("invoices_extracted")
            .select("id", count="exact")
            .eq("organisation_id", organisation_id)
            .eq("posting_status", "posted")
            .gte("due_date", today)
            .lte("due_date", due_soon_end)
            .execute()
        )
        count = due_soon.count or 0
        if count > 0:
            _upsert_notification(
                db,
                organisation_id=organisation_id,
                type="payment_due",
                title=f"{count} invoice{'s' if count != 1 else ''} due in the next 7 days",
                body="Check aged payables to see what's coming up.",
                link="/reports/aged-payables",
                source_key=f"payment_due_7days_{today}",
            )
    except Exception:
        pass


def list_notifications(db, organisation_id: str, limit: int = 30) -> dict:
    """Refresh and return notifications for the org."""
    refresh_notifications(db, organisation_id)

    rows = (
        db.table("notifications")
        .select("id, type, title, body, link, read, read_at, created_at")
        .eq("organisation_id", organisation_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data or []
    )

    unread_count = sum(1 for r in rows if not r.get("read"))

    return {
        "notifications": rows,
        "unread_count": unread_count,
        "total": len(rows),
    }


def mark_read(db, organisation_id: str, notification_id: str) -> None:
    from datetime import datetime, timezone
    db.table("notifications").update({
        "read": True,
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", notification_id).eq("organisation_id", organisation_id).execute()


def mark_all_read(db, organisation_id: str) -> None:
    from datetime import datetime, timezone
    db.table("notifications").update({
        "read": True,
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("organisation_id", organisation_id).eq("read", False).execute()
