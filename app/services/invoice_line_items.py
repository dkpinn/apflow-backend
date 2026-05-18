from __future__ import annotations

from typing import Optional


def _numeric_amount(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    clean = str(value).strip()
    if not clean:
        return None

    clean = clean.replace("R", "").replace("ZAR", "").replace(" ", "")
    if "," in clean and "." not in clean:
        clean = clean.replace(",", ".")
    else:
        clean = clean.replace(",", "")

    try:
        return float(clean)
    except Exception:
        return None


def build_line_item_diagnostics(
    *,
    line_items: list[dict],
    invoice_total,
    inserted_count: int = 0,
    insert_error: Optional[str] = None,
) -> dict:
    line_items_total = 0.0
    total_seen = False

    for item in line_items or []:
        line_total = _numeric_amount(item.get("line_total"))
        if line_total is None:
            continue
        total_seen = True
        line_items_total += line_total

    parsed_invoice_total = _numeric_amount(invoice_total)
    rounded_line_total = round(line_items_total, 2) if total_seen else None
    totals_match = None
    if rounded_line_total is not None and parsed_invoice_total is not None:
        totals_match = abs(rounded_line_total - parsed_invoice_total) <= 0.02

    return {
        "line_items_found_count": len(line_items or []),
        "line_items_inserted_count": inserted_count,
        "line_items_insert_error": insert_error,
        "line_items_total": rounded_line_total,
        "invoice_total": parsed_invoice_total,
        "line_items_match_invoice_total": totals_match,
    }


def build_line_item_payload(
    *,
    invoice_extracted_id: str,
    organisation_id: str,
    line_items: list[dict],
) -> list[dict]:
    return [
        {
            "invoice_extracted_id": invoice_extracted_id,
            "organisation_id": organisation_id,
            "description": item.get("description"),
            "quantity": item.get("quantity"),
            "unit_price": item.get("unit_price"),
            "tax_amount": item.get("tax_amount"),
            "line_total": item.get("line_total"),
            "raw_line": item.get("raw_line"),
            "code": item.get("code"),
        }
        for item in line_items or []
    ]


def replace_invoice_line_items(
    supabase,
    *,
    invoice_extracted_id: str,
    organisation_id: str,
    line_items: list[dict],
    invoice_total,
    delete_when_empty: bool = False,
    raise_on_error: bool = False,
) -> dict:
    """
    Replace stored line items and return diagnostics used by extract/re-extract.

    delete_when_empty keeps legacy extraction behavior, where a fresh extraction
    clears any previous rows even if no new rows were parsed. Re-extract passes
    False so existing line items are left alone when deep OCR finds nothing.
    """
    diagnostics = build_line_item_diagnostics(
        line_items=line_items,
        invoice_total=invoice_total,
    )

    if not invoice_extracted_id:
        return diagnostics

    try:
        if line_items or delete_when_empty:
            supabase.table("invoice_line_items").delete().eq(
                "invoice_extracted_id",
                invoice_extracted_id,
            ).execute()

        if line_items:
            payload = build_line_item_payload(
                invoice_extracted_id=invoice_extracted_id,
                organisation_id=organisation_id,
                line_items=line_items,
            )
            insert_res = supabase.table("invoice_line_items").insert(payload).execute()
            diagnostics["line_items_inserted_count"] = len(insert_res.data or payload)
    except Exception as exc:
        diagnostics["line_items_insert_error"] = str(exc)
        if raise_on_error:
            raise

    return diagnostics
