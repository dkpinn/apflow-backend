from fastapi.responses import Response
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import os
import fitz  # PyMuPDF
from supabase import create_client
from app.services.invoice_ocr_pipeline import extract_text_with_fallback, parse_invoice_fields
from app.services.invoice_extraction.file_naming import build_invoice_storage_filename

router = APIRouter(prefix="/api/invoices", tags=["invoices"])

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY environment variable")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class ExtractInvoiceRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: Optional[str] = None

def rename_invoice_file_after_extraction(
    *,
    raw: dict,
    organisation_id: str,
    invoice_raw_id: str,
    parsed_data: dict,
) -> dict:
    """
    Rename/move uploaded invoice file in Supabase Storage after extraction.

    Keeps original upload if rename fails.
    Returns updated file_name and file_path.
    """

    old_file_path = raw.get("file_path")
    old_file_name = raw.get("file_name") or "invoice.pdf"

    if not old_file_path:
        return {
            "file_name": old_file_name,
            "file_path": old_file_path,
            "renamed": False,
            "reason": "missing_old_file_path",
        }

    new_file_name = build_invoice_storage_filename(
        original_filename=old_file_name,
        supplier_name=parsed_data.get("supplier_name_extracted"),
        invoice_number=parsed_data.get("invoice_number"),
        invoice_date=parsed_data.get("invoice_date"),
        total_amount=parsed_data.get("total_amount"),
        invoice_raw_id=invoice_raw_id,
    )

    new_file_path = f"{organisation_id}/invoices/processed/{new_file_name}"

    if new_file_path == old_file_path:
        return {
            "file_name": new_file_name,
            "file_path": new_file_path,
            "renamed": False,
            "reason": "same_path",
        }

    try:
        # Supabase Storage move: old path -> new path
        supabase.storage.from_("invoices").move(old_file_path, new_file_path)

        supabase.table("invoices_raw").update({
            "file_name": new_file_name,
            "file_path": new_file_path,
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", invoice_raw_id).execute()

        return {
            "file_name": new_file_name,
            "file_path": new_file_path,
            "renamed": True,
            "reason": None,
        }

    except Exception as e:
        print("FILE RENAME FAILED:", str(e))

        return {
            "file_name": old_file_name,
            "file_path": old_file_path,
            "renamed": False,
            "reason": str(e),
        }


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
        supabase.table("invoices_raw").update({
            "parse_status": "failed",
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", payload.invoice_raw_id).execute()

        raise HTTPException(status_code=400, detail="Missing file_path on invoices_raw row")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as e:
        supabase.table("invoices_raw").update({
            "parse_status": "failed",
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", payload.invoice_raw_id).execute()

        raise HTTPException(status_code=400, detail=f"Storage download error: {str(e)}")

    try:
        text_result = extract_text_with_fallback(file_bytes, raw.get("file_type"))
        text = text_result["text"]
        parsed_data = parse_invoice_fields(text)
        extraction_needs_review = (
            parsed_data.get("confidence_score", 0) < 0.70
            or not parsed_data.get("invoice_number")
            or not parsed_data.get("total_amount")
            or not parsed_data.get("supplier_name_extracted")
        )
    except Exception as e:
        supabase.table("invoices_raw").update({
            "parse_status": "failed",
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", payload.invoice_raw_id).execute()

        raise HTTPException(status_code=400, detail=f"Invoice extraction failed: {str(e)}")

    organisation_id = payload.organisation_id or raw.get("organisation_id")

    # Build the payload that must be saved to invoices_extracted
    extracted_payload = {
        "organisation_id": organisation_id,
        "invoice_raw_id": payload.invoice_raw_id,
        "supplier_id": raw.get("supplier_id"),
        "supplier_name_extracted": parsed_data.get("supplier_name_extracted"),
        "invoice_number": parsed_data.get("invoice_number"),
        "invoice_date": parsed_data.get("invoice_date"),
        "due_date": parsed_data.get("due_date"),
        "subtotal": parsed_data.get("subtotal"),
        "tax_amount": parsed_data.get("tax_amount"),
        "total_amount": parsed_data.get("total_amount"),
        "currency": parsed_data.get("currency"),
        "confidence_score": parsed_data.get("confidence_score"),
        "supplier_del_address_extracted": parsed_data.get("supplier_del_address_extracted"),
        "supplier_pos_address_extracted": parsed_data.get("supplier_pos_address_extracted"),
        "supplier_email_extracted": parsed_data.get("supplier_email_extracted"),
        "supplier_acc_email_extracted": parsed_data.get("supplier_acc_email_extracted"),
        "supplier_telephone_extracted": parsed_data.get("supplier_telephone_extracted"),
        "supplier_fax_extracted": parsed_data.get("supplier_fax_extracted"),
        "supplier_cell_extracted": parsed_data.get("supplier_cell_extracted"),
        "supplier_website_extracted": parsed_data.get("supplier_website_extracted"),
        "vat_number_extracted": parsed_data.get("vat_number_extracted"),
        "cus_code_extracted": parsed_data.get("cus_code_extracted"),
        "company_registration_number_extracted": parsed_data.get("company_registration_number_extracted"),
        "review_status": "needs_info" if extraction_needs_review else "pending",
        "notes": (
            "Low-confidence extraction. Manual review required."
            if extraction_needs_review
            else "Extracted/re-extracted by FastAPI invoice parser."
        ),
        "bank_account_name_extracted": parsed_data.get("bank_account_name_extracted"),
        "bank_name_extracted": parsed_data.get("bank_name_extracted"),
        "bank_account_number_extracted": parsed_data.get("bank_account_number_extracted"),
        "bank_branch_code_extracted": parsed_data.get("bank_branch_code_extracted"),
        "bank_swift_code_extracted": parsed_data.get("bank_swift_code_extracted"),
        "updated_at": datetime.utcnow().isoformat(),
    }

    print("EXTRACTED PAYLOAD TO SAVE:", extracted_payload)

    # Find existing extracted invoice for this raw upload
    # Save extracted invoice safely:
    # - update existing row if invoice_raw_id already exists
    # - insert only if no extracted row exists yet

    existing_res = (
        supabase
        .table("invoices_extracted")
        .select("id")
        .eq("invoice_raw_id", payload.invoice_raw_id)
        .limit(1)
        .execute()
    )

    if existing_res.data:
        extracted_invoice_id = existing_res.data[0]["id"]

        update_res = (
            supabase
            .table("invoices_extracted")
            .update(extracted_payload)
            .eq("id", extracted_invoice_id)
            .execute()
        )

        print("UPDATED INVOICES_EXTRACTED:", update_res.data)

    else:
        insert_res = (
            supabase
            .table("invoices_extracted")
            .insert(extracted_payload)
            .execute()
        )

        extracted_invoice_id = insert_res.data[0]["id"] if insert_res.data else None

        print("INSERTED INVOICES_EXTRACTED:", insert_res.data)

    line_items = parsed_data.get("line_items", [])

    if extracted_invoice_id:
        # Delete old line items first so re-extract does not duplicate lines
        supabase.table("invoice_line_items").delete().eq(
            "invoice_extracted_id",
            extracted_invoice_id
        ).execute()

        if line_items:
            line_item_payload = []

            for item in line_items:
                line_item_payload.append({
                    "invoice_extracted_id": extracted_invoice_id,
                    "organisation_id": organisation_id,
                    "description": item.get("description"),
                    "quantity": item.get("quantity"),
                    "unit_price": item.get("unit_price"),
                    "tax_amount": item.get("tax_amount"),
                    "line_total": item.get("line_total"),
                    "raw_line": item.get("raw_line"),
                })

            inserted_line_items = (
                supabase
                .table("invoice_line_items")
                .insert(line_item_payload)
                .execute()
            )

            print("INSERTED LINE ITEMS:", inserted_line_items.data)
        else:
            print("NO LINE ITEMS EXTRACTED")

    # Update raw upload status
    
    file_rename_result = rename_invoice_file_after_extraction(
    raw=raw,
    organisation_id=organisation_id,
    invoice_raw_id=payload.invoice_raw_id,
    parsed_data=parsed_data,
    )

    supabase.table("invoices_raw").update({
        "parse_status": "completed",
        "parse_completed_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
        
    }).eq("id", payload.invoice_raw_id).execute()
    
    response = {
        "success": True,
        "status": "completed",
        "invoice_raw_id": payload.invoice_raw_id,
        "extracted_invoice_id": extracted_invoice_id,
        "organisation_id": organisation_id,
        "file_path": file_rename_result.get("file_path"),
        "file_name": file_rename_result.get("file_name"),
        "file_renamed": file_rename_result.get("renamed"),
        "file_rename_reason": file_rename_result.get("reason"),
        "text_preview": text[:2000],
        "supplier_name": parsed_data.get("supplier_name_extracted"),
        "invoice_number": parsed_data.get("invoice_number"),
        "invoice_date": parsed_data.get("invoice_date"),
        "due_date": parsed_data.get("due_date"),
        "subtotal": parsed_data.get("subtotal"),
        "vat_amount": parsed_data.get("tax_amount"),
        "total_amount": parsed_data.get("total_amount"),
        "currency": parsed_data.get("currency"),
        "confidence_score": parsed_data.get("confidence_score"),
        "debug": {
            "ocr_method": text_result.get("method"),
            "ocr_used": text_result.get("ocr_used"),
            "text_preview": text[:2000],
        },
    }

    print("EXTRACT RESPONSE:", response)

    return response

@router.get("/raw/{invoice_raw_id}/file")
def get_invoice_raw_file(invoice_raw_id: str):
    raw_res = (
        supabase
        .table("invoices_raw")
        .select("*")
        .eq("id", invoice_raw_id)
        .limit(1)
        .execute()
    )

    if not raw_res.data:
        raise HTTPException(status_code=404, detail="Raw invoice not found")

    raw = raw_res.data[0]
    file_path = raw.get("file_path")
    file_type = raw.get("file_type") or "application/pdf"

    if not file_path:
        raise HTTPException(status_code=400, detail="Missing file_path")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Storage download error: {str(e)}")

    return Response(
        content=file_bytes,
        media_type=file_type,
        headers={
            "Content-Disposition": f'inline; filename="{raw.get("file_name", "invoice.pdf")}"'
        },
    )

@router.get("/raw/{invoice_raw_id}/preview-image")
def get_invoice_preview_image(invoice_raw_id: str, page: int = 0):
    raw_res = (
        supabase
        .table("invoices_raw")
        .select("*")
        .eq("id", invoice_raw_id)
        .limit(1)
        .execute()
    )

    if not raw_res.data:
        raise HTTPException(status_code=404, detail="Raw invoice not found")

    raw = raw_res.data[0]
    file_path = raw.get("file_path")
    file_type = raw.get("file_type") or "application/pdf"

    if not file_path:
        raise HTTPException(status_code=400, detail="Missing file_path")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Storage download error: {str(e)}")

    try:
        if file_type == "application/pdf" or file_path.lower().endswith(".pdf"):
            doc = fitz.open(stream=file_bytes, filetype="pdf")

            if page < 0 or page >= len(doc):
                raise HTTPException(status_code=400, detail="Invalid page number")

            pdf_page = doc[page]

            matrix = fitz.Matrix(2, 2)  # about 144 DPI
            pix = pdf_page.get_pixmap(matrix=matrix, alpha=False)

            image_bytes = pix.tobytes("png")

            return Response(
                content=image_bytes,
                media_type="image/png",
                headers={
                    "Cache-Control": "no-store"
                },
            )

        return Response(
            content=file_bytes,
            media_type=file_type,
            headers={
                "Cache-Control": "no-store"
            },
        )

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Preview rendering failed: {str(e)}")