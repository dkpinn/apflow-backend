from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


HEADER_FIELDS = (
    "document_type",
    "document_direction",
    "supplier_name_extracted",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "subtotal",
    "tax_amount",
    "total_amount",
    "vat_number_extracted",
    "company_registration_number_extracted",
    "cus_code_extracted",
)

LINE_FIELDS = (
    "code",
    "description",
    "quantity",
    "unit_price",
    "discount_percent",
    "discount_amount",
    "tax_amount",
    "line_total",
)

MONEY_FIELDS = {"subtotal", "tax_amount", "total_amount", "unit_price", "discount_amount", "line_total"}
NUMBER_FIELDS = {"quantity", "discount_percent"}
DATE_FIELDS = {"invoice_date", "due_date"}
CRITICAL_HEADER_FIELDS = {
    "document_type",
    "document_direction",
    "supplier_name_extracted",
    "invoice_number",
    "invoice_date",
    "currency",
    "subtotal",
    "tax_amount",
    "total_amount",
}

PILOT_MIN_LOCKED_DOCUMENTS = 120
PILOT_MIN_PER_STRATUM = 20
PILOT_TARGET_ACCURACY = 0.95
PILOT_TARGET_WITHIN_TWO = 0.95


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _date(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    raw = str(value).strip()
    for candidate in (raw, raw[:10]):
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            pass
    return raw.casefold()


def values_match(field: str, actual: Any, expected: Any) -> bool:
    if field in MONEY_FIELDS:
        left, right = _decimal(actual), _decimal(expected)
        return left == right if left is None or right is None else abs(left - right) <= Decimal("0.01")
    if field in NUMBER_FIELDS:
        left, right = _decimal(actual), _decimal(expected)
        return left == right if left is None or right is None else abs(left - right) <= Decimal("0.0001")
    if field in DATE_FIELDS:
        return _date(actual) == _date(expected)
    return _text(actual) == _text(expected)


def build_invoice_benchmark_snapshot(
    *,
    invoice: dict[str, Any],
    line_items: list[dict[str, Any]],
    raw: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "document": {field: invoice.get(field) for field in HEADER_FIELDS},
        "line_items": [
            {field: item.get(field) for field in LINE_FIELDS}
            for item in sorted(
                line_items or [],
                key=lambda item: (int(item.get("sort_order") or 0), str(item.get("id") or "")),
            )
        ],
        "source": {
            "file_name": (raw or {}).get("file_name"),
            "file_type": (raw or {}).get("file_type"),
            "invoice_raw_id": invoice.get("invoice_raw_id"),
            "invoice_extracted_id": invoice.get("id"),
        },
    }


def validate_gold_snapshot(snapshot: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    document = snapshot.get("document") if isinstance(snapshot, dict) else None
    lines = snapshot.get("line_items") if isinstance(snapshot, dict) else None
    if not isinstance(document, dict):
        return ["Gold snapshot must contain a document object."]
    if not isinstance(lines, list):
        blockers.append("Gold snapshot must contain a line_items array.")
    for field in ("document_type", "supplier_name_extracted", "invoice_date", "currency", "total_amount"):
        if document.get(field) in (None, ""):
            blockers.append(f"Gold document is missing {field}.")
    if document.get("document_type") not in {"tax_invoice", "invoice", "credit_note", "card_receipt", "receipt"}:
        blockers.append("Gold document_type must be invoice, tax_invoice, credit_note, receipt, or card_receipt.")
    return blockers


def evaluate_invoice_against_gold(actual: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    actual_document = actual.get("document") or {}
    gold_document = gold.get("document") or {}
    actual_lines = actual.get("line_items") or []
    gold_lines = gold.get("line_items") or []

    discrepancies: list[dict[str, Any]] = []
    critical_errors: list[dict[str, Any]] = []
    correct = 0
    total = 0

    for field in HEADER_FIELDS:
        total += 1
        actual_value, expected_value = actual_document.get(field), gold_document.get(field)
        if values_match(field, actual_value, expected_value):
            correct += 1
            continue
        discrepancy = {
            "scope": "document",
            "field": field,
            "expected": expected_value,
            "actual": actual_value,
            "critical": field in CRITICAL_HEADER_FIELDS,
        }
        discrepancies.append(discrepancy)
        if discrepancy["critical"]:
            critical_errors.append(discrepancy)

    max_lines = max(len(actual_lines), len(gold_lines))
    for index in range(max_lines):
        actual_line = actual_lines[index] if index < len(actual_lines) else None
        gold_line = gold_lines[index] if index < len(gold_lines) else None
        if actual_line is None or gold_line is None:
            discrepancy = {
                "scope": "line_item",
                "line": index + 1,
                "field": "row",
                "expected": gold_line,
                "actual": actual_line,
                "critical": True,
            }
            discrepancies.append(discrepancy)
            critical_errors.append(discrepancy)
            total += len(LINE_FIELDS)
            continue
        for field in LINE_FIELDS:
            total += 1
            if values_match(field, actual_line.get(field), gold_line.get(field)):
                correct += 1
                continue
            discrepancy = {
                "scope": "line_item",
                "line": index + 1,
                "field": field,
                "expected": gold_line.get(field),
                "actual": actual_line.get(field),
                "critical": field in {"quantity", "unit_price", "tax_amount", "line_total"},
            }
            discrepancies.append(discrepancy)
            if discrepancy["critical"]:
                critical_errors.append(discrepancy)

    accuracy = correct / total if total else 0.0
    correction_count = len(discrepancies)
    return {
        "correct_values": correct,
        "total_values": total,
        "accuracy": round(accuracy, 6),
        "accuracy_percent": round(accuracy * 100, 2),
        "correction_count": correction_count,
        "within_two_corrections": correction_count <= 2,
        "exact_document": correction_count == 0,
        "expected_line_count": len(gold_lines),
        "extracted_line_count": len(actual_lines),
        "critical_error_count": len(critical_errors),
        "critical_errors": critical_errors,
        "discrepancies": discrepancies,
    }


def wilson_lower_bound(successes: int, trials: int, *, z: float = 1.6448536269514722) -> float:
    """One-sided 95% Wilson lower confidence bound."""
    if trials <= 0:
        return 0.0
    p = successes / trials
    z2 = z * z
    centre = p + z2 / (2 * trials)
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * trials)) / trials)
    return max(0.0, (centre - margin) / (1 + z2 / trials))


def build_pilot_accuracy_summary(cases: list[dict[str, Any]], latest_runs: list[dict[str, Any]]) -> dict[str, Any]:
    locked = [case for case in cases if case.get("dataset_split") == "locked"]
    latest_by_case = {str(run.get("gold_document_id")): run for run in latest_runs}
    scored = [latest_by_case[str(case.get("id"))] for case in locked if str(case.get("id")) in latest_by_case]
    correct = sum(int(run.get("correct_values") or 0) for run in scored)
    total = sum(int(run.get("total_values") or 0) for run in scored)
    within_two = sum(bool(run.get("within_two_corrections")) for run in scored)
    critical = sum(int(run.get("critical_error_count") or 0) for run in scored)
    accuracy = correct / total if total else 0.0
    within_two_rate = within_two / len(scored) if scored else 0.0
    accuracy_lower_bound = wilson_lower_bound(correct, total)
    within_two_lower_bound = wilson_lower_bound(within_two, len(scored))

    strata: dict[str, int] = {}
    for case in locked:
        key = f"{case.get('document_kind')}:{case.get('source_format')}"
        strata[key] = strata.get(key, 0) + 1
    required_strata = [
        f"{kind}:{source_format}"
        for kind in ("invoice", "credit_note", "receipt")
        for source_format in ("pdf", "image")
    ]
    sample_ready = (
        len(locked) >= PILOT_MIN_LOCKED_DOCUMENTS
        and len(scored) == len(locked)
        and all(strata.get(key, 0) >= PILOT_MIN_PER_STRATUM for key in required_strata)
    )
    gate_passed = (
        sample_ready
        and accuracy >= PILOT_TARGET_ACCURACY
        and accuracy_lower_bound >= PILOT_TARGET_ACCURACY
        and within_two_rate >= PILOT_TARGET_WITHIN_TWO
        and within_two_lower_bound >= PILOT_TARGET_WITHIN_TWO
        and critical == 0
    )
    return {
        "locked_documents": len(locked),
        "scored_documents": len(scored),
        "correct_values": correct,
        "total_values": total,
        "accuracy": round(accuracy, 6),
        "accuracy_percent": round(accuracy * 100, 2),
        "accuracy_lower_bound_95": round(accuracy_lower_bound, 6),
        "within_two_corrections_documents": within_two,
        "within_two_corrections_rate": round(within_two_rate, 6),
        "within_two_corrections_lower_bound_95": round(within_two_lower_bound, 6),
        "critical_error_count": critical,
        "strata": strata,
        "required_strata": required_strata,
        "minimum_locked_documents": PILOT_MIN_LOCKED_DOCUMENTS,
        "minimum_per_stratum": PILOT_MIN_PER_STRATUM,
        "sample_ready": sample_ready,
        "pilot_gate_passed": gate_passed,
    }
