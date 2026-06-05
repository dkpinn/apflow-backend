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
        "allocations_inserted_count": 0,
        "allocations_insert_error": None,
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
            "discount_amount": item.get("discount_amount", item.get("discount")),
            "discount_percent": item.get("discount_percent"),
            "discounted_unit_price": item.get("discounted_unit_price"),
            "pricing_basis": item.get("pricing_basis"),
            "pricing_notes": {
                **(item.get("pricing_notes") or {}),
                # Store VLM-returned bounding box for document highlighting
                **({ "source_bbox": item["source_bbox"] } if item.get("source_bbox") else {}),
            },
            "tax_amount": item.get("tax_amount"),
            "line_total": item.get("line_total"),
            "raw_line": item.get("raw_line"),
            "code": item.get("code"),
            "expense_account": item.get("expense_account"),
            "vat_treatment": item.get("vat_treatment"),  # may be enriched later by account lookup
            "tracking": item.get("tracking"),
        }
        for item in line_items or []
    ]


def _round_money(value) -> Optional[float]:
    numeric = _numeric_amount(value)
    if numeric is None:
        return None
    return round(numeric, 2)


def _normalise_tracking(value) -> dict:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items() if v not in (None, "")}
    return {}


def _normalise_allocations(item: dict) -> list[dict]:
    raw_allocations = item.get("allocations") or []
    if not isinstance(raw_allocations, list):
        return []

    allocations: list[dict] = []
    for index, allocation in enumerate(raw_allocations):
        if not isinstance(allocation, dict):
            continue
        amount = _round_money(allocation.get("amount"))
        if amount is None:
            continue
        allocations.append({
            "expense_account": allocation.get("expense_account"),
            "tracking": _normalise_tracking(allocation.get("tracking")),
            "amount": amount,
            "percent": allocation.get("percent"),
            "note": allocation.get("note"),
            "sort_order": int(allocation.get("sort_order") or index),
        })
    return allocations


def validate_line_item_allocations(line_items: list[dict], *, tolerance: float = 0.02) -> None:
    """
    Allocation rows must distribute the parent line amount exactly enough for accounting.

    They are deliberately validated separately from invoice totals so splits cannot
    change OCR/document calculations.
    """
    for index, item in enumerate(line_items or []):
        allocations = _normalise_allocations(item)
        if not allocations:
            continue

        line_total = _round_money(item.get("line_total") if item.get("line_total") is not None else item.get("amount"))
        if line_total is None:
            raise ValueError(f"Line {index + 1} has allocations but no line total.")

        expected_total = abs(line_total)
        allocation_total = round(sum(float(allocation["amount"]) for allocation in allocations), 2)
        if abs(allocation_total - expected_total) > tolerance:
            raise ValueError(
                f"Line {index + 1} allocations total {allocation_total:.2f}, "
                f"but line total is {line_total:.2f}."
            )


def _build_allocation_payloads(
    *,
    organisation_id: str,
    inserted_rows: list[dict],
    source_line_items: list[dict],
) -> list[dict]:
    payload: list[dict] = []

    for row, item in zip(inserted_rows or [], source_line_items or []):
        line_item_id = row.get("id")
        if not line_item_id:
            continue
        for allocation in _normalise_allocations(item):
            payload.append({
                "invoice_line_item_id": line_item_id,
                "organisation_id": organisation_id,
                "expense_account": allocation.get("expense_account"),
                "tracking": allocation.get("tracking") or {},
                "amount": allocation.get("amount"),
                "percent": allocation.get("percent"),
                "note": allocation.get("note"),
                "sort_order": allocation.get("sort_order") or 0,
            })

    return payload


def _enrich_vat_treatment(supabase, organisation_id: Optional[str], payload: list[dict]) -> None:
    """
    Populate vat_treatment on each payload row from the matched GL account.

    The expense_account field stores either account.code (preferred) or account.name
    as the key (matching AccountSelector.accountKey logic).
    Non-fatal: any error leaves the rows with whatever vat_treatment they already have.
    """
    if not organisation_id or not payload:
        return

    account_values = {row.get("expense_account") for row in payload if row.get("expense_account")}
    if not account_values:
        return

    try:
        res = (
            supabase
            .table("accounts")
            .select("code, name, vat_treatment")
            .eq("organisation_id", organisation_id)
            .execute()
        )
        accounts = res.data or []

        # Build two lookup tables: code→treatment and name→treatment
        by_code: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for acc in accounts:
            treatment = acc.get("vat_treatment") or "full"
            if acc.get("code"):
                by_code[acc["code"]] = treatment
            if acc.get("name"):
                by_name[acc["name"]] = treatment

        for row in payload:
            acc_val = row.get("expense_account")
            if not acc_val:
                continue
            treatment = by_code.get(acc_val) or by_name.get(acc_val)
            if treatment:
                # Only write non-full treatments to avoid storing redundant NULLs→"full"
                row["vat_treatment"] = treatment if treatment != "full" else None
    except Exception:
        pass  # Non-fatal — leave vat_treatment unset (DB column defaults to NULL / full)


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
        validate_line_item_allocations(line_items)

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
            _enrich_vat_treatment(supabase, organisation_id, payload)
            insert_res = supabase.table("invoice_line_items").insert(payload).execute()
            diagnostics["line_items_inserted_count"] = len(insert_res.data or payload)

            allocation_payload = _build_allocation_payloads(
                organisation_id=organisation_id,
                inserted_rows=insert_res.data or [],
                source_line_items=line_items,
            )
            if allocation_payload:
                try:
                    allocation_res = (
                        supabase
                        .table("invoice_line_item_allocations")
                        .insert(allocation_payload)
                        .execute()
                    )
                    diagnostics["allocations_inserted_count"] = len(allocation_res.data or allocation_payload)
                except Exception as allocation_exc:
                    diagnostics["allocations_insert_error"] = str(allocation_exc)
                    if raise_on_error:
                        raise
    except Exception as exc:
        diagnostics["line_items_insert_error"] = str(exc)
        if raise_on_error:
            raise

    return diagnostics
