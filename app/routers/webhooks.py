"""
webhooks.py
-----------
Inbound webhook handlers for external document-ingestion channels.

All routes are unauthenticated at the HTTP level — each handler performs its
own channel-specific signature / token verification before trusting any payload.

Current channels
----------------
  POST /api/webhooks/email-inbound   — Mailgun Inbound Routes
  GET  /api/webhooks/whatsapp        — Meta webhook verification challenge
  POST /api/webhooks/whatsapp        — Meta WhatsApp Cloud API inbound messages
"""
from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.db.supabase_client import get_supabase_client
from app.services.email_ingestion import (
    ingest_email_attachment,
    resolve_member_user_id,
    resolve_org_id,
    verify_mailgun_signature,
)
from app.services.whatsapp_ingestion import (
    download_whatsapp_media,
    get_and_clear_pending,
    ingest_whatsapp_media,
    normalise_phone,
    resolve_orgs_for_phone,
    send_whatsapp_text,
    store_pending_selection,
    verify_meta_signature,
)
from app.services.invoice_extraction_service import run_extract_worker_until_empty

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


# ═══════════════════════════════════════════════════════════════════════════════
# Email — Mailgun Inbound Routes
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/email-inbound")
async def email_inbound(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """
    Receive inbound emails forwarded by Mailgun Inbound Routes.

    Mailgun sends a multipart/form-data POST containing:
      - Standard fields: recipient, sender, subject, body-plain, timestamp, token, signature
      - Attachment fields: attachment-1 … attachment-N  (UploadFile-like objects)
      - attachment-count: how many attachment fields to expect

    Each PDF or image attachment is stored in Supabase Storage and queued
    for invoice extraction.  Non-document attachments (inline images, vCards, etc.)
    are silently skipped.

    Returns 200 for every Mailgun delivery attempt (even for unknown recipients)
    to avoid Mailgun retry storms.  Use the response body to diagnose routing
    issues during integration testing.
    """
    form = await request.form()

    # ── Signature verification ───────────────────────────────────────────
    signing_key = os.environ.get("MAILGUN_WEBHOOK_SIGNING_KEY", "")
    if signing_key:
        token = str(form.get("token", ""))
        timestamp = str(form.get("timestamp", ""))
        signature = str(form.get("signature", ""))
        if not verify_mailgun_signature(signing_key, token, timestamp, signature):
            raise HTTPException(status_code=401, detail="Invalid Mailgun webhook signature")

    # ── Resolve organisation ─────────────────────────────────────────────
    recipient = str(form.get("recipient", ""))
    if not recipient:
        return {"status": "ignored", "reason": "no recipient address"}

    supabase = get_supabase_client()
    org_id = resolve_org_id(supabase, recipient)
    if not org_id:
        # Unknown address — return 200 to stop Mailgun retrying.
        return {"status": "ignored", "reason": "recipient not linked to any organisation"}

    # ── Resolve sender to an organisation member (best-effort) ──────────
    sender = str(form.get("sender", ""))
    uploaded_by = resolve_member_user_id(supabase, org_id, sender) if sender else None

    # ── Process attachments ──────────────────────────────────────────────
    attachment_count = int(form.get("attachment-count", 0) or 0)
    processed_ids: list[str] = []
    skipped = 0

    for i in range(1, attachment_count + 1):
        file_field = form.get(f"attachment-{i}")
        if file_field is None or not hasattr(file_field, "filename"):
            skipped += 1
            continue

        file_bytes: bytes = await file_field.read()
        filename: str = file_field.filename or f"email-attachment-{i}"
        content_type: str = file_field.content_type or "application/octet-stream"

        raw_id = await ingest_email_attachment(
            supabase,
            org_id=org_id,
            uploaded_by=uploaded_by,
            filename=filename,
            content_type=content_type,
            file_bytes=file_bytes,
        )
        if raw_id:
            processed_ids.append(raw_id)
        else:
            skipped += 1

    # ── Kick the extraction worker ───────────────────────────────────────
    if processed_ids:
        background_tasks.add_task(run_extract_worker_until_empty)

    return {
        "status": "ok",
        "processed": len(processed_ids),
        "skipped": skipped,
        "invoice_raw_ids": processed_ids,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# WhatsApp — Meta WhatsApp Cloud API
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/whatsapp", response_class=PlainTextResponse)
def whatsapp_verify(request: Request) -> str:
    """
    Meta webhook verification endpoint.

    During initial webhook registration in Meta Business Manager, Meta sends
    a GET with hub.mode=subscribe, hub.verify_token, and hub.challenge.
    We must return the challenge string if the token matches our configured value.

    Note: FastAPI Query(alias=...) does not correctly parse dotted parameter names
    (hub.mode, hub.verify_token, hub.challenge) — we use request.query_params directly.
    """
    hub_mode = request.query_params.get("hub.mode", "")
    hub_verify_token = request.query_params.get("hub.verify_token", "")
    hub_challenge = request.query_params.get("hub.challenge", "")
    expected_token = os.environ.get("META_WEBHOOK_VERIFY_TOKEN", "")

    print(f"[WhatsApp] Verify challenge: mode={hub_mode!r} match={hub_verify_token == expected_token}")

    if hub_mode == "subscribe" and hub_verify_token == expected_token and expected_token:
        return hub_challenge
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@router.post("/whatsapp")
async def whatsapp_inbound(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """
    Receive inbound WhatsApp messages from Meta WhatsApp Cloud API.

    Meta sends a JSON POST for every message/status event.  We only act on
    inbound messages of type 'image' or 'document'.

    The handler:
      1. Reads the raw body bytes first (required for HMAC verification)
      2. Verifies X-Hub-Signature-256
      3. Parses the payload JSON
      4. Processes each incoming message via _handle_whatsapp_message()
      5. Always returns 200 — Meta retries on any non-2xx response

    See: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks
    """
    payload_bytes = await request.body()

    # ── Signature verification ───────────────────────────────────────────
    app_secret = os.environ.get("META_APP_SECRET", "")
    if app_secret:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        if not verify_meta_signature(app_secret, payload_bytes, sig_header):
            raise HTTPException(status_code=403, detail="Invalid Meta webhook signature")

    try:
        payload = json.loads(payload_bytes)
    except Exception:
        return {"status": "ignored", "reason": "invalid JSON"}

    # ── Walk the entries ─────────────────────────────────────────────────
    access_token = os.environ.get("META_WHATSAPP_ACCESS_TOKEN", "")
    phone_number_id = os.environ.get("META_WHATSAPP_PHONE_NUMBER_ID", "")
    supabase = get_supabase_client()

    any_ingested = False
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                ingested = await _handle_whatsapp_message(
                    supabase=supabase,
                    message=message,
                    access_token=access_token,
                    phone_number_id=phone_number_id,
                    background_tasks=background_tasks,
                )
                if ingested:
                    any_ingested = True

    if any_ingested:
        background_tasks.add_task(run_extract_worker_until_empty)

    # Always 200 to prevent Meta retrying
    return {"status": "ok"}


async def _handle_whatsapp_message(
    *,
    supabase,
    message: dict,
    access_token: str,
    phone_number_id: str,
    background_tasks: BackgroundTasks,
) -> bool:
    """
    Process one inbound WhatsApp message.

    Returns True if a document was successfully ingested (caller kicks the worker).
    """
    from_wa_id: str = message.get("from", "")
    msg_type: str = message.get("type", "")

    def reply(text: str) -> None:
        if access_token and phone_number_id and from_wa_id:
            send_whatsapp_text(phone_number_id, access_token, from_wa_id, text)

    # ── Case 1: text message — check for pending org selection reply ─────
    if msg_type == "text":
        text_body = (message.get("text") or {}).get("body", "").strip()
        pending = get_and_clear_pending(supabase, from_wa_id)

        if pending and text_body.isdigit():
            choice = int(text_body) - 1  # convert "1" → index 0
            options: list[dict] = pending.get("options", [])
            if 0 <= choice < len(options):
                chosen = options[choice]
                org_id = chosen["org_id"]
                org_name = chosen["org_name"]
                uploaded_by = pending.get("uploaded_by")
                media_id = pending["media_id"]
                mime_type = pending["mime_type"]
                filename = pending.get("filename") or f"whatsapp-{media_id[:8]}"

                try:
                    file_bytes, resolved_mime, resolved_name = download_whatsapp_media(
                        media_id, access_token
                    )
                    actual_mime = mime_type or resolved_mime
                    actual_name = filename or resolved_name
                    raw_id = ingest_whatsapp_media(
                        supabase,
                        org_id=org_id,
                        uploaded_by=uploaded_by,
                        file_bytes=file_bytes,
                        mime_type=actual_mime,
                        filename=actual_name,
                    )
                    if raw_id:
                        sender_type = chosen.get("sender_type", "member")
                        if sender_type == "supplier":
                            reply(f"✓ Received your document for {org_name}! It will be processed shortly.")
                        else:
                            reply(f"✓ Processing for {org_name}. You'll see it in your invoice list shortly.")
                        return True
                    else:
                        reply("Sorry, that file type isn't supported. Please send a PDF or photo.")
                except Exception as exc:  # noqa: BLE001
                    print(f"[WhatsApp] Media download/ingest failed: {exc}")
                    reply("Sorry, I couldn't process that file. Please try again.")
            else:
                reply(
                    f"Please reply with a number between 1 and {len(options)}. "
                    "Or send your invoice image/PDF again."
                )
        elif pending:
            # They had a pending selection but sent something other than a digit
            reply(
                "I was waiting for you to choose an organisation. "
                "Please reply with just the number (e.g. 1 or 2), "
                "or send your invoice image/PDF again to restart."
            )
        else:
            # General text with no pending
            reply(
                "Please send a photo or PDF of your invoice and I'll process it for you."
            )
        return False

    # ── Case 2: image or document — the main flow ────────────────────────
    if msg_type not in ("image", "document"):
        # Ignore audio, video, stickers, reactions, etc.
        return False

    media_block = message.get(msg_type, {})
    media_id: str = media_block.get("id", "")
    mime_type: str = media_block.get("mime_type", "")
    filename: str = media_block.get("filename") or ""  # documents only

    if not media_id:
        return False

    # ── Resolve sender to organisations ──────────────────────────────────
    orgs = resolve_orgs_for_phone(supabase, from_wa_id)

    if not orgs:
        reply(
            "Hi! Your number isn't linked to any APPayPal organisation.\n\n"
            "• Team member? Go to Settings → Personal → add your mobile number.\n"
            "• Supplier? Ask your client to add your number to your supplier record in APPayPal."
        )
        return False

    uploaded_by: str | None = orgs[0].get("user_id")  # None for suppliers — that's fine
    sender_type: str = orgs[0].get("sender_type", "member")

    # ── Single org: ingest immediately ───────────────────────────────────
    if len(orgs) == 1:
        org_id = orgs[0]["org_id"]
        org_name = orgs[0]["org_name"]
        try:
            file_bytes, resolved_mime, resolved_name = download_whatsapp_media(
                media_id, access_token
            )
            actual_mime = mime_type or resolved_mime
            actual_name = filename or resolved_name
            raw_id = ingest_whatsapp_media(
                supabase,
                org_id=org_id,
                uploaded_by=uploaded_by,
                file_bytes=file_bytes,
                mime_type=actual_mime,
                filename=actual_name,
            )
            if raw_id:
                if sender_type == "supplier":
                    reply(f"✓ Received your document for {org_name}! It will be processed shortly.")
                else:
                    reply(f"✓ Received! Processing for {org_name}. You'll see it in your invoice list shortly.")
                return True
            else:
                reply("Sorry, that file type isn't supported. Please send a PDF or photo.")
        except Exception as exc:  # noqa: BLE001
            print(f"[WhatsApp] Media download/ingest failed: {exc}")
            reply("Sorry, I couldn't process that file. Please try again.")
        return False

    # ── Multiple orgs: ask which one ─────────────────────────────────────
    org_lines = "\n".join(f"{i + 1}) {o['org_name']}" for i, o in enumerate(orgs))
    reply(f"Which organisation is this for?\n{org_lines}\nReply with the number.")

    store_pending_selection(
        supabase,
        phone=normalise_phone(from_wa_id),
        orgs=orgs,
        media_id=media_id,
        mime_type=mime_type,
        filename=filename or None,
        uploaded_by=uploaded_by,
    )
    return False
