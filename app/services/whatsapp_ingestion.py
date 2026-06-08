"""
whatsapp_ingestion.py
---------------------
Handles inbound WhatsApp messages from Meta WhatsApp Cloud API.

Responsibilities:
  - Verify Meta webhook signature (X-Hub-Signature-256)
  - Normalise phone numbers to E.164 for DB comparison
  - Resolve which organisations a sender belongs to via organisation_users.phone
  - Download media (image / document) from Meta Graph API (two-step)
  - Reply to the sender via the Graph API (text messages)
  - Manage pending org-selection state in whatsapp_pending_selections table
  - Upload accepted media to Supabase Storage and queue invoice extraction

Environment variables required:
  META_APP_SECRET                — HMAC signing secret (App Settings → Basic → App Secret)
  META_WHATSAPP_ACCESS_TOKEN     — System User permanent access token
  META_WHATSAPP_PHONE_NUMBER_ID  — Phone Number ID from Meta Business Manager
  META_WHATSAPP_DISPLAY_NUMBER   — E.164 display number, e.g. +27123456789 (optional, for UI)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.services.invoice_extraction_service import queue_invoice_job

# ── Constants ─────────────────────────────────────────────────────────────────

GRAPH_API_BASE = "https://graph.facebook.com/v20.0"
ALLOWED_MIME_PREFIXES = ("image/", "application/pdf")


# ── Signature verification ────────────────────────────────────────────────────

def verify_meta_signature(app_secret: str, payload_bytes: bytes, sig_header: str) -> bool:
    """
    Return True when the Meta X-Hub-Signature-256 header is valid.

    Meta signs each webhook POST with:
        HMAC-SHA256(app_secret, raw_body_bytes)
    The header value is 'sha256=<hexdigest>'.
    """
    if not sig_header.startswith("sha256="):
        return False
    provided = sig_header[len("sha256="):]
    expected = hmac.new(
        app_secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, provided)


# ── Phone normalisation ───────────────────────────────────────────────────────

def normalise_phone(raw: str) -> str:
    """
    Convert any phone representation to E.164 digit string.

    Meta sends wa_id without '+' (e.g. '27821234567').
    DB stores E.164 with '+' (e.g. '+27821234567').
    We strip all non-digits and prepend '+' so both sides compare equally.
    """
    digits = re.sub(r"\D", "", raw)
    return f"+{digits}" if digits else raw


def _digits_only(phone: str) -> str:
    return re.sub(r"\D", "", phone)


# ── Entity resolution ─────────────────────────────────────────────────────────

def resolve_orgs_for_phone(supabase, wa_id: str) -> list[dict]:
    """
    Return all active organisations linked to the sender's phone number.

    Resolution order:
      1. organisation_users.phone  — registered team members (internal users)
      2. suppliers.cell / suppliers.phone — external suppliers sending invoices

    Each returned dict contains:
      org_id, org_name, user_id (None for suppliers), sender_type ('member'|'supplier'),
      and supplier_name (only for suppliers).

    Strategy: digits-suffix matching — compare last N digits so country-code
    formatting differences (with/without '+', '0', country prefix) don't break the match.
    """
    digits = _digits_only(wa_id)
    if not digits:
        return []

    matched: list[dict] = []
    seen_org_ids: set[str] = set()

    # ── Step 1: organisation_users (registered team members) ─────────────
    res = (
        supabase.table("organisation_users")
        .select("user_id, organisation_id, organisations(name)")
        .eq("status", "active")
        .not_.is_("phone", "null")
        .execute()
    )

    for row in res.data or []:
        stored_digits = _digits_only(row.get("phone") or "")
        if not stored_digits:
            continue
        # Match on suffix: compare last min(len1,len2) digits so country-code
        # formatting differences don't break the match
        min_len = min(len(digits), len(stored_digits))
        if min_len >= 9 and digits[-min_len:] == stored_digits[-min_len:]:
            org_id = row["organisation_id"]
            if org_id not in seen_org_ids:
                seen_org_ids.add(org_id)
                org_name = (row.get("organisations") or {}).get("name") or org_id
                matched.append({
                    "org_id": org_id,
                    "org_name": org_name,
                    "user_id": row["user_id"],
                    "sender_type": "member",
                })

    if matched:
        return matched  # internal user found — skip supplier check

    # ── Step 2: suppliers (external suppliers sending invoices) ───────────
    # Check both 'cell' (mobile) and 'phone' (landline/general) fields.
    # Priority: cell first, then phone — avoids double-counting the same supplier.
    sup_res = (
        supabase.table("suppliers")
        .select("organisation_id, supplier_name, phone, cell, organisations(name)")
        .eq("active", True)
        .execute()
    )

    for row in sup_res.data or []:
        for phone_field in ("cell", "phone"):
            stored_digits = _digits_only(row.get(phone_field) or "")
            if not stored_digits:
                continue
            min_len = min(len(digits), len(stored_digits))
            if min_len >= 9 and digits[-min_len:] == stored_digits[-min_len:]:
                org_id = row["organisation_id"]
                if org_id not in seen_org_ids:
                    seen_org_ids.add(org_id)
                    org_name = (row.get("organisations") or {}).get("name") or org_id
                    matched.append({
                        "org_id": org_id,
                        "org_name": org_name,
                        "user_id": None,            # supplier — no APPayPal user account
                        "sender_type": "supplier",
                        "supplier_name": row.get("supplier_name") or "Supplier",
                    })
                break  # matched on one phone field — don't double-count same supplier

    return matched


# ── WhatsApp Graph API ────────────────────────────────────────────────────────

def send_whatsapp_text(
    phone_number_id: str,
    access_token: str,
    to_wa_id: str,
    text: str,
) -> None:
    """Send a plain-text WhatsApp reply to the given wa_id."""
    url = f"{GRAPH_API_BASE}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "text",
        "text": {"body": text},
    }
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        # Replies are best-effort — log but don't fail the webhook handler
        print(f"[WhatsApp] Failed to send reply to {to_wa_id}: {exc}")


def download_whatsapp_media(
    media_id: str,
    access_token: str,
) -> tuple[bytes, str, str]:
    """
    Download a media file from Meta's Graph API (two-step).

    Step 1: GET /{media_id} → {"url": "...", "mime_type": "...", "file_size": ...}
    Step 2: GET {url} with the same Bearer token → binary file content

    Returns: (file_bytes, mime_type, filename)
    filename is guessed from mime_type when not provided.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=30) as client:
        # Step 1 — get the download URL
        meta_resp = client.get(
            f"{GRAPH_API_BASE}/{media_id}",
            headers=headers,
        )
        meta_resp.raise_for_status()
        meta = meta_resp.json()

        download_url: str = meta["url"]
        mime_type: str = meta.get("mime_type", "application/octet-stream")

        # Step 2 — download the actual file
        file_resp = client.get(download_url, headers=headers)
        file_resp.raise_for_status()
        file_bytes = file_resp.content

    # Guess filename extension from mime_type
    ext_map = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/heic": "heic",
        "application/pdf": "pdf",
    }
    ext = ext_map.get(mime_type, "bin")
    filename = f"whatsapp-{media_id[:12]}.{ext}"

    return file_bytes, mime_type, filename


# ── Pending org-selection state ───────────────────────────────────────────────

def store_pending_selection(
    supabase,
    *,
    phone: str,
    orgs: list[dict],
    media_id: str,
    mime_type: str,
    filename: Optional[str],
    uploaded_by: Optional[str],
) -> None:
    """
    Upsert a pending org-selection row for the given phone.
    Replaces any existing row for that phone (unique index on phone).
    """
    row = {
        "phone": normalise_phone(phone),
        "options": orgs,
        "media_id": media_id,
        "mime_type": mime_type,
        "filename": filename,
        "uploaded_by": uploaded_by,
        # expires_at defaults to now() + 10 minutes in the DB
    }
    # Delete existing then insert — cleaner than upsert for jsonb arrays
    supabase.table("whatsapp_pending_selections").delete().eq("phone", normalise_phone(phone)).execute()
    supabase.table("whatsapp_pending_selections").insert(row).execute()


def get_and_clear_pending(supabase, wa_id: str) -> Optional[dict]:
    """
    Retrieve the non-expired pending selection for a phone number, then delete it.
    Returns the row dict or None if not found / expired.
    """
    phone = normalise_phone(wa_id)
    now_iso = datetime.now(timezone.utc).isoformat()

    res = (
        supabase.table("whatsapp_pending_selections")
        .select("*")
        .eq("phone", phone)
        .gt("expires_at", now_iso)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None

    row = res.data[0]
    # Clear it regardless — user gets one attempt
    supabase.table("whatsapp_pending_selections").delete().eq("id", row["id"]).execute()
    return row


# ── Storage + DB ──────────────────────────────────────────────────────────────

def _sanitize_filename(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "whatsapp-attachment"


def ingest_whatsapp_media(
    supabase,
    *,
    org_id: str,
    uploaded_by: Optional[str],
    file_bytes: bytes,
    mime_type: str,
    filename: str,
) -> Optional[str]:
    """
    Upload WhatsApp media to Supabase Storage and create an invoices_raw row.

    Returns the new invoice_raw_id (UUID string), or None if the MIME type
    is not an accepted invoice document format.

    Mirrors email_ingestion.ingest_email_attachment() with source_type='whatsapp'.
    """
    if not any(mime_type.startswith(p) for p in ALLOWED_MIME_PREFIXES):
        return None

    safe = _sanitize_filename(filename)
    storage_path = f"{org_id}/invoices/{int(time.time() * 1000)}-{safe}"

    supabase.storage.from_("invoices").upload(
        storage_path,
        file_bytes,
        {"content-type": mime_type},
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    row = {
        "organisation_id": org_id,
        "file_path": storage_path,
        "file_name": filename,
        "file_type": mime_type,
        "source_type": "whatsapp",
        "upload_status": "uploaded",
        "parse_status": "pending",
        "uploaded_by": uploaded_by,
        "uploaded_at": now_iso,
    }

    insert_res = supabase.table("invoices_raw").insert(row).execute()
    invoice_raw_id: str = insert_res.data[0]["id"]

    queue_invoice_job(invoice_raw_id=invoice_raw_id, organisation_id=org_id)
    return invoice_raw_id


# ── Config helpers (used by channels router) ──────────────────────────────────

def whatsapp_channel_status() -> dict:
    """Return WhatsApp channel configuration for the /api/channels/status endpoint."""
    phone_number_id = os.environ.get("META_WHATSAPP_PHONE_NUMBER_ID", "")
    access_token = os.environ.get("META_WHATSAPP_ACCESS_TOKEN", "")
    configured = bool(phone_number_id and access_token)

    display_number = os.environ.get("META_WHATSAPP_DISPLAY_NUMBER", "")
    if not display_number and configured:
        # Attempt to fetch it from Graph API at startup if not explicitly configured
        display_number = ""  # populated lazily by the router if needed

    wa_digits = _digits_only(display_number)
    chat_link = f"https://wa.me/{wa_digits}" if wa_digits else None

    return {
        "configured": configured,
        "phone_number": display_number or None,
        "wa_id": wa_digits or None,
        "chat_link": chat_link,
    }
