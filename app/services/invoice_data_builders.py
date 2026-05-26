"""
invoice_data_builders.py
------------------------
Pure data-transformation helpers for invoice extraction.

Nothing in this module touches HTTP or the database.  All functions take plain
dicts / primitives and return plain dicts / primitives, making them easy to
unit-test in isolation.

Moved here from app/routers/invoices.py as part of the thin-router refactor.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Tiny timestamp helper
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Supplier / document profile builders
# ---------------------------------------------------------------------------

def build_extracted_supplier_profile(parsed_data: dict) -> dict:
    return {
        "supplier_name": parsed_data.get("supplier_name_extracted") or parsed_data.get("issuer_name_extracted"),
        "supplier_code": parsed_data.get("cus_code_extracted"),
        "account_number": parsed_data.get("cus_code_extracted"),
        "currency": parsed_data.get("currency"),
        "default_email": parsed_data.get("supplier_email_extracted"),
        "accounting_email": parsed_data.get("supplier_acc_email_extracted"),
        "phone": parsed_data.get("supplier_telephone_extracted"),
        "telephone": parsed_data.get("supplier_telephone_extracted"),
        "fax": parsed_data.get("supplier_fax_extracted"),
        "cell": parsed_data.get("supplier_cell_extracted"),
        "website": parsed_data.get("supplier_website_extracted"),
        "delivery_address": parsed_data.get("supplier_del_address_extracted"),
        "postal_address": parsed_data.get("supplier_pos_address_extracted"),
        "vat_number": parsed_data.get("vat_number_extracted"),
        "tax_number": parsed_data.get("vat_number_extracted"),
        "company_registration_number": parsed_data.get("company_registration_number_extracted"),
        "registration_number": parsed_data.get("company_registration_number_extracted"),
        "bank_account_name": parsed_data.get("bank_account_name_extracted"),
        "bank_name": parsed_data.get("bank_name_extracted"),
        "bank_account_number": parsed_data.get("bank_account_number_extracted"),
        "bank_branch_code": parsed_data.get("bank_branch_code_extracted"),
        "bank_swift_code": parsed_data.get("bank_swift_code_extracted"),
        # Raw extraction aliases for frontends that render invoices_extracted names.
        "supplier_name_extracted": parsed_data.get("supplier_name_extracted"),
        "supplier_email_extracted": parsed_data.get("supplier_email_extracted"),
        "supplier_acc_email_extracted": parsed_data.get("supplier_acc_email_extracted"),
        "supplier_telephone_extracted": parsed_data.get("supplier_telephone_extracted"),
        "supplier_fax_extracted": parsed_data.get("supplier_fax_extracted"),
        "supplier_cell_extracted": parsed_data.get("supplier_cell_extracted"),
        "supplier_website_extracted": parsed_data.get("supplier_website_extracted"),
        "supplier_del_address_extracted": parsed_data.get("supplier_del_address_extracted"),
        "supplier_pos_address_extracted": parsed_data.get("supplier_pos_address_extracted"),
        "vat_number_extracted": parsed_data.get("vat_number_extracted"),
        "company_registration_number_extracted": parsed_data.get("company_registration_number_extracted"),
        "cus_code_extracted": parsed_data.get("cus_code_extracted"),
    }


def build_extracted_document_profile(parsed_data: dict) -> dict:
    return {
        "invoice_number": parsed_data.get("invoice_number"),
        "invoice_date": parsed_data.get("invoice_date"),
        "due_date": parsed_data.get("due_date"),
        "subtotal": parsed_data.get("subtotal"),
        "tax_amount": parsed_data.get("tax_amount"),
        "total_amount": parsed_data.get("total_amount"),
        "currency": parsed_data.get("currency"),
        "line_items": parsed_data.get("line_items") or [],
        "supplier": build_extracted_supplier_profile(parsed_data),
    }


def build_supplier_create_payload(
    *,
    organisation_id: str,
    invoice_raw_id: str,
    invoice_extracted_id: Optional[str],
    parsed_data: dict,
) -> dict:
    profile = build_extracted_supplier_profile(parsed_data)
    return {
        "organisation_id": organisation_id,
        "supplier_name": profile.get("supplier_name"),
        "supplier_code": profile.get("supplier_code"),
        "account_number": profile.get("supplier_code"),
        "currency": profile.get("currency"),
        "default_email": profile.get("accounting_email") or profile.get("default_email"),
        "phone": profile.get("phone") or profile.get("cell"),
        "vat_number": profile.get("vat_number"),
        "tax_number": profile.get("vat_number"),
        "registration_number": profile.get("company_registration_number"),
        "company_registration_number": profile.get("company_registration_number"),
        "bank_account_name": profile.get("bank_account_name"),
        "bank_name": profile.get("bank_name"),
        "bank_account_number": profile.get("bank_account_number"),
        "bank_branch_code": profile.get("bank_branch_code"),
        "bank_swift_code": profile.get("bank_swift_code"),
        "bank_country": "ZA" if (profile.get("currency") or "ZAR") == "ZAR" else None,
        "delivery_address": profile.get("delivery_address"),
        "postal_address": profile.get("postal_address"),
        "accounting_email": profile.get("accounting_email"),
        "fax": profile.get("fax"),
        "cell": profile.get("cell"),
        "website": profile.get("website"),
        "invoice_extracted_id": invoice_extracted_id,
        "invoice_raw_id": invoice_raw_id,
        "link_invoice": True,
    }


# ---------------------------------------------------------------------------
# Constants used by the re-extraction field-merge logic
# ---------------------------------------------------------------------------

REEXTRACT_FIELD_MAP = {
    "supplier_name_extracted": "supplier_name_extracted",
    "invoice_number": "invoice_number",
    "invoice_date": "invoice_date",
    "due_date": "due_date",
    "subtotal": "subtotal",
    "tax_amount": "tax_amount",
    "total_amount": "total_amount",
    "currency": "currency",
    "supplier_del_address_extracted": "supplier_del_address_extracted",
    "supplier_pos_address_extracted": "supplier_pos_address_extracted",
    "supplier_email_extracted": "supplier_email_extracted",
    "supplier_acc_email_extracted": "supplier_acc_email_extracted",
    "supplier_telephone_extracted": "supplier_telephone_extracted",
    "supplier_fax_extracted": "supplier_fax_extracted",
    "supplier_cell_extracted": "supplier_cell_extracted",
    "supplier_website_extracted": "supplier_website_extracted",
    "vat_number_extracted": "vat_number_extracted",
    "cus_code_extracted": "cus_code_extracted",
    "company_registration_number_extracted": "company_registration_number_extracted",
    "bank_account_name_extracted": "bank_account_name_extracted",
    "bank_name_extracted": "bank_name_extracted",
    "bank_account_number_extracted": "bank_account_number_extracted",
    "bank_branch_code_extracted": "bank_branch_code_extracted",
    "bank_swift_code_extracted": "bank_swift_code_extracted",
    "issuer_name_extracted": "issuer_name_extracted",
    "recipient_name_extracted": "recipient_name_extracted",
    "document_direction": "document_direction",
    "organisation_match_status": "organisation_match_status",
    "validation_status": "validation_status",
    "validation_notes": "validation_notes",
}

SUPPLIER_RECOVERY_FIELDS = [
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
]

SUPPLIER_RECOVERY_SUPPORT_FIELDS = {
    "supplier_del_address_extracted",
    "supplier_pos_address_extracted",
    "supplier_email_extracted",
    "supplier_acc_email_extracted",
    "supplier_telephone_extracted",
    "supplier_fax_extracted",
    "supplier_cell_extracted",
    "supplier_website_extracted",
    "vat_number_extracted",
    "company_registration_number_extracted",
}

MISSING_SUPPLIER_VALIDATION_STATUS = "failed_missing_supplier"
MISSING_SUPPLIER_NOTE = (
    "No supplier name could be obtained from the document after OCR, supplier recovery, "
    "VLM fallback, and issuer detection. Manual supplier editing is required."
)

# Status/classification fields whose latest extraction value should always win.
# The "has old value" guard does not apply to these.
_ALWAYS_UPDATE_FIELDS = {"validation_status", "document_direction", "organisation_match_status"}


# ---------------------------------------------------------------------------
# Field-level value validators / helpers
# ---------------------------------------------------------------------------

def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)):
        return value != 0  # treat 0 the same as null — a zeroed numeric field has no useful data
    return True


def _looks_suspicious_value(field_name: str, value) -> bool:
    if not _has_value(value):
        return True
    if not isinstance(value, str):
        return False

    clean = value.strip()
    if field_name == "supplier_name_extracted":
        alpha_count = sum(char.isalpha() for char in clean)
        if clean.startswith("_") or alpha_count < 3:
            return True
        if len(clean) <= 4 and clean.lower() in {"pty", "ltd", "copy"}:
            return True
        if clean.lower() in {"original", "customer copy", "copy of original", "welcome", "welcame", "welkom"}:
            return True
    if field_name == "supplier_del_address_extracted":
        lower = clean.lower()
        if "scan to rate" in lower or "survey" in lower:
            return True
    return False


def _valid_reextract_value(field_name: str, value) -> bool:
    if not _has_value(value):
        return False

    if field_name in {"subtotal", "total_amount"}:
        try:
            return float(value) > 0
        except Exception:
            return False
    if field_name == "tax_amount":
        try:
            return float(value) >= 0
        except Exception:
            return False
    if field_name in {"vat_number_extracted", "bank_account_number_extracted"}:
        return len("".join(char for char in str(value) if char.isdigit())) >= 7
    if field_name in {"supplier_telephone_extracted", "supplier_fax_extracted", "supplier_cell_extracted"}:
        return len("".join(char for char in str(value) if char.isdigit())) >= 7
    if field_name == "supplier_name_extracted":
        clean = str(value).strip()
        lower = clean.lower()
        if lower in {"copy", "original", "customer copy", "copy of original", "tax invoice", "welcome", "welcame", "welkom"}:
            return False
        if "copy of original" in lower or "customer copy" in lower:
            return False
        if clean.startswith("_"):
            return False
        words = [word for word in re.findall(r"[A-Za-z]+", clean) if word]
        if not words:
            return False
        if len(words) == 1 and len(words[0]) <= 4:
            return False
        if len(words) == 1 and not re.search(r"\b(build|builders|makro|massmart|pinetown)\b", lower):
            return False
        return True
    if field_name == "invoice_number":
        return any(char.isdigit() for char in str(value))
    if field_name in {"invoice_date", "due_date"}:
        return bool(str(value).strip())
    return True


def _append_validation_note(existing: Optional[str], note: str) -> str:
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing} {note}"


def _supplier_candidate_from_recovery_text(text: str) -> Optional[str]:
    blocked = re.compile(
        r"\b(vat|tax|invoice|receipt|tel|telephone|fax|email|address|total|subtotal|tendered|cashier|date|time|slip)\b",
        re.IGNORECASE,
    )
    for raw_line in text.splitlines()[:12]:
        candidate = re.sub(r"\s+", " ", raw_line).strip(" :-*\t")
        if not candidate or blocked.search(candidate):
            continue
        if _valid_reextract_value("supplier_name_extracted", candidate):
            return candidate.title() if candidate.isupper() else candidate
    return None


# ---------------------------------------------------------------------------
# Supplier recovery (fills missing supplier fields from a second OCR pass)
# ---------------------------------------------------------------------------

def merge_supplier_recovery_fields(parsed_data: dict, text_result: dict) -> dict:
    """
    Fill missing supplier identity/contact fields from full-page OCR only.

    This intentionally never updates amounts, dates, invoice numbers or line
    items, so supplier recovery cannot change accounting calculations.
    """
    # Imported here to avoid a top-level circular-import risk.
    from app.services.invoice_ocr_pipeline import parse_invoice_fields  # noqa: PLC0415

    pages = text_result.get("pages") or []
    recovery_texts = [
        ((page.get("supplier_recovery_ocr") or {}).get("text") or "")
        for page in pages
    ]
    recovery_text = "\n\n".join(text for text in recovery_texts if text.strip()).strip()
    if not recovery_text:
        return {"applied": False, "fields": [], "text_length": 0}

    recovery_parsed = parse_invoice_fields(recovery_text)
    has_supporting_supplier_evidence = any(
        _has_value(recovery_parsed.get(field))
        for field in SUPPLIER_RECOVERY_SUPPORT_FIELDS
    )
    if (
        has_supporting_supplier_evidence
        and not _has_value(recovery_parsed.get("supplier_name_extracted"))
    ):
        recovery_parsed["supplier_name_extracted"] = _supplier_candidate_from_recovery_text(recovery_text)

    applied_fields: list[str] = []
    for field in SUPPLIER_RECOVERY_FIELDS:
        if _has_value(parsed_data.get(field)):
            continue

        value = recovery_parsed.get(field)
        if not _valid_reextract_value(field, value):
            continue

        if field == "supplier_name_extracted" and not has_supporting_supplier_evidence:
            continue

        parsed_data[field] = value
        applied_fields.append(field)

    if applied_fields:
        parsed_data["validation_notes"] = _append_validation_note(
            parsed_data.get("validation_notes"),
            "Supplier identity/contact fields were recovered from a full-page OCR pass.",
        )

    return {
        "applied": bool(applied_fields),
        "fields": applied_fields,
        "text_length": len(recovery_text),
        "supplier_candidate": recovery_parsed.get("supplier_name_extracted"),
        "supporting_supplier_evidence": has_supporting_supplier_evidence,
    }


def apply_missing_supplier_failure(parsed_data: dict) -> bool:
    if _has_value(parsed_data.get("supplier_name_extracted")):
        return False

    parsed_data["validation_status"] = MISSING_SUPPLIER_VALIDATION_STATUS
    parsed_data["validation_notes"] = _append_validation_note(
        parsed_data.get("validation_notes"),
        MISSING_SUPPLIER_NOTE,
    )
    parsed_data["confidence_score"] = min(float(parsed_data.get("confidence_score") or 0), 0.45)
    return True


# ---------------------------------------------------------------------------
# Re-extraction field merge
# ---------------------------------------------------------------------------

def build_reextract_update(
    *,
    existing: dict,
    parsed: dict,
    force_update: bool = False,
) -> tuple[dict, list[dict], list[str]]:
    update_payload: dict = {}
    improved_fields: list[dict] = []
    unchanged_fields: list[str] = []
    old_confidence = existing.get("confidence_score") or 0
    new_confidence = parsed.get("confidence_score") or 0
    confidence_materially_improved = new_confidence >= old_confidence + 0.15

    for target_field, parsed_key in REEXTRACT_FIELD_MAP.items():
        new_value = parsed.get(parsed_key)
        old_value = existing.get(target_field)

        if not _valid_reextract_value(target_field, new_value):
            if target_field == "supplier_name_extracted" and _looks_suspicious_value(target_field, old_value):
                update_payload[target_field] = None
                improved_fields.append({
                    "field": target_field,
                    "old_value": old_value,
                    "new_value": None,
                })
                continue
            unchanged_fields.append(target_field)
            continue

        should_update = (
            force_update
            or target_field in _ALWAYS_UPDATE_FIELDS
            or not _has_value(old_value)
            or _looks_suspicious_value(target_field, old_value)
            or confidence_materially_improved
        )

        if should_update and new_value != old_value:
            update_payload[target_field] = new_value
            improved_fields.append({
                "field": target_field,
                "old_value": old_value,
                "new_value": new_value,
            })
        else:
            unchanged_fields.append(target_field)

    if new_confidence is not None and (force_update or not old_confidence or new_confidence > old_confidence):
        update_payload["confidence_score"] = new_confidence

    if update_payload:
        update_payload["review_status"] = "needs_info"
        update_payload["updated_at"] = utc_now_iso()

    return update_payload, improved_fields, unchanged_fields


# ---------------------------------------------------------------------------
# OCR region text utilities
# ---------------------------------------------------------------------------

def _trim_region_text(region_ocr: dict, limit: int = 700) -> dict:
    region_text = (region_ocr.get("region_text_by_name") or {})
    return {
        name: (text[:limit] if text else "")
        for name, text in region_text.items()
    }
