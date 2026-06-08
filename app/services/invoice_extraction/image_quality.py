from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
from PIL import Image


@dataclass
class ImageQualityMetrics:
    image_quality_score: float
    blur_score: float
    contrast_score: float
    brightness_score: float
    text_density_score: float
    width: int
    height: int
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "image_quality_score": self.image_quality_score,
            "blur_score": self.blur_score,
            "contrast_score": self.contrast_score,
            "brightness_score": self.brightness_score,
            "text_density_score": self.text_density_score,
            "width": self.width,
            "height": self.height,
            "notes": self.notes,
        }


def _normalise(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def analyse_image_quality(image: Image.Image) -> ImageQualityMetrics:
    """
    Compute simple, deterministic image quality metrics for OCR routing.

    The score is not a guarantee of OCR accuracy. It is a practical signal used
    to decide whether an OCR result should be trusted or pushed to review.
    """
    gray = np.array(image.convert("L"))
    height, width = gray.shape[:2]

    # Laplacian variance: low values usually mean blur.
    blur_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    blur_score = _normalise(blur_variance, 50.0, 650.0)

    # Standard deviation of grayscale values: low values usually mean low contrast.
    contrast_raw = float(np.std(gray))
    contrast_score = _normalise(contrast_raw, 25.0, 85.0)

    # Brightness near mid-range is preferred. Too dark/too light hurts OCR.
    brightness_raw = float(np.mean(gray))
    brightness_score = max(0.0, 1.0 - abs(brightness_raw - 180.0) / 180.0)

    # Crude text density estimate: percentage of pixels not close to white.
    non_white_ratio = float(np.mean(gray < 245))
    text_density_score = _normalise(non_white_ratio, 0.01, 0.18)

    resolution_score = 1.0 if min(width, height) >= 900 else _normalise(min(width, height), 300.0, 900.0)

    image_quality_score = round(
        0.32 * blur_score
        + 0.26 * contrast_score
        + 0.18 * brightness_score
        + 0.14 * text_density_score
        + 0.10 * resolution_score,
        3,
    )

    notes: list[str] = []
    if blur_score < 0.35:
        notes.append("image_may_be_blurry")
    if contrast_score < 0.35:
        notes.append("low_contrast")
    if brightness_raw < 80:
        notes.append("image_too_dark")
    if brightness_raw > 235:
        notes.append("image_too_light")
    if text_density_score < 0.20:
        notes.append("low_text_density_or_large_blank_area")
    if resolution_score < 0.50:
        notes.append("low_resolution")

    return ImageQualityMetrics(
        image_quality_score=image_quality_score,
        blur_score=round(blur_score, 3),
        contrast_score=round(contrast_score, 3),
        brightness_score=round(brightness_score, 3),
        text_density_score=round(text_density_score, 3),
        width=width,
        height=height,
        notes=notes,
    )
