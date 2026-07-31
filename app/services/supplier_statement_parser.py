from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from app.services.invoice_extraction.contact_parser import (
    extract_supplier_email,
    extract_supplier_fax,
    extract_supplier_postal_address,
    extract_supplier_telephone,
)
from app.services.invoice_extraction.entity_detection import classify_document_direction


def _parse_date(value: str) -> Optional[str]:
    clean = re.sub(r"\s+", " ", value.strip())
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(clean, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def extract_supplier_statement_date(text: str) -> Optional[str]:
    match = re.search(
        r"statement\s*(?:date|dt)\s*[:#\-]?\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        text or "",
        re.IGNORECASE,
    )
    return _parse_date(match.group(1)) if match else None


def _money(value: str) -> Optional[float]:
    clean = re.sub(r"[^0-9,.-]", "", value or "").replace(" ", "")
    if not clean:
        return None
    if "," in clean and "." not in clean:
        clean = clean.replace(",", ".") if len(clean.rsplit(",", 1)[-1]) == 2 else clean.replace(",", "")
    else:
        clean = clean.replace(",", "")
    try:
        return round(float(clean), 2)
    except ValueError:
        return None


def extract_supplier_statement_balance(text: str) -> Optional[float]:
    compact = re.sub(r"[\t ]+", " ", text or "")
    for pattern in (
        r"current\s+statement\s+total[^\dR]{0,40}(R?\s*[\d ,]+\.\d{2})",
        r"balance\s+due[\s\S]{0,180}?(R\s*[\d ,]+\.\d{2})",
        r"closing\s+balance[^\dR]{0,40}(R?\s*[\d ,]+\.\d{2})",
    ):
        match = re.search(pattern, compact, re.IGNORECASE)
        if match:
            amount = _money(match.group(1))
            if amount is not None:
                return amount
    return None


def normalise_supplier_statement(parsed: dict, text: str, organisation: Optional[dict]) -> dict:
    direction = classify_document_direction(text, organisation)
    statement_date = extract_supplier_statement_date(text)
    supplier_header = re.split(r"\bATTN\s*:", text or "", maxsplit=1, flags=re.IGNORECASE)[0]
    issuer = direction.issuer_name

    parsed.update({
        "document_type": "statement",
        "supplier_name_extracted": issuer,
        "issuer_name_extracted": issuer,
        "recipient_name_extracted": direction.recipient_name,
        "document_direction": direction.document_direction,
        "organisation_match_status": direction.organisation_match_status,
        "invoice_number": f"STATEMENT-{statement_date}" if statement_date else None,
        "invoice_date": statement_date,
        "due_date": None,
        "subtotal": None,
        "tax_amount": None,
        "total_amount": extract_supplier_statement_balance(text),
        "line_items": [],
        "supplier_telephone_extracted": extract_supplier_telephone(supplier_header),
        "supplier_fax_extracted": extract_supplier_fax(supplier_header),
        "supplier_email_extracted": extract_supplier_email(supplier_header),
        "supplier_acc_email_extracted": extract_supplier_email(supplier_header),
        "supplier_del_address_extracted": None,
        "supplier_pos_address_extracted": extract_supplier_postal_address(supplier_header),
        "cus_code_extracted": None,
        "bank_account_name_extracted": None,
        "bank_name_extracted": None,
        "bank_account_number_extracted": None,
        "bank_branch_code_extracted": None,
        "bank_swift_code_extracted": None,
        "validation_status": "passed" if issuer and statement_date else "needs_review",
        "validation_notes": "Supplier statement detected and routed out of the invoice workflow.",
    })
    return parsed
