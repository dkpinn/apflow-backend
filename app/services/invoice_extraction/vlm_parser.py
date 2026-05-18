from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Optional

from pydantic import BaseModel, Field

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
    "If the document uses commas as a decimal separator (e.g. South African format '1 234,56'), "
    "convert to a decimal point. "
    "For line_items: extract EVERY individual product or service line visible in the document. "
    "unit_price must be the EXCLUDING-VAT (ex-VAT / net) price per unit. "
    "line_total must be the EXCLUDING-VAT (ex-VAT / net) total for the line (unit_price × quantity). "
    "VAT / tax is applied at the invoice level, not per line — do not include tax in unit_price or line_total. "
    "Each line item should include the item description, quantity, unit price (ex-VAT), line total (ex-VAT), "
    "and the item/product code or SKU if printed. "
    "Even if the image is dark or low contrast, do your best to read each line. "
    "Set confidence_score to your confidence that the extraction is accurate and complete "
    "(1.0 = highly confident, 0.0 = unable to extract)."
)


class _VLMLineItem(BaseModel):
    description: str = Field("", description="Product or service name / description")
    quantity: Optional[float] = Field(None, description="Quantity of units")
    unit_price: Optional[float] = Field(None, description="Ex-VAT (net) price per unit, excluding tax")
    line_total: Optional[float] = Field(None, description="Ex-VAT (net) line total: unit_price × quantity, excluding tax")
    code: Optional[str] = Field(None, description="Product code, SKU, or barcode printed on the line")


class _VLMInvoiceSchema(BaseModel):
    supplier_name_extracted: Optional[str] = Field(None, description="Legal or trading name of the invoice issuer/vendor")
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
]


def _to_png_bytes(file_bytes: bytes) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _preprocess_for_vlm(file_bytes: bytes, effective_mime: str) -> tuple[bytes, str]:
    """
    Render PDFs to a contrast-enhanced image before sending to Gemini.

    Raw scanned-receipt PDFs are often dark and low-contrast. Applying the same
    receipt preprocessing that helps Tesseract also helps Gemini read line items.
    Returns (bytes, mime_type) ready to pass to the Gemini API.
    """
    if "pdf" not in effective_mime:
        return file_bytes, effective_mime

    try:
        import fitz
        from PIL import Image
        from app.services.invoice_ocr_pipeline import DEEP_OCR_RENDER_DPI, resize_for_ocr
        from app.services.invoice_extraction.receipt_preprocessing import preprocess_receipt_photo

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        if not doc.page_count:
            return file_bytes, effective_mime

        scale = DEEP_OCR_RENDER_DPI / 72
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        image = resize_for_ocr(image)

        processed = preprocess_receipt_photo(image)
        buf = io.BytesIO()
        processed.processed_image.save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    except Exception:
        return file_bytes, effective_mime


def _call_gemini(file_bytes: bytes, effective_mime: str) -> Optional[dict]:
    """Blocking Gemini API call. Run inside a thread via extract_with_gemini."""
    from google import genai
    from google.genai import types

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None

    file_bytes, effective_mime = _preprocess_for_vlm(file_bytes, effective_mime)

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type=effective_mime),
            _EXTRACTION_PROMPT,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_VLMInvoiceSchema,
            temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )

    vlm = _VLMInvoiceSchema.model_validate_json(response.text)

    parsed: dict = {}
    for field in VLM_MERGE_FIELDS:
        if field == "line_items":
            parsed["line_items"] = [item.model_dump() for item in vlm.line_items]
        else:
            parsed[field] = getattr(vlm, field, None)

    parsed["confidence_score"] = round(float(vlm.confidence_score), 2)
    return parsed


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
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None

    try:
        from google import genai  # noqa: F401 — check import before spawning thread
    except ImportError:
        print("VLM EXTRACTION SKIPPED: install google-genai to enable Gemini fallback")
        return None

    effective_mime = (mime_type or "application/pdf").lower()
    if "pdf" in effective_mime:
        effective_mime = "application/pdf"
    elif effective_mime not in _SUPPORTED_MIME_TYPES:
        try:
            file_bytes = _to_png_bytes(file_bytes)
            effective_mime = "image/png"
        except Exception:
            effective_mime = "application/pdf"

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call_gemini, file_bytes, effective_mime)
            try:
                return future.result(timeout=VLM_TIMEOUT_SECONDS)
            except FuturesTimeoutError:
                print(f"VLM EXTRACTION TIMEOUT: Gemini did not respond within {VLM_TIMEOUT_SECONDS}s")
                future.cancel()
                return None
    except Exception as exc:
        print(f"VLM EXTRACTION FAILED: {exc}")
        return None
