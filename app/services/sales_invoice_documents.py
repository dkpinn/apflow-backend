from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import fitz
import requests


CUSTOMER_DOCUMENT_BUCKET = "customer-documents"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _currency(value: Any, currency: str) -> str:
    amount = Decimal(str(value or 0))
    return f"{currency} {amount:,.2f}"


def _draw_wrapped(
    page: fitz.Page,
    rect: fitz.Rect,
    text: str,
    *,
    fontsize: float = 9,
    color: tuple[float, float, float] = (0, 0, 0),
    fontname: str = "helv",
    align: int = fitz.TEXT_ALIGN_LEFT,
) -> None:
    page.insert_textbox(
        rect,
        text,
        fontsize=fontsize,
        fontname=fontname,
        color=color,
        align=align,
        lineheight=1.15,
    )


def render_sales_invoice_pdf(invoice: dict[str, Any], lines: list[dict[str, Any]]) -> bytes:
    issuer = invoice.get("issuer_snapshot") or {}
    customer = invoice.get("customer_snapshot") or {}
    branding = invoice.get("branding_snapshot") or {}
    currency = _text(invoice.get("currency")) or "ZAR"
    document_title = "CREDIT NOTE" if invoice.get("document_type") == "credit_note" else "TAX INVOICE"
    primary = branding.get("primary_color") or "#174EA6"
    primary_rgb = tuple(int(primary[i : i + 2], 16) / 255 for i in (1, 3, 5))

    doc = fitz.open()
    page_width, page_height = 595, 842
    margin = 42
    row_height = 24
    rows_per_page = 20
    pages = [lines[index : index + rows_per_page] for index in range(0, len(lines), rows_per_page)] or [[]]

    for page_index, page_lines in enumerate(pages):
        page = doc.new_page(width=page_width, height=page_height)
        page.draw_rect(fitz.Rect(0, 0, page_width, 10), fill=primary_rgb, color=primary_rgb)
        _draw_wrapped(
            page,
            fitz.Rect(margin, 30, 330, 85),
            "\n".join(
                part
                for part in [
                    _text(issuer.get("name")),
                    _text(issuer.get("address_line_1")),
                    _text(issuer.get("address_line_2")),
                    " ".join(
                        filter(
                            None,
                            [
                                _text(issuer.get("city")),
                                _text(issuer.get("province")),
                                _text(issuer.get("postal_code")),
                            ],
                        )
                    ),
                    f"VAT No: {_text(issuer.get('vat_number'))}" if issuer.get("vat_number") else "",
                    f"Reg No: {_text(issuer.get('registration_number'))}"
                    if issuer.get("registration_number")
                    else "",
                ]
                if part
            ),
            fontsize=9,
            fontname="hebo",
        )
        _draw_wrapped(
            page,
            fitz.Rect(355, 30, page_width - margin, 80),
            document_title,
            fontsize=20,
            color=primary_rgb,
            fontname="hebo",
            align=fitz.TEXT_ALIGN_RIGHT,
        )
        _draw_wrapped(
            page,
            fitz.Rect(355, 60, page_width - margin, 115),
            "\n".join(
                [
                    f"No: {_text(invoice.get('invoice_number'))}",
                    f"Date: {_text(invoice.get('issue_date'))}",
                    f"Due: {_text(invoice.get('due_date'))}",
                    f"Page: {page_index + 1}/{len(pages)}",
                ]
            ),
            fontsize=9,
            align=fitz.TEXT_ALIGN_RIGHT,
        )

        page.draw_line((margin, 125), (page_width - margin, 125), color=primary_rgb, width=1)
        _draw_wrapped(page, fitz.Rect(margin, 138, 120, 158), "BILL TO", fontsize=9, color=primary_rgb, fontname="hebo")
        _draw_wrapped(
            page,
            fitz.Rect(margin, 158, 340, 225),
            "\n".join(
                part
                for part in [
                    _text(customer.get("legal_name")),
                    _text(customer.get("billing_address")),
                    f"VAT No: {_text(customer.get('vat_number'))}" if customer.get("vat_number") else "",
                    f"Customer code: {_text(customer.get('customer_code'))}"
                    if customer.get("customer_code")
                    else "",
                ]
                if part
            ),
            fontsize=9,
        )
        _draw_wrapped(
            page,
            fitz.Rect(355, 145, page_width - margin, 220),
            "\n".join(
                part
                for part in [
                    f"Reference: {_text(invoice.get('customer_reference'))}"
                    if invoice.get("customer_reference")
                    else "",
                    f"PO: {_text(invoice.get('purchase_order_number'))}"
                    if invoice.get("purchase_order_number")
                    else "",
                    f"Original invoice: {_text(invoice.get('original_invoice_number'))}"
                    if invoice.get("original_invoice_number")
                    else "",
                    f"Reason: {_text(invoice.get('credit_reason'))}"
                    if invoice.get("credit_reason")
                    else "",
                ]
                if part
            ),
            fontsize=9,
            align=fitz.TEXT_ALIGN_RIGHT,
        )

        y = 235
        columns = [margin, 90, 300, 345, 415, 480, page_width - margin]
        page.draw_rect(fitz.Rect(margin, y, page_width - margin, y + 24), fill=primary_rgb, color=primary_rgb)
        headers = ["Code", "Description", "Qty", "Net", "VAT", "Gross"]
        for index, header in enumerate(headers):
            _draw_wrapped(
                page,
                fitz.Rect(columns[index] + 3, y + 6, columns[index + 1] - 3, y + 22),
                header,
                fontsize=8,
                color=(1, 1, 1),
                fontname="hebo",
                align=fitz.TEXT_ALIGN_RIGHT if index >= 2 else fitz.TEXT_ALIGN_LEFT,
            )
        y += 24
        for line in page_lines:
            page.draw_rect(fitz.Rect(margin, y, page_width - margin, y + row_height), color=(0.8, 0.8, 0.8), width=0.4)
            values = [
                _text(line.get("item_code")),
                _text(line.get("description")),
                f"{Decimal(str(line.get('quantity') or 0)):,.2f}",
                _currency(line.get("net_amount"), currency),
                _currency(line.get("tax_amount"), currency),
                _currency(line.get("gross_amount"), currency),
            ]
            for index, value in enumerate(values):
                _draw_wrapped(
                    page,
                    fitz.Rect(columns[index] + 3, y + 5, columns[index + 1] - 3, y + row_height - 2),
                    value,
                    fontsize=7.5,
                    align=fitz.TEXT_ALIGN_RIGHT if index >= 2 else fitz.TEXT_ALIGN_LEFT,
                )
            y += row_height

        if page_index == len(pages) - 1:
            totals_y = max(y + 18, 650)
            labels = [
                ("Subtotal", invoice.get("subtotal")),
                ("VAT", invoice.get("tax_total")),
                ("Total", invoice.get("total_amount")),
            ]
            for index, (label, value) in enumerate(labels):
                is_total = index == len(labels) - 1
                _draw_wrapped(
                    page,
                    fitz.Rect(345, totals_y, 455, totals_y + 20),
                    label,
                    fontsize=10 if is_total else 9,
                    fontname="hebo" if is_total else "helv",
                )
                _draw_wrapped(
                    page,
                    fitz.Rect(455, totals_y, page_width - margin, totals_y + 20),
                    _currency(value, currency),
                    fontsize=10 if is_total else 9,
                    fontname="hebo" if is_total else "helv",
                    align=fitz.TEXT_ALIGN_RIGHT,
                )
                totals_y += 20

            terms = _text(branding.get("terms_and_conditions"))
            banking = "\n".join(
                part
                for part in [
                    _text(branding.get("bank_name")),
                    _text(branding.get("account_holder")),
                    f"Account: {_text(branding.get('account_number'))}"
                    if branding.get("account_number")
                    else "",
                    f"Branch: {_text(branding.get('branch_code'))}"
                    if branding.get("branch_code")
                    else "",
                ]
                if part
            )
            _draw_wrapped(page, fitz.Rect(margin, 735, 300, 820), f"Terms\n{terms}", fontsize=7.5)
            _draw_wrapped(page, fitz.Rect(315, 735, page_width - margin, 820), f"Banking details\n{banking}", fontsize=7.5)

    payload = doc.tobytes(garbage=4, deflate=True, no_new_id=True)
    doc.close()
    return payload


def persist_sales_invoice_pdf(db, invoice: dict[str, Any], lines: list[dict[str, Any]]) -> str:
    pdf_bytes = render_sales_invoice_pdf(invoice, lines)
    organisation_id = str(invoice["organisation_id"])
    number = _text(invoice.get("invoice_number")).replace("/", "-")
    path = f"{organisation_id}/sales-invoices/{invoice['id']}/{number}.pdf"
    db.storage.from_(CUSTOMER_DOCUMENT_BUCKET).upload(
        path,
        pdf_bytes,
        {"content-type": "application/pdf", "upsert": "true"},
    )
    db.table("sales_invoices").update({"pdf_storage_path": path}).eq("id", invoice["id"]).execute()
    return path


def send_sales_invoice_email(
    db,
    *,
    invoice: dict[str, Any],
    pdf_bytes: bytes,
    recipient_email: str,
    actor_user_id: str,
) -> dict[str, Any]:
    api_key = os.getenv("MAILGUN_API_KEY", "")
    domain = os.getenv("MAILGUN_DOMAIN", "")
    from_email = os.getenv("MAILGUN_FROM_EMAIL", f"invoices@{domain}" if domain else "")
    if not api_key or not domain or not from_email:
        raise ValueError("Mailgun outbound email is not configured")

    issuer = invoice.get("issuer_snapshot") or {}
    reply_to = _text(issuer.get("email"))
    sender_name = _text(issuer.get("name")) or "APPayPal"
    number = _text(invoice.get("invoice_number"))
    subject_prefix = "Credit note" if invoice.get("document_type") == "credit_note" else "Invoice"
    event = {
        "organisation_id": invoice["organisation_id"],
        "sales_invoice_id": invoice["id"],
        "event_type": "queued",
        "recipient_email": recipient_email,
        "provider": "mailgun",
        "created_by": actor_user_id,
    }
    db.table("sales_invoice_delivery_events").insert(event).execute()
    try:
        response = requests.post(
            f"https://api.mailgun.net/v3/{domain}/messages",
            auth=("api", api_key),
            data={
                "from": f"{sender_name} <{from_email}>",
                "to": recipient_email,
                "subject": f"{subject_prefix} {number}",
                "text": f"Please find {subject_prefix.lower()} {number} attached.",
                **({"h:Reply-To": reply_to} if reply_to else {}),
            },
            files={"attachment": (f"{number}.pdf", pdf_bytes, "application/pdf")},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        message_id = payload.get("id")
        db.table("sales_invoice_delivery_events").insert(
            {
                **event,
                "event_type": "sent",
                "provider_message_id": message_id,
                "details": payload,
            }
        ).execute()
        return {"success": True, "provider_message_id": message_id}
    except Exception as exc:
        db.table("sales_invoice_delivery_events").insert(
            {**event, "event_type": "failed", "details": {"error": str(exc)}}
        ).execute()
        raise
