"""_vat_reconciliation.py
VAT treatment detection and line-item normalisation.

Determines whether extracted line-item prices are VAT-inclusive or exclusive by
comparing SUM(line_totals) against the document total, then corrects accordingly.
"""
from __future__ import annotations

from decimal import Decimal


def _auto_reconcile_vat(parsed_data: dict, vat_rate: float = 0.15) -> None:
    """
    Detect whether extracted line item prices are VAT-inclusive or exclusive by
    Determine VAT treatment using the canonical decision tree:
      1. No VAT number on document → non-VAT supplier, no VAT claimed
      2. SUM(line_totals) ≈ doc_total → prices are VAT-INCLUSIVE → strip VAT from lines
      3. SUM × (1+rate) ≈ doc_total  → prices are EX-VAT → use as-is, derive VAT
      4. Neither matches → cannot determine, user sees Solve button

    VLM now returns prices EXACTLY as printed — this function normalises to ex-VAT.
    """
    doc_total_raw = parsed_data.get("total_amount")
    line_items = parsed_data.get("line_items") or []
    vat_number = parsed_data.get("vat_number_extracted")

    if not doc_total_raw or not line_items:
        return

    try:
        doc_total = float(doc_total_raw)
    except (TypeError, ValueError):
        return

    line_sum = sum(float(it.get("line_total") or 0) for it in line_items)
    if line_sum <= 0 or doc_total <= 0:
        return

    # Case 1: No VAT number → non-VAT supplier, use line totals as-is
    if not vat_number:
        parsed_data["prices_include_vat_detected"] = None  # not applicable, not a DB enum value
        parsed_data["subtotal"] = round(line_sum, 2)
        parsed_data["tax_amount"] = 0.0
        return

    TOLERANCE = 0.03  # 3%

    # Case 2: Prices inclusive (SUM ≈ doc_total)
    diff_inclusive = abs(line_sum - doc_total) / doc_total

    # Case 3: Prices exclusive (SUM × (1+rate) ≈ doc_total)
    diff_exclusive = abs(line_sum * (1 + vat_rate) - doc_total) / doc_total

    if diff_inclusive <= diff_exclusive and diff_inclusive < TOLERANCE:
        # VAT-INCLUSIVE: strip VAT from printed prices → store ex-VAT
        parsed_data["prices_include_vat_detected"] = "inclusive"
        new_items = []
        scale = Decimal(str(1 + vat_rate))
        for it in line_items:
            raw_total = float(it.get("line_total") or 0)
            ex_total = round(float(Decimal(str(raw_total)) / scale), 2)
            raw_unit = float(it.get("unit_price") or 0)
            ex_unit = round(float(Decimal(str(raw_unit)) / scale), 4) if raw_unit else 0
            new_items.append({**it, "unit_price": ex_unit, "line_total": ex_total})
        parsed_data["line_items"] = new_items
        ex_sum = round(sum(it["line_total"] for it in new_items), 2)
        parsed_data["subtotal"] = ex_sum
        parsed_data["tax_amount"] = round(doc_total - ex_sum, 2)

    elif diff_exclusive < diff_inclusive and diff_exclusive < TOLERANCE:
        # EX-VAT: prices already ex-VAT → derive VAT from doc_total
        parsed_data["prices_include_vat_detected"] = "exclusive"
        parsed_data["subtotal"] = round(line_sum, 2)
        parsed_data["tax_amount"] = round(doc_total - line_sum, 2)

    # else: cannot determine — leave as-is, user sees Solve button
