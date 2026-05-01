from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional

import fitz  # PyMuPDF
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client


load_dotenv()

router = APIRouter(prefix="/api/invoices", tags=["invoices"])

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class ExtractInvoiceRequest(BaseModel):
    invoice_raw_id: str


class ParsedInvoice(BaseModel):
    invoice_number: Optional[str] = None
    supplier_name_extracted: Optional[str] = None
    invoice_date: Optional[str] = None
    total_amount: Optional[float] = None
    currency: str = "GBP"
    confidence_score: float = 0.0


def extract_text_from_pdf(file_bytes: bytes) -> str:
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    return "\n".join(page.get_text() for page in doc)


def normalise_amount(value: str) -> float:
    cleaned = value.replace(",", "").replace("£", "").replace("$", "").replace("R", "").strip()
    return float(cleaned)


def parse_invoice(text: str) -> ParsedInvoice:
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    supplier_name = lines[0] if lines else None

    invoice_number = None
    inv_match = re.search(r"\b(INV[-\s]?\d+|Invoice\s*(No\.?|Number)?\s*[:#]?\s*[A-Z0-9\-]+)\b", text, re.IGNORECASE)
    if inv_match:
        invoice_number = inv_match.group(0).replace("Invoice Number", "").replace("Invoice No", "").replace("Invoice", "").replace(":", "").replace("#", "").strip()

    invoice_date = None
    date_match = re.search(r"\b\d{2}/\d{2}/\d{4}\b", text)
    if date_match:
        invoice_date = date_match.group(0)

    total_amount = None
    total_patterns = [
        r"(?:Invoice\s+Total|Amount\s+Due|Total\s+Due|Grand\s+Total|Total)\s*[:\s]*[A-Z$£€R]*\s*([\d,]+\.\d{2})",
        r"[A-Z$£€R]+\s*([\d,]+\.\d{2})\s*(?:Total|Due)",
    ]

    for pattern in total_patterns:
        total_match = re.search(pattern, text, re.IGNORECASE)
        if total_match:
            total_amount = normalise_amount(total_match.group(1))
            break

    confidence = 0.0
    if supplier_name:
        confidence += 0.25
    if invoice_number:
        confidence += 0.30
    if invoice_date:
        confidence += 0.15
    if total_amount is not None:
        confidence += 0.30

    return ParsedInvoice(
        invoice_number=invoice_number,
        supplier_name_extracted=supplier_name,
        invoice_date=invoice_date,
        total_amount=total_amount,
        currency="GBP",
        confidence_score=round(confidence, 2),
    )


def mark_raw_failed(invoice_raw_id: str, reason: str) -> None:
    supabase.table("invoices_raw").update({
        "parse_status": "failed",
    }).eq("id", invoice_raw_id).execute()


def mark_raw_completed(invoice_raw_id: str) -> None:
    supabase.table("invoices_raw").update({
        "parse_status": "completed",
    }).eq("id", invoice_raw_id).execute()


def resolve_supplier_id(
    organisation_id: str,
    supplier_hint: Optional[str],
    supplier_name_extracted: Optional[str],
    existing_supplier_id: Optional[str],
) -> Optional[str]:
    if existing_supplier_id:
        return existing_supplier_id

    search_value = supplier_hint or supplier_name_extracted

    if not search_value:
        return None

    result = (
        supabase.table("suppliers")
        .select("id, supplier_name")
        .eq("organisation_id", organisation_id)
        .ilike("supplier_name", f"%{search_value}%")
        .limit(1)
        .execute()
    )

    if result.data:
        return result.data[0]["id"]

    return None


@router.post("/extract")
def extract_invoice(payload: ExtractInvoiceRequest):
    raw_response = (
        supabase.table("invoices_raw")
        .select("*")
        .eq("id", payload.invoice_raw_id)
        .single()
        .execute()
    )

    if not raw_response.data:
        raise HTTPException(status_code=404, detail="Invoice raw record not found")

    raw = raw_response.data
    file_path = raw.get("file_path")

    if not file_path:
        mark_raw_failed(payload.invoice_raw_id, "Missing file_path")
        raise HTTPException(status_code=400, detail="Invoice raw record has no file_path")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as exc:
        mark_raw_failed(payload.invoice_raw_id, "Storage download failed")
        raise HTTPException(
            status_code=400,
            detail=f"Storage download failed for path '{file_path}': {str(exc)}",
        )

    try:
        extracted_text = extract_text_from_pdf(file_bytes)
        parsed = parse_invoice(extracted_text)
    except Exception as exc:
        mark_raw_failed(payload.invoice_raw_id, "PDF extraction failed")
        raise HTTPException(status_code=400, detail=f"PDF extraction failed: {str(exc)}")

    if not parsed.invoice_number or parsed.total_amount is None:
        mark_raw_failed(payload.invoice_raw_id, "Missing invoice_number or total_amount")
        return {
            "status": "failed",
            "reason": "Missing invoice_number or total_amount",
            "invoice_raw_id": payload.invoice_raw_id,
            "file_path": file_path,
            "data": parsed.model_dump(),
        }

    supplier_id = resolve_supplier_id(
        organisation_id=raw.get("organisation_id"),
        supplier_hint=None,
        supplier_name_extracted=parsed.supplier_name_extracted,
        existing_supplier_id=raw.get("supplier_id"),
    )

    insert_payload = {
        "organisation_id": raw.get("organisation_id"),
        "invoice_raw_id": raw.get("id"),
        "supplier_id": supplier_id,
        "supplier_name_extracted": parsed.supplier_name_extracted,
        "invoice_number": parsed.invoice_number,
        "invoice_date": parsed.invoice_date,
        "subtotal": None,
        "tax_amount": None,
        "total_amount": parsed.total_amount,
        "currency": parsed.currency,
        "confidence_score": parsed.confidence_score,
        "review_status": "pending",
        "notes": "Extracted by FastAPI invoice parser.",
    }

    insert_response = supabase.table("invoices_extracted").insert(insert_payload).execute()

    mark_raw_completed(payload.invoice_raw_id)

    return {
        "status": "completed",
        "invoice_raw_id": payload.invoice_raw_id,
        "file_path": file_path,
        "extracted_invoice_id": insert_response.data[0]["id"] if insert_response.data else None,
        "supplier_id": supplier_id,
        "data": parsed.model_dump(),
    }