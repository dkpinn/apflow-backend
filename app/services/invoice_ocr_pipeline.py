from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Optional
import fitz  # PyMuPDF
import pytesseract
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
from PIL import Image

from app.services.invoice_extraction.layout_analyser import analyse_invoice_layout

from app.services.invoice_extraction.supplier_parser import extract_supplier_name

from app.services.invoice_extraction.preprocessing import (
    preprocess_for_ocr_variants,
    preprocess_image_for_ocr,
)

from app.services.invoice_extraction.banking_parser import (
    extract_bank_account_name,
    extract_bank_account_number,
    extract_bank_branch_code,
    extract_bank_name,
    extract_swift_code,
)
from app.services.invoice_extraction.totals_parser import (
    extract_subtotal,
    extract_tax_amount,
    extract_total_amount,
    infer_total_if_missing_or_zero,
)

from app.services.invoice_extraction.invoice_number_parser import extract_invoice_number

from app.services.invoice_extraction.contact_parser import (
    extract_company_registration_number,
    extract_customer_code,
    extract_supplier_accounting_email,
    extract_supplier_cell,
    extract_supplier_delivery_address,
    extract_supplier_email,
    extract_supplier_fax,
    extract_supplier_postal_address,
    extract_supplier_telephone,
    extract_supplier_website,
    extract_vat_number,
)

from app.services.invoice_extraction.line_item_parser import extract_line_items
# ------------------------------------------------------------
# TEXT EXTRACTION
# ------------------------------------------------------------

def extract_selectable_text_from_pdf(file_bytes: bytes) -> str:
    """
    Extract selectable text from a digital PDF.
    This is the preferred path because it is faster and more accurate than OCR.
    """
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages: list[str] = []

    for page in doc:
        pages.append(page.get_text("text"))

    return "\n".join(pages).strip()

def pdf_to_images(file_bytes: bytes, dpi: int = 250) -> list[Image.Image]:
    """
    Convert PDF pages to images for OCR fallback.
    """
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    images: list[Image.Image] = []

    for page in doc:
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        image = Image.open(io.BytesIO(pix.tobytes("png")))
        images.append(image)

    return images

def ocr_image(image: Image.Image) -> str:
    best_text = ""

    variants = preprocess_for_ocr_variants(image)

    for variant_name, processed_image in variants:
        for psm in ["6", "4", "11"]:
            text = pytesseract.image_to_string(
                processed_image,
                config=f"--oem 3 --psm {psm}",
            ).strip()

            if len(text) > len(best_text):
                best_text = text

    return best_text

def extract_text_with_fallback(file_bytes: bytes, file_type: Optional[str] = None) -> dict:
    """
    Extraction priority:
    1. Try selectable PDF text.
    2. If too little text, OCR PDF pages.
    3. If not a PDF, OCR as image.
    """

    selectable_text = ""

    is_pdf = (
        file_type == "application/pdf"
        or file_type is None
        or file_type == ""
        or file_type.lower().endswith("pdf")
    )

    if is_pdf:
        try:
            selectable_text = extract_selectable_text_from_pdf(file_bytes)
        except Exception:
            selectable_text = ""

    if len(selectable_text.strip()) >= 80:
        return {
            "method": "pdf_text",
            "ocr_used": False,
            "text": selectable_text,
        }

    ocr_parts: list[str] = []

    try:
        if is_pdf:
            images = pdf_to_images(file_bytes)
        else:
            images = [Image.open(io.BytesIO(file_bytes))]
    except Exception:
        images = [Image.open(io.BytesIO(file_bytes))]

    for image in images:
        ocr_parts.append(ocr_image(image))

    return {
        "method": "ocr",
        "ocr_used": True,
        "text": "\n".join(ocr_parts).strip(),
    }

# ------------------------------------------------------------
# PARSING HELPERS
# ------------------------------------------------------------

def normalise_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]

def clean_amount(value: str) -> Optional[float]:
    if value is None:
        return None

    try:
        cleaned = (
            str(value)
            .replace("R", "")
            .replace("ZAR", "")
            .replace("£", "")
            .replace("GBP", "")
            .replace("$", "")
            .replace("USD", "")
            .replace("€", "")
            .replace("EUR", "")
            .replace(" ", "")
            .strip()
        )

        # Handles South African format: 2 890,96 or 2890,96
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")

        # Handles normal thousands comma: 2,890.96
        elif "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")

        return float(cleaned)

    except Exception:
        return None

def detect_currency(text: str) -> str:
    upper = text.upper()

    if "ZAR" in upper or re.search(r"\bR\s*\d", text):
        return "ZAR"
    if "GBP" in upper or "£" in text:
        return "GBP"
    if "USD" in upper or "$" in text:
        return "USD"
    if "EUR" in upper or "€" in text:
        return "EUR"

    return "ZAR"

def parse_date_to_iso(value: str) -> Optional[str]:
    value = value.strip().replace(",", "")

    formats = [
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%Y-%m-%d",
        "%d/%m/%y",
        "%d-%m-%y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d %Y",
        "%B %d %Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue

    return None

# ------------------------------------------------------------
# FIELD EXTRACTION
# ------------------------------------------------------------

def extract_invoice_date(text: str) -> Optional[str]:
    label_patterns = [
        r"(?:Invoice\s*Date|Tax\s*Invoice\s*Date|Date)\s*[:#\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        r"(?:Invoice\s*Date|Tax\s*Invoice\s*Date|Date)\s*[:#\-]?\s*(\d{4}[\/\-]\d{1,2}[\/\-]\d{1,2})",
        r"(?:Invoice\s*Date|Tax\s*Invoice\s*Date|Date)\s*[:#\-]?\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
        r"(?:Invoice\s*Date|Tax\s*Invoice\s*Date|Date)\s*[:#\-]?\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
    ]

    for pattern in label_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            parsed = parse_date_to_iso(match.group(1))
            if parsed:
                return parsed

    fallback_patterns = [
        r"\b\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4}\b",
        r"\b\d{4}[\/\-]\d{1,2}[\/\-]\d{1,2}\b",
        r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
        r"\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}\b",
    ]

    for pattern in fallback_patterns:
        match = re.search(pattern, text)
        if match:
            parsed = parse_date_to_iso(match.group(0))
            if parsed:
                return parsed

    return None

def extract_due_date(text: str) -> Optional[str]:
    patterns = [
        r"(?:Due\s*Date|Payment\s*Due|Due)\s*[:#\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        r"(?:Due\s*Date|Payment\s*Due|Due)\s*[:#\-]?\s*(\d{4}[\/\-]\d{1,2}[\/\-]\d{1,2})",
        r"(?:Due\s*Date|Payment\s*Due|Due)\s*[:#\-]?\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            parsed = parse_date_to_iso(match.group(1))
            if parsed:
                return parsed

    return None

def calculate_confidence(parsed: dict) -> float:
    score = 0.0

    if parsed.get("supplier_name_extracted"):
        score += 0.20
    if parsed.get("invoice_number"):
        score += 0.25
    if parsed.get("invoice_date"):
        score += 0.15
    if parsed.get("total_amount") is not None:
        score += 0.25
    if parsed.get("currency"):
        score += 0.05
    if parsed.get("tax_amount") is not None:
        score += 0.05
    if parsed.get("bank_account_number_extracted"):
        score += 0.05

    return round(min(score, 1.0), 2)

def parse_invoice_fields(text: str) -> dict:
    layout = analyse_invoice_layout(text)

    invoice_number = extract_invoice_number(text)
    invoice_date = extract_invoice_date(text)
    due_date = extract_due_date(text)

    line_items = extract_line_items(text, layout_type=layout.layout_type)

    total_amount = extract_total_amount(text)
    tax_amount = extract_tax_amount(text)
    subtotal = extract_subtotal(text, total_amount, tax_amount)
    total_amount = infer_total_if_missing_or_zero(total_amount, subtotal, tax_amount)

    if subtotal is None and line_items:
        line_total_sum = sum(item.get("line_total") or 0 for item in line_items)
        if line_total_sum > 0:
            subtotal = round(line_total_sum, 2)

    if total_amount is None and subtotal is not None and tax_amount is not None:
        total_amount = round(subtotal + tax_amount, 2)

    parsed = {
        "layout_type": layout.layout_type,

        "invoice_number": invoice_number,
        "supplier_name_extracted": extract_supplier_name(text, layout_type=layout.layout_type),
        "invoice_date": invoice_date,
        "due_date": due_date,

        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
        "currency": detect_currency(text),

        "supplier_del_address_extracted": extract_supplier_delivery_address(text),
        "supplier_pos_address_extracted": extract_supplier_postal_address(text),
        "supplier_email_extracted": extract_supplier_email(text),
        "supplier_acc_email_extracted": extract_supplier_accounting_email(text),
        "supplier_telephone_extracted": extract_supplier_telephone(text),
        "supplier_fax_extracted": extract_supplier_fax(text),
        "supplier_cell_extracted": extract_supplier_cell(text),
        "supplier_website_extracted": extract_supplier_website(text),

        "vat_number_extracted": extract_vat_number(text),
        "cus_code_extracted": extract_customer_code(text),
        "company_registration_number_extracted": extract_company_registration_number(text),

        "bank_account_name_extracted": extract_bank_account_name(text),
        "bank_name_extracted": extract_bank_name(text),
        "bank_account_number_extracted": extract_bank_account_number(text),
        "bank_branch_code_extracted": extract_bank_branch_code(text),
        "bank_swift_code_extracted": extract_swift_code(text),

        "line_items": line_items,
    }

    parsed["confidence_score"] = calculate_confidence(parsed)

    return parsed