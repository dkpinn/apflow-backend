"""
sales_invoice_delivery.py
-------------------------
Channel-agnostic dispatch for issued customer (sales) invoices.

The router should not know how each channel works — it picks a channel and a
recipient, and this module renders the PDF once and routes to the right sender:
  - 'email'    → send_sales_invoice_email() (Mailgun, unchanged)
  - 'whatsapp' → send_sales_invoice_whatsapp() (Meta Cloud API, document message)

Both paths log to sales_invoice_delivery_events with the channel set, so adding a
channel later means adding one sender here — callers stay the same.
"""
from __future__ import annotations

from typing import Any, Literal

from app.services.sales_invoice_documents import (
    render_sales_invoice_pdf,
    send_sales_invoice_email,
    _text,
)
from app.services.whatsapp_ingestion import _digits_only
from app.services.whatsapp_outbound import (
    send_whatsapp_document,
    upload_whatsapp_media,
    whatsapp_outbound_config,
)

Channel = Literal["email", "whatsapp"]


def send_sales_invoice_whatsapp(
    db,
    *,
    invoice: dict[str, Any],
    pdf_bytes: bytes,
    recipient_phone: str,
    actor_user_id: str,
) -> dict[str, Any]:
    """Upload the invoice PDF to Meta and send it to the recipient over WhatsApp."""
    phone_number_id, access_token = whatsapp_outbound_config()
    to_wa_id = _digits_only(recipient_phone)
    if not to_wa_id:
        raise ValueError("Recipient phone number is invalid")

    number = _text(invoice.get("invoice_number"))
    subject_prefix = "Credit note" if invoice.get("document_type") == "credit_note" else "Invoice"
    filename = f"{number or 'document'}.pdf"
    event = {
        "organisation_id": invoice["organisation_id"],
        "sales_invoice_id": invoice["id"],
        "event_type": "queued",
        "channel": "whatsapp",
        "recipient_phone": to_wa_id,
        "provider": "meta_whatsapp",
        "created_by": actor_user_id,
    }
    db.table("sales_invoice_delivery_events").insert(event).execute()
    try:
        media_id = upload_whatsapp_media(
            phone_number_id,
            access_token,
            file_bytes=pdf_bytes,
            filename=filename,
            mime_type="application/pdf",
        )
        message_id = send_whatsapp_document(
            phone_number_id,
            access_token,
            to_wa_id=to_wa_id,
            media_id=media_id,
            filename=filename,
            caption=f"{subject_prefix} {number}".strip(),
        )
        db.table("sales_invoice_delivery_events").insert(
            {**event, "event_type": "sent", "provider_message_id": message_id}
        ).execute()
        return {"success": True, "provider_message_id": message_id}
    except Exception as exc:  # noqa: BLE001 — record failure then re-raise
        db.table("sales_invoice_delivery_events").insert(
            {**event, "event_type": "failed", "details": {"error": str(exc)}}
        ).execute()
        raise


def dispatch_sales_invoice(
    db,
    *,
    invoice: dict[str, Any],
    lines: list[dict[str, Any]],
    channel: Channel,
    recipient: str,
    actor_user_id: str,
) -> dict[str, Any]:
    """Render the invoice PDF once and deliver it over the chosen channel."""
    pdf_bytes = render_sales_invoice_pdf(invoice, lines)
    if channel == "email":
        return send_sales_invoice_email(
            db,
            invoice=invoice,
            pdf_bytes=pdf_bytes,
            recipient_email=recipient,
            actor_user_id=actor_user_id,
        )
    if channel == "whatsapp":
        return send_sales_invoice_whatsapp(
            db,
            invoice=invoice,
            pdf_bytes=pdf_bytes,
            recipient_phone=recipient,
            actor_user_id=actor_user_id,
        )
    raise ValueError(f"Unsupported delivery channel: {channel}")
