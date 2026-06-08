from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from app.services.bank_statement_extraction import (
    MONEY_ZERO,
    ParsedBankLine,
    bank_statement_vlm_json_schema,
    dec_to_float,
    extract_bank_reference,
    extract_pdf_text,
    extract_statement,
    infer_column,
    infer_signed_amount,
    money,
    normalize_text,
    parse_csv_statement,
    parse_date,
    parse_text_statement,
    parse_text_statement_from_text,
    parse_transaction_blocks,
    parse_vlm_statement,
    parse_xlsx_statement,
    split_transaction_type_and_reference,
    stamp_extractor_selection,
    transaction_fingerprint,
)


def detect_line_duplicates(
    *,
    db,
    organisation_id: str,
    bank_account_id: str,
    lines: list[ParsedBankLine],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    hashes = [line.transaction_hash for line in lines]
    existing_hashes: set[str] = set()
    if hashes:
        try:
            res = (
                db.table("bank_statement_lines")
                .select("transaction_hash")
                .eq("organisation_id", organisation_id)
                .eq("bank_account_id", bank_account_id)
                .in_("transaction_hash", hashes)
                .execute()
            )
            existing_hashes = {row["transaction_hash"] for row in (res.data or [])}
        except Exception:
            existing_hashes = set()

    seen: set[str] = set()
    payloads: list[dict[str, Any]] = []
    duplicate_count = 0
    for line in lines:
        status = "clear"
        if line.transaction_hash in seen or line.transaction_hash in existing_hashes:
            status = "possible_duplicate"
            duplicate_count += 1
        seen.add(line.transaction_hash)
        payloads.append({"line": line, "duplicate_status": status})

    return payloads, {
        "duplicate_line_count": duplicate_count,
        "duplicate_status": "possible_duplicates" if duplicate_count else "clear",
        "checked_hashes": len(hashes),
    }


def validate_balances(
    *,
    account_current_balance: Decimal,
    header: dict[str, Any],
    lines: list[ParsedBankLine],
) -> dict[str, Any]:
    opening = money(header.get("opening_balance")) if header.get("opening_balance") is not None else None
    closing = money(header.get("closing_balance")) if header.get("closing_balance") is not None else None
    if opening is None or closing is None:
        return {"balance_status": "missing_balance", "expected_closing": None, "difference": None}
    if abs(opening - account_current_balance) > Decimal("0.01"):
        return {
            "balance_status": "opening_mismatch",
            "expected_opening": dec_to_float(account_current_balance),
            "actual_opening": dec_to_float(opening),
            "difference": dec_to_float(opening - account_current_balance),
        }
    expected = opening + sum((line.signed_amount for line in lines), MONEY_ZERO)
    if abs(expected - closing) > Decimal("0.01"):
        return {
            "balance_status": "closing_mismatch",
            "expected_closing": dec_to_float(expected),
            "actual_closing": dec_to_float(closing),
            "difference": dec_to_float(closing - expected),
        }
    return {"balance_status": "balanced", "expected_closing": dec_to_float(expected), "difference": 0}


def line_to_insert(
    line: ParsedBankLine,
    *,
    organisation_id: str,
    bank_account_id: str,
    upload_id: str,
    duplicate_status: str,
) -> dict[str, Any]:
    return {
        "organisation_id": organisation_id,
        "bank_account_id": bank_account_id,
        "bank_statement_upload_id": upload_id,
        "line_date": line.line_date,
        "value_date": line.value_date,
        "description": line.description,
        "reference": line.reference,
        "counterparty": line.counterparty,
        "transaction_type": line.transaction_type,
        "bank_reference": line.bank_reference,
        "raw_text": line.raw_text,
        "raw_lines": line.raw_lines or [],
        "source_page": line.source_page,
        "source_row_index": line.source_row_index,
        "extraction_confidence": line.extraction_confidence,
        "extraction_warnings": line.extraction_warnings or [],
        "debit_amount": dec_to_float(line.debit_amount) or 0,
        "credit_amount": dec_to_float(line.credit_amount) or 0,
        "signed_amount": dec_to_float(line.signed_amount) or 0,
        "balance_amount": dec_to_float(line.balance_amount),
        "currency": line.currency,
        "transaction_hash": line.transaction_hash,
        "duplicate_status": duplicate_status,
        "match_status": "unmatched",
        "allocation_status": "unallocated",
        "posting_status": "unposted",
    }


def score_invoice_suggestions(
    db,
    *,
    organisation_id: str,
    line: dict[str, Any],
    limit: int = 5,
) -> list[dict[str, Any]]:
    amount = abs(money(line.get("signed_amount")))
    text = " ".join(
        normalize_text(line.get(key))
        for key in ["reference", "bank_reference", "counterparty", "description", "raw_text"]
    ).lower()
    try:
        invoices = (
            db.table("invoices_extracted")
            .select("id, invoice_number, supplier_name, supplier_id, total_amount, invoice_date, review_status, approval_status")
            .eq("organisation_id", organisation_id)
            .limit(500)
            .execute()
            .data
            or []
        )
    except Exception:
        return []

    suggestions: list[dict[str, Any]] = []
    for invoice in invoices:
        invoice_total = money(invoice.get("total_amount"))
        difference = abs(invoice_total - amount)
        reference = normalize_text(invoice.get("invoice_number")).lower()
        supplier_name = normalize_text(invoice.get("supplier_name")).lower()
        confidence = Decimal("0.00")
        reasons: list[str] = []
        if reference and reference in text:
            confidence += Decimal("0.60")
            reasons.append("reference matches invoice number")
        if difference <= Decimal("0.01"):
            confidence += Decimal("0.30")
            reasons.append("amount matches")
        elif difference <= Decimal("1.00"):
            confidence += Decimal("0.15")
            reasons.append("amount is within tolerance")
        if supplier_name and supplier_name in text:
            confidence += Decimal("0.10")
            reasons.append("counterparty resembles supplier")
        if confidence <= Decimal("0.20"):
            continue
        suggestions.append(
            {
                "suggestion_type": "supplier_invoice",
                "confidence_score": float(min(confidence, Decimal("0.99"))),
                "rationale": "; ".join(reasons) or "possible invoice match",
                "matched_invoice_id": invoice.get("id"),
                "matched_invoice_number": invoice.get("invoice_number"),
                "evidence": {
                    "amount_difference": float(difference),
                    "invoice_total": float(invoice_total),
                    "line_amount": float(amount),
                },
            }
        )
    suggestions.sort(key=lambda suggestion: suggestion["confidence_score"], reverse=True)
    return suggestions[:limit]


RULE_FIELDS = {"description", "raw_text", "counterparty", "reference", "bank_reference"}


def bank_rule_search_fields(line: dict[str, Any]) -> dict[str, str]:
    return {
        "description": normalize_text(line.get("description")).lower(),
        "raw_text": normalize_text(line.get("raw_text")).lower(),
        "counterparty": normalize_text(line.get("counterparty")).lower(),
        "reference": normalize_text(line.get("reference")).lower(),
        "bank_reference": normalize_text(line.get("bank_reference")).lower(),
    }


def normalize_rule_criteria(criteria: Any) -> list[dict[str, str]]:
    if not isinstance(criteria, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in criteria:
        if not isinstance(item, dict):
            continue
        field = normalize_text(item.get("field")).lower()
        operator = normalize_text(item.get("operator") or "contains").lower()
        value = normalize_text(item.get("value"))
        if field not in RULE_FIELDS or operator != "contains" or not value:
            continue
        normalized.append({"field": field, "operator": "contains", "value": value})
    return normalized


def default_rule_criteria_from_line(line: dict[str, Any]) -> list[dict[str, str]]:
    candidates = [
        ("bank_reference", line.get("bank_reference")),
        ("counterparty", line.get("counterparty")),
        ("raw_text", line.get("raw_text")),
        ("description", line.get("description")),
        ("reference", line.get("reference")),
    ]
    criteria: list[dict[str, str]] = []
    for field, value in candidates:
        text = normalize_text(value)
        if not text:
            continue
        criteria.append({"field": field, "operator": "contains", "value": text[:80]})
        if len(criteria) >= 2:
            break
    return criteria


def rule_matches_criteria(rule: dict[str, Any], line: dict[str, Any]) -> bool:
    criteria = normalize_rule_criteria(rule.get("criteria"))
    if not criteria:
        return False
    mode = normalize_text(rule.get("criteria_mode") or "and").lower()
    fields = bank_rule_search_fields(line)
    matches = [
        normalize_text(item["value"]).lower() in fields.get(item["field"], "")
        for item in criteria
    ]
    if mode == "or":
        return any(matches)
    return all(matches)


def rule_criteria_rationale(rule: dict[str, Any]) -> str:
    criteria = normalize_rule_criteria(rule.get("criteria"))
    if not criteria:
        return f"matched rule: {rule.get('name')}"
    mode = normalize_text(rule.get("criteria_mode") or "and").upper()
    joined = f" {mode} ".join(f"{item['field']} contains '{item['value']}'" for item in criteria)
    return f"matched rule: {rule.get('name')} ({joined})"


def score_rule_suggestions(
    db,
    *,
    organisation_id: str,
    bank_account_id: str,
    line: dict[str, Any],
    limit: int = 5,
) -> list[dict[str, Any]]:
    try:
        rules = (
            db.table("bank_transaction_rules")
            .select("*")
            .eq("organisation_id", organisation_id)
            .eq("active", True)
            .order("priority", desc=False)
            .limit(200)
            .execute()
            .data
            or []
        )
    except Exception:
        return []

    fields = bank_rule_search_fields(line)
    amount = money(line.get("signed_amount"))
    direction = "money_in" if amount >= 0 else "money_out"
    suggestions: list[dict[str, Any]] = []
    for rule in rules:
        rule_account = rule.get("bank_account_id")
        if rule_account and str(rule_account) != bank_account_id:
            continue
        if rule.get("amount_direction") not in (None, "any", direction):
            continue
        min_amount = rule.get("min_amount")
        max_amount = rule.get("max_amount")
        absolute_amount = abs(amount)
        if min_amount is not None and absolute_amount < money(min_amount):
            continue
        if max_amount is not None and absolute_amount > money(max_amount):
            continue
        matched = rule_matches_criteria(rule, line)
        if not matched:
            for field_value, pattern in [
                (fields["description"], rule.get("description_pattern")),
                (fields["reference"], rule.get("reference_pattern")),
                (fields["counterparty"], rule.get("counterparty_pattern")),
            ]:
                pattern_text = normalize_text(pattern).lower()
                if not pattern_text:
                    continue
                if rule.get("match_type") == "exact" and field_value == pattern_text:
                    matched = True
                elif rule.get("match_type") == "regex":
                    try:
                        matched = bool(re.search(pattern_text, field_value))
                    except re.error:
                        matched = False
                elif pattern_text in field_value:
                    matched = True
        if matched:
            suggestions.append(
                {
                    "suggestion_type": "rule",
                    "confidence_score": 0.85,
                    "rationale": rule_criteria_rationale(rule),
                    "suggested_account_id": rule.get("gl_account_id"),
                    "suggested_tracking": rule.get("tracking") or {},
                    "suggested_tax_treatment": rule.get("tax_treatment"),
                    "evidence": {
                        "rule_id": rule.get("id"),
                        "rule_name": rule.get("name"),
                        "criteria": normalize_rule_criteria(rule.get("criteria")),
                    },
                }
            )
    return suggestions[:limit]


def journal_lines_for_bank_transaction(
    *,
    organisation_id: str,
    bank_account_gl_id: str,
    allocation_account_id: str,
    amount: Decimal,
    description: str,
    tracking: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    absolute = abs(amount)
    if absolute == MONEY_ZERO:
        raise ValueError("Cannot create a journal for a zero-value transaction")
    if amount >= 0:
        return [
            {
                "organisation_id": organisation_id,
                "account_id": bank_account_gl_id,
                "description": description,
                "debit_amount": float(absolute),
                "credit_amount": 0,
                "tracking": {},
                "sort_order": 0,
            },
            {
                "organisation_id": organisation_id,
                "account_id": allocation_account_id,
                "description": description,
                "debit_amount": 0,
                "credit_amount": float(absolute),
                "tracking": tracking or {},
                "sort_order": 1,
            },
        ]
    return [
        {
            "organisation_id": organisation_id,
            "account_id": allocation_account_id,
            "description": description,
            "debit_amount": float(absolute),
            "credit_amount": 0,
            "tracking": tracking or {},
            "sort_order": 0,
        },
        {
            "organisation_id": organisation_id,
            "account_id": bank_account_gl_id,
            "description": description,
            "debit_amount": 0,
            "credit_amount": float(absolute),
            "tracking": {},
            "sort_order": 1,
        },
    ]


def reversal_lines_for_journal(
    lines: list[dict[str, Any]],
    *,
    description: str,
) -> list[dict[str, Any]]:
    reversed_lines: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        reversed_lines.append(
            {
                "organisation_id": line["organisation_id"],
                "account_id": line.get("account_id"),
                "description": description,
                "debit_amount": dec_to_float(money(line.get("credit_amount"))) or 0,
                "credit_amount": dec_to_float(money(line.get("debit_amount"))) or 0,
                "tracking": line.get("tracking") or {},
                "sort_order": index,
            }
        )
    return reversed_lines


def new_uuid() -> str:
    return str(uuid4())
