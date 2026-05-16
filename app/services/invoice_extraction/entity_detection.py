from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.services.invoice_extraction.supplier_parser import extract_supplier_from_receipt_text

LEGAL_ENTITY_RE = re.compile(
    r"\b(pty\s*\)?\s*ltd|pty|ltd|limited|cc|close corporation|inc|inc\.|company|trust|npc|npo)\b",
    re.IGNORECASE,
)

BAD_ENTITY_TERMS = {
    "tax invoice",
    "invoice",
    "invoice date",
    "invoice number",
    "reference",
    "description",
    "quantity",
    "unit price",
    "amount",
    "subtotal",
    "total",
    "amount due",
    "banking details",
    "name of bank",
    "branch code",
    "account number",
    "south africa",
    "scan",
    "rate",
    "scan to rate us",
    "survey",
    "win a voucher",
    "terms and conditions",
    "valid for 5 days",
}

SURVEY_NOISE_TERMS = [
    "scan to rate",
    "survey",
    "win a voucher",
    "terms and conditions",
    "valid for 5 days",
]

KNOWN_RECEIPT_ISSUER_PATTERNS = [
    (re.compile(r"\bbuilders\b", re.IGNORECASE), "Builders"),
    (re.compile(r"\bmassmart\b", re.IGNORECASE), "Massmart"),
]

RECIPIENT_LABELS = [
    "bill to",
    "billed to",
    "invoice to",
    "customer",
    "client",
    "to",
    "your vat number",
    "your email address",
]

ISSUER_LABELS = ["from", "supplier", "vendor", "seller"]


@dataclass
class EntityDetectionResult:
    issuer_name: Optional[str]
    recipient_name: Optional[str]
    document_direction: str
    organisation_match_status: str
    validation_status: str
    validation_notes: str
    confidence_adjustment: float = 0.0


def normalise_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def normalise_name(value: Optional[str]) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"\b(pty|ltd|limited|cc|inc|company|the|and|&|\(pty\)|\(bo\))\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def is_survey_noise_line(line: str) -> bool:
    lower = line.lower().strip()
    if lower in {"scan", "rate", "rate us"}:
        return True
    return any(term in lower for term in SURVEY_NOISE_TERMS)


def is_survey_noise_dominated(text: str) -> bool:
    lines = normalise_lines(text)
    if not lines:
        return False

    first_lines = "\n".join(lines[:6]).lower()
    if "scan to rate" in first_lines:
        return True

    noise_lines = sum(1 for line in lines[:12] if is_survey_noise_line(line))
    return noise_lines >= 2


def extract_known_receipt_issuer(text: str) -> Optional[str]:
    receipt_supplier = extract_supplier_from_receipt_text(text)
    if receipt_supplier:
        return receipt_supplier

    for pattern, issuer in KNOWN_RECEIPT_ISSUER_PATTERNS:
        if pattern.search(text):
            return issuer
    return None


def is_known_retail_receipt(text: str) -> bool:
    lower = (text or "").lower()
    return bool(
        extract_known_receipt_issuer(text)
        and re.search(r"\b(receipt|tax invoice|till|terminal|cash|card|change)\b", lower)
    )


def receipt_has_named_customer(text: str) -> bool:
    lines = normalise_lines(text)
    for index, line in enumerate(lines[:120]):
        lower = line.lower().strip(" :;,.")
        if lower not in {"customer", "customer name", "bill to", "billed to", "invoice to", "client"}:
            continue
        window = " ".join(lines[index + 1:index + 4])
        if re.search(r"\b(cash|card|sale|receipt|tax invoice|customer copy)\b", window, re.IGNORECASE):
            continue
        if re.search(r"[A-Za-z]{3,}", window):
            return True
    return False


def is_probable_entity(line: str) -> bool:
    clean = line.strip()
    lower = clean.lower()
    if not clean or len(clean) < 3 or len(clean) > 100:
        return False
    if lower in BAD_ENTITY_TERMS:
        return False
    if is_survey_noise_line(clean):
        return False
    if any(term in lower for term in ["www.", "@", "tel", "phone", "fax", "branch", "account number"]):
        return False
    if re.fullmatch(r"[\W_\d]+", clean):
        return False
    if LEGAL_ENTITY_RE.search(clean):
        return True
    # Title-case names without too many numbers can be entity-like.
    words = clean.split()
    if 1 <= len(words) <= 6 and sum(bool(re.search(r"[A-Za-z]", w)) for w in words) >= 1:
        if not re.search(r"\d{4,}", clean):
            return True
    return False


def best_legal_entity(lines: list[str]) -> Optional[str]:
    for line in lines:
        if is_probable_entity(line) and LEGAL_ENTITY_RE.search(line):
            return line.strip()
    for line in lines:
        if is_probable_entity(line):
            return line.strip()
    return None


def extract_issuer_name(text: str) -> Optional[str]:
    lines = normalise_lines(text)
    if not lines:
        return None

    if is_survey_noise_dominated(text):
        return extract_known_receipt_issuer(text)

    # 1. Explicit FROM/SUPPLIER/VENDOR labels.
    for idx, line in enumerate(lines[:80]):
        lower = line.lower().strip()
        same_line = re.match(r"^(from|supplier|vendor|seller)\s*[:\-]\s*(.+)$", line, re.IGNORECASE)
        if same_line and is_probable_entity(same_line.group(2)):
            return same_line.group(2).strip()

        if lower in ISSUER_LABELS or lower.rstrip(":") in ISSUER_LABELS:
            candidate = best_legal_entity(lines[idx + 1 : idx + 8])
            if candidate:
                return candidate

    # 2. Header area before TAX INVOICE / INVOICE DATE / INVOICE NUMBER.
    boundary = min(len(lines), 20)
    for idx, line in enumerate(lines[:35]):
        if re.search(r"\b(tax invoice|invoice date|invoice number|invoice no|document number)\b", line, re.IGNORECASE):
            boundary = max(1, idx)
            break

    header_lines = lines[:boundary]
    candidate = best_legal_entity(header_lines)
    if candidate:
        return candidate

    return best_legal_entity(lines[:12])


def extract_recipient_name(text: str) -> Optional[str]:
    lines = normalise_lines(text)
    if is_survey_noise_dominated(text):
        return None

    # 1. Labelled recipient blocks.
    for idx, line in enumerate(lines[:120]):
        lower = line.lower().strip()
        label = lower.rstrip(":")

        same_line = re.match(
            r"^(bill to|billed to|invoice to|customer|client|to)\s*[:\-]\s*(.+)$",
            line,
            re.IGNORECASE,
        )
        if same_line and is_probable_entity(same_line.group(2)):
            return same_line.group(2).strip()

        if label in RECIPIENT_LABELS:
            candidate = best_legal_entity(lines[idx + 1 : idx + 10])
            if candidate:
                return candidate

    # 2. Xero-style blocks: YOUR VAT NUMBER, then customer name follows.
    for idx, line in enumerate(lines[:120]):
        if re.search(r"\byour\s+vat\s+number\b", line, re.IGNORECASE):
            candidate = best_legal_entity(lines[idx + 1 : idx + 8])
            if candidate:
                return candidate

    return None


def name_matches_org(candidate: Optional[str], organisation: dict) -> bool:
    candidate_norm = normalise_name(candidate)
    if not candidate_norm:
        return False

    org_names = [
        organisation.get("name"),
        organisation.get("legal_name"),
        organisation.get("trading_name"),
    ]

    for org_name in org_names:
        org_norm = normalise_name(org_name)
        if not org_norm:
            continue
        if candidate_norm == org_norm:
            return True
        if len(org_norm) >= 5 and org_norm in candidate_norm:
            return True
        if len(candidate_norm) >= 5 and candidate_norm in org_norm:
            return True

    return False


def classify_document_direction(text: str, organisation: Optional[dict]) -> EntityDetectionResult:
    issuer = extract_issuer_name(text)
    recipient = extract_recipient_name(text)
    survey_noise_dominated = is_survey_noise_dominated(text)
    known_receipt_issuer = extract_known_receipt_issuer(text)
    retail_receipt = is_known_retail_receipt(text)

    if survey_noise_dominated or retail_receipt:
        issuer = known_receipt_issuer
        if not receipt_has_named_customer(text):
            recipient = "Cash/Card"

    if not organisation:
        return EntityDetectionResult(
            issuer_name=issuer,
            recipient_name=recipient,
            document_direction="unknown",
            organisation_match_status="organisation_not_loaded",
            validation_status="needs_review",
            validation_notes=(
                "OCR text is dominated by receipt survey/header noise; issuer/recipient require review."
                if survey_noise_dominated
                else "Organisation record was not available for document direction validation."
            ),
        )

    if survey_noise_dominated and not issuer:
        return EntityDetectionResult(
            issuer_name=None,
            recipient_name=None,
            document_direction="unknown",
            organisation_match_status="selected_org_not_found",
            validation_status="needs_review",
            validation_notes=(
                "OCR text is dominated by receipt survey/header noise. "
                "Issuer and recipient were left unknown instead of using survey words as entities."
            ),
            confidence_adjustment=-0.20,
        )

    issuer_matches = name_matches_org(issuer, organisation)
    recipient_matches = name_matches_org(recipient, organisation)

    if retail_receipt and issuer and recipient == "Cash/Card" and not issuer_matches:
        return EntityDetectionResult(
            issuer_name=issuer,
            recipient_name=recipient,
            document_direction="supplier_invoice_payable",
            organisation_match_status="cash_card_receipt",
            validation_status="passed",
            validation_notes="Retail cash/card receipt detected; recipient defaulted to Cash/Card.",
            confidence_adjustment=0.05,
        )

    if recipient_matches and not issuer_matches:
        return EntityDetectionResult(
            issuer_name=issuer,
            recipient_name=recipient,
            document_direction="supplier_invoice_payable",
            organisation_match_status="selected_org_is_recipient",
            validation_status="passed" if issuer else "needs_review",
            validation_notes=(
                "Selected organisation appears to be the invoice recipient."
                if issuer
                else "Selected organisation appears to be the recipient, but issuer could not be confidently detected."
            ),
            confidence_adjustment=0.05 if issuer else -0.05,
        )

    if issuer_matches and not recipient_matches:
        return EntityDetectionResult(
            issuer_name=issuer,
            recipient_name=recipient,
            document_direction="customer_sales_invoice",
            organisation_match_status="selected_org_is_issuer",
            validation_status="needs_review",
            validation_notes="Selected organisation appears to be the invoice issuer. This may be a sales invoice, not a supplier invoice payable.",
            confidence_adjustment=-0.15,
        )

    if issuer_matches and recipient_matches:
        return EntityDetectionResult(
            issuer_name=issuer,
            recipient_name=recipient,
            document_direction="unknown",
            organisation_match_status="ambiguous",
            validation_status="needs_review",
            validation_notes="Selected organisation appears in both issuer and recipient positions. Manual review required.",
            confidence_adjustment=-0.10,
        )

    if issuer or recipient:
        return EntityDetectionResult(
            issuer_name=issuer,
            recipient_name=recipient,
            document_direction="wrong_organisation",
            organisation_match_status="selected_org_not_found",
            validation_status="needs_review",
            validation_notes="Selected organisation was not detected as issuer or recipient on this document.",
            confidence_adjustment=-0.20,
        )

    return EntityDetectionResult(
        issuer_name=issuer,
        recipient_name=recipient,
        document_direction="unknown",
        organisation_match_status="selected_org_not_found",
        validation_status="needs_review",
        validation_notes="Could not confidently detect issuer or recipient. Manual review required.",
        confidence_adjustment=-0.20,
    )
