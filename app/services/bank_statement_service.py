from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

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


def correct_amounts_from_balance(
    lines: list[ParsedBankLine],
    *,
    bank_account_id: str,
) -> list[ParsedBankLine]:
    """Fix amounts that a VLM misread by recomputing them from the running balance.

    If balance[n] - balance[n-1] disagrees with signed_amount by more than
    one cent the amount is likely a column-misalignment artefact (e.g. the
    FNB '0.000.00Cr' nil-amount rows that VLM reads as the next row's value).
    The balance column is almost always correct, so we trust it — BUT only when
    the majority of lines carry balance data and fewer than half need correction
    (if more than half need correcting the balance column itself was likely
    misread, and applying corrections would corrupt good amounts).
    """
    if not lines:
        return lines

    # Require at least 60% of lines to have a balance before trusting it.
    lines_with_balance = sum(1 for ln in lines if ln.balance_amount is not None)
    if lines_with_balance < max(2, len(lines) * 0.6):
        logger.debug("[BALANCE-CORRECT] Skipping: only %d/%d lines have balance_amount", lines_with_balance, len(lines))
        return lines

    # Dry-run: count how many corrections would be applied.
    previous_balance: Optional[Decimal] = None
    corrections_needed = 0
    for line in lines:
        if line.balance_amount is not None and previous_balance is not None:
            expected = money(line.balance_amount) - money(previous_balance)
            if abs(expected - line.signed_amount) > Decimal("0.01"):
                corrections_needed += 1
        if line.balance_amount is not None:
            previous_balance = line.balance_amount

    # If more than half the lines need "correction" the balance column is suspect.
    if corrections_needed > lines_with_balance * 0.5:
        logger.warning(
            "[BALANCE-CORRECT] Skipping: %d/%d lines would be corrected — balance column likely misread",
            corrections_needed, lines_with_balance,
        )
        return lines

    # Apply corrections.
    previous_balance = None
    for line in lines:
        if line.balance_amount is not None and previous_balance is not None:
            expected = money(line.balance_amount) - money(previous_balance)
            if abs(expected - line.signed_amount) > Decimal("0.01"):
                line.signed_amount = expected
                line.debit_amount = abs(expected) if expected < MONEY_ZERO else MONEY_ZERO
                line.credit_amount = expected if expected >= MONEY_ZERO else MONEY_ZERO
                line.transaction_hash = transaction_fingerprint(
                    bank_account_id=bank_account_id,
                    line_date=line.line_date,
                    amount=line.signed_amount,
                    reference=line.reference,
                    counterparty=line.counterparty,
                    bank_reference=line.bank_reference,
                    description=line.description,
                )
        if line.balance_amount is not None:
            previous_balance = line.balance_amount
    if corrections_needed:
        logger.debug("[BALANCE-CORRECT] Applied %d corrections from running balance", corrections_needed)
    return lines


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
            logger.exception("detect_line_duplicates DB query failed for org=%s", organisation_id)
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
    account_current_balance: Optional[Decimal],
    header: dict[str, Any],
    lines: list[ParsedBankLine],
) -> dict[str, Any]:
    opening = money(header.get("opening_balance")) if header.get("opening_balance") is not None else None
    closing = money(header.get("closing_balance")) if header.get("closing_balance") is not None else None
    if opening is None or closing is None:
        return {"balance_status": "missing_balance", "expected_closing": None, "difference": None}
    if account_current_balance is not None:
        current = money(account_current_balance)
        if abs(opening - current) > Decimal("0.01"):
            return {
                "balance_status": "opening_mismatch",
                "expected_opening": dec_to_float(current),
                "actual_opening": dec_to_float(opening),
                "difference": dec_to_float(opening - current),
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


def validate_running_balance(lines: list[ParsedBankLine], header: dict[str, Any]) -> dict[str, Any]:
    """Walk each line's balance column: prev_balance + credit - debit = curr_balance.

    Returns a summary dict included in extraction_evidence.
    """
    if not lines:
        return {"balance_walk_status": "no_lines", "balance_walk_mismatches": 0}

    mismatches: list[dict] = []
    prev_balance: Optional[Decimal] = (
        money(header["opening_balance"]) if header.get("opening_balance") is not None else None
    )

    for i, line in enumerate(lines):
        if line.balance_amount is None:
            continue
        curr = line.balance_amount
        if prev_balance is not None:
            expected = prev_balance + line.credit_amount - line.debit_amount
            diff = abs(expected - curr)
            if diff > Decimal("0.02"):
                mismatches.append({
                    "row_index": i,
                    "date": str(line.line_date or ""),
                    "description": (line.description or "")[:60],
                    "expected_balance": dec_to_float(expected),
                    "actual_balance": dec_to_float(curr),
                    "diff": dec_to_float(diff),
                })
        prev_balance = curr

    status = "balanced" if not mismatches else "balance_walk_failed"
    return {
        "balance_walk_status": status,
        "balance_walk_mismatches": len(mismatches),
        "balance_walk_details": mismatches[:20],
    }


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
    signed_amount = money(line.get("signed_amount"))
    amount = abs(signed_amount)
    text = " ".join(
        normalize_text(line.get(key))
        for key in ["reference", "bank_reference", "counterparty", "description", "raw_text"]
    ).lower()
    if signed_amount >= 0:
        _date_floor: str | None = None
        _line_date_str = line.get("line_date")
        if _line_date_str:
            try:
                _ld = date.fromisoformat(str(_line_date_str)[:10])
                _date_floor = (_ld - timedelta(days=180)).isoformat()
            except (ValueError, TypeError):
                pass
        try:
            _q = (
                db.table("sales_invoices")
                .select(
                    "id, invoice_number, customer_id, total_amount, amount_outstanding, "
                    "issue_date, due_date, status, payment_status, customer_snapshot"
                )
                .eq("organisation_id", organisation_id)
                .eq("document_type", "invoice")
                .eq("status", "issued")
            )
            if _date_floor:
                _q = _q.gte("issue_date", _date_floor)
            invoices = _q.limit(1000).execute().data or []
            receivables = (
                db.table("accounts")
                .select("id")
                .eq("organisation_id", organisation_id)
                .eq("system_key", "trade_receivables")
                .limit(1)
                .execute()
                .data
                or []
            )
            receivables_id = receivables[0].get("id") if receivables else None
        except Exception:
            logger.exception("score_invoice_suggestions receivables query failed for org=%s", organisation_id)
            return []

        suggestions: list[dict[str, Any]] = []
        for invoice in invoices:
            outstanding = money(invoice.get("amount_outstanding") or invoice.get("total_amount"))
            if outstanding <= 0:
                continue
            difference = abs(outstanding - amount)
            reference = normalize_text(invoice.get("invoice_number")).lower()
            customer_snapshot = invoice.get("customer_snapshot") or {}
            customer_name = normalize_text(
                customer_snapshot.get("legal_name") or customer_snapshot.get("trading_name")
            ).lower()
            confidence = Decimal("0.00")
            reasons: list[str] = []
            if reference and reference in text:
                confidence += Decimal("0.60")
                reasons.append("reference matches sales invoice number")
            if difference <= Decimal("0.01"):
                confidence += Decimal("0.30")
                reasons.append("amount matches outstanding balance")
            elif amount < outstanding:
                confidence += Decimal("0.15")
                reasons.append("amount is a possible partial receipt")
            elif difference <= Decimal("1.00"):
                confidence += Decimal("0.15")
                reasons.append("amount is within tolerance")
            if customer_name and customer_name in text:
                confidence += Decimal("0.10")
                reasons.append("counterparty resembles customer")
            if confidence <= Decimal("0.20"):
                continue
            suggestions.append(
                {
                    "suggestion_type": "receivable_invoice",
                    "confidence_score": float(min(confidence, Decimal("0.99"))),
                    "rationale": "; ".join(reasons),
                    "matched_sales_invoice_id": invoice.get("id"),
                    "matched_invoice_number": invoice.get("invoice_number"),
                    "suggested_account_id": receivables_id,
                    "evidence": {
                        "amount_difference": float(difference),
                        "invoice_outstanding": float(outstanding),
                        "line_amount": float(amount),
                        "customer_id": invoice.get("customer_id"),
                    },
                }
            )
        suggestions.sort(key=lambda suggestion: suggestion["confidence_score"], reverse=True)
        return suggestions[:limit]

    _date_floor_s: str | None = None
    _line_date_str_s = line.get("line_date")
    if _line_date_str_s:
        try:
            _ld_s = date.fromisoformat(str(_line_date_str_s)[:10])
            _date_floor_s = (_ld_s - timedelta(days=180)).isoformat()
        except (ValueError, TypeError):
            pass
    try:
        _sq = (
            db.table("invoices_extracted")
            .select("id, invoice_number, supplier_name, supplier_id, total_amount, invoice_date, review_status, approval_status")
            .eq("organisation_id", organisation_id)
        )
        if _date_floor_s:
            _sq = _sq.gte("invoice_date", _date_floor_s)
        invoices = _sq.limit(1000).execute().data or []
    except Exception:
        logger.exception("score_invoice_suggestions supplier query failed for org=%s", organisation_id)
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
        logger.exception("score_rule_suggestions DB query failed for org=%s", organisation_id)
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
