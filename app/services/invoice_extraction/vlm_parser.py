from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Optional

from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

# Read at call-time (inside function) so hot-reloads and test overrides work.
# VLM_TIMEOUT_SECONDS caps how long we wait for the Gemini network round-trip.
VLM_TIMEOUT_SECONDS = int(os.getenv("VLM_TIMEOUT_SECONDS", "45"))

_SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}

_EXTRACTION_PROMPT = (
    "Extract all invoice and receipt fields from this financial document. "
    "Return all dates in YYYY-MM-DD format. "
    "Return all monetary amounts as plain numbers (no currency symbols, no commas). "
    "supplier_name_extracted must be the invoice issuer/vendor: the business that supplied the goods or services. "
    "Never use the invoice date, due date, VAT status, customer name, address, invoice label, or other document metadata as the supplier. "
    "When an invoice shows a 'Deliver To', 'Bill To', 'Sold To', 'Customer', or 'Recipient' block alongside the supplier's header or logo, "
    "the supplier is the business in the top header/logo area — NOT the entity in the delivery or billing block. "
    "The 'Deliver To' or 'Bill To' block is always the customer (buyer) and is never the supplier. "
    "If the supplier is unclear, set supplier_name_extracted to null instead of guessing. "
    "If the document uses commas as a decimal separator (e.g. South African format '1 234,56'), "
    "convert to a decimal point. "
    "For line_items: extract EVERY individual product or service line visible in the document. "
    "CRITICAL: Extract ALL monetary amounts EXACTLY as printed on the document. "
    "Do NOT adjust, strip, add, or estimate VAT. If the document shows 75.99, extract 75.99. "
    "The system will determine whether prices include VAT by comparing totals. "
    "unit_price must be the printed original/list unit price before line-level discount when both original and discounted prices are shown. "
    "If a discounted/net unit price is printed (for example Disc Price, Nett Price, Net Unit), put it in discounted_unit_price. "
    "If a discount percentage or amount is printed, put it in discount_percent or discount_amount. "
    "line_total must be the PRINTED total for that line as shown in the document's line total column — exactly as printed. "
    "IMPORTANT: when a receipt or till slip shows a 'Total' column value on the line item row AND also has a separate "
    "'Exclusive Total' or 'Ex VAT' summary row below the item table, use the value from the line item's own Total column "
    "as line_total — do NOT use the summary subtotal row. "
    "Extract the VAT amount from the 'Vat Total', 'Tax Total', or 'VAT' summary row and place it in tax_amount. "
    "IMPORTANT: 'Total Items' on a receipt is the COUNT of line items purchased (e.g. 'Total Items: 1.00' means 1 item), "
    "NOT a currency amount. Never use a 'Total Items' value as tax_amount, subtotal, or any monetary field. "
    "For till slips and POS receipts, use the receipt number, sale number, or transaction number as invoice_number. "
    "If the only candidate for invoice_number is the date, time, or a timestamp, set invoice_number to null instead. "
    "Each line item should include the item description, quantity, unit price, discounted unit price/discount if printed, line total, "
    "and the item/product code or SKU if printed. "
    "Even if the image is dark, low contrast, or taken at an angle, do your best to read every field accurately. "
    "For South African documents: "
    "Phone numbers are 10 digits starting with 0 (e.g. 031 464 6175). "
    "Never confuse digits 3 and 8, or 0 and 8 — read each digit exactly as printed. "
    "Dates on till slips are printed day-first: DD/MM/YYYY or DD-MM-YYYY. "
    "Do not reorder dates to YYYY/MM/DD or MM/DD/YYYY. "
    "VAT registration numbers are exactly 10 digits starting with 4 (e.g. 4920141654). "
    "Invoice and receipt numbers may contain letters and digits (e.g. k1T214675, INV-00123) — extract them exactly as printed. "
    "Company names on logos may use decorative or stylized fonts — read the complete full name carefully, "
    "including the legal entity suffix (CC, Pty Ltd, etc.) if visible. "
    "Set confidence_score to your confidence that the extraction is accurate and complete "
    "(1.0 = highly confident, 0.0 = unable to extract). "
    "Set document_type to one of: "
    "'tax_invoice' (a VAT invoice with an invoice number and supplier VAT registration number), "
    "'card_receipt' (a card machine / POS payment slip showing card type and last 4 digits — no line items, no VAT number), "
    "'till_slip' (a cash register or POS till receipt that may have item descriptions), "
    "'credit_note' (a supplier credit note), "
    "'statement' (an account statement), "
    "'quotation' (a quote or pro-forma invoice), "
    "'delivery_note' (a delivery docket without pricing), "
    "'other' (anything that does not fit the above). "
    "Set document_count to the number of physically separate documents visible on this page "
    "(e.g. a page with 3 stapled till slips has document_count = 3; a single invoice has document_count = 1). "
    "For each line_item, set source_bbox to [x1_pct, y1_pct, x2_pct, y2_pct] where each value is the "
    "percentage (0–100) of the page image width/height. Locate the bounding box of that actual line row "
    "as it appears in the document image. This enables the document viewer to highlight the correct region."
)
DEFAULT_EXTRACTION_PROMPT = _EXTRACTION_PROMPT


class _VLMLineItem(BaseModel):
    description: str = Field("", description="Product or service name / description")
    quantity: Optional[float] = Field(None, description="Quantity of units")
    unit_price: Optional[float] = Field(None, description="Printed original/list price per unit before discount when shown")
    discount_amount: Optional[float] = Field(None, description="Total discount amount for this line, if printed or inferable")
    discount_percent: Optional[float] = Field(None, description="Discount percentage for this line, if printed")
    discounted_unit_price: Optional[float] = Field(None, description="Printed discounted/net unit price, such as Disc Price or Nett Price")
    pricing_basis: Optional[str] = Field(None, description="How the line total was derived: unit_price, discount_amount, discount_percent, discounted_unit_price, or extended_price")
    pricing_notes: Optional[str] = Field(None, description="Small pricing evidence notes")
    line_total: Optional[float] = Field(None, description="Printed net/extended line total after discount, excluding tax when labelled ex-VAT")
    code: Optional[str] = Field(None, description="Product code, SKU, or barcode printed on the line")
    source_bbox: Optional[list[float]] = Field(
        None,
        description="Bounding box [x1_pct, y1_pct, x2_pct, y2_pct] where each value is 0–100 percent of page width/height. Locate the actual row/line as it appears in the document image.",
    )


class _VLMInvoiceSchema(BaseModel):
    supplier_name_extracted: Optional[str] = Field(
        None,
        description=(
            "Legal or trading name of the invoice issuer/vendor only. "
            "Must not be a date, VAT status, invoice label, customer name, address, or document metadata."
        ),
    )
    invoice_number: Optional[str] = Field(None, description="Invoice or tax invoice reference number")
    invoice_date: Optional[str] = Field(None, description="Invoice date in YYYY-MM-DD format")
    due_date: Optional[str] = Field(None, description="Payment due date in YYYY-MM-DD format")
    subtotal: Optional[float] = Field(None, description="Subtotal amount before tax")
    tax_amount: Optional[float] = Field(None, description="VAT or tax amount")
    total_amount: Optional[float] = Field(None, description="Total amount payable including tax")
    currency: Optional[str] = Field(None, description="ISO currency code: ZAR, USD, GBP, EUR")
    supplier_del_address_extracted: Optional[str] = Field(None, description="Supplier physical or delivery address")
    supplier_pos_address_extracted: Optional[str] = Field(None, description="Supplier postal address")
    supplier_email_extracted: Optional[str] = Field(None, description="Supplier general email address")
    supplier_acc_email_extracted: Optional[str] = Field(None, description="Supplier accounting or remittance email")
    supplier_telephone_extracted: Optional[str] = Field(None, description="Supplier telephone number")
    supplier_fax_extracted: Optional[str] = Field(None, description="Supplier fax number")
    supplier_cell_extracted: Optional[str] = Field(None, description="Supplier cell or mobile number")
    supplier_website_extracted: Optional[str] = Field(None, description="Supplier website URL")
    vat_number_extracted: Optional[str] = Field(None, description="VAT registration number")
    cus_code_extracted: Optional[str] = Field(None, description="Customer account code assigned by the supplier")
    company_registration_number_extracted: Optional[str] = Field(None, description="Company registration number")
    bank_account_name_extracted: Optional[str] = Field(None, description="Bank account holder name")
    bank_name_extracted: Optional[str] = Field(None, description="Bank name")
    bank_account_number_extracted: Optional[str] = Field(None, description="Bank account number")
    bank_branch_code_extracted: Optional[str] = Field(None, description="Bank branch or sort code")
    bank_swift_code_extracted: Optional[str] = Field(None, description="SWIFT or BIC code")
    line_items: list[_VLMLineItem] = Field(default_factory=list)
    confidence_score: float = Field(
        0.0,
        description="Confidence 0.0-1.0 that the extraction is accurate and complete",
    )
    document_type: str = Field(
        "tax_invoice",
        description=(
            "Document classification: tax_invoice, card_receipt, till_slip, credit_note, "
            "statement, quotation, delivery_note, or other"
        ),
    )
    document_count: int = Field(
        1,
        description="Number of distinct separate documents visible on this page",
    )


# Fields that the VLM result can overwrite in the Tesseract-parsed dict.
# Entity-detection fields (issuer_name_extracted, document_direction, etc.)
# are excluded — those are set by classify_document_direction after this step.
VLM_MERGE_FIELDS: list[str] = [
    "supplier_name_extracted",
    "invoice_number",
    "invoice_date",
    "due_date",
    "subtotal",
    "tax_amount",
    "total_amount",
    "currency",
    "supplier_del_address_extracted",
    "supplier_pos_address_extracted",
    "supplier_email_extracted",
    "supplier_acc_email_extracted",
    "supplier_telephone_extracted",
    "supplier_fax_extracted",
    "supplier_cell_extracted",
    "supplier_website_extracted",
    "vat_number_extracted",
    "cus_code_extracted",
    "company_registration_number_extracted",
    "bank_account_name_extracted",
    "bank_name_extracted",
    "bank_account_number_extracted",
    "bank_branch_code_extracted",
    "bank_swift_code_extracted",
    "line_items",
    "document_type",
    "document_count",
]


def normalise_vlm_invoice(vlm: _VLMInvoiceSchema) -> dict:
    parsed: dict = {}
    for field in VLM_MERGE_FIELDS:
        if field == "line_items":
            parsed["line_items"] = [item.model_dump() for item in vlm.line_items]
        else:
            parsed[field] = getattr(vlm, field, None)

    parsed["confidence_score"] = round(float(vlm.confidence_score), 2)
    return parsed


def normalise_vlm_json_response(response_text: str) -> dict:
    return normalise_vlm_invoice(_VLMInvoiceSchema.model_validate_json(response_text))


def vlm_invoice_json_schema() -> dict:
    return _VLMInvoiceSchema.model_json_schema()


def _to_png_bytes(file_bytes: bytes) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_MAX_VLM_PAGES = 10  # Gemini handles up to ~10 pages reliably before latency/token issues


def _preprocess_for_vlm(file_bytes: bytes, effective_mime: str) -> list[tuple[bytes, str]]:
    """
    Render each PDF page to a contrast-enhanced PNG before sending to Gemini.

    Returns a list of (bytes, mime_type) — one item per page for PDFs, one item
    for image inputs. Gemini sees all pages so line items that span pages are captured.
    """
    if "pdf" not in effective_mime:
        return [(file_bytes, effective_mime)]

    try:
        import fitz
        from PIL import Image
        from app.services.invoice_ocr_pipeline import DEEP_OCR_RENDER_DPI, resize_for_ocr

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        if not doc.page_count:
            return [(file_bytes, effective_mime)]

        scale = DEEP_OCR_RENDER_DPI / 72
        page_parts: list[tuple[bytes, str]] = []
        for page_num in range(min(doc.page_count, _MAX_VLM_PAGES)):
            try:
                page = doc[page_num]
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                image = resize_for_ocr(image)
                buf = io.BytesIO()
                image.save(buf, format="PNG")
                page_parts.append((buf.getvalue(), "image/png"))
            except Exception:
                continue

        return page_parts if page_parts else [(file_bytes, effective_mime)]
    except Exception:
        return [(file_bytes, effective_mime)]


def preprocess_for_vlm(file_bytes: bytes, effective_mime: str) -> list[tuple[bytes, str]]:
    return _preprocess_for_vlm(file_bytes, effective_mime)


def _call_gemini(
    file_bytes: bytes,
    effective_mime: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    prompt: Optional[str] = None,
) -> Optional[dict]:
    """Blocking Gemini API call. Run inside a thread via extract_with_gemini."""
    from google import genai
    from google.genai import types

    effective_api_key = api_key or os.getenv("GOOGLE_API_KEY")
    if not effective_api_key:
        return None

    page_parts = _preprocess_for_vlm(file_bytes, effective_mime)

    client = genai.Client(api_key=effective_api_key)
    contents: list = [
        types.Part.from_bytes(data=page_bytes, mime_type=page_mime)
        for page_bytes, page_mime in page_parts
    ]
    contents.append(prompt or _EXTRACTION_PROMPT)

    effective_model = model or os.getenv("GEMINI_VLM_MODEL") or "gemini-2.5-flash"
    response = client.models.generate_content(
        model=effective_model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_VLMInvoiceSchema,
            temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )

    usage: dict = {}
    try:
        um = getattr(response, "usage_metadata", None)
        if um:
            usage = {
                "input_tokens": getattr(um, "prompt_token_count", None),
                "output_tokens": getattr(um, "candidates_token_count", None),
            }
    except Exception:
        pass

    return {"data": normalise_vlm_json_response(response.text), "usage": usage, "model": effective_model}


def extract_with_gemini(
    file_bytes: bytes,
    mime_type: Optional[str] = None,
) -> Optional[dict]:
    """
    Extract invoice fields via Gemini 2.5 Flash vision with structured output.

    Returns a dict with the same keys as parse_invoice_fields (VLM_MERGE_FIELDS),
    or None when GOOGLE_API_KEY is absent, google-genai is not installed, the call
    times out, or any other error occurs. Always safe to call — never raises.
    """
    return extract_with_gemini_diagnostic(file_bytes, mime_type).get("data")


def extract_with_gemini_diagnostic(
    file_bytes: bytes,
    mime_type: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    prompt: Optional[str] = None,
) -> dict:
    """
    Extract invoice fields via Gemini and return failure diagnostics.

    The legacy extract_with_gemini wrapper still returns only parsed data or
    None, while the pipeline can use this function to write specific audit
    reasons when VLM fallback was needed but could not complete.
    """
    effective_api_key = api_key or os.getenv("GOOGLE_API_KEY")
    if not effective_api_key:
        return {
            "data": None,
            "reason": "missing_google_api_key",
            "error": "GOOGLE_API_KEY is not configured for Gemini VLM fallback.",
        }

    try:
        from google import genai  # noqa: F401
    except ImportError:
        return {
            "data": None,
            "reason": "missing_google_genai_package",
            "error": "Install google-genai to enable Gemini VLM fallback.",
        }

    effective_mime = (mime_type or "application/pdf").lower()
    conversion_warning = None
    if "pdf" in effective_mime:
        effective_mime = "application/pdf"
    elif effective_mime not in _SUPPORTED_MIME_TYPES:
        try:
            file_bytes = _to_png_bytes(file_bytes)
            effective_mime = "image/png"
        except Exception as conversion_exc:
            conversion_warning = str(conversion_exc)[:500]
            effective_mime = "application/pdf"

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                _call_gemini,
                file_bytes,
                effective_mime,
                effective_api_key,
                model,
                prompt,
            )
            try:
                raw_result = future.result(timeout=VLM_TIMEOUT_SECONDS)
            except FuturesTimeoutError:
                future.cancel()
                return {
                    "data": None,
                    "reason": "timeout",
                    "error": f"Gemini did not respond within {VLM_TIMEOUT_SECONDS}s.",
                    "mime_type": effective_mime,
                }
    except Exception as exc:
        return {
            "data": None,
            "reason": "api_error",
            "error_type": exc.__class__.__name__,
            "error": str(exc)[:1000],
            "mime_type": effective_mime,
        }

    # _call_gemini now returns {"data": ..., "usage": {...}, "model": "..."}
    if isinstance(raw_result, dict):
        data = raw_result.get("data")
        usage = raw_result.get("usage") or {}
        effective_model = raw_result.get("model") or model or os.getenv("GEMINI_VLM_MODEL") or "gemini-2.5-flash"
    else:
        data = raw_result  # backward compat
        usage = {}
        effective_model = model or os.getenv("GEMINI_VLM_MODEL") or "gemini-2.5-flash"

    if data is None:
        return {
            "data": None,
            "reason": "empty_response",
            "error": "Gemini fallback returned no structured extraction data.",
            "mime_type": effective_mime,
            "usage": usage,
            "model": effective_model,
        }

    result = {
        "data": data,
        "reason": None,
        "error": None,
        "mime_type": effective_mime,
        "usage": usage,
        "model": effective_model,
    }
    if conversion_warning:
        result["conversion_warning"] = conversion_warning
    return result
