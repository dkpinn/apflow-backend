"""
whatsapp_outbound.py
--------------------
Outbound document delivery over the Meta WhatsApp Cloud API.

The inbound side (whatsapp_ingestion.py) already downloads media and replies with
text. This module adds the reverse: upload a generated PDF to Meta and send it to
a recipient as a `document` message. It is channel plumbing only — it reads
credentials from the environment today; a per-org integration store can be wired
in later without touching callers.

Environment variables:
  META_WHATSAPP_ACCESS_TOKEN     — System User permanent access token
  META_WHATSAPP_PHONE_NUMBER_ID  — Phone Number ID from Meta Business Manager

Note on business-initiated messaging: sending a document outside the 24-hour
customer-service window requires an approved WhatsApp message template. In-session
document messages work directly. That template approval is the remaining
integration decision, not a code change here.
"""
from __future__ import annotations

import logging
import os

import httpx

from app.services.whatsapp_ingestion import GRAPH_API_BASE

logger = logging.getLogger(__name__)


def whatsapp_outbound_config() -> tuple[str, str]:
    """Return (phone_number_id, access_token) or raise if not configured."""
    phone_number_id = os.getenv("META_WHATSAPP_PHONE_NUMBER_ID", "")
    access_token = os.getenv("META_WHATSAPP_ACCESS_TOKEN", "")
    if not phone_number_id or not access_token:
        raise ValueError("WhatsApp outbound is not configured")
    return phone_number_id, access_token


def upload_whatsapp_media(
    phone_number_id: str,
    access_token: str,
    *,
    file_bytes: bytes,
    filename: str,
    mime_type: str = "application/pdf",
) -> str:
    """Upload a file to Meta and return its media_id (reusable for a message)."""
    url = f"{GRAPH_API_BASE}/{phone_number_id}/media"
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (filename, file_bytes, mime_type)},
        )
        resp.raise_for_status()
        return resp.json()["id"]


def send_whatsapp_document(
    phone_number_id: str,
    access_token: str,
    *,
    to_wa_id: str,
    media_id: str,
    filename: str,
    caption: str | None = None,
) -> str:
    """Send a previously-uploaded document to a recipient. Returns the message id."""
    url = f"{GRAPH_API_BASE}/{phone_number_id}/messages"
    document: dict[str, str] = {"id": media_id, "filename": filename}
    if caption:
        document["caption"] = caption
    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "document",
        "document": document,
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        messages = resp.json().get("messages") or [{}]
        return messages[0].get("id", "")
