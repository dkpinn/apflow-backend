from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import cv2
import fitz  # PyMuPDF
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from app.services.invoice_extraction.image_quality import analyse_image_quality


RECEIPT_OCR_MAX_WIDTH = int(os.getenv("RECEIPT_OCR_MAX_WIDTH", os.getenv("OCR_MAX_WIDTH", "2500")))
RECEIPT_OCR_MAX_HEIGHT = int(os.getenv("RECEIPT_OCR_MAX_HEIGHT", os.getenv("OCR_MAX_HEIGHT", "3500")))
RECEIPT_OCR_MAX_PIXELS = int(os.getenv("RECEIPT_OCR_MAX_PIXELS", os.getenv("OCR_MAX_PIXELS", "10_000_000")))
RECEIPT_RENDER_MAX_PIXELS = int(os.getenv("RECEIPT_RENDER_MAX_PIXELS", os.getenv("PDF_RENDER_MAX_PIXELS", "12_000_000")))
PREVIEW_MAX_WIDTH = int(os.getenv("PREVIEW_MAX_WIDTH", "1400"))
PREVIEW_MAX_HEIGHT = int(os.getenv("PREVIEW_MAX_HEIGHT", "1800"))
PREVIEW_MAX_PIXELS = int(os.getenv("PREVIEW_MAX_PIXELS", "2_500_000"))


@dataclass
class ReceiptPreprocessingResult:
    processed_image: Image.Image
    crop_applied: bool
    deskew_applied: bool
    preprocessing_notes: list[str]
    image_quality: dict[str, Any]
    original_image_quality: dict[str, Any]
    crop_box: Optional[dict[str, int]] = None

    def notes_text(self) -> str:
        return "; ".join(self.preprocessing_notes)


@dataclass
class PreviewImages:
    original_preview: Image.Image
    processed_preview: Image.Image


@dataclass
class ReceiptOcrRegion:
    name: str
    image: Image.Image


def _scale_for_limits(width: float, height: float, *, max_width: int, max_height: int, max_pixels: int) -> float:
    if width <= 0 or height <= 0:
        return 1.0

    pixels = width * height
    scale_by_width = max_width / width
    scale_by_height = max_height / height
    scale_by_pixels = (max_pixels / pixels) ** 0.5 if pixels > 0 else 1.0
    return min(scale_by_width, scale_by_height, scale_by_pixels, 1.0)


def _resize_to_limits(image: Image.Image, *, max_width: int, max_height: int, max_pixels: int) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGB")
    width, height = image.size
    scale = _scale_for_limits(
        float(width),
        float(height),
        max_width=max_width,
        max_height=max_height,
        max_pixels=max_pixels,
    )

    if scale >= 1.0:
        return image

    return image.resize(
        (max(1, int(width * scale)), max(1, int(height * scale))),
        Image.Resampling.LANCZOS,
    )


def resize_for_ocr(image: Image.Image) -> Image.Image:
    """
    Cap receipt/photo dimensions before OCR so large camera images cannot hang
    Tesseract or exhaust memory.
    """
    return _resize_to_limits(
        image,
        max_width=RECEIPT_OCR_MAX_WIDTH,
        max_height=RECEIPT_OCR_MAX_HEIGHT,
        max_pixels=RECEIPT_OCR_MAX_PIXELS,
    )


def _resize_for_preview(image: Image.Image) -> Image.Image:
    return _resize_to_limits(
        image,
        max_width=PREVIEW_MAX_WIDTH,
        max_height=PREVIEW_MAX_HEIGHT,
        max_pixels=PREVIEW_MAX_PIXELS,
    )


def render_pdf_page_to_image(file_path_or_bytes: str | Path | bytes, page_number: int, dpi: int = 180) -> Image.Image:
    """
    Render one PDF page to a bounded RGB image.

    page_number is one-based to match document_pages.page_number.
    """
    if isinstance(file_path_or_bytes, (str, Path)):
        doc = fitz.open(str(file_path_or_bytes))
    else:
        doc = fitz.open(stream=file_path_or_bytes, filetype="pdf")

    if len(doc) == 0:
        raise ValueError("PDF contains no pages")

    page_index = max(0, min(page_number - 1, len(doc) - 1))
    page = doc[page_index]

    base_scale = dpi / 72
    target_width = float(page.rect.width) * base_scale
    target_height = float(page.rect.height) * base_scale
    limit_scale = _scale_for_limits(
        target_width,
        target_height,
        max_width=RECEIPT_OCR_MAX_WIDTH,
        max_height=RECEIPT_OCR_MAX_HEIGHT,
        max_pixels=RECEIPT_RENDER_MAX_PIXELS,
    )

    matrix = fitz.Matrix(max(0.5, base_scale * limit_scale), max(0.5, base_scale * limit_scale))
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    return resize_for_ocr(image)


def _pil_to_bgr(image: Image.Image) -> np.ndarray:
    rgb = np.array(image.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _cv_to_pil(image: np.ndarray) -> Image.Image:
    if len(image.shape) == 2:
        return Image.fromarray(image)
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


def _adaptive_block_size(width: int, height: int, preferred: int = 51) -> int:
    largest = max(3, min(width, height))
    block_size = min(preferred, largest if largest % 2 == 1 else largest - 1)
    return max(3, block_size)


def _scale_crop_box(
    box: tuple[int, int, int, int],
    *,
    scale_x: float,
    scale_y: float,
    image_width: int,
    image_height: int,
    padding: int,
) -> tuple[int, int, int, int]:
    x, y, w, h = box
    x1 = max(0, int((x * scale_x) - padding))
    y1 = max(0, int((y * scale_y) - padding))
    x2 = min(image_width, int(((x + w) * scale_x) + padding))
    y2 = min(image_height, int(((y + h) * scale_y) + padding))
    return x1, y1, x2, y2


def find_document_or_receipt_crop(image: Image.Image) -> Optional[tuple[int, int, int, int]]:
    """
    Find a likely document/receipt bounding box in a photographed page.

    Returns an (x1, y1, x2, y2) crop box in original image coordinates, or None
    when the detector is not confident. The caller should fall back to the full
    resized image when None is returned.
    """
    image = resize_for_ocr(image)
    width, height = image.size

    analysis = _resize_to_limits(image, max_width=1200, max_height=1200, max_pixels=1_200_000)
    analysis_width, analysis_height = analysis.size
    scale_x = width / analysis_width
    scale_y = height / analysis_height

    bgr = _pil_to_bgr(analysis)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    candidates: list[tuple[int, int, int, int, float]] = []

    edges = cv2.Canny(blurred, 50, 150)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = list(contours)

    block_size = _adaptive_block_size(analysis_width, analysis_height)
    thresholded = cv2.adaptiveThreshold(
        blurred,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        block_size,
        11,
    )
    thresholded = cv2.morphologyEx(thresholded, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8), iterations=2)
    text_contours, _ = cv2.findContours(thresholded, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours.extend(text_contours)

    analysis_area = float(analysis_width * analysis_height)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area_ratio = (w * h) / analysis_area
        if area_ratio < 0.05 or area_ratio > 0.96:
            continue
        if w < analysis_width * 0.18 or h < analysis_height * 0.18:
            continue

        aspect = h / max(w, 1)
        aspect_bonus = 1.15 if aspect >= 1.1 else 1.0
        candidates.append((x, y, w, h, area_ratio * aspect_bonus))

    if not candidates:
        return None

    x, y, w, h, _ = max(candidates, key=lambda candidate: candidate[4])
    padding = max(16, int(min(width, height) * 0.025))
    x1, y1, x2, y2 = _scale_crop_box(
        (x, y, w, h),
        scale_x=scale_x,
        scale_y=scale_y,
        image_width=width,
        image_height=height,
        padding=padding,
    )

    crop_width = x2 - x1
    crop_height = y2 - y1
    if crop_width <= 0 or crop_height <= 0:
        return None

    crop_area_ratio = (crop_width * crop_height) / float(width * height)
    if crop_area_ratio > 0.94 or crop_area_ratio < 0.04:
        return None

    return x1, y1, x2, y2


def crop_receipt_region(image: Image.Image) -> tuple[Image.Image, bool, Optional[dict[str, int]], list[str]]:
    resized = resize_for_ocr(image)
    notes: list[str] = []
    crop_box = find_document_or_receipt_crop(resized)

    if not crop_box:
        notes.append("receipt_crop_not_detected_using_full_image")
        return resized, False, None, notes

    x1, y1, x2, y2 = crop_box
    cropped = resized.crop(crop_box)
    notes.append("receipt_crop_applied")
    return cropped, True, {"x1": x1, "y1": y1, "x2": x2, "y2": y2}, notes


def _detect_skew_angle(gray: np.ndarray) -> Optional[float]:
    threshold = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(threshold > 0))
    if coords.size == 0:
        return None

    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle

    if abs(angle) < 0.5 or abs(angle) > 12:
        return None

    return float(angle)


def deskew_image(image: Image.Image) -> tuple[Image.Image, bool, Optional[float]]:
    gray = np.array(image.convert("L"))
    angle = _detect_skew_angle(gray)
    if angle is None:
        return image, False, None

    height, width = gray.shape[:2]
    center = (width // 2, height // 2)
    rotation_matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        _pil_to_bgr(image),
        rotation_matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return _cv_to_pil(rotated).convert("RGB"), True, round(angle, 2)


def enhance_for_receipt_ocr(image: Image.Image) -> Image.Image:
    """
    Produce a high-contrast receipt-friendly OCR image while preserving enough
    grayscale detail for faint thermal-printer text.
    """
    image = resize_for_ocr(image)
    gray = np.array(image.convert("L"))

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    denoised = cv2.fastNlMeansDenoising(enhanced, None, h=12, templateWindowSize=7, searchWindowSize=21)

    pil = Image.fromarray(denoised)
    pil = ImageEnhance.Contrast(pil).enhance(1.6)
    pil = pil.filter(ImageFilter.SHARPEN)

    return resize_for_ocr(pil.convert("RGB"))


def preprocess_receipt_photo(image: Image.Image) -> ReceiptPreprocessingResult:
    original = resize_for_ocr(image)
    original_quality = analyse_image_quality(original).as_dict()

    try:
        working, crop_applied, crop_box, notes = crop_receipt_region(original)
        deskewed, deskew_applied, deskew_angle = deskew_image(working)

        if deskew_applied:
            notes.append(f"deskew_applied_angle={deskew_angle}")
        else:
            notes.append("deskew_not_applied")

        enhanced = enhance_for_receipt_ocr(deskewed)
        processed_quality = analyse_image_quality(enhanced).as_dict()
        quality_notes = processed_quality.get("notes") or []
        notes.extend(str(note) for note in quality_notes if str(note) not in notes)

        return ReceiptPreprocessingResult(
            processed_image=enhanced,
            crop_applied=crop_applied,
            deskew_applied=deskew_applied,
            preprocessing_notes=notes,
            image_quality=processed_quality,
            original_image_quality=original_quality,
            crop_box=crop_box,
        )
    except Exception as exc:
        notes = [
            "receipt_preprocessing_failed_using_resized_full_image",
            str(exc)[:300],
        ]
        return ReceiptPreprocessingResult(
            processed_image=original,
            crop_applied=False,
            deskew_applied=False,
            preprocessing_notes=notes,
            image_quality=original_quality,
            original_image_quality=original_quality,
            crop_box=None,
        )


def generate_preview_images(original_image: Image.Image, processed_image: Image.Image) -> PreviewImages:
    return PreviewImages(
        original_preview=_resize_for_preview(original_image),
        processed_preview=_resize_for_preview(processed_image),
    )


def split_receipt_ocr_regions(processed_image: Image.Image) -> list[ReceiptOcrRegion]:
    """
    Split an enhanced receipt image into overlapping semantic OCR regions.

    Receipts often have survey/marketing text near the top. Reading top,
    middle and bottom independently lets the OCR pipeline recover totals and
    payment lines even when the full-page OCR latches onto the wrong area.
    """
    image = resize_for_ocr(processed_image)
    width, height = image.size

    if width <= 0 or height <= 0:
        return [ReceiptOcrRegion("full_processed", image)]

    def crop_region(name: str, y1_ratio: float, y2_ratio: float) -> ReceiptOcrRegion:
        y1 = max(0, min(height - 1, int(height * y1_ratio)))
        y2 = max(y1 + 1, min(height, int(height * y2_ratio)))
        return ReceiptOcrRegion(name=name, image=image.crop((0, y1, width, y2)))

    return [
        ReceiptOcrRegion("full_processed", image),
        crop_region("header_top_30_percent", 0.0, 0.30),
        crop_region("middle_40_percent", 0.30, 0.70),
        crop_region("bottom_35_percent", 0.65, 1.0),
    ]


def split_deep_document_ocr_regions(processed_image: Image.Image) -> list[ReceiptOcrRegion]:
    """
    Split a processed invoice/receipt page into field-oriented OCR regions.

    These ratios intentionally overlap a little. The goal is not perfect layout
    analysis; it is a cheap second pass that gives small text like VAT, fax,
    invoice number and table rows more focused OCR attention.
    """
    image = resize_for_ocr(processed_image)
    width, height = image.size

    if width <= 0 or height <= 0:
        return [ReceiptOcrRegion("full_processed", image)]

    def crop_region(
        name: str,
        x1_ratio: float,
        y1_ratio: float,
        x2_ratio: float,
        y2_ratio: float,
    ) -> ReceiptOcrRegion:
        x1 = max(0, min(width - 1, int(width * x1_ratio)))
        y1 = max(0, min(height - 1, int(height * y1_ratio)))
        x2 = max(x1 + 1, min(width, int(width * x2_ratio)))
        y2 = max(y1 + 1, min(height, int(height * y2_ratio)))
        return ReceiptOcrRegion(name=name, image=image.crop((x1, y1, x2, y2)))

    return [
        ReceiptOcrRegion("full_processed", image),
        crop_region("supplier_header_region", 0.00, 0.00, 0.55, 0.42),
        crop_region("supplier_contact_region", 0.42, 0.00, 1.00, 0.45),
        crop_region("invoice_summary_region", 0.00, 0.48, 1.00, 0.72),
        crop_region("line_items_region", 0.00, 0.66, 1.00, 1.00),
    ]
