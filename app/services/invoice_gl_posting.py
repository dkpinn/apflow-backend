from __future__ import annotations

from typing import Any

from app.services.vat_report import allocate_amount_by_weights, allocate_invoice_vat


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
