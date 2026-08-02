from __future__ import annotations

from typing import Any, Optional

from app.services.invoice_extraction.entity_detection import name_matches_org
from app.services.invoice_extraction.extraction_rules import looks_like_location_cluster
from app.services.invoice_extraction.supplier_parser import is_valid_supplier_candidate


_IMAGE_MAGIC: tuple[bytes, ...] = (
    b"\xff\xd8\xff",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"II\x2a\x00",
    b"MM\x00\x2a",
)

_SUPPLIER_PROFILE_FIELDS = {
    "supplier_name_extracted",
    "supplier_del_address_extracted",
    "supplier_pos_address_extracted",
    "supplier_email_extracted",
    "supplier_acc_email_extracted",
    "supplier_telephone_extracted",
    "supplier_fax_extracted",
    "supplier_cell_extracted",
    "supplier_website_extracted",
    "vat_number_extracted",
    "cus_code_extracted",
    "company_registration_number_extracted",
    "bank_account_name_extracted",
    "bank_name_extracted",
    "bank_account_number_extracted",
    "bank_branch_code_extracted",
    "bank_swift_code_extracted",
}
_SUPPLIER_RECHECK_REASONS = {
    "missing_supplier",
    "invalid_supplier",
    "supplier_matches_organisation",
    "supplier_looks_like_location",
}


def is_image_document(file_bytes: bytes, file_type: Optional[str]) -> bool:
    if str(file_type or "").lower().startswith("image/"):
        return True
    head = file_bytes[:12]
    if any(head.startswith(signature) for signature in _IMAGE_MAGIC):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    return head[4:8] == b"ftyp"


def vlm_routing_reasons(
    parsed_data: dict[str, Any],
    *,
    force_vlm: bool = False,
    organisation: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Explain why visual extraction is required for this parse candidate."""
    if parsed_data.get("document_type") == "statement":
        return []

    reasons: list[str] = []
    supplier = parsed_data.get("supplier_name_extracted")
    if force_vlm:
        reasons.append("visual_source")
    if float(parsed_data.get("confidence_score") or 0) < 0.70:
        reasons.append("low_confidence")
    if not parsed_data.get("invoice_number"):
        reasons.append("missing_invoice_number")
    if not parsed_data.get("total_amount"):
        reasons.append("missing_total")
    if not supplier:
        reasons.append("missing_supplier")
    elif not is_valid_supplier_candidate(str(supplier)):
        reasons.append("invalid_supplier")
    if supplier and name_matches_org(str(supplier), organisation or {}):
        reasons.append("supplier_matches_organisation")
    if supplier and looks_like_location_cluster(str(supplier)):
        reasons.append("supplier_looks_like_location")
    if not parsed_data.get("line_items") and parsed_data.get("total_amount"):
        reasons.append("missing_line_items")
    return list(dict.fromkeys(reasons))


def should_try_vlm(
    parsed_data: dict[str, Any],
    *,
    force_vlm: bool = False,
    organisation: Optional[dict[str, Any]] = None,
) -> bool:
    return bool(vlm_routing_reasons(parsed_data, force_vlm=force_vlm, organisation=organisation))


def should_replace_with_vlm(
    field: str,
    *,
    current_value: Any,
    vlm_value: Any,
    force_vlm: bool,
    routing_reasons: list[str],
    vlm_confidence: float,
    text_confidence: float,
) -> bool:
    """Use source/field evidence instead of one global confidence comparison."""
    if vlm_value in (None, "", []):
        return False
    if current_value in (None, "", []):
        return True
    if force_vlm:
        return True
    if field in _SUPPLIER_PROFILE_FIELDS and _SUPPLIER_RECHECK_REASONS.intersection(routing_reasons):
        return True
    return vlm_confidence > text_confidence
