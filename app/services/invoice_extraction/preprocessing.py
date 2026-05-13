from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter


def pil_to_cv(image: Image.Image) -> np.ndarray:
    """
    Convert PIL image to OpenCV BGR image.
    """
    rgb = np.array(image.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def cv_to_pil(image: np.ndarray) -> Image.Image:
    """
    Convert OpenCV image to PIL image.
    """
    if len(image.shape) == 2:
        return Image.fromarray(image)

    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


def crop_to_content(image: Image.Image, padding: int = 25) -> Image.Image:
    """
    Crop large white borders around a scanned invoice/receipt.

    This is important where the actual receipt is small and centered
    on a mostly blank page.
    """
    cv_img = pil_to_cv(image)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)

    # Anything not near-white is treated as content.
    mask = gray < 245

    coords = np.argwhere(mask)

    if coords.size == 0:
        return image

    y_min, x_min = coords.min(axis=0)
    y_max, x_max = coords.max(axis=0)

    height, width = gray.shape

    x_min = max(x_min - padding, 0)
    y_min = max(y_min - padding, 0)
    x_max = min(x_max + padding, width)
    y_max = min(y_max + padding, height)

    cropped = cv_img[y_min:y_max, x_min:x_max]

    return cv_to_pil(cropped)


def upscale_image(image: Image.Image, scale: float = 2.0) -> Image.Image:
    """
    Upscale small/faint invoice images before OCR.
    """
    width, height = image.size

    new_size = (
        int(width * scale),
        int(height * scale),
    )

    return image.resize(new_size, Image.Resampling.LANCZOS)


def enhance_contrast(image: Image.Image, factor: float = 1.8) -> Image.Image:
    """
    Increase contrast for faded scans.
    """
    grayscale = image.convert("L")
    enhancer = ImageEnhance.Contrast(grayscale)
    return enhancer.enhance(factor)


def sharpen_image(image: Image.Image) -> Image.Image:
    """
    Sharpen text edges slightly before OCR.
    """
    return image.filter(ImageFilter.SHARPEN)


def denoise_image(image: Image.Image) -> Image.Image:
    """
    Denoise using OpenCV.
    """
    cv_img = np.array(image.convert("L"))

    denoised = cv2.fastNlMeansDenoising(
        cv_img,
        None,
        h=20,
        templateWindowSize=7,
        searchWindowSize=21,
    )

    return Image.fromarray(denoised)


def adaptive_threshold(image: Image.Image) -> Image.Image:
    """
    Convert to high-contrast black-and-white image.
    Useful for some scans, but not always best for faint invoices.
    """
    gray = np.array(image.convert("L"))

    thresholded = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        35,
        11,
    )

    return Image.fromarray(thresholded)


def otsu_threshold(image: Image.Image) -> Image.Image:
    """
    Alternative thresholding method.
    Sometimes better than adaptive threshold for receipt scans.
    """
    gray = np.array(image.convert("L"))

    _, thresholded = cv2.threshold(
        gray,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )

    return Image.fromarray(thresholded)


def deskew_image(image: Image.Image) -> Image.Image:
    """
    Attempt to deskew a scanned document.

    If skew detection fails, returns the original image.
    """
    cv_img = np.array(image.convert("L"))

    # Invert so text becomes white on black for coordinate detection.
    inverted = cv2.bitwise_not(cv_img)

    coords = np.column_stack(np.where(inverted > 0))

    if coords.size == 0:
        return image

    angle = cv2.minAreaRect(coords)[-1]

    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle

    # Ignore tiny skew corrections.
    if abs(angle) < 0.5 or abs(angle) > 15:
        return image

    height, width = cv_img.shape
    center = (width // 2, height // 2)

    rotation_matrix = cv2.getRotationMatrix2D(center, angle, 1.0)

    rotated = cv2.warpAffine(
        cv_img,
        rotation_matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )

    return Image.fromarray(rotated)


def preprocess_for_ocr_variants(image: Image.Image) -> list[tuple[str, Image.Image]]:
    """
    Return multiple OCR-ready image variants.

    Different invoices respond better to different preprocessing.
    The OCR layer should run Tesseract against several variants and
    keep the best text result.
    """
    variants: list[tuple[str, Image.Image]] = []

    cropped = crop_to_content(image)
    upscaled = upscale_image(cropped, scale=2.0)
    contrasted = enhance_contrast(upscaled, factor=1.8)
    denoised = denoise_image(contrasted)
    sharpened = sharpen_image(denoised)
    deskewed = deskew_image(sharpened)

    variants.append(("enhanced_grayscale", deskewed))
    variants.append(("adaptive_threshold", adaptive_threshold(deskewed)))
    variants.append(("otsu_threshold", otsu_threshold(deskewed)))

    # Also keep a less aggressive version.
    mild = sharpen_image(enhance_contrast(upscale_image(cropped, scale=2.0), factor=1.4))
    variants.append(("mild_enhancement", mild))

    return variants


def preprocess_image_for_ocr(image: Image.Image) -> Image.Image:
    """
    Backwards-compatible single-image preprocessing function.

    Use this where the older pipeline expects one processed image only.
    """
    variants = preprocess_for_ocr_variants(image)

    # Default to mild enhancement because aggressive thresholding can destroy
    # faint receipt text.
    for name, variant in variants:
        if name == "mild_enhancement":
            return variant

    return variants[0][1]