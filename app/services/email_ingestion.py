"""
email_ingestion.py
------------------
Handles inbound email webhook payloads from Mailgun Inbound Routes.

Responsibilities:
  - Verify Mailgun webhook HMAC-SHA256 signature
  - Resolve organisation from the TO (recipient) address via organisations.inbound_email
  - Resolve member (uploaded_by) from the FROM (sender) address via:
      1. Primary: auth.users email match in organisation_users
      2. Fallback: organisation_users.external_sender_emails array
  - Upload each valid attachment (PDF / image) to Supabase Storage
  - Insert an invoices_raw row with source_type='email' and queue extraction
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from app.services.invoice_extraction_service import queue_invoice_job

if TYPE_CHECKING:
    pass  # supabase client typed via duck-typing

# MIME types accepted as invoice documents
ALLOWED_MIME_PREFIXES = ("image/", "application/pdf")


# ── Signature verification ────────────────────────────────────────────────────

def verify_mailgun_signature(signing_key: str, token: str, timestamp: str, signature: str) -> bool:
    """
    Return True when the Mailgun HMAC-SHA256 signature is valid.

    Mailgun signs each webhook with:
        HMAC-SHA256(signing_key, timestamp + token)

    See: https://documentation.mailgun.com/en/latest/user_manual.html#securing-webhooks
    """
    value = f"{timestamp}{token}"
    expected = hmac.new(
        signing_key.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Entity resolution ─────────────────────────────────────────────────────────

def _extract_plain_email(address: str) -> str:
    """Strip display-name if present: 'Name <email>' → 'email'."""
    match = re.search(r"<([^>]+)>", address)
    return (match.group(1) if match else address).strip().lower()


def resolve_org_id(supabase, recipient_email: str) -> Optional[str]:
    """
    Look up the organisation whose inbound_email matches the recipient address.
    Returns the org UUID or None if not found.
    """
    clean = _extract_plain_email(recipient_email)
    res = (
        supabase.table("organisations")
        .select("id")
        .eq("inbound_email", clean)
        .limit(1)
        .execute()
    )
    return res.data[0]["id"] if res.data else None


def resolve_member_user_id(supabase, org_id: str, sender_email: str) -> Optional[str]:
    """
    Attempt to match the sender's email to an active organisation member.

    Strategy:
      1. Compare clean sender address against auth.users email for active members.
      2. Compare against organisation_users.external_sender_emails array.

    Returns user_id (UUID string) or None if no match.
    """
    clean = _extract_plain_email(sender_email)

    # -- 1. Primary match: the member's Supabase auth email ----------------
    # We join organisation_users → auth.users via the user_id FK.
    # The Supabase Python client can only join tables that have defined
    # foreign-key relationships exposed via PostgREST.
    # Fetch all active members in the org and filter client-side to avoid
    # exposing auth.users unnecessarily.
    res = (
        supabase.table("organisation_users")
        .select("user_id")
        .eq("organisation_id", org_id)
        .eq("status", "active")
        .execute()
    )
    user_ids = [row["user_id"] for row in (res.data or [])]

    if user_ids:
        # Look up emails in auth.users for these user_ids (service role only)
        users_res = (
            supabase.table("users")  # auth.users exposed via service role
            .select("id, email")
            .in_("id", user_ids)
            .execute()
        )
        for u in users_res.data or []:
            if (u.get("email") or "").lower() == clean:
                return u["id"]

    # -- 2. Fallback: external_sender_emails array -------------------------
    res2 = (
        supabase.table("organisation_users")
        .select("user_id")
        .eq("organisation_id", org_id)
        .eq("status", "active")
        .contains("external_sender_emails", [clean])
        .limit(1)
        .execute()
    )
    return res2.data[0]["user_id"] if res2.data else None


# ── Storage + DB ──────────────────────────────────────────────────────────────

def _sanitize_filename(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "attachment"


async def ingest_email_attachment(
    supabase,
    *,
    org_id: str,
    uploaded_by: Optional[str],
    filename: str,
    content_type: str,
    file_bytes: bytes,
) -> Optional[str]:
    """
    Upload one email attachment to Supabase Storage and create an invoices_raw row.

    Returns the new invoice_raw_id (UUID string), or None if the file type is not
    an accepted invoice document format.
    """
    if not any(content_type.startswith(p) for p in ALLOWED_MIME_PREFIXES):
        return None  # Ignore non-document attachments (e.g. inline images, vcards)

    safe = _sanitize_filename(filename)
    storage_path = f"{org_id}/invoices/{int(time.time() * 1000)}-{safe}"

    # Upload to the shared 'invoices' bucket using service-role credentials
    supabase.storage.from_("invoices").upload(
        storage_path,
        file_bytes,
        {"content-type": content_type},
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    row = {
        "organisation_id": org_id,
        "file_path": storage_path,
        "file_name": filename,
        "file_type": content_type,
        "source_type": "email",
        "upload_status": "uploaded",
        "parse_status": "pending",
        "uploaded_by": uploaded_by,
        "uploaded_at": now_iso,
    }

    insert_res = supabase.table("invoices_raw").insert(row).execute()
    invoice_raw_id: str = insert_res.data[0]["id"]

    # Queue the extraction job (mirrors the /api/invoices/extract endpoint)
    queue_invoice_job(invoice_raw_id=invoice_raw_id, organisation_id=org_id)

    return invoice_raw_id
