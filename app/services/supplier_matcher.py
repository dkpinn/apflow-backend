from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Optional

from app.services.invoice_extraction.entity_detection import normalise_name

NAME_FUZZY_THRESHOLD = 0.85


def _norm_id(v: Optional[str]) -> str:
    """Lowercase and strip whitespace/hyphens/slashes from an identifier field."""
    if not v:
        return ""
    return re.sub(r"[\s\-/]", "", v.lower().strip())


def _fetch_suppliers(supabase, org_id: str) -> list[dict]:
    res = (
        supabase
        .table("suppliers")
        .select("id, supplier_name, vat_number, company_registration_number, account_number, bank_account_number")
        .eq("organisation_id", org_id)
        .eq("active", True)
        .execute()
    )
    return res.data or []


def attempt_supplier_auto_link(
    supabase,
    *,
    org_id: str,
    supplier_name_extracted: Optional[str] = None,
    vat_number_extracted: Optional[str] = None,
    company_registration_number_extracted: Optional[str] = None,
    cus_code_extracted: Optional[str] = None,
    bank_account_number_extracted: Optional[str] = None,
) -> Optional[str]:
    """
    Return a supplier_id if an exact identifier match is found, else None.
    Checks (in priority order): VAT number, registration number, account/customer
    code, bank account number. Name fuzzy match is intentionally excluded here
    to avoid false positives — use find_name_match_suggestion() for that.
    """
    suppliers = _fetch_suppliers(supabase, org_id)
    if not suppliers:
        return None

    checks = [
        ("vat_number",                   vat_number_extracted),
        ("company_registration_number",  company_registration_number_extracted),
        ("account_number",               cus_code_extracted),
        ("bank_account_number",          bank_account_number_extracted),
    ]
    for field, extracted_val in checks:
        norm_extracted = _norm_id(extracted_val)
        if not norm_extracted:
            continue
        for supplier in suppliers:
            if _norm_id(supplier.get(field)) == norm_extracted:
                return str(supplier["id"])

    return None


def find_name_match_suggestion(
    supabase,
    *,
    org_id: str,
    supplier_name_extracted: Optional[str],
) -> Optional[dict]:
    """
    Return the best fuzzy name match above NAME_FUZZY_THRESHOLD as a suggestion
    dict {id, supplier_name, confidence}, or None. Never auto-links — caller
    must confirm with the user.
    """
    if not supplier_name_extracted:
        return None

    norm_extracted = normalise_name(supplier_name_extracted)
    if not norm_extracted:
        return None

    suppliers = _fetch_suppliers(supabase, org_id)
    best: Optional[dict] = None
    best_score = 0.0

    for supplier in suppliers:
        norm_name = normalise_name(supplier.get("supplier_name") or "")
        if not norm_name:
            continue
        score = SequenceMatcher(None, norm_extracted, norm_name).ratio()
        if score > best_score:
            best_score = score
            best = supplier

    if best and best_score >= NAME_FUZZY_THRESHOLD:
        return {
            "id": str(best["id"]),
            "supplier_name": best.get("supplier_name"),
            "confidence": round(best_score, 3),
        }
    return None
