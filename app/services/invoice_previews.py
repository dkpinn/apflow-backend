from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Optional

from PIL import Image


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def image_to_jpeg_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=86, optimize=True)
    return buffer.getvalue()


def upload_invoice_preview_image(
    supabase,
    *,
    storage_path: str,
    image: Image.Image,
) -> str:
    """
    Upload a generated preview image into the invoices storage bucket.

    Supabase Storage client versions vary slightly, so this helper attempts an
    upsert-style upload and falls back to update/upload combinations.
    """
    image_bytes = image_to_jpeg_bytes(image)
    bucket = supabase.storage.from_("invoices")
    file_options = {
        "content-type": "image/jpeg",
        "cache-control": "3600",
        "x-upsert": "true",
    }

    attempts = [
        lambda: bucket.upload(storage_path, image_bytes, file_options),
        lambda: bucket.upload(storage_path, image_bytes, file_options=file_options),
        lambda: bucket.update(storage_path, image_bytes, file_options),
        lambda: bucket.update(storage_path, image_bytes, file_options=file_options),
    ]

    last_error: Optional[Exception] = None
    for attempt in attempts:
        try:
            attempt()
            return storage_path
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"Preview upload failed: {str(last_error)}")


def persist_preview_artifacts(
    supabase,
    *,
    organisation_id: str,
    invoice_raw_id: str,
    text_result: dict,
) -> dict:
    """
    Store original and processed OCR preview images for ALL pages, then attach
    storage paths back onto each page dict in text_result so _capture_document_pages
    can persist them to the document_pages table.
    """
    pages = text_result.get("pages") or []
    if not pages:
        return {}

    result: dict = {}

    for page in pages:
        page_number = page.get("page_number") or 1
        original_image = page.get("original_preview_image")
        processed_image = page.get("processed_preview_image")
        if not original_image and not processed_image:
            continue

        try:
            if original_image:
                original_path = upload_invoice_preview_image(
                    supabase,
                    storage_path=f"{organisation_id}/invoices/previews/{invoice_raw_id}/page-{page_number}-original.jpg",
                    image=original_image,
                )
                page["original_preview_path"] = original_path
                if page_number == 1:
                    text_result["original_preview_path"] = original_path
                    result["preview_path"] = original_path

            if processed_image:
                processed_path = upload_invoice_preview_image(
                    supabase,
                    storage_path=f"{organisation_id}/invoices/previews/{invoice_raw_id}/page-{page_number}-processed.jpg",
                    image=processed_image,
                )
                page["processed_preview_path"] = processed_path
                if page_number == 1:
                    text_result["processed_preview_path"] = processed_path
                    result["processed_preview_path"] = processed_path

        except Exception as exc:
            print(f"PREVIEW ARTIFACT STORAGE FAILED (page {page_number}):", str(exc))
            result["error"] = str(exc)

    update_payload = {"updated_at": utc_now_iso()}
    if result.get("preview_path"):
        update_payload["preview_path"] = result["preview_path"]
    if result.get("processed_preview_path"):
        update_payload["processed_preview_path"] = result["processed_preview_path"]
    if len(update_payload) > 1:
        try:
            supabase.table("invoices_raw").update(update_payload).eq("id", invoice_raw_id).execute()
        except Exception as exc:
            print("INVOICES_RAW PREVIEW UPDATE FAILED:", str(exc))

    return result
