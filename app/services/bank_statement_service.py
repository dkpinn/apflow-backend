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


def _line_value(line: Any, key: str, default: Any = None) -> Any:
    if isinstance(line, dict):
        return line.get(key, default)
    return getattr(line, key, default)


def correct_amounts_from_balance(
    lines: list[ParsedBankLine],
    *,
    bank_account_id: str,
) -> dict[str, Any]:
    """Fix amounts that a VLM misread by recomputing them from the running balance.

    If balance[n] - balance[n-1] disagrees with signed_amount by more than
    one cent the amount is likely a column-misalignment artefact (e.g. the
    FNB '0.000.00Cr' nil-amount rows that VLM reads as the next row's value).
    The balance column is almost always correct, so we trust it — BUT only when
    the majority of lines carry balance data and fewer than half need correction
    (if more than half need correcting the balance column itself was likely
    misread, and applying corrections would corrupt good amounts).
    """
    summary: dict[str, Any] = {
        "status": "no_lines",
        "line_count": len(lines),
        "lines_with_balance": 0,
        "corrections_needed": 0,
        "corrections_applied": 0,
        "corrections": [],
    }
    if not lines:
        return summary

    # Require at least 60% of lines to have a balance before trusting it.
    lines_with_balance = sum(1 for ln in lines if _line_value(ln, "balance_amount") is not None)
    summary["lines_with_balance"] = lines_with_balance
    if lines_with_balance < max(2, len(lines) * 0.6):
        logger.debug("[BALANCE-CORRECT] Skipping: only %d/%d lines have balance_amount", lines_with_balance, len(lines))
        summary["status"] = "skipped_insufficient_balance_data"
        return summary

    # Dry-run: count how many corrections would be applied.
    previous_balance: Optional[Decimal] = None
    corrections_needed = 0
    for line in lines:
        balance_amount = _line_value(line, "balance_amount")
        if balance_amount is not None and previous_balance is not None:
            expected = money(balance_amount) - money(previous_balance)
            if abs(expected - _line_value(line, "signed_amount", MONEY_ZERO)) > Decimal("0.01"):
                corrections_needed += 1
        if balance_amount is not None:
            previous_balance = balance_amount
    summary["corrections_needed"] = corrections_needed

    # If more than half the lines need "correction" the balance column is suspect.
    if corrections_needed > lines_with_balance * 0.5:
        logger.warning(
            "[BALANCE-CORRECT] Skipping: %d/%d lines would be corrected — balance column likely misread",
            corrections_needed, lines_with_balance,
        )
        summary["status"] = "skipped_untrusted_balance_column"
        return summary

    # Apply corrections.
    previous_balance = None
    for row_index, line in enumerate(lines):
        balance_amount = _line_value(line, "balance_amount")
        if balance_amount is not None and previous_balance is not None:
            expected = money(balance_amount) - money(previous_balance)
            current_amount = _line_value(line, "signed_amount", MONEY_ZERO)
            if abs(expected - current_amount) > Decimal("0.01"):
                correction = {
                    "row_index": row_index,
                    "date": _line_value(line, "line_date"),
                    "description": (_line_value(line, "description", "") or "")[:80],
                    "previous_amount": dec_to_float(money(current_amount)),
                    "corrected_amount": dec_to_float(expected),
                    "previous_balance": dec_to_float(money(previous_balance)),
                    "balance": dec_to_float(money(balance_amount)),
                }
                if isinstance(line, dict):
                    line["signed_amount"] = expected
                    line["debit_amount"] = abs(expected) if expected < MONEY_ZERO else MONEY_ZERO
                    line["credit_amount"] = expected if expected >= MONEY_ZERO else MONEY_ZERO
                    line["transaction_hash"] = transaction_fingerprint(
                        bank_account_id=bank_account_id,
                        line_date=line.get("line_date"),
                        amount=expected,
                        reference=line.get("reference"),
                        counterparty=line.get("counterparty"),
                        bank_reference=line.get("bank_reference"),
                        description=line.get("description"),
                    )
                else:
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
                summary["corrections"].append(correction)
                summary["corrections_applied"] += 1
        if balance_amount is not None:
            previous_balance = balance_amount
    if corrections_needed:
        logger.debug("[BALANCE-CORRECT] Applied %d corrections from running balance", corrections_needed)
    summary["status"] = "applied" if summary["corrections_applied"] else "no_corrections"
    return summary


def _parse_iso_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None  # e.g. 29 Feb on a non-leap year


def _set_line_date(line: ParsedBankLine, new_iso: str, bank_account_id: str) -> None:
    """Overwrite a line's date and recompute its dedup fingerprint (hash includes the date)."""
    amount = money(_line_value(line, "signed_amount", MONEY_ZERO))
    new_hash = transaction_fingerprint(
        bank_account_id=bank_account_id,
        line_date=new_iso,
        amount=amount,
        reference=_line_value(line, "reference"),
        counterparty=_line_value(line, "counterparty"),
        bank_reference=_line_value(line, "bank_reference"),
        description=_line_value(line, "description"),
    )
    if isinstance(line, dict):
        line["line_date"] = new_iso
        line["transaction_hash"] = new_hash
    else:
        line.line_date = new_iso
        line.transaction_hash = new_hash


def normalize_dates_from_period(
    lines: list[ParsedBankLine],
    header: dict[str, Any],
    *,
    bank_account_id: str,
) -> dict[str, Any]:
    """Derive each transaction's YEAR from the statement period.

    The statement period is ground truth: every transaction must fall within it.
    VLM extractions frequently attach the wrong year (e.g. December 2025 on a
    statement running 11 Dec 2024 – 11 Jan 2025) and nothing downstream checks
    dates, so the error reaches the user unflagged. We keep each row's month and
    day but recompute the year from the period, using chronological monotonicity
    to resolve the Dec→Jan boundary, and flag any date that still cannot be
    placed inside the period.
    """
    summary: dict[str, Any] = {
        "status": "no_period",
        "line_count": len(lines),
        "corrections_applied": 0,
        "out_of_period_count": 0,
        "out_of_period_row_indexes": [],
        "corrections": [],
    }
    period_from = header.get("statement_period_from")
    period_to = header.get("statement_period_to")
    from_date = _parse_iso_date(period_from)
    to_date = _parse_iso_date(period_to)
    if not lines or from_date is None or to_date is None:
        return summary
    if to_date < from_date:
        summary["status"] = "invalid_period"
        return summary

    tol = timedelta(days=5)  # allow a few edge-posting days on each end
    lo, hi = from_date - tol, to_date + tol
    candidate_years = sorted({from_date.year, to_date.year})

    summary["status"] = "applied"
    prev_date: Optional[date] = None
    applied = 0
    out_of_period_rows: list[int] = []

    for row_index, line in enumerate(lines):
        cur = _parse_iso_date(_line_value(line, "line_date"))
        if cur is None:
            continue
        candidates = [c for c in (_safe_date(y, cur.month, cur.day) for y in candidate_years) if c is not None]
        if not candidates:
            continue

        in_range = [c for c in candidates if lo <= c <= hi]
        if in_range:
            if prev_date is not None:
                forward = [c for c in in_range if c >= prev_date]
                chosen = min(forward) if forward else min(in_range, key=lambda c: abs((c - prev_date).days))
            else:
                chosen = min(in_range)
        else:
            chosen = cur  # cannot be placed in the period — keep, but flag below

        if lo <= chosen <= hi:
            prev_date = chosen
        else:
            out_of_period_rows.append(row_index)

        new_iso = chosen.isoformat()
        if new_iso != cur.isoformat():
            _set_line_date(line, new_iso, bank_account_id)
            summary["corrections"].append({
                "row_index": row_index,
                "from": cur.isoformat(),
                "to": new_iso,
                "description": (_line_value(line, "description", "") or "")[:80],
            })
            applied += 1

    summary["corrections_applied"] = applied
    summary["out_of_period_count"] = len(out_of_period_rows)
    summary["out_of_period_row_indexes"] = out_of_period_rows
    return summary


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


# Single source of truth for balance reconciliation tolerance (one cent).
BALANCE_TOLERANCE = Decimal("0.01")


def analyze_balance_integrity(
    lines: list[ParsedBankLine], header: dict[str, Any]
) -> dict[str, Any]:
    """Walk the running-balance column and classify every row.

    The running balance is an arithmetic checksum: for each row that carries a
    balance, ``prev_balance + credit - debit`` must equal ``curr_balance``. When
    that equation breaks it means one of two things:
      * the row's own amount was misread (``amount_wrong``), or
      * one or more transactions are MISSING between the previous balanced row
        and this one (``row_missing_before``) — a dropped row makes the balance
        jump by more than the row's own amount.

    Returns per-row diagnostics (aligned to the extraction order, so the review
    UI can highlight the exact row) plus a summary. ``row_index`` is 0-based and
    matches the position in ``lines``.
    """
    if not lines:
        return {
            "balance_walk_status": "no_lines",
            "balance_walk_mismatches": 0,
            "balance_walk_details": [],
            "rows": [],
            "first_break_row_index": None,
            "missing_row_suspected": False,
            "balance_missing_count": 0,
        }

    opening = header.get("opening_balance")
    prev_balance: Optional[Decimal] = money(opening) if opening is not None else None

    rows: list[dict] = []
    mismatches: list[dict] = []
    first_break_row_index: Optional[int] = None
    missing_row_suspected = False
    balance_missing_count = 0

    for i, line in enumerate(lines):
        balance_amount = _line_value(line, "balance_amount")
        credit = money(_line_value(line, "credit_amount", MONEY_ZERO))
        debit = money(_line_value(line, "debit_amount", MONEY_ZERO))
        signed = credit - debit

        row: dict[str, Any] = {
            "row_index": i,
            "date": str(_line_value(line, "line_date") or ""),
            "description": (_line_value(line, "description", "") or "")[:60],
            "amount": dec_to_float(signed),
            "actual_balance": None,
            "expected_balance": None,
            "diff": None,
            "status": "ok",
        }

        if balance_amount is None:
            # No balance evidence on this row — we cannot verify it. Report it
            # rather than silently skipping (silent skips hid real breaks).
            balance_missing_count += 1
            row["status"] = "balance_missing"
            rows.append(row)
            # prev_balance is unchanged: we roll it forward by this row's amount
            # so a run of balance-less rows can still be checked at the next one.
            if prev_balance is not None:
                prev_balance = prev_balance + signed
            continue

        curr = money(balance_amount)
        row["actual_balance"] = dec_to_float(curr)

        if prev_balance is not None:
            expected = prev_balance + signed
            diff = abs(expected - curr)
            row["expected_balance"] = dec_to_float(expected)
            row["diff"] = dec_to_float(diff)
            if diff > BALANCE_TOLERANCE:
                # The chain broke. Decide whether the row's amount is wrong or a
                # transaction is missing before it.
                actual_delta = curr - prev_balance
                if abs(actual_delta - signed) > BALANCE_TOLERANCE:
                    # The real balance movement doesn't match this row's amount.
                    if abs(signed) <= BALANCE_TOLERANCE and abs(actual_delta) > BALANCE_TOLERANCE:
                        # This row moved the balance but carries no amount → a
                        # transaction is missing here.
                        row["status"] = "row_missing_before"
                        missing_row_suspected = True
                    else:
                        # The balance jumped by more than this row explains — the
                        # most common cause is a dropped row just before it.
                        row["status"] = "row_missing_before"
                        missing_row_suspected = True
                else:
                    row["status"] = "amount_wrong"
                if first_break_row_index is None:
                    first_break_row_index = i
                mismatches.append({
                    "row_index": i,
                    "date": row["date"],
                    "description": row["description"],
                    "expected_balance": row["expected_balance"],
                    "actual_balance": row["actual_balance"],
                    "diff": row["diff"],
                    "status": row["status"],
                })

        rows.append(row)
        prev_balance = curr

    status = "balanced" if not mismatches else "balance_walk_failed"
    return {
        "balance_walk_status": status,
        "balance_walk_mismatches": len(mismatches),
        "balance_walk_details": mismatches[:20],
        "rows": rows,
        "first_break_row_index": first_break_row_index,
        "missing_row_suspected": missing_row_suspected,
        "balance_missing_count": balance_missing_count,
    }


def validate_running_balance(lines: list[ParsedBankLine], header: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible summary wrapper around :func:`analyze_balance_integrity`.

    Existing callers expect ``balance_walk_status`` / ``balance_walk_mismatches``
    / ``balance_walk_details``; the richer analysis adds per-row diagnostics.
    """
    return analyze_balance_integrity(lines, header)


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
            .select(
                "id, invoice_number, supplier_name_extracted, supplier_id, "
                "total_amount, invoice_date, review_status, approval_status"
            )
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
        supplier_name = normalize_text(
            invoice.get("supplier_name_extracted") or invoice.get("supplier_name")
        ).lower()
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


def score_supplier_invoice_suggestions_from_rows(
    *,
    line: dict[str, Any],
    invoices: list[dict[str, Any]],
    limit: int = 5,
) -> list[dict[str, Any]]:
    signed_amount = money(line.get("signed_amount"))
    amount = abs(signed_amount)
    if signed_amount >= 0:
        return []
    text = " ".join(
        normalize_text(line.get(key))
        for key in ["reference", "bank_reference", "counterparty", "description", "raw_text"]
    ).lower()
    date_floor: date | None = None
    line_date_str = line.get("line_date")
    if line_date_str:
        try:
            date_floor = date.fromisoformat(str(line_date_str)[:10]) - timedelta(days=180)
        except (ValueError, TypeError):
            date_floor = None

    suggestions: list[dict[str, Any]] = []
    for invoice in invoices:
        if date_floor and invoice.get("invoice_date"):
            try:
                invoice_date = date.fromisoformat(str(invoice.get("invoice_date"))[:10])
            except (ValueError, TypeError):
                invoice_date = None
            if invoice_date and invoice_date < date_floor:
                continue
        invoice_total = money(invoice.get("total_amount"))
        difference = abs(invoice_total - amount)
        reference = normalize_text(invoice.get("invoice_number")).lower()
        supplier_name = normalize_text(
            invoice.get("supplier_name_extracted") or invoice.get("supplier_name")
        ).lower()
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
TEXT_OPERATORS = {"contains", "starts_with", "ends_with", "exact"}
AMOUNT_OPERATORS = {"eq", "gt", "gte", "lt", "lte", "between"}


def bank_rule_search_fields(line: dict[str, Any]) -> dict[str, str]:
    return {
        "description": normalize_text(line.get("description")).lower(),
        "raw_text": normalize_text(line.get("raw_text")).lower(),
        "counterparty": normalize_text(line.get("counterparty")).lower(),
        "reference": normalize_text(line.get("reference")).lower(),
        "bank_reference": normalize_text(line.get("bank_reference")).lower(),
    }


def _match_text(text: str, operator: str, value: str) -> bool:
    t, v = text.lower(), value.lower()
    if operator == "starts_with":
        return t.startswith(v)
    if operator == "ends_with":
        return t.endswith(v)
    if operator == "exact":
        return t == v
    return v in t


def _match_amount(amount: Decimal, operator: str, value: str, value2: str | None) -> bool:
    try:
        v = Decimal(value)
        a = abs(amount)
        if operator == "eq":
            return a == v
        if operator == "gt":
            return a > v
        if operator == "gte":
            return a >= v
        if operator == "lt":
            return a < v
        if operator == "lte":
            return a <= v
        if operator == "between" and value2:
            return Decimal(value2) >= a >= v
    except Exception:
        pass
    return False


def normalize_rule_criteria(criteria: Any) -> list[dict[str, Any]]:
    if not isinstance(criteria, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in criteria:
        if not isinstance(item, dict):
            continue
        field = normalize_text(item.get("field")).lower()
        operator = normalize_text(item.get("operator") or "contains").lower()
        value = normalize_text(item.get("value"))
        if field == "amount":
            if operator not in AMOUNT_OPERATORS or not value:
                continue
            entry: dict[str, Any] = {"field": "amount", "operator": operator, "value": value}
            if operator == "between":
                value2 = normalize_text(item.get("value2"))
                if not value2:
                    continue
                entry["value2"] = value2
            normalized.append(entry)
        else:
            if field not in RULE_FIELDS or operator not in TEXT_OPERATORS or not value:
                continue
            normalized.append({"field": field, "operator": operator, "value": value})
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
    text_fields = bank_rule_search_fields(line)
    signed_amount = money(line.get("signed_amount"))

    def _item_matches(item: dict[str, Any]) -> bool:
        field = item["field"]
        operator = item["operator"]
        value = item["value"]
        if field == "amount":
            return _match_amount(signed_amount, operator, value, item.get("value2"))
        return _match_text(text_fields.get(field, ""), operator, value)

    matches = [_item_matches(item) for item in criteria]
    if mode == "or":
        return any(matches)
    return all(matches)


def rule_criteria_rationale(rule: dict[str, Any]) -> str:
    criteria = normalize_rule_criteria(rule.get("criteria"))
    if not criteria:
        return f"matched rule: {rule.get('name')}"
    mode = normalize_text(rule.get("criteria_mode") or "and").upper()
    def _describe(item: dict[str, Any]) -> str:
        op = item.get("operator", "contains").replace("_", " ")
        val = item["value"]
        v2 = item.get("value2")
        return f"{item['field']} {op} '{val}'" + (f" and '{v2}'" if v2 else "")

    joined = f" {mode} ".join(_describe(item) for item in criteria)
    return f"matched rule: {rule.get('name')} ({joined})"


def bank_rule_matches(
    rule: dict[str, Any],
    *,
    bank_account_id: str,
    line: dict[str, Any],
) -> bool:
    rule_account = rule.get("bank_account_id")
    if rule_account and str(rule_account) != str(bank_account_id):
        return False
    amount = money(line.get("signed_amount"))
    direction = "money_in" if amount >= 0 else "money_out"
    if rule.get("amount_direction") not in (None, "any", direction):
        return False
    min_amount = rule.get("min_amount")
    max_amount = rule.get("max_amount")
    absolute_amount = abs(amount)
    if min_amount is not None and absolute_amount < money(min_amount):
        return False
    if max_amount is not None and absolute_amount > money(max_amount):
        return False

    if rule_matches_criteria(rule, line):
        return True

    fields = bank_rule_search_fields(line)
    for field_value, pattern in [
        (fields["description"], rule.get("description_pattern")),
        (fields["reference"], rule.get("reference_pattern")),
        (fields["counterparty"], rule.get("counterparty_pattern")),
    ]:
        pattern_text = normalize_text(pattern).lower()
        if not pattern_text:
            continue
        if rule.get("match_type") == "exact" and field_value == pattern_text:
            return True
        if rule.get("match_type") == "regex":
            try:
                if re.search(pattern_text, field_value):
                    return True
            except re.error:
                continue
        elif pattern_text in field_value:
            return True
    return False


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

    return score_rule_suggestions_from_rows(
        rules=rules,
        bank_account_id=bank_account_id,
        line=line,
        limit=limit,
    )


def score_rule_suggestions_from_rows(
    *,
    rules: list[dict[str, Any]],
    bank_account_id: str,
    line: dict[str, Any],
    limit: int = 5,
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for rule in rules:
        if bank_rule_matches(rule, bank_account_id=bank_account_id, line=line):
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
