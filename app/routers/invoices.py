from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime
import os
import re
import fitz  # PyMuPDF
from supabase import create_client


router = APIRouter(prefix="/api/invoices", tags=["invoices"])


SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY environment variable")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class ExtractInvoiceRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: Optional[str] = None


def extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        pages: list[str] = []

        for page in doc:
            pages.append(page.get_text("text"))

        return "\n".join(pages).strip()

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"PDF text extraction failed: {str(e)}")


def clean_money(value: Any) -> Optional[float]:
    if value is None:
        return None

    try:
        value_str = str(value)
        value_str = value_str.replace(",", "")
        value_str = value_str.replace("R", "")
        value_str = value_str.replace("£", "")
        value_str = value_str.replace("$", "")
        value_str = value_str.replace("€", "")
        value_str = value_str.strip()
        return float(value_str)
    except Exception:
        return None


def detect_currency(text: str) -> str:
    if "£" in text or " GBP" in text.upper():
        return "GBP"
    if "$" in text or " USD" in text.upper():
        return "USD"
    if "€" in text or " EUR" in text.upper():
        return "EUR"
    return "ZAR"


def find_invoice_number(text: str, debug: dict) -> Optional[str]:
    patterns = [
        r"Invoice\s*Number\s*[:#]?\s*([A-Z0-9\-\/]+)",
        r"Invoice\s*No\.?\s*[:#]?\s*([A-Z0-9\-\/]+)",
        r"Inv(?:oice)?\s*#\s*([A-Z0-9\-\/]+)",
        r"\b(INV[-\s]?\d+[A-Z0-9\-\/]*)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            debug["invoice_number_pattern"] = pattern
            debug["invoice_number_match"] = match.group(1)
            return match.group(1).strip()

    return None


def find_invoice_date(text: str, debug: dict) -> Optional[str]:
    patterns = [
        r"Invoice\s*Date\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        r"Date\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        r"Invoice\s*Date\s*[:\-]?\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
        r"Date\s*[:\-]?\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            debug["invoice_date_pattern"] = pattern
            debug["invoice_date_match"] = match.group(1)
            return match.group(1).strip()

    return None


def find_due_date(text: str, debug: dict) -> Optional[str]:
    patterns = [
        r"Due\s*Date\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        r"Payment\s*Due\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
        r"Due\s*Date\s*[:\-]?\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
        r"Payment\s*Due\s*[:\-]?\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            debug["due_date_pattern"] = pattern
            debug["due_date_match"] = match.group(1)
            return match.group(1).strip()

    return None


def find_supplier_name(text: str, debug: dict) -> Optional[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    ignored_terms = [
        "registered office",
        "company registration",
        "tax invoice",
        "invoice number",
        "invoice date",
        "description",
        "quantity",
        "unit price",
        "amount",
        "total",
        "vat",
        "page",
    ]

    candidates: list[str] = []

    for line in lines[:30]:
        lower = line.lower()

        if any(term in lower for term in ignored_terms):
            continue

        if len(line) < 3 or len(line) > 80:
            continue

        if re.search(r"\d{4,}", line):
            continue

        candidates.append(line)

    debug["supplier_candidates"] = candidates

    if candidates:
        return candidates[0]

    return None


def find_total_amount(text: str, debug: dict) -> Optional[float]:
    patterns = [
        r"Amount\s*Due\s*[:\-]?\s*[R£$€]?\s*([\d,]+\.\d{2})",
        r"Invoice\s*Total\s*[:\-]?\s*[R£$€]?\s*([\d,]+\.\d{2})",
        r"Total\s*Due\s*[:\-]?\s*[R£$€]?\s*([\d,]+\.\d{2})",
        r"\bTotal\b\s*[:\-]?\s*[R£$€]?\s*([\d,]+\.\d{2})",
    ]

    matches_found = []

    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            matches_found.append({"pattern": pattern, "matches": matches})

    debug["total_patterns"] = matches_found

    if not matches_found:
        return None

    last_match = matches_found[0]["matches"][-1]
    return clean_money(last_match)


def find_vat_amount(text: str, debug: dict) -> Optional[float]:
    patterns = [
        r"VAT\s*[:\-]?\s*[R£$€]?\s*([\d,]+\.\d{2})",
        r"Tax\s*Amount\s*[:\-]?\s*[R£$€]?\s*([\d,]+\.\d{2})",
        r"VAT\s*Amount\s*[:\-]?\s*[R£$€]?\s*([\d,]+\.\d{2})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            debug["vat_pattern"] = pattern
            debug["vat_match"] = match.group(1)
            return clean_money(match.group(1))

    return None


def find_subtotal(text: str, total_amount: Optional[float], vat_amount: Optional[float], debug: dict) -> Optional[float]:
    patterns = [
        r"Subtotal\s*[:\-]?\s*[R£$€]?\s*([\d,]+\.\d{2})",
        r"Sub\s*Total\s*[:\-]?\s*[R£$€]?\s*([\d,]+\.\d{2})",
        r"Net\s*Amount\s*[:\-]?\s*[R£$€]?\s*([\d,]+\.\d{2})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            debug["subtotal_pattern"] = pattern
            debug["subtotal_match"] = match.group(1)
            return clean_money(match.group(1))

    if total_amount is not None and vat_amount is not None:
        return round(total_amount - vat_amount, 2)

    return None


def parse_invoice_debug(text: str) -> dict:
    debug: dict[str, Any] = {
        "text_length": len(text),
        "first_20_lines": text.splitlines()[:20],
    }

    supplier_name = find_supplier_name(text, debug)
    invoice_number = find_invoice_number(text, debug)
    invoice_date = find_invoice_date(text, debug)
    due_date = find_due_date(text, debug)
    total_amount = find_total_amount(text, debug)
    vat_amount = find_vat_amount(text, debug)
    subtotal = find_subtotal(text, total_amount, vat_amount, debug)
    currency = detect_currency(text)

    parsed = {
        "supplier_name": supplier_name,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "due_date": due_date,
        "subtotal": subtotal,
        "vat_amount": vat_amount,
        "total_amount": total_amount,
        "currency": currency,
        "debug": debug,
    }

    print("PARSED OUTPUT:", parsed)

    return parsed


@router.post("/extract")
def extract_invoice(payload: ExtractInvoiceRequest):
    print("EXTRACT PAYLOAD:", payload.model_dump())

    raw_res = (
        supabase
        .table("invoices_raw")
        .select("*")
        .eq("id", payload.invoice_raw_id)
        .limit(1)
        .execute()
    )

    if not raw_res.data:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Raw invoice not found",
                "invoice_raw_id": payload.invoice_raw_id,
            },
        )

    raw = raw_res.data[0]
    print("RAW RECORD:", raw)

    file_path = raw.get("file_path")

    if not file_path:
        raise HTTPException(status_code=400, detail="Missing file_path on invoices_raw row")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Storage download error: {str(e)}")

    text = extract_text_from_pdf(file_bytes)
    parsed = parse_invoice_debug(text)

    organisation_id = payload.organisation_id or raw.get("organisation_id")

    response = {
        "success": True,
        "invoice_raw_id": payload.invoice_raw_id,
        "organisation_id": organisation_id,
        "file_path": file_path,
        "text_preview": text[:2000],
        "raw_text": text,
        "supplier_name": parsed.get("supplier_name"),
        "invoice_number": parsed.get("invoice_number"),
        "invoice_date": parsed.get("invoice_date"),
        "due_date": parsed.get("due_date"),
        "subtotal": parsed.get("subtotal"),
        "vat_amount": parsed.get("vat_amount"),
        "total_amount": parsed.get("total_amount"),
        "currency": parsed.get("currency"),
        "parsed": parsed,
        "debug": parsed.get("debug"),
    }

    print("EXTRACT RESPONSE:", response)

    return response