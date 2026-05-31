from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Optional

from app.services.invoice_extraction.entity_detection import normalise_name

NAME_FUZZY_THRESHOLD = 0.85
NAME_PART_MIN_LENGTH = 6
DEFAULT_AUTO_LINK_MIN_MATCHES = 2


def _norm_id(v: Optional[str]) -> str:
    """Lowercase and strip whitespace/hyphens/slashes from an identifier field."""
    if not v:
        return ""
    return re.sub(r"[\s\-/]", "", v.lower().strip())


def _digits_only(v: Optional[str]) -> str:
    if not v:
        return ""
    return re.sub(r"\D+", "", v)


def _norm_email(v: Optional[str]) -> str:
    if not v:
        return ""
    return v.strip().lower()


def _safe_int(value: object, default: int = DEFAULT_AUTO_LINK_MIN_MATCHES) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed = default
    return min(4, max(1, parsed))


def _fetch_min_matches(supabase, org_id: str) -> int:
    try:
        res = (
            supabase
            .table("organisations")
            .select("supplier_auto_link_min_matches")
            .eq("id", org_id)
            .limit(1)
            .execute()
        )
        row = res.data[0] if res.data else {}
        return _safe_int(row.get("supplier_auto_link_min_matches"))
    except Exception:
        return DEFAULT_AUTO_LINK_MIN_MATCHES


def _fetch_suppliers(supabase, org_id: str) -> list[dict]:
    res = (
        supabase
        .table("suppliers")
        .select(
            "id, supplier_name, trading_name, vat_number, company_registration_number, "
            "account_number, bank_account_number, phone, default_email, accounting_email"
        )
        .eq("organisation_id", org_id)
        .eq("active", True)
        .execute()
    )
    return res.data or []


def _add_evidence(
    evidence: list[dict],
    *,
    field: str,
    label: str,
    confidence: float,
    extracted: Optional[str],
    supplier_value: Optional[str],
) -> None:
    evidence.append({
        "field": field,
        "label": label,
        "confidence": round(confidence, 3),
        "extracted": extracted,
        "supplier_value": supplier_value,
    })


def _name_match_evidence(
    *,
    extracted_name: Optional[str],
    supplier: dict,
) -> list[dict]:
    if not extracted_name:
        return []

    norm_extracted = normalise_name(extracted_name)
    if not norm_extracted:
        return []

    evidence: list[dict] = []
    seen_supplier_names: set[str] = set()
    candidates = [
        ("supplier_name", "Supplier legal/name matched", supplier.get("supplier_name")),
        ("trading_name", "Supplier trading name matched", supplier.get("trading_name")),
    ]

    for field, label, value in candidates:
        if not value:
            continue
        norm_supplier = normalise_name(str(value))
        if not norm_supplier or norm_supplier in seen_supplier_names:
            continue
        seen_supplier_names.add(norm_supplier)

        if norm_extracted == norm_supplier:
            _add_evidence(
                evidence,
                field=field,
                label=label,
                confidence=0.98,
                extracted=extracted_name,
                supplier_value=str(value),
            )
            continue

        score = SequenceMatcher(None, norm_extracted, norm_supplier).ratio()
        if score >= NAME_FUZZY_THRESHOLD:
            _add_evidence(
                evidence,
                field=field,
                label=f"{label} (similar)",
                confidence=max(0.85, min(0.95, score)),
                extracted=extracted_name,
                supplier_value=str(value),
            )
            continue

        if (
            min(len(norm_extracted), len(norm_supplier)) >= NAME_PART_MIN_LENGTH
            and (norm_extracted in norm_supplier or norm_supplier in norm_extracted)
        ):
            _add_evidence(
                evidence,
                field=field,
                label=f"{label} (part match)",
                confidence=0.72,
                extracted=extracted_name,
                supplier_value=str(value),
            )

    return evidence


def _supplier_match_evidence(
    supplier: dict,
    *,
    supplier_name_extracted: Optional[str] = None,
    vat_number_extracted: Optional[str] = None,
    company_registration_number_extracted: Optional[str] = None,
    cus_code_extracted: Optional[str] = None,
    bank_account_number_extracted: Optional[str] = None,
    supplier_telephone_extracted: Optional[str] = None,
    supplier_email_extracted: Optional[str] = None,
    supplier_acc_email_extracted: Optional[str] = None,
) -> list[dict]:
    evidence = _name_match_evidence(
        extracted_name=supplier_name_extracted,
        supplier=supplier,
    )

    identifier_checks = [
        ("vat_number", "VAT number matched", vat_number_extracted, 1.0),
        ("company_registration_number", "Company registration matched", company_registration_number_extracted, 0.98),
        ("account_number", "Supplier account number matched", cus_code_extracted, 0.96),
        ("bank_account_number", "Bank account number matched", bank_account_number_extracted, 0.96),
    ]
    for field, label, extracted_val, confidence in identifier_checks:
        norm_extracted = _norm_id(extracted_val)
        if norm_extracted and _norm_id(supplier.get(field)) == norm_extracted:
            _add_evidence(
                evidence,
                field=field,
                label=label,
                confidence=confidence,
                extracted=extracted_val,
                supplier_value=supplier.get(field),
            )

    extracted_phone = _digits_only(supplier_telephone_extracted)
    supplier_phone = _digits_only(supplier.get("phone"))
    if extracted_phone and supplier_phone and extracted_phone == supplier_phone:
        _add_evidence(
            evidence,
            field="phone",
            label="Telephone matched",
            confidence=0.98,
            extracted=supplier_telephone_extracted,
            supplier_value=supplier.get("phone"),
        )

    extracted_emails = {
        _norm_email(supplier_email_extracted),
        _norm_email(supplier_acc_email_extracted),
    } - {""}
    supplier_emails = [
        ("default_email", supplier.get("default_email")),
        ("accounting_email", supplier.get("accounting_email")),
    ]
    for field, supplier_email in supplier_emails:
        norm_supplier_email = _norm_email(supplier_email)
        if norm_supplier_email and norm_supplier_email in extracted_emails:
            _add_evidence(
                evidence,
                field=field,
                label="Email matched",
                confidence=0.98,
                extracted=norm_supplier_email,
                supplier_value=supplier_email,
            )
            break

    return evidence


def score_supplier_matches(
    supabase,
    *,
    org_id: str,
    supplier_name_extracted: Optional[str] = None,
    vat_number_extracted: Optional[str] = None,
    company_registration_number_extracted: Optional[str] = None,
    cus_code_extracted: Optional[str] = None,
    bank_account_number_extracted: Optional[str] = None,
    supplier_telephone_extracted: Optional[str] = None,
    supplier_email_extracted: Optional[str] = None,
    supplier_acc_email_extracted: Optional[str] = None,
) -> list[dict]:
    matches: list[dict] = []
    for supplier in _fetch_suppliers(supabase, org_id):
        evidence = _supplier_match_evidence(
            supplier,
            supplier_name_extracted=supplier_name_extracted,
            vat_number_extracted=vat_number_extracted,
            company_registration_number_extracted=company_registration_number_extracted,
            cus_code_extracted=cus_code_extracted,
            bank_account_number_extracted=bank_account_number_extracted,
            supplier_telephone_extracted=supplier_telephone_extracted,
            supplier_email_extracted=supplier_email_extracted,
            supplier_acc_email_extracted=supplier_acc_email_extracted,
        )
        if not evidence:
            continue
        confidence = sum(item["confidence"] for item in evidence) / len(evidence)
        matches.append({
            "id": str(supplier["id"]),
            "supplier_id": str(supplier["id"]),
            "supplier_name": supplier.get("supplier_name"),
            "trading_name": supplier.get("trading_name"),
            "match_count": len(evidence),
            "confidence": round(confidence, 3),
            "evidence": evidence,
        })

    return sorted(matches, key=lambda item: (item["match_count"], item["confidence"]), reverse=True)


def find_supplier_match_result(
    supabase,
    *,
    org_id: str,
    min_matches: Optional[int] = None,
    **kwargs,
) -> Optional[dict]:
    threshold = _safe_int(min_matches) if min_matches is not None else _fetch_min_matches(supabase, org_id)
    matches = score_supplier_matches(supabase, org_id=org_id, **kwargs)
    if not matches:
        return None

    best = matches[0]
    tied = len(matches) > 1 and matches[1]["match_count"] == best["match_count"]
    return {
        **best,
        "threshold": threshold,
        "auto_link": best["match_count"] >= threshold and not tied,
        "ambiguous": tied,
    }


def attempt_supplier_auto_link(
    supabase,
    *,
    org_id: str,
    supplier_name_extracted: Optional[str] = None,
    vat_number_extracted: Optional[str] = None,
    company_registration_number_extracted: Optional[str] = None,
    cus_code_extracted: Optional[str] = None,
    bank_account_number_extracted: Optional[str] = None,
    supplier_telephone_extracted: Optional[str] = None,
    supplier_email_extracted: Optional[str] = None,
    supplier_acc_email_extracted: Optional[str] = None,
    min_matches: Optional[int] = None,
) -> Optional[str]:
    """
    Return a supplier_id only when the organisation's configured number of
    identity signals match one unambiguous active supplier.
    """
    result = find_supplier_match_result(
        supabase,
        org_id=org_id,
        min_matches=min_matches,
        supplier_name_extracted=supplier_name_extracted,
        vat_number_extracted=vat_number_extracted,
        company_registration_number_extracted=company_registration_number_extracted,
        cus_code_extracted=cus_code_extracted,
        bank_account_number_extracted=bank_account_number_extracted,
        supplier_telephone_extracted=supplier_telephone_extracted,
        supplier_email_extracted=supplier_email_extracted,
        supplier_acc_email_extracted=supplier_acc_email_extracted,
    )
    if result and result.get("auto_link"):
        return str(result["supplier_id"])
    return None


def find_name_match_suggestion(
    supabase,
    *,
    org_id: str,
    supplier_name_extracted: Optional[str],
) -> Optional[dict]:
    """
    Return the best name-based match suggestion above NAME_FUZZY_THRESHOLD.
    Never auto-links; caller must confirm with the user.
    """
    result = find_supplier_match_result(
        supabase,
        org_id=org_id,
        min_matches=1,
        supplier_name_extracted=supplier_name_extracted,
    )
    if not result:
        return None
    name_evidence = [
        item for item in result.get("evidence", [])
        if item.get("field") in {"supplier_name", "trading_name"}
    ]
    if not name_evidence:
        return None
    return {
        "id": result["supplier_id"],
        "supplier_id": result["supplier_id"],
        "supplier_name": result.get("supplier_name"),
        "trading_name": result.get("trading_name"),
        "confidence": result.get("confidence"),
        "match_count": result.get("match_count"),
        "evidence": name_evidence,
    }
