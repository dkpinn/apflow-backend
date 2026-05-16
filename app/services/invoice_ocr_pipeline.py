from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Optional
import fitz  # PyMuPDF
import os
import pytesseract

# Do not hardcode a Windows-only Tesseract path.
# Set TESSERACT_CMD in local Windows development if needed, e.g.
# C:\Program Files\Tesseract-OCR\tesseract.exe. On Linux/Render,
# Tesseract should be installed in PATH.
TESSERACT_CMD = os.getenv("TESSERACT_CMD")
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

from PIL import Image

# Keep Pillow decompression-bomb protection enabled, but avoid warnings for
# images we create intentionally from PDF pages. We render PDFs at a capped
# resolution below this limit and immediately resize image uploads for OCR.
Image.MAX_IMAGE_PIXELS = 200_000_000

OCR_RENDER_DPI = int(os.getenv("OCR_RENDER_DPI", "180"))
OCR_MAX_WIDTH = int(os.getenv("OCR_MAX_WIDTH", "2500"))
OCR_MAX_HEIGHT = int(os.getenv("OCR_MAX_HEIGHT", "3500"))
OCR_MAX_PIXELS = int(os.getenv("OCR_MAX_PIXELS", "10_000_000"))
PDF_RENDER_MAX_PIXELS = int(os.getenv("PDF_RENDER_MAX_PIXELS", "12_000_000"))
DEEP_OCR_RENDER_DPI = int(os.getenv("DEEP_OCR_RENDER_DPI", "300"))

from app.services.invoice_extraction.layout_analyser import analyse_invoice_layout

from app.services.invoice_extraction.supplier_parser import extract_supplier_name

from app.services.invoice_extraction.preprocessing import (
    preprocess_for_ocr_variants,
    preprocess_image_for_ocr,
)
from app.services.invoice_extraction.receipt_preprocessing import (
    generate_preview_images,
    preprocess_receipt_photo,
    split_deep_document_ocr_regions,
    split_receipt_ocr_regions,
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
from app.services.invoice_extraction.template_cleanups import apply_template_cleanups
# ------------------------------------------------------------
# TEXT EXTRACTION / OCR QUALITY
# ------------------------------------------------------------

from app.services.invoice_extraction.image_quality import analyse_image_quality


def _average(values: list[float | None]) -> Optional[float]:
    valid = [float(v) for v in values if v is not None]
    if not valid:
        return None
    return round(sum(valid) / len(valid), 3)


def extract_pdf_page_texts(file_bytes: bytes) -> list[dict]:
    """
    Extract selectable text per PDF page.
    """
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages: list[dict] = []

    for index, page in enumerate(doc):
        text = page.get_text("text").strip()
        pages.append({
            "page_number": index + 1,
            "page_count": len(doc),
            "method": "pdf_text",
            "ocr_used": False,
            "text": text,
            "text_length": len(text),
            "ocr_confidence": None,
            "image_quality_score": None,
            "quality_notes": [],
        })

    return pages


def extract_selectable_text_from_pdf(file_bytes: bytes) -> str:
    """
    Backwards-compatible helper returning all selectable PDF text.
    """
    pages = extract_pdf_page_texts(file_bytes)
    return "\n".join(page["text"] for page in pages if page.get("text")).strip()


def _scale_for_limits(width: float, height: float, *, max_width: int, max_height: int, max_pixels: int) -> float:
    """Return a scale <= 1.0 so rendered/resized images stay within OCR limits."""
    if width <= 0 or height <= 0:
        return 1.0

    pixels = width * height
    scale_by_width = max_width / width
    scale_by_height = max_height / height
    scale_by_pixels = (max_pixels / pixels) ** 0.5 if pixels > 0 else 1.0
    return min(scale_by_width, scale_by_height, scale_by_pixels, 1.0)


def resize_for_ocr(image: Image.Image) -> Image.Image:
    """
    Cap image dimensions before OCR.

    Large scanned PDFs/photos can render to 100M+ pixels and make Tesseract hang
    or exhaust memory. OCR does not need that much resolution for invoices, so
    keep images within a predictable size budget.
    """
    width, height = image.size
    scale = _scale_for_limits(
        float(width),
        float(height),
        max_width=OCR_MAX_WIDTH,
        max_height=OCR_MAX_HEIGHT,
        max_pixels=OCR_MAX_PIXELS,
    )

    if scale >= 1.0:
        return image

    new_width = max(1, int(width * scale))
    new_height = max(1, int(height * scale))
    return image.resize((new_width, new_height), Image.Resampling.LANCZOS)


def pdf_to_images(file_bytes: bytes, dpi: int = OCR_RENDER_DPI) -> list[Image.Image]:
    """
    Convert PDF pages to capped-size images for OCR fallback.

    We start at OCR_RENDER_DPI, then reduce the render scale per page so the
    created bitmap stays below PDF_RENDER_MAX_PIXELS and within OCR size limits.
    This prevents very large scans from hanging the backend.
    """
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    images: list[Image.Image] = []

    for page in doc:
        base_scale = dpi / 72
        rect = page.rect
        target_width = float(rect.width) * base_scale
        target_height = float(rect.height) * base_scale

        limit_scale = _scale_for_limits(
            target_width,
            target_height,
            max_width=OCR_MAX_WIDTH,
            max_height=OCR_MAX_HEIGHT,
            max_pixels=PDF_RENDER_MAX_PIXELS,
        )
        effective_scale = max(0.5, base_scale * limit_scale)

        matrix = fitz.Matrix(effective_scale, effective_scale)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        images.append(resize_for_ocr(image))

    return images


def _ocr_confidence_from_data(data: dict) -> Optional[float]:
    raw_conf = data.get("conf") or []
    values: list[float] = []

    for value in raw_conf:
        try:
            numeric = float(value)
        except Exception:
            continue
        if numeric >= 0:
            values.append(numeric)

    if not values:
        return None

    # Tesseract returns 0-100. Store as 0-1.
    return round(sum(values) / len(values) / 100.0, 3)


RECEIPT_INDICATORS = [
    "tax invoice",
    "vat",
    "total",
    "subtotal",
    "amount",
    "card",
    "cash",
    "paid",
    "change",
    "invoice no",
    "invoice number",
    "till",
    "branch",
    "builders",
    "receipt",
    "date",
    "terminal",
    "item",
    "qty",
    "price",
]

OCR_NOISE_TERMS = [
    "scan to rate",
    "survey",
    "win a voucher",
    "terms and conditions",
    "valid for 5 days",
]


def _receipt_indicator_score(text: str) -> float:
    lower = (text or "").lower()
    if not lower:
        return 0.0
    matches = sum(1 for term in RECEIPT_INDICATORS if term in lower)
    return min(matches / 6.0, 1.0)


def _ocr_noise_score(text: str) -> float:
    lower = (text or "").lower()
    if not lower:
        return 0.0
    matches = sum(1 for term in OCR_NOISE_TERMS if term in lower)
    first_lines = "\n".join(lower.splitlines()[:4])
    if "scan to rate" in first_lines:
        matches += 2
    return min(matches / 4.0, 1.0)


def _score_ocr_candidate(*, text: str, ocr_confidence: Optional[float], image_quality_score: float) -> float:
    """
    Choose OCR result by confidence, quality and receipt/invoice usefulness.
    Long survey/marketing text should not automatically win.
    """
    text_length_score = min(len(text.strip()) / 1200.0, 1.0)
    confidence_score = ocr_confidence if ocr_confidence is not None else 0.35
    indicator_score = _receipt_indicator_score(text)
    noise_score = _ocr_noise_score(text)
    score = (
        (0.42 * confidence_score)
        + (0.18 * text_length_score)
        + (0.15 * image_quality_score)
        + (0.25 * indicator_score)
        - (0.30 * noise_score)
    )
    return round(max(0.0, min(score, 1.0)), 3)


def _dedupe_ocr_lines(text_parts: list[str]) -> str:
    seen: set[str] = set()
    lines: list[str] = []

    for part in text_parts:
        for raw_line in (part or "").splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue
            key = re.sub(r"[^a-z0-9]+", "", line.lower())
            if not key or key in seen:
                continue
            seen.add(key)
            lines.append(line)

    return "\n".join(lines).strip()


def _ocr_image_region(image: Image.Image, *, region_name: str, psms: list[str] | None = None) -> dict:
    psms = psms or ["6", "4", "11", "3"]
    quality = analyse_image_quality(image)
    best: dict | None = None

    for psm in psms:
        config = f"--oem 3 --psm {psm}"
        try:
            data = pytesseract.image_to_data(
                image,
                config=config,
                output_type=pytesseract.Output.DICT,
            )
            text = "\n".join(
                part.strip()
                for part in data.get("text", [])
                if part and part.strip()
            ).strip()
            confidence = _ocr_confidence_from_data(data)
        except Exception:
            try:
                text = pytesseract.image_to_string(image, config=config).strip()
                confidence = None
            except Exception:
                text = ""
                confidence = None

        score = _score_ocr_candidate(
            text=text,
            ocr_confidence=confidence,
            image_quality_score=quality.image_quality_score,
        )
        candidate = {
            "region": region_name,
            "text": text,
            "ocr_confidence": confidence,
            "text_length": len(text),
            "ocr_psm": psm,
            "ocr_candidate_score": score,
            "receipt_indicator_score": _receipt_indicator_score(text),
            "ocr_noise_score": _ocr_noise_score(text),
        }
        if best is None or candidate["ocr_candidate_score"] > best["ocr_candidate_score"]:
            best = candidate

    return best or {
        "region": region_name,
        "text": "",
        "ocr_confidence": None,
        "text_length": 0,
        "ocr_psm": None,
        "ocr_candidate_score": 0.0,
        "receipt_indicator_score": 0.0,
        "ocr_noise_score": 0.0,
    }


def _ocr_receipt_regions(processed_image: Image.Image) -> dict:
    region_images = split_receipt_ocr_regions(processed_image)
    region_results = [
        _ocr_image_region(region.image, region_name=region.name, psms=["6", "4", "11"])
        for region in region_images
    ]

    by_name = {region["region"]: region for region in region_results}
    header = by_name.get("header_top_30_percent", {})
    middle = by_name.get("middle_40_percent", {})
    bottom = by_name.get("bottom_35_percent", {})
    full = by_name.get("full_processed", {})

    combined = _dedupe_ocr_lines([
        header.get("text") or "",
        middle.get("text") or "",
        bottom.get("text") or "",
    ])

    combined_indicator_score = _receipt_indicator_score(combined)
    combined_noise_score = _ocr_noise_score(combined)
    if (
        len(combined) < 80
        or combined_indicator_score < 0.25
        or combined_noise_score > combined_indicator_score
    ):
        combined = _dedupe_ocr_lines([combined, full.get("text") or ""])

    confidence = _average([
        header.get("ocr_confidence"),
        middle.get("ocr_confidence"),
        bottom.get("ocr_confidence"),
    ])
    if confidence is None:
        confidence = full.get("ocr_confidence")

    return {
        "strategy": "combined_regions",
        "text": combined,
        "ocr_confidence": confidence,
        "text_length": len(combined),
        "regions": region_results,
        "regions_attempted": [region["region"] for region in region_results],
        "confidence_by_region": {
            region["region"]: region.get("ocr_confidence")
            for region in region_results
        },
        "receipt_indicator_score": _receipt_indicator_score(combined),
        "ocr_noise_score": _ocr_noise_score(combined),
    }


def _upscale_deep_region(image: Image.Image) -> Image.Image:
    width, height = image.size
    if width <= 0 or height <= 0:
        return image
    upscaled = image.resize(
        (max(1, int(width * 1.7)), max(1, int(height * 1.7))),
        Image.Resampling.LANCZOS,
    )
    return resize_for_ocr(upscaled.convert("RGB"))


def _ocr_deep_document_regions(processed_image: Image.Image) -> dict:
    region_images = split_deep_document_ocr_regions(processed_image)
    region_results = [
        _ocr_image_region(
            _upscale_deep_region(region.image),
            region_name=region.name,
            psms=["6", "4", "11", "3"],
        )
        for region in region_images
    ]
    by_name = {region["region"]: region for region in region_results}

    ordered_text = [
        by_name.get("supplier_header_region", {}).get("text") or "",
        by_name.get("supplier_contact_region", {}).get("text") or "",
        by_name.get("invoice_summary_region", {}).get("text") or "",
        by_name.get("line_items_region", {}).get("text") or "",
    ]
    combined = _dedupe_ocr_lines(ordered_text)

    if len(combined) < 80:
        combined = _dedupe_ocr_lines([combined, by_name.get("full_processed", {}).get("text") or ""])

    return {
        "strategy": "deep_region_ocr",
        "text": combined,
        "text_length": len(combined),
        "regions": region_results,
        "regions_attempted": [region["region"] for region in region_results],
        "confidence_by_region": {
            region["region"]: region.get("ocr_confidence")
            for region in region_results
        },
        "region_text_by_name": {
            region["region"]: region.get("text") or ""
            for region in region_results
        },
        "ocr_confidence": _average([region.get("ocr_confidence") for region in region_results]),
    }


def _parse_deep_region_fields(region_ocr: dict) -> dict:
    region_text = region_ocr.get("region_text_by_name") or {}
    combined_text = region_ocr.get("text") or ""
    parsed = parse_invoice_fields(combined_text)

    supplier_header_text = region_text.get("supplier_header_region") or ""
    supplier_contact_text = region_text.get("supplier_contact_region") or ""
    invoice_summary_text = region_text.get("invoice_summary_region") or ""
    line_items_text = region_text.get("line_items_region") or ""

    header_supplier = extract_supplier_name(supplier_header_text)
    if header_supplier:
        parsed["supplier_name_extracted"] = header_supplier
    else:
        combined_supplier = extract_supplier_name(combined_text)
        if combined_supplier:
            parsed["supplier_name_extracted"] = combined_supplier

    header_address = extract_supplier_delivery_address(supplier_header_text)
    if header_address:
        parsed["supplier_del_address_extracted"] = header_address
    else:
        combined_address = extract_supplier_delivery_address(combined_text)
        if combined_address:
            parsed["supplier_del_address_extracted"] = combined_address

    contact_email = extract_supplier_email(supplier_contact_text)
    if contact_email:
        parsed["supplier_email_extracted"] = contact_email

    contact_accounting_email = extract_supplier_accounting_email(supplier_contact_text)
    if contact_accounting_email:
        parsed["supplier_acc_email_extracted"] = contact_accounting_email

    contact_telephone = extract_supplier_telephone(supplier_contact_text)
    if contact_telephone:
        parsed["supplier_telephone_extracted"] = contact_telephone

    contact_fax = extract_supplier_fax(supplier_contact_text)
    if contact_fax:
        parsed["supplier_fax_extracted"] = contact_fax

    contact_vat = extract_vat_number(supplier_contact_text)
    if contact_vat:
        parsed["vat_number_extracted"] = contact_vat

    summary_invoice_number = extract_invoice_number(invoice_summary_text)
    if summary_invoice_number:
        parsed["invoice_number"] = summary_invoice_number

    summary_customer_code = extract_customer_code(invoice_summary_text)
    if summary_customer_code:
        parsed["cus_code_extracted"] = summary_customer_code

    summary_date = extract_invoice_date(invoice_summary_text)
    if summary_date:
        parsed["invoice_date"] = summary_date

    line_items = extract_line_items(line_items_text)
    if line_items:
        parsed["line_items"] = line_items

    parsed = apply_template_cleanups(combined_text, parsed)
    parsed["confidence_score"] = calculate_confidence(parsed)
    return parsed


def deep_extract_text_with_regions(file_bytes: bytes, file_type: Optional[str] = None) -> dict:
    is_pdf = (
        file_type == "application/pdf"
        or file_type is None
        or file_type == ""
        or str(file_type).lower().endswith("pdf")
    )

    try:
        if is_pdf:
            images = pdf_to_images(file_bytes, dpi=DEEP_OCR_RENDER_DPI)
        else:
            images = [Image.open(io.BytesIO(file_bytes))]
    except Exception:
        images = [Image.open(io.BytesIO(file_bytes))]

    image = resize_for_ocr(images[0].convert("RGB"))
    receipt_preprocessing = preprocess_receipt_photo(image)
    region_ocr = _ocr_deep_document_regions(receipt_preprocessing.processed_image)
    parsed = _parse_deep_region_fields(region_ocr)

    return {
        "method": "deep_region_ocr",
        "ocr_used": True,
        "text": region_ocr.get("text") or "",
        "page_count": len(images) or 1,
        "ocr_confidence": region_ocr.get("ocr_confidence"),
        "regions_attempted": region_ocr.get("regions_attempted") or [],
        "confidence_by_region": region_ocr.get("confidence_by_region") or {},
        "region_ocr": region_ocr,
        "parsed_data": parsed,
        "preprocessing_notes": receipt_preprocessing.preprocessing_notes + [
            "deep_region_ocr_applied",
            f"deep_region_ocr_dpi={DEEP_OCR_RENDER_DPI}",
        ],
    }


def ocr_image_detailed(image: Image.Image, page_number: int = 1, page_count: int = 1) -> dict:
    """
    OCR one image using multiple preprocessing variants and PSM modes.
    Returns text, selected variant, confidence and image quality metadata.
    """
    original_size = image.size
    image = resize_for_ocr(image.convert("RGB"))
    resized_size = image.size

    original_quality = analyse_image_quality(image)
    receipt_preprocessing = preprocess_receipt_photo(image)
    preview_images = generate_preview_images(image, receipt_preprocessing.processed_image)
    receipt_region_ocr = _ocr_receipt_regions(receipt_preprocessing.processed_image)
    receipt_region_notes = [
        "receipt_regions_ocr_applied",
        f"receipt_region_header_ocr_confidence={receipt_region_ocr.get('confidence_by_region', {}).get('header_top_30_percent')}",
        f"receipt_region_middle_ocr_confidence={receipt_region_ocr.get('confidence_by_region', {}).get('middle_40_percent')}",
        f"receipt_region_bottom_ocr_confidence={receipt_region_ocr.get('confidence_by_region', {}).get('bottom_35_percent')}",
        "receipt_region_selected_strategy=combined_regions",
    ]

    variants = [
        ("receipt_processed_crop", receipt_preprocessing.processed_image),
        *preprocess_for_ocr_variants(image),
    ]
    variants = [
        (variant_name, resize_for_ocr(processed_image.convert("RGB")))
        for variant_name, processed_image in variants
    ]

    region_quality = analyse_image_quality(receipt_preprocessing.processed_image)
    region_score = _score_ocr_candidate(
        text=receipt_region_ocr.get("text") or "",
        ocr_confidence=receipt_region_ocr.get("ocr_confidence"),
        image_quality_score=region_quality.image_quality_score,
    )
    if receipt_region_ocr.get("receipt_indicator_score", 0) >= 0.25:
        region_score = round(min(region_score + 0.08, 1.0), 3)

    best: dict | None = {
        "page_number": page_number,
        "page_count": page_count,
        "method": "ocr",
        "ocr_used": True,
        "text": receipt_region_ocr.get("text") or "",
        "text_length": receipt_region_ocr.get("text_length") or 0,
        "ocr_confidence": receipt_region_ocr.get("ocr_confidence"),
        "image_quality_score": region_quality.image_quality_score,
        "quality_notes": sorted(set(
            original_quality.notes
            + region_quality.notes
            + receipt_preprocessing.preprocessing_notes
            + receipt_region_notes
        )),
        "ocr_variant": "receipt_regions_combined",
        "ocr_psm": None,
        "ocr_candidate_score": region_score,
        "crop_applied": receipt_preprocessing.crop_applied,
        "deskew_applied": receipt_preprocessing.deskew_applied,
        "preprocessing_notes": receipt_preprocessing.preprocessing_notes + receipt_region_notes,
        "crop_box": receipt_preprocessing.crop_box,
        "receipt_region_ocr": receipt_region_ocr,
        "original_image_size": {"width": original_size[0], "height": original_size[1]},
        "resized_image_size": {"width": resized_size[0], "height": resized_size[1]},
        "original_image_quality": original_quality.as_dict(),
        "processed_image_quality": region_quality.as_dict(),
        "receipt_preprocessing_quality": receipt_preprocessing.image_quality,
        "original_preview_image": preview_images.original_preview if page_number == 1 else None,
        "processed_preview_image": preview_images.processed_preview if page_number == 1 else None,
    }

    for variant_name, processed_image in variants:
        quality = analyse_image_quality(processed_image)

        for psm in ["6", "4", "11", "3"]:
            config = f"--oem 3 --psm {psm}"
            try:
                data = pytesseract.image_to_data(
                    processed_image,
                    config=config,
                    output_type=pytesseract.Output.DICT,
                )
                text = "\n".join(
                    part.strip()
                    for part in data.get("text", [])
                    if part and part.strip()
                ).strip()
                confidence = _ocr_confidence_from_data(data)
            except Exception:
                try:
                    text = pytesseract.image_to_string(processed_image, config=config).strip()
                    confidence = None
                except Exception:
                    text = ""
                    confidence = None

            score = _score_ocr_candidate(
                text=text,
                ocr_confidence=confidence,
                image_quality_score=quality.image_quality_score,
            )
            if variant_name == "receipt_processed_crop":
                score = round(min(score + 0.04, 1.0), 3)

            candidate = {
                "page_number": page_number,
                "page_count": page_count,
                "method": "ocr",
                "ocr_used": True,
                "text": text,
                "text_length": len(text),
                "ocr_confidence": confidence,
                "image_quality_score": quality.image_quality_score,
                "quality_notes": sorted(set(
                    original_quality.notes
                    + quality.notes
                    + receipt_preprocessing.preprocessing_notes
                    + receipt_region_notes
                )),
                "ocr_variant": variant_name,
                "ocr_psm": psm,
                "ocr_candidate_score": score,
                "crop_applied": receipt_preprocessing.crop_applied,
                "deskew_applied": receipt_preprocessing.deskew_applied,
                "preprocessing_notes": receipt_preprocessing.preprocessing_notes + receipt_region_notes,
                "crop_box": receipt_preprocessing.crop_box,
                "receipt_region_ocr": receipt_region_ocr,
                "original_image_size": {"width": original_size[0], "height": original_size[1]},
                "resized_image_size": {"width": resized_size[0], "height": resized_size[1]},
                "original_image_quality": original_quality.as_dict(),
                "processed_image_quality": quality.as_dict(),
                "receipt_preprocessing_quality": receipt_preprocessing.image_quality,
                "original_preview_image": preview_images.original_preview if page_number == 1 else None,
                "processed_preview_image": preview_images.processed_preview if page_number == 1 else None,
            }

            if candidate["ocr_candidate_score"] > best["ocr_candidate_score"]:
                best = candidate

    return best or {
        "page_number": page_number,
        "page_count": page_count,
        "method": "ocr",
        "ocr_used": True,
        "text": "",
        "text_length": 0,
        "ocr_confidence": None,
        "image_quality_score": original_quality.image_quality_score,
        "quality_notes": sorted(set(original_quality.notes + receipt_preprocessing.preprocessing_notes)),
        "ocr_variant": None,
        "ocr_psm": None,
        "ocr_candidate_score": 0.0,
        "crop_applied": receipt_preprocessing.crop_applied,
        "deskew_applied": receipt_preprocessing.deskew_applied,
        "preprocessing_notes": receipt_preprocessing.preprocessing_notes,
        "crop_box": receipt_preprocessing.crop_box,
        "original_image_size": {"width": original_size[0], "height": original_size[1]},
        "resized_image_size": {"width": resized_size[0], "height": resized_size[1]},
        "original_image_quality": original_quality.as_dict(),
        "processed_image_quality": receipt_preprocessing.image_quality,
        "receipt_preprocessing_quality": receipt_preprocessing.image_quality,
        "original_preview_image": preview_images.original_preview if page_number == 1 else None,
        "processed_preview_image": preview_images.processed_preview if page_number == 1 else None,
    }


def ocr_image(image: Image.Image) -> str:
    """
    Backwards-compatible helper returning only OCR text.
    """
    return ocr_image_detailed(image).get("text") or ""


def extract_text_with_fallback(file_bytes: bytes, file_type: Optional[str] = None) -> dict:
    """
    Extraction priority:
    1. Use selectable PDF text if it is sufficiently useful.
    2. OCR PDF pages when selectable text is poor/missing.
    3. OCR image uploads directly.

    Returns document-level text plus per-page quality/OCR metadata.
    """
    is_pdf = (
        file_type == "application/pdf"
        or file_type is None
        or file_type == ""
        or str(file_type).lower().endswith("pdf")
    )

    if is_pdf:
        try:
            pdf_pages = extract_pdf_page_texts(file_bytes)
            selectable_text = "\n".join(page["text"] for page in pdf_pages if page.get("text")).strip()
            pages_with_text = sum(1 for page in pdf_pages if len((page.get("text") or "").strip()) >= 80)

            if len(selectable_text) >= 80 and pages_with_text >= 1:
                return {
                    "method": "pdf_text",
                    "ocr_used": False,
                    "text": selectable_text,
                    "page_count": len(pdf_pages),
                    "pages": pdf_pages,
                    "ocr_confidence": None,
                    "image_quality_score": None,
                    "quality_notes": [],
                }
        except Exception:
            pdf_pages = []

    try:
        if is_pdf:
            images = pdf_to_images(file_bytes)
        else:
            images = [Image.open(io.BytesIO(file_bytes))]
    except Exception:
        images = [Image.open(io.BytesIO(file_bytes))]

    page_count = len(images) or 1
    ocr_pages: list[dict] = []

    for index, image in enumerate(images):
        ocr_pages.append(ocr_image_detailed(image, page_number=index + 1, page_count=page_count))

    combined_text = "\n\n".join(page.get("text") or "" for page in ocr_pages).strip()
    avg_ocr_confidence = _average([page.get("ocr_confidence") for page in ocr_pages])
    avg_image_quality = _average([page.get("image_quality_score") for page in ocr_pages])
    notes = sorted({note for page in ocr_pages for note in (page.get("quality_notes") or [])})

    return {
        "method": "ocr",
        "ocr_used": True,
        "text": combined_text,
        "page_count": page_count,
        "pages": ocr_pages,
        "ocr_confidence": avg_ocr_confidence,
        "image_quality_score": avg_image_quality,
        "quality_notes": notes,
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
        r"\b\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2}\b",
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
    parsed = apply_template_cleanups(text, parsed)
    parsed["confidence_score"] = calculate_confidence(parsed)

    return parsed
