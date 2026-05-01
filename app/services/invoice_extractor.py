# app/services/invoice_extractor.py

from app.db.supabase_client import supabase
from app.parsers.invoice_parser import parse_invoice_pdf


def extract_invoice_from_storage(invoice_raw_id: str):

    # 1. Get raw record
    raw = supabase.table("invoices_raw") \
        .select("*") \
        .eq("id", invoice_raw_id) \
        .single() \
        .execute()

    if not raw.data:
        raise Exception("Invoice not found")

    file_path = raw.data["file_path"]

    # 2. Download file from Supabase Storage
    file_bytes = supabase.storage.from_("invoices").download(file_path)

    # 3. Parse invoice
    extracted = parse_invoice_pdf(file_bytes)

    # 4. VALIDATION (ADD HERE)
    try:
        extracted = parse_invoice_pdf(file_bytes)

        if not extracted.get("invoice_number") or not extracted.get("total_amount"):
            raise Exception("Missing key fields")

        # insert into invoices_extracted
        insert = supabase.table("invoices_extracted").insert({
            "organisation_id": raw.data["organisation_id"],
            "supplier_name": extracted["supplier_name"],
            "invoice_number": extracted["invoice_number"],
            "invoice_date": extracted["invoice_date"],
            "total_amount": extracted["total_amount"],
            "currency": extracted["currency"],
            "raw_invoice_id": invoice_raw_id
        }).execute()

        supabase.table("invoices_raw").update({
            "parse_status": "extracted"
        }).eq("id", invoice_raw_id).execute()

        return {"status": "success"}

    except Exception as e:
        supabase.table("invoices_raw").update({
            "parse_status": "failed"
        }).eq("id", invoice_raw_id).execute()

        return {
            "status": "failed",
            "error": str(e)
        }

    # 5. Update raw status
    supabase.table("invoices_raw").update({
        "parse_status": "extracted"
    }).eq("id", invoice_raw_id).execute()

    return {
        "status": "success",
        "invoice_id": insert.data[0]["id"]
    }