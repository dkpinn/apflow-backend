from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Optional

from app.services.audit_log import log_invoice_event
from app.services.invoice_line_items import replace_invoice_line_items
from app.services.invoice_parse_attempts import fetch_parse_attempts


SUPPLIER_PROCESSING_SELECT = (
    "parse_line_items, line_items_include_vat, default_vat_rate, "
    "default_expense_account, default_tracking, track_inventory, use_uom_from_description"
)

DEFAULT_SUPPLIER_PROCESSING_SETTINGS = {
    "parse_line_items": True,
    "line_items_include_vat": False,
    "default_vat_rate": None,
    "default_expense_account": None,
    "default_tracking": {},
    "track_inventory": False,
    "use_uom_from_description": False,
    "allocation_rules": [],
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


def _round_money(value) -> Optional[float]:
    numeric = _numeric_amount(value)
    if numeric is None:
        return None
    return round(numeric, 2)


def _normalise_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _document_scope(parsed_data: dict) -> str:
    document_type = _normalise_text(parsed_data.get("document_type"))
    if "credit" in document_type:
        return "credit_note"
    try:
        total = _numeric_amount(parsed_data.get("total_amount"))
        if total is not None and total < 0:
            return "credit_note"
    except Exception:
        pass
    return "invoice"


def _rule_specificity(rule: dict) -> int:
    match_type = rule.get("match_type")
    if match_type == "regex":
        return 4
    if match_type == "exact":
        return 3
    if match_type == "contains":
        return 2
    return 1


def _sorted_rules(rules: list[dict]) -> list[dict]:
    return sorted(
        [rule for rule in rules or [] if rule.get("active", True)],
        key=lambda rule: (
            int(rule.get("priority") or 100),
            -_rule_specificity(rule),
            str(rule.get("name") or ""),
        ),
    )


def _line_match_value(item: dict, rule: dict) -> str:
    match_field = rule.get("match_field") or "description"
    description = str(item.get("description") or "")
    code = str(item.get("code") or "")
    if match_field == "code":
        return code
    if match_field == "description_or_code":
        return f"{description} {code}"
    return description


def _rule_matches_document(rule: dict, parsed_data: dict) -> bool:
    scope = rule.get("document_scope") or "all"
    return scope == "all" or scope == _document_scope(parsed_data)


def _rule_matches_line(rule: dict, item: dict) -> bool:
    match_type = rule.get("match_type") or "all_lines"
    if match_type == "all_lines":
        return True

    pattern = str(rule.get("pattern") or "").strip()
    if not pattern:
        return False

    value = _line_match_value(item, rule)
    normalised_value = _normalise_text(value)
    normalised_pattern = _normalise_text(pattern)
    if match_type == "contains":
        return normalised_pattern in normalised_value
    if match_type == "exact":
        return normalised_pattern == normalised_value
    if match_type == "regex":
        try:
            return re.search(pattern, value, re.IGNORECASE) is not None
        except re.error:
            return False
    return False


def _normalise_tracking(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {str(key): val for key, val in value.items() if val not in (None, "")}


def _normalise_rule_splits(rule: dict) -> list[dict]:
    splits = rule.get("splits") or []
    normalised: list[dict] = []
    for index, split in enumerate(splits):
        if not isinstance(split, dict):
            continue
        percent = _numeric_amount(split.get("percent"))
        if percent is None or percent <= 0:
            continue
        normalised.append({
            "expense_account": split.get("expense_account"),
            "tracking": _normalise_tracking(split.get("tracking")),
            "percent": percent,
            "note": split.get("note"),
            "sort_order": int(split.get("sort_order") or index),
        })
    return sorted(normalised, key=lambda split: split.get("sort_order") or 0)


def _allocation_amounts(line_total: float, splits: list[dict]) -> list[float]:
    allocation_total = abs(line_total)
    amounts: list[float] = []
    running = 0.0
    for index, split in enumerate(splits):
        if index == len(splits) - 1:
            amount = round(allocation_total - running, 2)
        else:
            amount = round(allocation_total * (float(split.get("percent") or 0) / 100), 2)
            running = round(running + amount, 2)
        amounts.append(amount)
    return amounts


def _apply_allocation_rule_to_line(item: dict, rule: dict) -> dict:
    splits = _normalise_rule_splits(rule)
    if not splits:
        return item

    updated = dict(item)
    first_split = splits[0]
    base_tracking = _normalise_tracking(item.get("tracking"))
    if first_split.get("expense_account"):
        updated["expense_account"] = first_split.get("expense_account")
    updated["tracking"] = {
        **base_tracking,
        **_normalise_tracking(first_split.get("tracking")),
    }

    line_total = _round_money(item.get("line_total") if item.get("line_total") is not None else item.get("amount"))
    if line_total is not None:
        amounts = _allocation_amounts(line_total, splits)
        updated["allocations"] = [
            {
                "expense_account": split.get("expense_account"),
                "tracking": {
                    **base_tracking,
                    **_normalise_tracking(split.get("tracking")),
                },
                "amount": amounts[index],
                "percent": split.get("percent"),
                "note": split.get("note") or rule.get("name"),
                "sort_order": index,
            }
            for index, split in enumerate(splits)
        ]

    updated["pricing_notes"] = {
        **(item.get("pricing_notes") or {}),
        "supplier_allocation_rule_id": rule.get("id"),
        "supplier_allocation_rule_name": rule.get("name"),
    }
    return updated


def apply_supplier_allocation_rules(parsed_data: dict, line_items: list[dict], rules: list[dict]) -> list[dict]:
    matching_rules = [
        rule for rule in _sorted_rules(rules)
        if _rule_matches_document(rule, parsed_data)
    ]
    if not matching_rules or not line_items:
        return line_items

    updated_items: list[dict] = []
    for item in line_items:
        matched_rule = next(
            (rule for rule in matching_rules if _rule_matches_line(rule, item)),
            None,
        )
        updated_items.append(
            _apply_allocation_rule_to_line(item, matched_rule)
            if matched_rule
            else item
        )
    return updated_items


def _sum_line_totals(line_items: list[dict]) -> Optional[float]:
    total = 0.0
    found = False
    for item in line_items or []:
        line_total = _numeric_amount(item.get("line_total"))
        if line_total is None:
            continue
        total += line_total
        found = True
    return round(total, 2) if found else None


def _strip_vat_from_line_item(item: dict, vat_rate: float) -> dict:
    divisor = 1 + vat_rate
    if divisor <= 0:
        return item

    quantity = _numeric_amount(item.get("quantity")) or 1
    unit_price = _round_money((_numeric_amount(item.get("unit_price")) or 0) / divisor) if item.get("unit_price") is not None else None
    discounted_unit_price = (
        _round_money((_numeric_amount(item.get("discounted_unit_price")) or 0) / divisor)
        if item.get("discounted_unit_price") is not None
        else None
    )
    discount_amount = (
        _round_money((_numeric_amount(item.get("discount_amount") or item.get("discount")) or 0) / divisor)
        if item.get("discount_amount") is not None or item.get("discount") is not None
        else None
    )
    line_total = (
        _round_money((_numeric_amount(item.get("line_total")) or 0) / divisor)
        if item.get("line_total") is not None
        else None
    )

    updated = dict(item)
    if unit_price is not None:
        updated["unit_price"] = unit_price
    if discounted_unit_price is not None:
        updated["discounted_unit_price"] = discounted_unit_price
        if discount_amount is None and unit_price is not None:
            discount_amount = _round_money((unit_price - discounted_unit_price) * quantity)
    if discount_amount is not None:
        updated["discount_amount"] = discount_amount
    if line_total is not None:
        updated["line_total"] = line_total
    elif discounted_unit_price is not None:
        updated["line_total"] = _round_money(discounted_unit_price * quantity)
    elif unit_price is not None:
        updated["line_total"] = _round_money((unit_price * quantity) - (discount_amount or 0))

    if updated != item:
        updated["pricing_notes"] = {
            **(item.get("pricing_notes") or {}),
            "vat_stripped_from_line_item": True,
            "vat_rate": vat_rate,
        }
    return updated


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

    settings["allocation_rules"] = fetch_supplier_allocation_rules(supabase, supplier_id)
    return settings


def fetch_supplier_allocation_rules(supabase, supplier_id: Optional[str], *, active_only: bool = True) -> list[dict]:
    if not supplier_id:
        return []

    try:
        query = (
            supabase
            .table("supplier_line_item_allocation_rules")
            .select("*")
            .eq("supplier_id", supplier_id)
            .order("priority", desc=False)
            .order("created_at", desc=False)
        )
        if active_only:
            query = query.eq("active", True)
        rules = query.execute().data or []
    except Exception:
        return []

    rule_ids = [rule.get("id") for rule in rules if rule.get("id")]
    if not rule_ids:
        return rules

    try:
        splits_query = (
            supabase
            .table("supplier_line_item_allocation_rule_splits")
            .select("*")
            .order("sort_order", desc=False)
        )
        if hasattr(splits_query, "in_"):
            splits_query = splits_query.in_("rule_id", rule_ids)
        splits = splits_query.execute().data or []
    except Exception:
        splits = []

    by_rule: dict[str, list[dict]] = {}
    rule_id_set = set(rule_ids)
    for split in splits:
        rule_id = split.get("rule_id")
        if rule_id in rule_id_set:
            by_rule.setdefault(rule_id, []).append(split)

    return [
        {
            **rule,
            "splits": by_rule.get(rule.get("id"), []),
        }
        for rule in rules
    ]


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
    default_tracking = _normalise_tracking(settings.get("default_tracking"))

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
        total_amount = _numeric_amount(parsed_data.get("total_amount"))
        default_vat_rate = _numeric_amount(settings.get("default_vat_rate"))

        if tax_amount is not None and subtotal and subtotal > 0:
            vat_rate = round((tax_amount / subtotal) * 10000) / 10000
        elif default_vat_rate is not None:
            vat_rate = default_vat_rate / 100
        else:
            vat_rate = 0.15

        line_total_sum = _sum_line_totals(line_items)
        line_items_already_ex_vat = (
            line_total_sum is not None
            and subtotal is not None
            and abs(line_total_sum - subtotal) <= 0.02
        )
        # Don't strip only if the line sum significantly exceeds the invoice total —
        # that would imply items are already ex-VAT and the total is wrong.
        # Extra charges (delivery, rounding) mean total_amount > line_total_sum is fine.
        line_sum_exceeds_total = (
            line_total_sum is not None
            and total_amount is not None
            and line_total_sum > total_amount + 0.50
        )
        should_strip_line_items = not line_items_already_ex_vat and not line_sum_exceeds_total

        if should_strip_line_items:
            stripped: list[dict] = []
            for item in line_items:
                try:
                    stripped.append(_strip_vat_from_line_item(item, vat_rate))
                except (TypeError, ValueError, ZeroDivisionError):
                    stripped.append(item)
            line_items = stripped

    if default_expense_account:
        invoice_patch["expense_account"] = default_expense_account
        if line_items:
            line_items = [{**item, "expense_account": default_expense_account} for item in line_items]

    if default_tracking and line_items:
        line_items = [
            {
                **item,
                "tracking": {
                    **default_tracking,
                    **_normalise_tracking(item.get("tracking")),
                },
                **({
                    "allocations": [
                        {
                            **allocation,
                            "tracking": {
                                **default_tracking,
                                **_normalise_tracking(allocation.get("tracking")),
                            },
                        }
                        for allocation in item.get("allocations") or []
                    ],
                } if item.get("allocations") else {}),
            }
            for item in line_items
        ]

    line_items = apply_supplier_allocation_rules(
        parsed_data,
        line_items,
        settings.get("allocation_rules") or [],
    )

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
    # Only block on the generated-summary-line guard when parse_line_items is ON for this
    # supplier.  When parse_line_items=False (or None/unset), the single summary line IS the
    # correct extracted representation — blocking and asking for re-extraction is a dead end.
    parse_line_items_on = settings.get("parse_line_items", True)
    if (
        source == "current_invoice"
        and parse_line_items_on
        and _looks_like_generated_summary_line(raw_line_items, invoice)
    ):
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

    # Safety guard: if rule processing returned no items but the source had items,
    # do NOT delete existing line items — a silent wipe is worse than stale data.
    if not applied.get("line_items") and raw_line_items:
        return {
            "skipped": True,
            "reason": "processing_produced_no_items",
            "source": source,
            "needs_reextract": False,
        }

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
