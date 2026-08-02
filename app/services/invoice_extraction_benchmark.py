from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, Optional


BENCHMARK_SCHEMA_VERSION = 2

HEADER_FIELDS = (
    "document_type",
    "document_direction",
    "document_count",
    "supplier_name_extracted",
    "issuer_name_extracted",
    "recipient_name_extracted",
    "invoice_number",
    "document_reference",
    "invoice_date",
    "due_date",
    "currency",
    "subtotal",
    "tax_amount",
    "total_amount",
    "vat_number_extracted",
    "company_registration_number_extracted",
    "cus_code_extracted",
    "supplier_email_extracted",
    "supplier_acc_email_extracted",
    "supplier_telephone_extracted",
    "supplier_fax_extracted",
    "supplier_cell_extracted",
    "supplier_website_extracted",
    "supplier_del_address_extracted",
    "supplier_pos_address_extracted",
    "bank_account_name_extracted",
    "bank_name_extracted",
    "bank_account_number_extracted",
    "bank_branch_code_extracted",
    "bank_swift_code_extracted",
    "prices_include_vat_detected",
)

LINE_FIELDS = (
    "code",
    "description",
    "quantity",
    "unit_price",
    "discounted_unit_price",
    "discount_percent",
    "discount_amount",
    "tax_amount",
    "line_total",
    "vat_treatment",
)

MONEY_FIELDS = {
    "subtotal",
    "tax_amount",
    "total_amount",
    "unit_price",
    "discounted_unit_price",
    "discount_amount",
    "line_total",
}
NUMBER_FIELDS = {"document_count", "quantity", "discount_percent"}
DATE_FIELDS = {"invoice_date", "due_date"}
CRITICAL_HEADER_FIELDS = {
    "document_type",
    "document_direction",
    "document_count",
    "supplier_name_extracted",
    "invoice_number",
    "invoice_date",
    "currency",
    "subtotal",
    "tax_amount",
    "total_amount",
    "vat_number_extracted",
    "bank_account_number_extracted",
    "bank_branch_code_extracted",
    "bank_swift_code_extracted",
    "prices_include_vat_detected",
}
CRITICAL_LINE_FIELDS = {
    "quantity",
    "unit_price",
    "discounted_unit_price",
    "tax_amount",
    "line_total",
    "vat_treatment",
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
    if field == "vat_treatment":
        # invoice_line_items uses NULL as the persisted default for fully
        # claimable VAT; the review UI presents that value as "full".
        return _text(actual or "full") == _text(expected or "full")
    return _text(actual) == _text(expected)


def build_invoice_benchmark_snapshot(
    *,
    invoice: dict[str, Any],
    line_items: list[dict[str, Any]],
    raw: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
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
    if not isinstance(snapshot, dict):
        return ["Gold snapshot must be an object."]
    if int(snapshot.get("schema_version") or 0) < BENCHMARK_SCHEMA_VERSION:
        blockers.append(
            "Gold snapshot schema is outdated; recapture the corrected document before benchmarking."
        )
    document = snapshot.get("document") if isinstance(snapshot, dict) else None
    lines = snapshot.get("line_items") if isinstance(snapshot, dict) else None
    if not isinstance(document, dict):
        return ["Gold snapshot must contain a document object."]
    if not isinstance(lines, list):
        blockers.append("Gold snapshot must contain a line_items array.")
    for field in (
        "document_type",
        "document_count",
        "supplier_name_extracted",
        "invoice_date",
        "currency",
        "total_amount",
    ):
        if document.get(field) in (None, ""):
            blockers.append(f"Gold document is missing {field}.")
    if document.get("document_type") not in {
        "tax_invoice",
        "invoice",
        "credit_note",
        "card_receipt",
        "receipt",
        "till_slip",
    }:
        blockers.append(
            "Gold document_type must be invoice, tax_invoice, credit_note, receipt, till_slip, or card_receipt."
        )
    return blockers


def _line_similarity(actual: dict[str, Any], expected: dict[str, Any]) -> float:
    """Return a stable similarity score used only to align neighbouring rows."""
    weighted_score = 0.0
    total_weight = 0.0
    weights = {
        "code": 3.0,
        "description": 6.0,
        "quantity": 2.0,
        "unit_price": 3.0,
        "discounted_unit_price": 2.0,
        "line_total": 4.0,
    }
    for field, weight in weights.items():
        left, right = actual.get(field), expected.get(field)
        if left in (None, "") and right in (None, ""):
            continue
        total_weight += weight
        if values_match(field, left, right):
            weighted_score += weight
        elif field in {"code", "description"}:
            weighted_score += weight * SequenceMatcher(None, _text(left), _text(right)).ratio()
    return weighted_score / total_weight if total_weight else 0.0


def _align_line_items(
    actual_lines: list[dict[str, Any]],
    gold_lines: list[dict[str, Any]],
) -> list[tuple[Optional[int], Optional[int]]]:
    """Align rows so one omitted/extra row does not cascade into false errors."""
    actual_count, gold_count = len(actual_lines), len(gold_lines)
    gap_penalty = -0.35
    scores = [[0.0] * (gold_count + 1) for _ in range(actual_count + 1)]
    paths = [[""] * (gold_count + 1) for _ in range(actual_count + 1)]
    for actual_index in range(1, actual_count + 1):
        scores[actual_index][0] = actual_index * gap_penalty
        paths[actual_index][0] = "extra"
    for gold_index in range(1, gold_count + 1):
        scores[0][gold_index] = gold_index * gap_penalty
        paths[0][gold_index] = "missing"

    for actual_index in range(1, actual_count + 1):
        for gold_index in range(1, gold_count + 1):
            candidates = (
                (
                    scores[actual_index - 1][gold_index - 1]
                    + _line_similarity(actual_lines[actual_index - 1], gold_lines[gold_index - 1]),
                    2,
                    "match",
                ),
                (scores[actual_index - 1][gold_index] + gap_penalty, 1, "extra"),
                (scores[actual_index][gold_index - 1] + gap_penalty, 0, "missing"),
            )
            best_score, _priority, best_path = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
            scores[actual_index][gold_index] = best_score
            paths[actual_index][gold_index] = best_path

    aligned: list[tuple[Optional[int], Optional[int]]] = []
    actual_index, gold_index = actual_count, gold_count
    while actual_index or gold_index:
        path = paths[actual_index][gold_index]
        if path == "match":
            actual_index -= 1
            gold_index -= 1
            aligned.append((actual_index, gold_index))
        elif path == "extra":
            actual_index -= 1
            aligned.append((actual_index, None))
        else:
            gold_index -= 1
            aligned.append((None, gold_index))
    aligned.reverse()
    return aligned


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

    for actual_index, gold_index in _align_line_items(actual_lines, gold_lines):
        actual_line = actual_lines[actual_index] if actual_index is not None else None
        gold_line = gold_lines[gold_index] if gold_index is not None else None
        if actual_line is None or gold_line is None:
            discrepancy = {
                "scope": "line_item",
                "line": (gold_index if gold_index is not None else actual_index or 0) + 1,
                "actual_line": actual_index + 1 if actual_index is not None else None,
                "expected_line": gold_index + 1 if gold_index is not None else None,
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
                "line": gold_index + 1,
                "actual_line": actual_index + 1,
                "expected_line": gold_index + 1,
                "field": field,
                "expected": gold_line.get(field),
                "actual": actual_line.get(field),
                "critical": field in CRITICAL_LINE_FIELDS,
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


def _timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _fresh_reextract_run(case: dict[str, Any], run: dict[str, Any]) -> bool:
    """Only genuine extraction reruns performed after gold verification count."""
    if run.get("extraction_rerun") is not True:
        return False
    run_at = _timestamp(run.get("created_at"))
    truth_timestamps = [
        parsed
        for parsed in (
            _timestamp(case.get("verified_at")),
            _timestamp(case.get("updated_at")),
            _timestamp(case.get("created_at")),
        )
        if parsed is not None
    ]
    truth_at = max(truth_timestamps) if truth_timestamps else None
    return bool(run_at and truth_at and run_at >= truth_at)


def build_pilot_accuracy_summary(cases: list[dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, Any]:
    locked = [case for case in cases if case.get("dataset_split") == "locked"]
    outdated_gold = [
        case
        for case in locked
        if isinstance(case.get("gold_json"), dict)
        and int(case["gold_json"].get("schema_version") or 0) < BENCHMARK_SCHEMA_VERSION
    ]
    outdated_ids = {str(case.get("id")) for case in outdated_gold}
    locked_by_id = {
        str(case.get("id")): case
        for case in locked
        if str(case.get("id")) not in outdated_ids
    }
    eligible_by_case: dict[str, dict[str, Any]] = {}
    run_history_by_case: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        case_id = str(run.get("gold_document_id"))
        case = locked_by_id.get(case_id)
        if not case:
            continue
        run_history_by_case.setdefault(case_id, []).append(run)
        if not _fresh_reextract_run(case, run):
            continue
        existing = eligible_by_case.get(case_id)
        if existing is None or (_timestamp(run.get("created_at")) or datetime.min) > (
            _timestamp(existing.get("created_at")) or datetime.min
        ):
            eligible_by_case[case_id] = run

    scored = list(eligible_by_case.values())
    correct = sum(int(run.get("correct_values") or 0) for run in scored)
    total = sum(int(run.get("total_values") or 0) for run in scored)
    within_two = sum(bool(run.get("within_two_corrections")) for run in scored)
    critical = sum(int(run.get("critical_error_count") or 0) for run in scored)
    accuracy = correct / total if total else 0.0
    within_two_rate = within_two / len(scored) if scored else 0.0
    accuracy_lower_bound = wilson_lower_bound(correct, total)
    within_two_lower_bound = wilson_lower_bound(within_two, len(scored))

    strata: dict[str, int] = {}
    stratum_results: dict[str, dict[str, Any]] = {}
    for case in locked:
        key = f"{case.get('document_kind')}:{case.get('source_format')}"
        strata[key] = strata.get(key, 0) + 1
        metric = stratum_results.setdefault(key, {
            "locked_documents": 0,
            "scored_documents": 0,
            "correct_values": 0,
            "total_values": 0,
            "within_two_corrections_documents": 0,
            "critical_error_count": 0,
        })
        metric["locked_documents"] += 1
        run = eligible_by_case.get(str(case.get("id")))
        if not run:
            continue
        metric["scored_documents"] += 1
        metric["correct_values"] += int(run.get("correct_values") or 0)
        metric["total_values"] += int(run.get("total_values") or 0)
        metric["within_two_corrections_documents"] += int(bool(run.get("within_two_corrections")))
        metric["critical_error_count"] += int(run.get("critical_error_count") or 0)

    for metric in stratum_results.values():
        metric_total = int(metric["total_values"])
        metric_scored = int(metric["scored_documents"])
        metric["accuracy"] = round(int(metric["correct_values"]) / metric_total, 6) if metric_total else 0.0
        metric["accuracy_percent"] = round(float(metric["accuracy"]) * 100, 2)
        metric["within_two_corrections_rate"] = round(
            int(metric["within_two_corrections_documents"]) / metric_scored,
            6,
        ) if metric_scored else 0.0

    failure_fields: dict[tuple[str, str], dict[str, Any]] = {}
    for case_id, run in eligible_by_case.items():
        for discrepancy in run.get("discrepancies") or []:
            if not isinstance(discrepancy, dict):
                continue
            scope = str(discrepancy.get("scope") or "unknown")
            field = str(discrepancy.get("field") or "unknown")
            entry = failure_fields.setdefault((scope, field), {
                "scope": scope,
                "field": field,
                "correction_count": 0,
                "document_ids": set(),
                "critical_correction_count": 0,
            })
            entry["correction_count"] += 1
            entry["document_ids"].add(case_id)
            entry["critical_correction_count"] += int(bool(discrepancy.get("critical")))
    top_failure_fields = []
    for entry in failure_fields.values():
        top_failure_fields.append({
            "scope": entry["scope"],
            "field": entry["field"],
            "correction_count": entry["correction_count"],
            "affected_documents": len(entry["document_ids"]),
            "critical_correction_count": entry["critical_correction_count"],
        })
    top_failure_fields.sort(
        key=lambda item: (
            -int(item["critical_correction_count"]),
            -int(item["affected_documents"]),
            -int(item["correction_count"]),
            str(item["scope"]),
            str(item["field"]),
        )
    )

    stale_reextract_documents = sum(
        bool(history)
        and any(run.get("extraction_rerun") is True for run in history)
        and case_id not in eligible_by_case
        for case_id, history in run_history_by_case.items()
    )
    diagnostic_only_documents = sum(
        bool(history)
        and any(run.get("extraction_rerun") is not True for run in history)
        and case_id not in eligible_by_case
        for case_id, history in run_history_by_case.items()
    )
    required_strata = [
        f"{kind}:{source_format}"
        for kind in ("invoice", "credit_note", "receipt")
        for source_format in ("pdf", "image")
    ]
    sample_ready = (
        len(locked) >= PILOT_MIN_LOCKED_DOCUMENTS
        and len(scored) == len(locked)
        and not outdated_gold
        and all(strata.get(key, 0) >= PILOT_MIN_PER_STRATUM for key in required_strata)
    )
    failing_strata = [
        key
        for key in required_strata
        if (
            int((stratum_results.get(key) or {}).get("locked_documents") or 0) < PILOT_MIN_PER_STRATUM
            or int((stratum_results.get(key) or {}).get("scored_documents") or 0)
            != int((stratum_results.get(key) or {}).get("locked_documents") or 0)
            or float((stratum_results.get(key) or {}).get("accuracy") or 0) < PILOT_TARGET_ACCURACY
            or float((stratum_results.get(key) or {}).get("within_two_corrections_rate") or 0)
            < PILOT_TARGET_WITHIN_TWO
            or int((stratum_results.get(key) or {}).get("critical_error_count") or 0) > 0
        )
    ]
    strata_gate_passed = sample_ready and not failing_strata
    gate_passed = (
        strata_gate_passed
        and accuracy >= PILOT_TARGET_ACCURACY
        and accuracy_lower_bound >= PILOT_TARGET_ACCURACY
        and within_two_rate >= PILOT_TARGET_WITHIN_TWO
        and within_two_lower_bound >= PILOT_TARGET_WITHIN_TWO
        and critical == 0
    )
    return {
        "locked_documents": len(locked),
        "scored_documents": len(scored),
        "fresh_reextract_documents": len(scored),
        "pending_reextract_documents": max(0, len(locked) - len(scored)),
        "stale_reextract_documents": stale_reextract_documents,
        "diagnostic_only_documents": diagnostic_only_documents,
        "outdated_gold_documents": len(outdated_gold),
        "benchmark_schema_version": BENCHMARK_SCHEMA_VERSION,
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
        "stratum_results": stratum_results,
        "top_failure_fields": top_failure_fields[:20],
        "failing_strata": failing_strata,
        "strata_gate_passed": strata_gate_passed,
        "required_strata": required_strata,
        "minimum_locked_documents": PILOT_MIN_LOCKED_DOCUMENTS,
        "minimum_per_stratum": PILOT_MIN_PER_STRATUM,
        "sample_ready": sample_ready,
        "pilot_gate_passed": gate_passed,
    }
