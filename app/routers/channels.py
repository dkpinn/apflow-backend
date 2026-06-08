"""
channels.py
-----------
Read-only channel configuration endpoint.

Returns which document-ingestion channels are configured on this deployment,
and their public metadata (phone numbers, domain, etc.).  No secrets are
exposed — only enough for the frontend settings page to show live status.
"""
from __future__ import annotations

import os

from fastapi import APIRouter

from app.services.whatsapp_ingestion import whatsapp_channel_status

router = APIRouter(prefix="/api/channels", tags=["channels"])


@router.get("/status")
def channel_status() -> dict:
    """
    Return the active configuration for each document-ingestion channel.

    Used by the settings page to display real-time channel status badges,
    phone numbers, chat links, and domain info without exposing secrets.
    """
    # ── WhatsApp ─────────────────────────────────────────────────────────
    whatsapp = whatsapp_channel_status()

    # ── Email ─────────────────────────────────────────────────────────────
    # The inbound email domain is baked into the migration; expose it for
    # display purposes so the frontend doesn't need to hard-code it.
    mailgun_key = os.environ.get("MAILGUN_WEBHOOK_SIGNING_KEY", "")
    email_domain = os.environ.get("INBOUND_EMAIL_DOMAIN", "mail.apflow.com")

    email = {
        "configured": bool(mailgun_key),
        "domain": email_domain,
    }

    return {
        "whatsapp": whatsapp,
        "email": email,
    }
