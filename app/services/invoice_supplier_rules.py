from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional

from app.services.audit_log import log_invoice_event
from app.services.invoice_line_items import replace_invoice_line_items
from app.services.invoice_parse_attempts import fetch_parse_attempts


SUPPLIER_PROCESSING_SELECT = (
    "parse_line_items, line_items_include_vat, default_vat_rate, "
    "default_expense_account, track_inventory, use_uom_from_description"
)

DEFAULT_SUPPLIER_PROCESSING_SETTINGS = {
    "parse_line_items": True,
    "line_items_include_vat": False,
    "default_vat_rate": None,
    "default_expense_account": None,
    "track_inventory": False,
    "use_uom_from_description": False,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def fetch_supplier_processing_settings(supabase, supplier_id: Optional[str]) -> dict:
    settings = dict(DEFAULT_SUPPLIER_PROCESSING_SETTINGS)
    settings["supplier_id"] = supplier_id

    if not supplier_id:
        return settings

    select_variants = [
        SUPPLIER_PROCESSING_SELECT,
        "parse_line_items, line_items_include_vat, default_vat_rate, default_expense_account",
        "parse_line_items, line_items_include_vat",
    ]
    for select_columns in select_variants:
        try:
            res = (
                supabase
                .table("suppliers")
                .select(select_columns)
                .eq("id", supplier_id)
                .limit(1)
                .execute()
            )
            if res.data:
                for key, value in res.data[0].items():
                    if value is not None or key in settings:
                        settings[key] = value
                break
        except Exception:
            continue

    return settings


def apply_supplier_processing_rules(
    parsed_data: dict,
    supplier_settings: Optional[dict],
    *,
    source_line_items: Optional[list[dict]] = None,
) -> dict:
    settings = {**DEFAULT_SUPPLIER_PROCESSING_SETTINGS, **(supplier_settings or {})}
    raw_line_items = source_line_items if source_line_items is not None else parsed_data.get("line_items")
    line_items = deepcopy(raw_line_items or [])
    invoice_patch: dict = {}

    parse_line_items = settings.get("parse_line_items", True)
    line_items_include_vat = settings.get("line_items_include_vat", False)
    default_expense_account = settings.get("default_expense_account")

    if parse_line_items is False:
        supplier_name = parsed_data.get("supplier_name_extracted") or parsed_data.get("issuer_name_extracted") or "Supplier"
        total = parsed_data.get("subtotal") or parsed_data.get("total_amount")
        line_items = [{
            "description": f"Purchase from {supplier_name}",
            "quantity": 1,
            "unit_price": total,
            "line_total": total,
        }]
    elif line_items_include_vat and line_items:
        subtotal = _numeric_amount(parsed_data.get("subtotal"))
        tax_amount = _numeric_amount(parsed_data.get("tax_amount"))
        default_vat_rate = _numeric_amount(settings.get("default_vat_rate"))

        if tax_amount is not None and subtotal and subtotal > 0:
            vat_rate = round((tax_amount / subtotal) * 10000) / 10000
        elif default_vat_rate is not None:
            vat_rate = default_vat_rate / 100
        else:
            vat_rate = 0.15

        stripped: list[dict] = []
        for item in line_items:
            unit_price = _numeric_amount(item.get("unit_price"))
            quantity = _numeric_amount(item.get("quantity")) or 1
            if unit_price is not None:
                try:
                    ex_price = round(unit_price / (1 + vat_rate), 2)
                    ex_total = round(ex_price * quantity, 2)
                    item = {**item, "unit_price": ex_price, "line_total": ex_total}
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            stripped.append(item)
        line_items = stripped

    if default_expense_account:
        invoice_patch["expense_account"] = default_expense_account
        if line_items:
            line_items = [{**item, "expense_account": default_expense_account} for item in line_items]

    return {
        "line_items": line_items,
        "invoice_patch": invoice_patch,
        "supplier_settings": settings,
    }


def _fetch_current_line_items(supabase, invoice_extracted_id: Optional[str]) -> list[dict]:
    if not invoice_extracted_id:
        return []
    try:
        res = (
            supabase
            .table("invoice_line_items")
            .select("*")
            .eq("invoice_extracted_id", invoice_extracted_id)
            .order("created_at", desc=False)
            .order("id", desc=False)
            .execute()
        )
        return res.data or []
    except Exception:
        return []


def build_invoice_rule_source(supabase, invoice: dict) -> tuple[dict, list[dict], str]:
    invoice_raw_id = invoice.get("invoice_raw_id")
    invoice_extracted_id = invoice.get("id")

    try:
        attempts, _ = fetch_parse_attempts(supabase, invoice_raw_id=invoice_raw_id)
        selected = next(
            (
                attempt
                for attempt in attempts
                if attempt.get("selected")
                and (attempt.get("line_items") or (attempt.get("parsed_data") or {}).get("line_items"))
            ),
            None,
        )
        attempt = selected or next(
            (
                attempt
                for attempt in attempts
                if attempt.get("line_items") or (attempt.get("parsed_data") or {}).get("line_items")
            ),
            None,
        )
        if attempt:
            parsed_data = deepcopy(attempt.get("parsed_data") or {})
            line_items = deepcopy(attempt.get("line_items") or parsed_data.get("line_items") or [])
            parsed_data["line_items"] = line_items
            return parsed_data, line_items, "selected_parse_attempt" if selected else "first_parse_attempt"
    except Exception:
        pass

    parsed_data = deepcopy(invoice or {})
    line_items = deepcopy(parsed_data.get("line_items") or [])
    if not line_items:
        line_items = _fetch_current_line_items(supabase, invoice_extracted_id)
        if line_items:
            parsed_data["line_items"] = line_items

    return parsed_data, line_items, "current_invoice"


def _looks_like_generated_summary_line(line_items: list[dict], invoice: dict) -> bool:
    if len(line_items or []) != 1:
        return False

    item = line_items[0] or {}
    description = str(item.get("description") or "").strip().lower()
    if not description.startswith("purchase from "):
        return False

    quantity = _numeric_amount(item.get("quantity"))
    if quantity is not None and quantity != 1:
        return False

    line_total = _numeric_amount(item.get("line_total"))
    invoice_total = _numeric_amount(invoice.get("total_amount"))
    subtotal = _numeric_amount(invoice.get("subtotal"))
    expected_total = subtotal if subtotal is not None else invoice_total
    if line_total is None or expected_total is None:
        return True

    return abs(line_total - expected_total) <= 0.02


def reapply_supplier_rules_to_invoice(
    supabase,
    *,
    invoice: dict,
    supplier_id: str,
    actor_type: str = "api",
    event_reason: str = "supplier_rules_applied",
) -> dict:
    organisation_id = invoice.get("organisation_id")
    invoice_extracted_id = invoice.get("id")
    invoice_raw_id = invoice.get("invoice_raw_id")

    settings = fetch_supplier_processing_settings(supabase, supplier_id)
    parsed_data, raw_line_items, source = build_invoice_rule_source(supabase, invoice)
    if source == "current_invoice" and _looks_like_generated_summary_line(raw_line_items, invoice):
        result = {
            "source": source,
            "supplier_id": supplier_id,
            "invoice_patch": {},
            "line_items_count": len(raw_line_items or []),
            "needs_reextract": True,
            "skipped": True,
            "reason": "missing_raw_extraction_snapshot",
        }
        if organisation_id:
            log_invoice_event(
                supabase,
                organisation_id=organisation_id,
                invoice_raw_id=invoice_raw_id,
                invoice_extracted_id=invoice_extracted_id,
                event_type="supplier_rules_reapply_skipped",
                stage="supplier_processing_rules",
                actor_type=actor_type,
                new_value=result,
                notes=(
                    "Supplier rules were not re-applied because only a generated "
                    "summary line is available. Re-extract once to rebuild the raw snapshot."
                ),
            )
        return result

    applied = apply_supplier_processing_rules(
        parsed_data,
        settings,
        source_line_items=raw_line_items,
    )

    invoice_patch = {
        "supplier_id": supplier_id,
        "updated_at": utc_now_iso(),
        **applied["invoice_patch"],
    }
    if invoice_extracted_id:
        supabase.table("invoices_extracted").update(invoice_patch).eq("id", invoice_extracted_id).execute()

    diagnostics = replace_invoice_line_items(
        supabase,
        invoice_extracted_id=invoice_extracted_id,
        organisation_id=organisation_id,
        line_items=applied["line_items"],
        invoice_total=parsed_data.get("total_amount") or invoice.get("total_amount"),
        delete_when_empty=True,
        raise_on_error=False,
    )

    result = {
        "source": source,
        "supplier_id": supplier_id,
        "invoice_patch": invoice_patch,
        "line_items_count": len(applied["line_items"]),
        "needs_reextract": False,
        "skipped": False,
        **diagnostics,
    }

    if organisation_id:
        log_invoice_event(
            supabase,
            organisation_id=organisation_id,
            invoice_raw_id=invoice_raw_id,
            invoice_extracted_id=invoice_extracted_id,
            event_type="supplier_rules_applied",
            stage="supplier_processing_rules",
            actor_type=actor_type,
            new_value=result,
            notes=event_reason,
        )

    return result
