from __future__ import annotations

from typing import Any, Optional

from app.services.organisation_module_settings import (
    required_tracking_dimensions,
    validate_supplier_allocations_tracking,
)
from app.services.vat_report import allocate_amount_by_weights, allocate_invoice_vat


def has_duplicate_invoice_reference(
    supabase,
    *,
    invoice_id: str,
    organisation_id: str,
    supplier_id: str | None,
    invoice_number: str | None,
) -> bool:
    if not supplier_id or not str(invoice_number or "").strip():
        return False
    result = (
        supabase.table("invoices_extracted")
        .select("id")
        .eq("organisation_id", organisation_id)
        .eq("supplier_id", supplier_id)
        .eq("invoice_number", invoice_number)
        .neq("id", invoice_id)
        .limit(1)
        .execute()
    )
    return bool(result.data)


def build_invoice_debit_lines(
    *,
    organisation_id: str,
    invoice: dict,
    line_items: list[dict],
    allocations_by_line: dict[str, list[dict]],
    supplier_has_vat_number: bool,
    vat_control_account_id: str | None,
) -> dict:
    invoice_id = str(invoice.get("id") or "")
    description_base = (
        f"{invoice.get('supplier_name_extracted') or 'Supplier'} "
        f"- {invoice.get('invoice_number') or invoice_id[:8]}"
    )
    vat_allocation = allocate_invoice_vat(
        invoice_tax=invoice.get("tax_amount"),
        line_items=line_items,
        supplier_has_vat_number=supplier_has_vat_number,
    )
    vat_by_line_id = {
        str(row.get("line_id")): row
        for row in vat_allocation["line_allocations"]
        if row.get("line_id")
    }

    journal_lines: list[dict[str, Any]] = []
    missing_accounts: list[str] = []
    sort_order = 0

    for line_item in line_items:
        line_desc = line_item.get("description") or "Invoice line"
        allocations = allocations_by_line.get(str(line_item.get("id")), [])
        tracking = line_item.get("tracking") or {}
        line_vat = vat_by_line_id.get(str(line_item.get("id"))) or {}
        blocked_line_vat = line_vat.get("blocked_tax") or 0

        if allocations:
            blocked_shares = allocate_amount_by_weights(
                blocked_line_vat,
                [allocation.get("amount") for allocation in allocations],
            )
            for allocation_index, allocation in enumerate(allocations):
                account_id = allocation.get("expense_account")
                if not account_id:
                    missing_accounts.append(f"{line_desc} (split)")
                    continue
                blocked_share = (
                    blocked_shares[allocation_index]
                    if allocation_index < len(blocked_shares)
                    else 0
                )
                amount = round(
                    float(allocation.get("amount") or 0) + float(blocked_share),
                    2,
                )
                if amount <= 0:
                    continue
                journal_lines.append({
                    "organisation_id": organisation_id,
                    "account_id": account_id,
                    "description": f"{description_base} / {line_desc}",
                    "debit_amount": amount,
                    "credit_amount": 0.0,
                    "tracking": allocation.get("tracking") or tracking,
                    "sort_order": sort_order,
                })
                sort_order += 1
            continue

        account_id = line_item.get("expense_account")
        if not account_id:
            missing_accounts.append(line_desc)
            continue
        amount = round(
            float(line_item.get("line_total") or 0) + float(blocked_line_vat),
            2,
        )
        if amount <= 0:
            continue
        journal_lines.append({
            "organisation_id": organisation_id,
            "account_id": account_id,
            "description": f"{description_base} / {line_desc}",
            "debit_amount": amount,
            "credit_amount": 0.0,
            "tracking": tracking,
            "sort_order": sort_order,
        })
        sort_order += 1

    claimable_tax = round(float(vat_allocation["claimable_tax"]), 2)
    if vat_control_account_id and claimable_tax > 0:
        journal_lines.append({
            "organisation_id": organisation_id,
            "account_id": vat_control_account_id,
            "description": f"{description_base} - VAT",
            "debit_amount": claimable_tax,
            "credit_amount": 0.0,
            "tracking": {},
            "sort_order": sort_order,
        })

    return {
        "description_base": description_base,
        "journal_lines": journal_lines,
        "missing_accounts": missing_accounts,
        "claimable_tax": claimable_tax,
        "blocked_tax": round(float(vat_allocation["blocked_tax"]), 2),
    }


def _validate_allocation_balances(
    line_items: list[dict],
    allocations_by_line: dict[str, list[dict]],
) -> None:
    failures: list[str] = []
    for index, line_item in enumerate(line_items):
        allocations = allocations_by_line.get(str(line_item.get("id")), [])
        if not allocations:
            continue
        line_total = round(abs(float(line_item.get("line_total") or 0)), 2)
        allocation_total = round(sum(float(row.get("amount") or 0) for row in allocations), 2)
        if abs(line_total - allocation_total) > 0.02:
            description = line_item.get("description") or f"Line {index + 1}"
            failures.append(f"{description}: {allocation_total:.2f} allocated vs {line_total:.2f}")
    if failures:
        raise ValueError("Invoice allocation splits do not balance. " + "; ".join(failures[:8]))


def prepare_invoice_gl_posting(
    supabase,
    *,
    invoice_id: str,
    org_id: str,
) -> dict:
    inv_res = (
        supabase.table("invoices_extracted")
        .select("*")
        .eq("id", invoice_id)
        .eq("organisation_id", org_id)
        .limit(1)
        .execute()
    )
    if not inv_res.data:
        raise ValueError(f"Invoice {invoice_id} not found")
    invoice = inv_res.data[0]

    if invoice.get("posting_status") == "posted":
        raise ValueError("Invoice has already been posted to GL")
    if has_duplicate_invoice_reference(
        supabase,
        invoice_id=invoice_id,
        organisation_id=org_id,
        supplier_id=invoice.get("supplier_id"),
        invoice_number=invoice.get("invoice_number"),
    ):
        raise ValueError("Duplicate invoices cannot be posted to GL")

    subtotal = float(invoice.get("subtotal") or 0)
    tax_amount = float(invoice.get("tax_amount") or 0)
    gross_total = round(subtotal + tax_amount, 2)

    if gross_total <= 0:
        raise ValueError("Invoice total is zero — nothing to post")

    li_res = (
        supabase.table("invoice_line_items")
        .select("*")
        .eq("invoice_extracted_id", invoice_id)
        .eq("organisation_id", org_id)
        .execute()
    )
    line_items = li_res.data or []
    if not line_items:
        raise ValueError("Invoice has no line items to post")

    supplier_vat_number = invoice.get("vat_number_extracted")
    if invoice.get("supplier_id"):
        supplier_res = (
            supabase.table("suppliers")
            .select("vat_number")
            .eq("id", invoice["supplier_id"])
            .eq("organisation_id", org_id)
            .limit(1)
            .execute()
        )
        if supplier_res.data:
            supplier_vat_number = supplier_res.data[0].get("vat_number") or supplier_vat_number

    line_ids = [li["id"] for li in line_items if li.get("id")]
    allocations_by_line: dict[str, list] = {}
    if line_ids:
        alloc_res = (
            supabase.table("invoice_line_item_allocations")
            .select("*")
            .in_("invoice_line_item_id", line_ids)
            .eq("organisation_id", org_id)
            .order("sort_order")
            .execute()
        )
        for a in (alloc_res.data or []):
            lid = a.get("invoice_line_item_id")
            allocations_by_line.setdefault(str(lid), []).append(a)

    _validate_allocation_balances(line_items, allocations_by_line)

    required_dimensions = required_tracking_dimensions(
        supabase,
        organisation_id=org_id,
        module_key="supplier",
    )
    validate_supplier_allocations_tracking(
        line_items=line_items,
        allocations_by_line=allocations_by_line,
        required_dimensions=required_dimensions,
    )

    all_accts_res = (
        supabase.table("accounts")
        .select("id, code, name, system_key")
        .eq("organisation_id", org_id)
        .execute()
    )
    all_accts = all_accts_res.data or []
    sys_accts = {row["system_key"]: row for row in all_accts if row.get("system_key")}
    trade_payables = sys_accts.get("trade_payables")
    vat_control = sys_accts.get("vat_control")

    if not trade_payables:
        raise ValueError(
            "Trade Payables system account not found for this organisation. "
            "Ensure the system accounts migration has been applied."
        )

    _by_code = {a["code"]: a["id"] for a in all_accts if a.get("code")}
    _by_name = {a["name"]: a["id"] for a in all_accts if a.get("name")}
    _account_ids = {str(a["id"]) for a in all_accts if a.get("id")}

    def _resolve(val: Optional[str]) -> Optional[str]:
        if not val:
            return val
        resolved = _by_code.get(val) or _by_name.get(val) or str(val)
        return resolved if str(resolved) in _account_ids else None

    line_items = [{**li, "expense_account": _resolve(li.get("expense_account"))} for li in line_items]
    allocations_by_line = {
        lid: [{**a, "expense_account": _resolve(a.get("expense_account"))} for a in allocs]
        for lid, allocs in allocations_by_line.items()
    }

    posting = build_invoice_debit_lines(
        organisation_id=org_id,
        invoice=invoice,
        line_items=line_items,
        allocations_by_line=allocations_by_line,
        supplier_has_vat_number=bool(str(supplier_vat_number or "").strip()),
        vat_control_account_id=str(vat_control["id"]) if vat_control else None,
    )
    journal_lines = posting["journal_lines"]
    missing_accounts = posting["missing_accounts"]
    claimable_tax = posting["claimable_tax"]
    description_base = posting["description_base"]

    if missing_accounts:
        raise ValueError(
            f"Cannot post — the following lines have no expense account: {', '.join(missing_accounts[:5])}"
        )

    if claimable_tax > 0 and not vat_control:
        raise ValueError("VAT Control system account not found for this organisation.")

    total_debit = round(sum(float(line["debit_amount"]) for line in journal_lines), 2)
    if abs(total_debit - gross_total) > 0.02:
        raise ValueError(
            f"VAT allocation did not reconcile to the invoice total "
            f"({total_debit:.2f} posted vs {gross_total:.2f} invoice)."
        )

    journal_lines.append({
        "organisation_id": org_id,
        "account_id": trade_payables["id"],
        "description": description_base,
        "debit_amount": 0.0,
        "credit_amount": total_debit,
        "tracking": {},
        "sort_order": len(journal_lines),
    })

    return {
        "invoice": invoice,
        "invoice_id": invoice_id,
        "organisation_id": org_id,
        "journal_date": invoice.get("invoice_date"),
        "description": description_base,
        "journal_lines": journal_lines,
        "gross_total": gross_total,
        "total_debit": total_debit,
        "total_credit": total_debit,
        "trade_payables_account": trade_payables.get("code"),
        "vat_control_account": vat_control.get("code") if vat_control else None,
    }


def persist_prepared_invoice_posting(
    supabase,
    *,
    prepared: dict,
    user_id: Optional[str] = None,
) -> dict:
    try:
        rpc_result = supabase.rpc(
            "post_invoice_to_gl_atomic",
            {
                "p_org_id": prepared["organisation_id"],
                "p_invoice_id": prepared["invoice_id"],
                "p_user_id": user_id,
                "p_journal_date": prepared.get("journal_date"),
                "p_description": prepared["description"],
                "p_total": prepared["total_debit"],
                "p_lines": prepared["journal_lines"],
            },
        ).execute()
    except Exception as exc:
        raise ValueError(f"GL posting transaction failed: {exc}") from exc
    result = rpc_result.data
    if isinstance(result, list):
        result = result[0] if result else None
    if not isinstance(result, dict) or not result.get("journal_id"):
        raise ValueError("GL posting transaction did not return a journal")

    return {
        "success": True,
        "journal_id": str(result["journal_id"]),
        "total_debit": float(result.get("total_debit", prepared["total_debit"])),
        "total_credit": float(result.get("total_credit", prepared["total_credit"])),
        "lines": int(result.get("lines", len(prepared["journal_lines"]))),
        "trade_payables_account": prepared["trade_payables_account"],
        "vat_control_account": prepared["vat_control_account"],
    }


def post_invoice_to_gl_service(
    supabase,
    *,
    invoice_id: str,
    org_id: str,
    user_id: Optional[str] = None,
    prepared: Optional[dict] = None,
) -> dict:
    """Compatibility facade for canonical preparation and atomic persistence."""
    posting = prepared or prepare_invoice_gl_posting(
        supabase,
        invoice_id=invoice_id,
        org_id=org_id,
    )
    if posting["invoice_id"] != invoice_id or posting["organisation_id"] != org_id:
        raise ValueError("Prepared journal does not belong to the requested invoice")
    return persist_prepared_invoice_posting(
        supabase,
        prepared=posting,
        user_id=user_id,
    )
