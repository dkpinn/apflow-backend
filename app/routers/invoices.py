from __future__ import annotations

import io as _io
import logging
import re
from typing import Annotated, Optional

logger = logging.getLogger(__name__)

import fitz  # PyMuPDF
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.dependencies import authenticated_user, ensure_org_read, ensure_org_write
from app.db.supabase_client import get_supabase_client
from app.services.audit_log import log_invoice_event
from app.services.invoice_readiness import evaluate_invoice_readiness
from app.services.invoice_supplier_rules import (
    fetch_supplier_processing_settings,
    reapply_supplier_rules_to_invoice,
)
from app.services.invoice_line_items import replace_invoice_line_items
from app.services.invoice_data_builders import utc_now_iso
from app.services.invoice_extraction_service import (
    get_raw_invoice,
    process_next_queued_invoice_job,
    queue_invoice_job,
    run_extract_worker_until_empty,
)

router = APIRouter(prefix="/api/invoices", tags=["invoices"])
UserAuth = Annotated[tuple, Depends(authenticated_user)]
try:
    supabase = get_supabase_client()
except Exception:
    supabase = None  # will fail on first API call; create .env with SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY


def _require_org_read(auth: UserAuth, organisation_id: Optional[str]) -> str:
    user_id, _db = auth
    ensure_org_read(user_id, organisation_id)
    return str(organisation_id)


def _require_org_write(auth: UserAuth, organisation_id: Optional[str]) -> str:
    user_id, _db = auth
    ensure_org_write(user_id, organisation_id)
    return str(organisation_id)


def _load_raw_invoice_for_auth(
    invoice_raw_id: str,
    auth: UserAuth,
    *,
    requested_org_id: Optional[str] = None,
    write: bool = False,
) -> tuple[dict, str]:
    raw = get_raw_invoice(invoice_raw_id)
    organisation_id = raw.get("organisation_id")
    if not organisation_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if requested_org_id and str(requested_org_id) != str(organisation_id):
        raise HTTPException(status_code=400, detail="Invoice does not belong to organisation_id")
    if write:
        _require_org_write(auth, organisation_id)
    else:
        _require_org_read(auth, organisation_id)
    return raw, str(organisation_id)


def _load_extracted_invoice_for_write(
    invoice_extracted_id: str,
    requested_org_id: Optional[str],
    auth: UserAuth,
    *,
    select: str = "id, organisation_id",
) -> dict:
    result = (
        supabase.table("invoices_extracted")
        .select(select)
        .eq("id", invoice_extracted_id)
        .limit(1)
        .execute()
    )
    invoice = result.data[0] if result.data else None
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    organisation_id = invoice.get("organisation_id")
    if requested_org_id and str(requested_org_id) != str(organisation_id):
        raise HTTPException(status_code=400, detail="Invoice does not belong to organisation_id")
    _require_org_write(auth, organisation_id)
    return invoice


class ProcessNextJobRequest(BaseModel):
    organisation_id: Optional[str] = None


class SaveLineItemsRequest(BaseModel):
    invoice_extracted_id: str
    organisation_id: str
    supplier_id: Optional[str] = None
    line_items: list[dict]
    document_total: Optional[float] = None  # original VLM-extracted total; rounding reference


class GeneratePreviewRequest(BaseModel):
    invoice_raw_id: str
    organisation_id: str


@router.post("/jobs/process-next")
def process_next_invoice_job(payload: ProcessNextJobRequest, auth: UserAuth):
    if not payload.organisation_id:
        raise HTTPException(status_code=400, detail="organisation_id is required")
    _require_org_write(auth, payload.organisation_id)
    return process_next_queued_invoice_job(organisation_id=payload.organisation_id)


@router.get("/raw/{invoice_raw_id}/file")
def get_invoice_raw_file(invoice_raw_id: str, auth: UserAuth):
    raw, _organisation_id = _load_raw_invoice_for_auth(invoice_raw_id, auth)
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
            "Content-Disposition": f'inline; filename="{raw.get("file_name", "invoice.pdf")}"',
        },
    )


@router.get("/raw/{invoice_raw_id}/preview-image")
def get_invoice_preview_image(invoice_raw_id: str, auth: UserAuth, page: int = 0):
    raw, _organisation_id = _load_raw_invoice_for_auth(invoice_raw_id, auth)
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
            matrix = fitz.Matrix(2, 2)
            pix = pdf_page.get_pixmap(matrix=matrix, alpha=False)
            image_bytes = pix.tobytes("png")

            return Response(
                content=image_bytes,
                media_type="image/png",
                headers={"Cache-Control": "no-store"},
            )

        return Response(
            content=file_bytes,
            media_type=file_type,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Preview rendering failed: {str(e)}")


@router.post("/save-line-items")
def save_invoice_line_items(req: SaveLineItemsRequest, auth: UserAuth):
    """
    Persist user-edited line items and recompute invoices_extracted totals.
    Line items are the source of truth: subtotal = SUM(line_total), VAT = subtotal * rate,
    total = subtotal + VAT.  Rounding differences vs document_total are absorbed automatically.
    """
    existing_inv = _load_extracted_invoice_for_write(
        req.invoice_extracted_id,
        req.organisation_id,
        auth,
        select="id, organisation_id, tax_amount",
    )
    organisation_id = str(existing_inv.get("organisation_id"))

    # 1. Determine VAT rate — only if supplier is a VAT vendor (has vat_number)
    vat_rate = 0.0
    if req.supplier_id:
        settings = fetch_supplier_processing_settings(supabase, req.supplier_id)
        if settings.get("vat_number"):
            raw_rate = settings.get("default_vat_rate")
            vat_rate = float(raw_rate) / 100 if raw_rate else 0.15

    # 2. Subtotal from line items
    subtotal = round(sum(float(it.get("line_total") or 0) for it in req.line_items), 2)

    # 3. Fetch document-extracted tax_amount — this is the source-of-truth from VLM/OCR
    # and must NOT be overwritten by a re-calculation from line items.
    existing_tax = round(float(existing_inv.get("tax_amount") or 0), 2)

    # computed_vat is used only for rounding/reconciliation logic below; it is
    # never written back to invoices_extracted.tax_amount.
    computed_vat = round(subtotal * vat_rate, 2)
    computed_total = round(subtotal + existing_tax, 2)

    # 4. Hybrid rounding adjustment vs original document total
    rounding_applied = None
    needs_review = False
    final_line_items = list(req.line_items)

    if req.document_total is not None:
        diff = round(req.document_total - computed_total, 2)
        abs_diff = abs(diff)
        if 0 < abs_diff <= 0.02:
            # Absorb silently — floating-point drift, total_amount already close enough
            rounding_applied = "vat_adjusted"
        elif 0 < abs_diff <= 0.50:
            # Named rounding line item for visible penny differences
            final_line_items.append({"description": "Rounding adjustment", "line_total": diff})
            subtotal = round(subtotal + diff, 2)
            computed_total = round(subtotal + existing_tax, 2)
            rounding_applied = "line_item_added"
        elif abs_diff > 0.50:
            # Too large to auto-fix — flag for human review
            needs_review = True
            rounding_applied = "needs_review"

    # 5. Persist line items (always delete-and-replace on explicit save)
    try:
        diagnostics = replace_invoice_line_items(
            supabase,
            invoice_extracted_id=req.invoice_extracted_id,
            organisation_id=organisation_id,
            line_items=final_line_items,
            invoice_total=computed_total,
            delete_when_empty=True,
            raise_on_error=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save line items: {exc}")

    # 6. Update invoices_extracted with derived totals.
    # total_amount and tax_amount are document-extracted values (set by VLM/OCR)
    # and must only change on re-extraction, not on line item saves.
    # Overwriting total_amount with a computed value causes double-counting when
    # prices include VAT (line totals are inclusive, so subtotal + existing_tax > document total).
    patch: dict = {
        "subtotal": subtotal,
    }
    if needs_review:
        patch["validation_status"] = "needs_review"

    supabase.table("invoices_extracted").update(patch).eq("id", req.invoice_extracted_id).eq("organisation_id", organisation_id).execute()

    readiness = evaluate_invoice_readiness(
        supabase,
        invoice_extracted_id=req.invoice_extracted_id,
        organisation_id=organisation_id,
        reason="Line items saved.",
        actor_type="api",
    )

    return {
        "subtotal": subtotal,
        "tax_amount": existing_tax,
        "total_amount": computed_total,
        "rounding_applied": rounding_applied,
        "needs_review": needs_review,
        "diagnostics": diagnostics,
        "readiness": readiness,
    }


class ReapplyRulesRequest(BaseModel):
    invoice_extracted_id: str
    organisation_id: str


@router.post("/reapply-supplier-rules")
def reapply_supplier_rules_endpoint(req: ReapplyRulesRequest, auth: UserAuth):
    _load_extracted_invoice_for_write(req.invoice_extracted_id, req.organisation_id, auth)
    result = (
        supabase.table("invoices_extracted")
        .select("*, supplier:suppliers(*)")
        .eq("id", req.invoice_extracted_id)
        .eq("organisation_id", req.organisation_id)
        .single()
        .execute()
    )
    invoice = result.data if result else None
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    supplier_id = invoice.get("supplier_id")
    if not supplier_id:
        raise HTTPException(status_code=422, detail="No supplier linked to this invoice")

    rules_applied = reapply_supplier_rules_to_invoice(
        supabase,
        invoice=invoice,
        supplier_id=supplier_id,
        actor_type="user",
        event_reason="Manual re-apply of supplier rules via UI.",
    )

    # Recompute and persist subtotal / tax / total from the fresh line items
    if not rules_applied.get("skipped"):
        _recompute_invoice_totals(supabase, req.invoice_extracted_id, invoice)

    readiness = evaluate_invoice_readiness(
        supabase,
        invoice_extracted_id=req.invoice_extracted_id,
        organisation_id=req.organisation_id,
        reason="Supplier rules re-applied.",
        actor_type="user",
    )

    return {"success": True, "rules_applied": rules_applied, "readiness": readiness}


def _recompute_invoice_totals(supabase_client, invoice_extracted_id: str, invoice: dict) -> None:
    """After line items are replaced, recompute subtotal and tax_amount on invoices_extracted.

    NOTE: total_amount is intentionally NOT updated here — it holds the value extracted
    from the source document (OCR/VLM) and must not be overwritten by a line-items
    recalculation (doing so would corrupt the 'Invoice total (from document)' display
    and break the reconciliation green/red dot logic).
    """
    try:
        rows = (
            supabase_client.table("invoice_line_items")
            .select("line_total")
            .eq("invoice_extracted_id", invoice_extracted_id)
            .execute()
        ).data or []

        subtotal = round(sum(float(r.get("line_total") or 0) for r in rows), 2)

        # Determine VAT rate from the linked supplier (already joined in the invoice dict)
        supplier = invoice.get("supplier") or {}
        vat_rate = 0.0
        if supplier.get("vat_number"):
            raw_rate = supplier.get("default_vat_rate")
            vat_rate = float(raw_rate) / 100 if raw_rate else 0.15

        tax_amount = round(subtotal * vat_rate, 2)

        supabase_client.table("invoices_extracted").update({
            "subtotal": subtotal,
            "tax_amount": tax_amount,
            # total_amount deliberately omitted — preserve the document-extracted value
        }).eq("id", invoice_extracted_id).execute()

        logger.info(
            "REAPPLY: recomputed totals for %s: subtotal=%s, tax=%s",
            invoice_extracted_id, subtotal, tax_amount,
        )
    except Exception:
        logger.exception("REAPPLY: failed to recompute totals for %s", invoice_extracted_id)


class MergeInvoicesPayload(BaseModel):
    invoice_raw_ids: list[str]
    organisation_id: str


@router.post("/merge")
def merge_invoices(payload: MergeInvoicesPayload, background_tasks: BackgroundTasks, auth: UserAuth):
    """
    Merge two or more single-page invoice uploads into one multi-page document and
    trigger a fresh extraction.  Old raw records (and all dependent data) are deleted.
    """
    import time as _time

    if len(payload.invoice_raw_ids) < 2:
        raise HTTPException(status_code=400, detail="At least two invoice_raw_ids are required")
    _require_org_write(auth, payload.organisation_id)

    # Step 1 — fetch raw records in caller-specified page order
    rows_result = (
        supabase.from_("invoices_raw")
        .select("id, file_path, file_name, file_type")
        .in_("id", payload.invoice_raw_ids)
        .eq("organisation_id", payload.organisation_id)
        .execute()
    )
    rows = rows_result.data or []
    if len(rows) != len(payload.invoice_raw_ids):
        raise HTTPException(status_code=404, detail="One or more invoices not found")

    id_to_row = {r["id"]: r for r in rows}
    ordered = [id_to_row[rid] for rid in payload.invoice_raw_ids]

    # Step 2 — download each file from Storage
    file_bytes_list: list[tuple[bytes, str]] = []
    for row in ordered:
        file_bytes = supabase.storage.from_("invoices").download(row["file_path"])
        file_bytes_list.append((file_bytes, row.get("file_type") or "application/pdf"))

    # Step 3 — merge into one PDF using PyMuPDF (fitz is already imported at module level)
    import io as _io
    from PIL import Image as _PILImage, ImageOps as _ImageOps

    merged_doc = fitz.open()
    for file_bytes, file_type in file_bytes_list:
        if file_type.startswith("image/"):
            # Apply EXIF orientation before embedding so the PDF is correctly oriented.
            pil_img = _ImageOps.exif_transpose(_PILImage.open(_io.BytesIO(file_bytes))).convert("RGB")
            corrected_buf = _io.BytesIO()
            pil_img.save(corrected_buf, format="PNG")
            img_doc = fitz.open(stream=corrected_buf.getvalue(), filetype="png")
            pdf_bytes = img_doc.convert_to_pdf()
            img_doc.close()
            src = fitz.open("pdf", pdf_bytes)
        else:
            src = fitz.open(stream=file_bytes, filetype="pdf")
        merged_doc.insert_pdf(src)
        src.close()
    merged_bytes = merged_doc.tobytes()
    merged_doc.close()

    # Step 4 — upload merged PDF to Storage
    base_name = re.sub(r"[^a-zA-Z0-9._-]", "_", ordered[0].get("file_name") or "merged")
    if not base_name.lower().endswith(".pdf"):
        base_name = base_name + ".pdf"
    new_file_name = f"{int(_time.time())}-merged-{base_name}"
    new_path = f"{payload.organisation_id}/invoices/{new_file_name}"

    supabase.storage.from_("invoices").upload(
        new_path,
        merged_bytes,
        {"content-type": "application/pdf"},
    )

    # Step 5 — create new invoices_raw record
    new_raw_result = (
        supabase.from_("invoices_raw")
        .insert({
            "organisation_id": payload.organisation_id,
            "file_path": new_path,
            "file_name": new_file_name,
            "file_type": "application/pdf",
            "parse_status": "pending",
            "upload_status": "uploaded",
        })
        .execute()
    )
    new_raw_id = new_raw_result.data[0]["id"]

    # Step 6 — delete old records (manual cascade: no FK cascade on invoices_raw)
    for old_id in payload.invoice_raw_ids:
        extracted_rows = (
            supabase.from_("invoices_extracted")
            .select("id")
            .eq("invoice_raw_id", old_id)
            .execute()
        ).data or []
        extracted_ids = [r["id"] for r in extracted_rows]

        if extracted_ids:
            supabase.from_("invoice_line_items").delete().in_("invoice_extracted_id", extracted_ids).execute()
            try:
                supabase.from_("invoice_extraction_feedback").delete().in_("invoice_extracted_id", extracted_ids).execute()
            except Exception:
                pass
            supabase.from_("invoices_extracted").delete().eq("invoice_raw_id", old_id).execute()

        supabase.from_("invoice_parse_attempts").delete().eq("invoice_raw_id", old_id).execute()
        supabase.from_("document_pages").delete().eq("invoice_raw_id", old_id).execute()
        supabase.from_("invoice_audit_events").delete().eq("invoice_raw_id", old_id).execute()

        try:
            supabase.storage.from_("invoices").remove([id_to_row[old_id]["file_path"]])
        except Exception:
            pass

        supabase.from_("invoices_raw").delete().eq("id", old_id).execute()

    # Step 7 — queue extraction on the merged record
    queue_invoice_job(invoice_raw_id=new_raw_id, organisation_id=payload.organisation_id)
    background_tasks.add_task(run_extract_worker_until_empty)

    return {"success": True, "new_invoice_raw_id": new_raw_id}


@router.post("/{raw_id}/split-into-pages")
def split_invoice_into_pages(raw_id: str, organisation_id: str, background_tasks: BackgroundTasks, auth: UserAuth):
    """
    Split a multi-page PDF into individual single-page invoices, one per page.
    Each page is uploaded as a new invoices_raw record and queued for extraction.
    The original record and all dependent data are deleted.
    """
    import time as _time
    _load_raw_invoice_for_auth(raw_id, auth, requested_org_id=organisation_id, write=True)

    # Step 1 — fetch the raw record
    row_result = (
        supabase.from_("invoices_raw")
        .select("id, file_path, file_name, file_type")
        .eq("id", raw_id)
        .eq("organisation_id", organisation_id)
        .single()
        .execute()
    )
    row = row_result.data
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Step 2 — download and open with PyMuPDF
    file_bytes = supabase.storage.from_("invoices").download(row["file_path"])
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    page_count = len(doc)
    if page_count < 2:
        doc.close()
        raise HTTPException(status_code=422, detail="Document has only one page — use the crop tool for within-page splits")

    # Step 3 — split into N single-page PDFs and upload each
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", row.get("file_name") or "split")
    if safe_name.lower().endswith(".pdf"):
        safe_name = safe_name[:-4]

    new_raw_ids: list[str] = []
    for i in range(page_count):
        single_doc = fitz.open()
        single_doc.insert_pdf(doc, from_page=i, to_page=i)
        page_bytes = single_doc.tobytes()
        single_doc.close()

        new_file_name = f"{int(_time.time())}-p{i + 1}-{safe_name}.pdf"
        new_path = f"{organisation_id}/invoices/{new_file_name}"

        supabase.storage.from_("invoices").upload(
            new_path,
            page_bytes,
            {"content-type": "application/pdf"},
        )

        new_raw_result = (
            supabase.from_("invoices_raw")
            .insert({
                "organisation_id": organisation_id,
                "file_path": new_path,
                "file_name": new_file_name,
                "file_type": "application/pdf",
                "parse_status": "pending",
                "upload_status": "uploaded",
            })
            .execute()
        )
        new_raw_ids.append(new_raw_result.data[0]["id"])

    doc.close()

    # Step 4 — delete original record (same cascade order as merge)
    extracted_rows = (
        supabase.from_("invoices_extracted")
        .select("id")
        .eq("invoice_raw_id", raw_id)
        .execute()
    ).data or []
    extracted_ids = [r["id"] for r in extracted_rows]

    if extracted_ids:
        supabase.from_("invoice_line_items").delete().in_("invoice_extracted_id", extracted_ids).execute()
        try:
            supabase.from_("invoice_extraction_feedback").delete().in_("invoice_extracted_id", extracted_ids).execute()
        except Exception:
            pass
        supabase.from_("invoices_extracted").delete().eq("invoice_raw_id", raw_id).execute()

    supabase.from_("invoice_parse_attempts").delete().eq("invoice_raw_id", raw_id).execute()
    supabase.from_("document_pages").delete().eq("invoice_raw_id", raw_id).execute()
    supabase.from_("invoice_audit_events").delete().eq("invoice_raw_id", raw_id).execute()

    try:
        supabase.storage.from_("invoices").remove([row["file_path"]])
    except Exception:
        pass

    supabase.from_("invoices_raw").delete().eq("id", raw_id).execute()

    # Step 5 — queue extraction for all new records and drain the worker
    for new_id in new_raw_ids:
        queue_invoice_job(invoice_raw_id=new_id, organisation_id=organisation_id)
    background_tasks.add_task(run_extract_worker_until_empty)

    return {"success": True, "page_count": page_count, "new_raw_ids": new_raw_ids}


class _PageCropModel(BaseModel):
    x: float
    y: float
    w: float
    h: float


class _PageRefModel(BaseModel):
    kind: str               # "full" | "crop"
    page_number: int        # 1-indexed
    crop: _PageCropModel | None = None


class _PageGroupModel(BaseModel):
    pages: list[_PageRefModel]


class ProcessPageGroupsPayload(BaseModel):
    invoice_raw_id: str
    organisation_id: str
    groups: list[_PageGroupModel]


@router.post("/process-page-groups")
def process_page_groups(payload: ProcessPageGroupsPayload, background_tasks: BackgroundTasks, auth: UserAuth):
    """
    Split a multi-page PDF into one output PDF per group, where each group is a user-defined
    set of full pages and/or cropped regions.  Supports both "each page is a doc" and
    "multiple docs on one page" scenarios.  Original record is deleted after splitting.
    """
    import time as _time

    if not payload.groups:
        raise HTTPException(status_code=400, detail="At least one group is required")
    _load_raw_invoice_for_auth(
        payload.invoice_raw_id,
        auth,
        requested_org_id=payload.organisation_id,
        write=True,
    )

    # Step 1 — fetch original raw record
    row_result = (
        supabase.from_("invoices_raw")
        .select("id, file_path, file_name, file_type")
        .eq("id", payload.invoice_raw_id)
        .eq("organisation_id", payload.organisation_id)
        .limit(1)
        .execute()
    )
    if not row_result.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    row = row_result.data[0]

    # Step 2 — download and open original PDF
    file_bytes = supabase.storage.from_("invoices").download(row["file_path"])
    doc = fitz.open(stream=file_bytes, filetype="pdf")

    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", row.get("file_name") or "split")
    if safe_name.lower().endswith(".pdf"):
        safe_name = safe_name[:-4]

    # Step 3 — build one output PDF per group
    new_raw_ids: list[str] = []
    for group_idx, group in enumerate(payload.groups):
        out_doc = fitz.open()
        for ref in group.pages:
            page_index = ref.page_number - 1   # 1-indexed → 0-indexed
            if page_index < 0 or page_index >= len(doc):
                continue
            src_page = doc[page_index]
            if ref.kind == "full":
                tmp = fitz.open()
                tmp.insert_pdf(doc, from_page=page_index, to_page=page_index)
                out_doc.insert_pdf(tmp)
                tmp.close()
            elif ref.kind == "crop" and ref.crop:
                c = ref.crop
                rect = fitz.Rect(
                    src_page.rect.width  * c.x,
                    src_page.rect.height * c.y,
                    src_page.rect.width  * (c.x + c.w),
                    src_page.rect.height * (c.y + c.h),
                )
                pix = src_page.get_pixmap(clip=rect, dpi=200)
                img_doc = fitz.open()
                img_page = img_doc.new_page(width=pix.width, height=pix.height)
                img_page.insert_image(img_page.rect, pixmap=pix)
                out_doc.insert_pdf(img_doc)
                img_doc.close()

        if len(out_doc) == 0:
            out_doc.close()
            continue

        out_bytes = out_doc.tobytes()
        out_doc.close()

        new_file_name = f"{int(_time.time())}-g{group_idx + 1}-{safe_name}.pdf"
        new_path = f"{payload.organisation_id}/invoices/{new_file_name}"
        supabase.storage.from_("invoices").upload(
            new_path, out_bytes, {"content-type": "application/pdf"}
        )
        new_raw_result = (
            supabase.from_("invoices_raw")
            .insert({
                "organisation_id": payload.organisation_id,
                "file_path": new_path,
                "file_name": new_file_name,
                "file_type": "application/pdf",
                "parse_status": "pending",
                "upload_status": "uploaded",
            })
            .execute()
        )
        new_raw_ids.append(new_raw_result.data[0]["id"])

    doc.close()

    # Step 4 — queue extraction for all new records (before deleting original,
    # so a queue failure leaves the original intact and the user can retry)
    for new_id in new_raw_ids:
        queue_invoice_job(invoice_raw_id=new_id, organisation_id=payload.organisation_id)
    background_tasks.add_task(run_extract_worker_until_empty)

    # Step 5 — delete original record now that all new records are safely queued
    extracted_rows = (
        supabase.from_("invoices_extracted")
        .select("id")
        .eq("invoice_raw_id", payload.invoice_raw_id)
        .execute()
    ).data or []
    extracted_ids = [r["id"] for r in extracted_rows]

    if extracted_ids:
        supabase.from_("invoice_line_items").delete().in_("invoice_extracted_id", extracted_ids).execute()
        try:
            supabase.from_("invoice_extraction_feedback").delete().in_("invoice_extracted_id", extracted_ids).execute()
        except Exception:
            pass
        supabase.from_("invoices_extracted").delete().eq("invoice_raw_id", payload.invoice_raw_id).execute()

    supabase.from_("invoice_parse_attempts").delete().eq("invoice_raw_id", payload.invoice_raw_id).execute()
    supabase.from_("document_pages").delete().eq("invoice_raw_id", payload.invoice_raw_id).execute()
    supabase.from_("invoice_audit_events").delete().eq("invoice_raw_id", payload.invoice_raw_id).execute()

    try:
        supabase.storage.from_("invoices").remove([row["file_path"]])
    except Exception:
        pass

    supabase.from_("invoices_raw").delete().eq("id", payload.invoice_raw_id).execute()

    return {"success": True, "group_count": len(new_raw_ids), "new_raw_ids": new_raw_ids}


@router.post("/generate-preview")
def generate_invoice_preview(req: GeneratePreviewRequest, auth: UserAuth):
    """
    Render preview images for an invoice without running VLM extraction (~1s).
    Saves images to Supabase Storage and upserts document_pages rows.
    Fixes missing previews for old invoices and PDFs processed via the selectable-text path.
    """
    import io as _io
    from PIL import Image as _Image
    from app.services.invoice_ocr_pipeline import pdf_to_images
    from app.services.invoice_extraction.receipt_preprocessing import generate_preview_images
    from app.services.invoice_previews import upload_invoice_preview_image

    raw_res = supabase.table("invoices_raw").select("file_path, file_type, organisation_id").eq("id", req.invoice_raw_id).single().execute()
    if not raw_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    raw = raw_res.data
    if str(raw.get("organisation_id")) != str(req.organisation_id):
        raise HTTPException(status_code=400, detail="Invoice does not belong to organisation_id")
    _require_org_write(auth, req.organisation_id)
    file_path = raw.get("file_path")
    if not file_path:
        raise HTTPException(status_code=400, detail="No file_path on invoices_raw record")

    try:
        file_bytes = supabase.storage.from_("invoices").download(file_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Storage download failed: {e}")

    file_type = raw.get("file_type") or "application/pdf"
    is_pdf = "pdf" in str(file_type).lower()

    try:
        if is_pdf:
            images = pdf_to_images(file_bytes)
        else:
            images = [_Image.open(_io.BytesIO(file_bytes)).convert("RGB")]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Page rendering failed: {e}")

    results = []
    for i, img in enumerate(images, 1):
        try:
            previews = generate_preview_images(img, img)
            orig_path = f"{req.organisation_id}/invoices/previews/{req.invoice_raw_id}/page-{i}-original.jpg"
            proc_path = f"{req.organisation_id}/invoices/previews/{req.invoice_raw_id}/page-{i}-processed.jpg"
            upload_invoice_preview_image(supabase, storage_path=orig_path, image=previews.original_preview)
            upload_invoice_preview_image(supabase, storage_path=proc_path, image=previews.processed_preview)
            results.append({
                "page_number": i,
                "original_preview_path": orig_path,
                "processed_preview_path": proc_path,
            })
        except Exception:
            logger.exception("[generate-preview] page %d upload failed", i)

    # Upsert document_pages rows
    for page in results:
        existing = supabase.table("document_pages").select("id").eq("invoice_raw_id", req.invoice_raw_id).eq("page_number", page["page_number"]).execute().data
        if existing:
            supabase.table("document_pages").update({
                "original_preview_path": page["original_preview_path"],
                "processed_preview_path": page["processed_preview_path"],
            }).eq("invoice_raw_id", req.invoice_raw_id).eq("page_number", page["page_number"]).execute()
        else:
            supabase.table("document_pages").insert({
                "invoice_raw_id": req.invoice_raw_id,
                "organisation_id": req.organisation_id,
                "page_number": page["page_number"],
                "original_preview_path": page["original_preview_path"],
                "processed_preview_path": page["processed_preview_path"],
            }).execute()

    # Update invoices_raw with page-1 preview path
    if results:
        supabase.table("invoices_raw").update({
            "preview_path": results[0]["original_preview_path"],
            "processed_preview_path": results[0]["processed_preview_path"],
            "updated_at": utc_now_iso(),
        }).eq("id", req.invoice_raw_id).execute()

    return {"generated": len(results), "pages": results}
