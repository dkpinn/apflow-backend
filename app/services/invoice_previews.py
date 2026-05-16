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
    Store page-1 original and processed OCR preview images, then attach their
    storage paths back onto the text_result page metadata.
    """
    pages = text_result.get("pages") or []
    page_one = next((page for page in pages if (page.get("page_number") or 1) == 1), pages[0] if pages else None)
    if not page_one:
        return {}

    original_image = page_one.get("original_preview_image")
    processed_image = page_one.get("processed_preview_image")
    if not original_image and not processed_image:
        return {}

    result: dict = {}

    try:
        if original_image:
            original_path = upload_invoice_preview_image(
                supabase,
                storage_path=f"{organisation_id}/invoices/previews/{invoice_raw_id}/page-1-original.jpg",
                image=original_image,
            )
            page_one["original_preview_path"] = original_path
            text_result["original_preview_path"] = original_path
            result["preview_path"] = original_path

        if processed_image:
            processed_path = upload_invoice_preview_image(
                supabase,
                storage_path=f"{organisation_id}/invoices/previews/{invoice_raw_id}/page-1-processed.jpg",
                image=processed_image,
            )
            page_one["processed_preview_path"] = processed_path
            text_result["processed_preview_path"] = processed_path
            result["processed_preview_path"] = processed_path

        if result:
            update_payload = {"updated_at": utc_now_iso()}
            if result.get("preview_path"):
                update_payload["preview_path"] = result["preview_path"]
            if result.get("processed_preview_path"):
                update_payload["processed_preview_path"] = result["processed_preview_path"]

            supabase.table("invoices_raw").update(update_payload).eq("id", invoice_raw_id).execute()

    except Exception as exc:
        print("PREVIEW ARTIFACT STORAGE FAILED:", str(exc))
        result["error"] = str(exc)

    return result
