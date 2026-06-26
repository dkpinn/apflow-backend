from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _rows(db, table: str, organisation_id: str, select: str = "*") -> list[dict[str, Any]]:
    return (
        db.table(table)
        .select(select)
        .eq("organisation_id", organisation_id)
        .limit(1000)
        .execute()
    ).data or []


def _one(db, table: str, organisation_id: str, select: str = "*") -> dict[str, Any] | None:
    rows = _rows(db, table, organisation_id, select)
    return rows[0] if rows else None


def _organisation(db, organisation_id: str) -> dict[str, Any] | None:
    response = (
        db.table("organisations")
        .select(
            "id, name, legal_name, registration_number, vat_number, tax_number, country, currency, base_currency, financial_year_end"
        )
        .eq("id", organisation_id)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None


def _status(value: Any, default: str = "") -> str:
    return str(value or default).strip().lower()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _money(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _item(
    item_id: str,
    label: str,
    status: str,
    required: bool,
    href: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "label": label,
        "status": status,
        "required": required,
        "href": href,
        "detail": detail,
    }


def _readiness_label(percent: int, blocking_count: int) -> str:
    if blocking_count == 0:
        return "Ready for pilot"
    if percent >= 75:
        return "Almost ready"
    if percent >= 40:
        return "Needs setup"
    return "Not ready"


def generate_go_live_checklist(db, *, organisation_id: str) -> dict[str, Any]:
    organisation = _organisation(db, organisation_id) or {}
    accounts = _rows(db, "accounts", organisation_id, "id, active, is_system, system_key")
    bank_accounts = _rows(db, "bank_accounts", organisation_id, "id, active, gl_account_id, opening_balance")
    users = _rows(db, "organisation_users", organisation_id, "id, role, status")
    tracking_dimensions = _rows(db, "tracking_dimensions", organisation_id, "id, active")
    suppliers = _rows(db, "suppliers", organisation_id, "id, active")
    customers = _rows(db, "customers", organisation_id, "id, active")
    inventory_items = _rows(db, "inventory_items", organisation_id, "id, active")
    branding = _one(
        db,
        "organisation_invoice_branding",
        organisation_id,
        "logo_storage_path, bank_name, account_holder, account_number, primary_color",
    )
    posted_journals = _rows(db, "gl_journals", organisation_id, "id, status")

    org_has_core = bool(
        _text(organisation.get("name"))
        and _text(organisation.get("country"))
        and (_text(organisation.get("base_currency")) or _text(organisation.get("currency")))
        and _text(organisation.get("financial_year_end"))
    )
    org_has_legal = bool(_text(organisation.get("legal_name")) or _text(organisation.get("registration_number")))
    org_detail = "Core details are complete." if org_has_core else "Add country, base currency, and financial year-end."
    if org_has_core and not org_has_legal:
        org_detail = "Core details are complete; legal name or registration number is still recommended."

    active_accounts = [row for row in accounts if row.get("active") is not False]
    system_keys = {_text(row.get("system_key")) for row in active_accounts if row.get("system_key")}
    has_core_system_accounts = {"trade_receivables", "trade_payables", "vat_control"}.issubset(system_keys)
    chart_ok = len(active_accounts) >= 5 and has_core_system_accounts

    active_bank_accounts = [row for row in bank_accounts if row.get("active") is not False]
    mapped_bank_accounts = [row for row in active_bank_accounts if row.get("gl_account_id")]
    bank_ok = bool(active_bank_accounts) and len(mapped_bank_accounts) == len(active_bank_accounts)

    active_users = [row for row in users if _status(row.get("status")) == "active"]
    admin_users = [row for row in active_users if _status(row.get("role")) in {"owner", "admin"}]

    active_tracking = [row for row in tracking_dimensions if row.get("active") is not False]
    active_suppliers = [row for row in suppliers if row.get("active") is not False]
    active_customers = [row for row in customers if row.get("active") is not False]
    active_inventory = [row for row in inventory_items if row.get("active") is not False]

    has_branding = bool(
        branding
        and (
            _text(branding.get("logo_storage_path"))
            or _text(branding.get("bank_name"))
            or _text(branding.get("account_number"))
            or _text(branding.get("account_holder"))
        )
    )
    has_tax_identity = bool(_text(organisation.get("vat_number")) or _text(organisation.get("tax_number")))
    has_opening_balance_signal = any(_money(row.get("opening_balance")) != 0 for row in active_bank_accounts)
    has_posted_journal = any(_status(row.get("status")) == "posted" for row in posted_journals)

    items = [
        _item(
            "organisation_details",
            "Organisation details",
            "complete" if org_has_core else "missing",
            True,
            "/settings",
            org_detail,
        ),
        _item(
            "tax_identifiers",
            "Tax identifiers",
            "complete" if has_tax_identity else "attention",
            False,
            "/settings",
            "VAT or income-tax number is captured." if has_tax_identity else "Add VAT or tax numbers before VAT/SARS workflows.",
        ),
        _item(
            "chart_of_accounts",
            "Chart of accounts",
            "complete" if chart_ok else "missing",
            True,
            "/settings",
            f"{len(active_accounts)} active accounts; required control accounts {'present' if has_core_system_accounts else 'missing'}.",
        ),
        _item(
            "bank_accounts",
            "Bank/cash accounts",
            "complete" if bank_ok else "missing",
            True,
            "/bank",
            (
                f"{len(active_bank_accounts)} active bank/cash accounts, all mapped to GL."
                if bank_ok
                else "Add at least one bank/cash account and link every active account to a GL account."
            ),
        ),
        _item(
            "users_roles",
            "Users and roles",
            "complete" if admin_users else "missing",
            True,
            "/settings",
            f"{len(active_users)} active users; {len(admin_users)} owner/admin users.",
        ),
        _item(
            "tracking_dimensions",
            "Tracking dimensions",
            "complete" if active_tracking else "attention",
            False,
            "/settings",
            (
                f"{len(active_tracking)} active tracking dimensions configured."
                if active_tracking
                else "Optional, but recommended if reporting by department, project, branch, or cost centre."
            ),
        ),
        _item(
            "invoice_branding",
            "Invoice branding and payment details",
            "complete" if has_branding else "attention",
            False,
            "/settings",
            "Branding/payment details are configured." if has_branding else "Add invoice colours, logo, terms, or payment details.",
        ),
        _item(
            "master_data",
            "Supplier/customer master data",
            "complete" if active_suppliers or active_customers else "attention",
            False,
            "/settings",
            f"{len(active_suppliers)} suppliers, {len(active_customers)} customers, {len(active_inventory)} inventory items.",
        ),
        _item(
            "opening_balances",
            "Opening balances",
            "complete" if has_opening_balance_signal or has_posted_journal else "attention",
            False,
            "/bank",
            (
                "Opening balance or posted journal activity detected."
                if has_opening_balance_signal or has_posted_journal
                else "Capture opening bank balances or opening journals before relying on reports."
            ),
        ),
        _item(
            "test_transaction",
            "Test transaction posted",
            "complete" if has_posted_journal else "missing",
            True,
            "/bank",
            "At least one posted journal exists." if has_posted_journal else "Post one test bank, supplier, or sales transaction.",
        ),
    ]

    required_items = [item for item in items if item["required"]]
    complete_required = [item for item in required_items if item["status"] == "complete"]
    blocking_items = [item for item in required_items if item["status"] != "complete"]
    optional_attention = [item for item in items if not item["required"] and item["status"] != "complete"]
    percent = round((len(complete_required) / len(required_items)) * 100) if required_items else 100

    return {
        "organisation_id": organisation_id,
        "readiness": {
            "percent": percent,
            "label": _readiness_label(percent, len(blocking_items)),
            "required_complete": len(complete_required),
            "required_total": len(required_items),
            "blocking_count": len(blocking_items),
            "optional_attention_count": len(optional_attention),
            "ready": len(blocking_items) == 0,
        },
        "items": items,
        "next_actions": sorted(
            [item for item in items if item["status"] != "complete"],
            key=lambda item: (not item["required"], item["status"] != "missing"),
        )[:5],
        "disclaimer": "Go-live readiness uses existing setup data; it does not lock periods or validate historical balances yet.",
    }
