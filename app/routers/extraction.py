# app/routers/extraction.py

from fastapi import APIRouter, HTTPException
from app.services.invoice_extractor import extract_invoice_from_storage

router = APIRouter(prefix="/api/extract", tags=["extraction"])


@router.post("/invoice")
def extract_invoice(payload: dict):
    invoice_raw_id = payload.get("invoice_raw_id")

    if not invoice_raw_id:
        raise HTTPException(status_code=400, detail="invoice_raw_id required")

    result = extract_invoice_from_storage(invoice_raw_id)

    return result