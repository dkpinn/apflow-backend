"""supplier_allocation_rules.py
Supplier line-item allocation rule routes (CRUD + from-invoice).

Private helpers (_normalise_allocation_rule_payload, etc.) moved here because
they are only used by these routes.  Shared models and org-lookup helpers are
imported from suppliers.py.  suppliers.py does NOT import this module, so there
is no circular-import risk.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.audit_log import log_invoice_event
from app.services.invoice_supplier_rules import fetch_supplier_allocation_rules
from app.services.supplier_service import get_extracted_invoice
from app.services.tracking_dimensions import list_tracking_dimensions

from app.routers.suppliers import (
    SupplierAllocationRuleRequest,
    SupplierAllocationRuleUpdateRequest,
    SupplierAllocationRulesFromInvoiceRequest,
    SupplierAllocationRuleSplitRequest,
    _org_for_allocation_rule,
    _org_for_supplier,
    supabase,
)

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])


# ── Private helpers ────────────────────────────────────────────────────────────

def _normalise_allocation_rule_payload(payload: dict) -> dict:
    allowed = {
        "organisation_id",
        "supplier_id",
        "name",
        "active",
        "priority",
        "document_scope",
        "match_type",
        "match_field",
        "pattern",
        "notes",
        "source_invoice_extracted_id",
    }
    return {
        key: value
        for key, value in payload.items()
        if key in allowed and value not in ("", None)
    }


def _normalise_allocation_split_payloads(
    *,
    rule_id: str,
    organisation_id: str,
    splits: list[SupplierAllocationRuleSplitRequest],
) -> list[dict]:
    payload: list[dict] = []
    for index, split in enumerate(splits or []):
        row = split.model_dump()
        percent = row.get("percent")
        if percent is None or percent <= 0:
            continue
        payload.append({
            "rule_id": rule_id,
            "organisation_id": organisation_id,
            "expense_account": row.get("expense_account"),
            "tracking": {
                str(key): value
                for key, value in (row.get("tracking") or {}).items()
                if value not in (None, "")
            },
            "vat_treatment": row.get("vat_treatment"),
            "percent": percent,
            "note": row.get("note"),
            "sort_order": row.get("sort_order") if row.get("sort_order") is not None else index,
        })
    return payload


def _validate_rule_splits(db, *, organisation_id: str, splits: list[SupplierAllocationRuleSplitRequest]) -> None:
    if not splits:
        raise HTTPException(status_code=422, detail="Add at least one allocation split")
    total = round(sum(float(split.percent or 0) for split in splits), 4)
    if abs(total - 100) > 0.0001:
        raise HTTPException(status_code=422, detail="Allocation split percentages must total 100")

    dimensions = list_tracking_dimensions(db, organisation_id=organisation_id)
    values_by_dimension = {
        str(dimension["id"]): {
            str(value["id"])
            for value in dimension.get("values") or []
            if value.get("active") is not False
        }
        for dimension in dimensions
    }
    for split in splits:
        for dimension_id, value_id in (split.tracking or {}).items():
            if str(value_id) not in values_by_dimension.get(str(dimension_id), set()):
                raise HTTPException(
                    status_code=422,
                    detail="Every tracking allocation must use an active value from an active organisation dimension",
                )


@router.get("/{supplier_id}/allocation-rule-options")
def get_supplier_allocation_rule_options(
    supplier_id: str,
    organisation_id: str = Query(...),
    auth: UserAuth = ...,
):
    user_id, db = auth
    supplier_org = _org_for_supplier(supplier_id)
    if supplier_org and supplier_org != organisation_id:
        raise HTTPException(status_code=404, detail="Supplier not found")
    ensure_org_read(user_id, organisation_id)
    dimensions = list_tracking_dimensions(db, organisation_id=organisation_id)
    accounts = (
        db.table("accounts")
        .select("id, code, name, type, active, vat_treatment")
        .eq("organisation_id", organisation_id)
        .eq("active", True)
        .execute()
        .data
        or []
    )
    return {
        "tracking_dimensions": dimensions,
        "vat_treatments": [
            {"value": "full", "label": "Full input VAT claim"},
            {"value": "blocked", "label": "Blocked VAT (expense it)"},
            {"value": "zero_rated", "label": "Zero-rated (0%)"},
            {"value": "exempt", "label": "Exempt / no VAT"},
        ],
        "accounts": accounts,
    }


def _rule_with_splits(rule: dict) -> dict:
    if rule.get("id") and not rule.get("supplier_id"):
        try:
            res = (
                supabase
                .table("supplier_line_item_allocation_rules")
                .select("*")
                .eq("id", rule["id"])
                .limit(1)
                .execute()
            )
            if res.data:
                rule = res.data[0]
        except Exception:
            pass
    rules = fetch_supplier_allocation_rules(supabase, rule.get("supplier_id"), active_only=False)
    return next((row for row in rules if row.get("id") == rule.get("id")), {**rule, "splits": []})


def _line_rule_pattern(description: str | None, code: str | None) -> tuple[str, str]:
    clean_description = re.sub(r"\s+", " ", str(description or "")).strip()
    clean_code = re.sub(r"\s+", " ", str(code or "")).strip()
    if clean_description:
        return "description", clean_description[:180]
    return "code", clean_code[:180]


def _splits_from_line_item(line_item: dict, allocations: list[dict]) -> list[SupplierAllocationRuleSplitRequest]:
    if allocations:
        line_total = None
        try:
            line_total = float(line_item.get("line_total") or line_item.get("amount") or 0)
        except Exception:
            line_total = None
        return [
            SupplierAllocationRuleSplitRequest(
                expense_account=allocation.get("expense_account") or line_item.get("expense_account"),
                tracking=allocation.get("tracking") or line_item.get("tracking") or {},
                vat_treatment=allocation.get("vat_treatment") or line_item.get("vat_treatment"),
                percent=float(
                    allocation.get("percent")
                    or (
                        round((float(allocation.get("amount") or 0) / line_total) * 100, 4)
                        if line_total
                        else 100
                    )
                ),
                note=allocation.get("note"),
                sort_order=index,
            )
            for index, allocation in enumerate(allocations)
        ]

    if not line_item.get("expense_account") and not line_item.get("tracking"):
        return []
    return [
        SupplierAllocationRuleSplitRequest(
            expense_account=line_item.get("expense_account"),
            tracking=line_item.get("tracking") or {},
            vat_treatment=line_item.get("vat_treatment"),
            percent=100,
            sort_order=0,
        )
    ]


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/{supplier_id}/allocation-rules")
def list_supplier_allocation_rules(
    supplier_id: str,
    organisation_id: str = Query(...),
    auth: UserAuth = ...,
):
    user_id, _db = auth
    supplier_org = _org_for_supplier(supplier_id)
    if supplier_org and supplier_org != organisation_id:
        raise HTTPException(status_code=404, detail="Supplier not found")
    ensure_org_read(user_id, organisation_id)
    rules = fetch_supplier_allocation_rules(supabase, supplier_id, active_only=False)
    return {"success": True, "rules": rules}


@router.post("/{supplier_id}/allocation-rules")
def create_supplier_allocation_rule(
    supplier_id: str,
    payload: SupplierAllocationRuleRequest,
    auth: UserAuth,
):
    user_id, _db = auth
    supplier_org = _org_for_supplier(supplier_id)
    if supplier_org and supplier_org != payload.organisation_id:
        raise HTTPException(status_code=400, detail="Supplier does not belong to the specified organisation")
    if supplier_id != payload.supplier_id:
        raise HTTPException(status_code=400, detail="Supplier id mismatch")
    ensure_org_write(user_id, payload.organisation_id)
    _validate_rule_splits(supabase, organisation_id=payload.organisation_id, splits=payload.splits)

    insert_payload = _normalise_allocation_rule_payload(payload.model_dump(exclude={"splits"}))
    if not insert_payload.get("name"):
        raise HTTPException(status_code=400, detail="Missing rule name")
    res = supabase.table("supplier_line_item_allocation_rules").insert(insert_payload).execute()
    rule = res.data[0] if res.data else None
    if not rule:
        raise HTTPException(status_code=400, detail="Allocation rule create failed")

    split_payload = _normalise_allocation_split_payloads(
        rule_id=rule["id"],
        organisation_id=payload.organisation_id,
        splits=payload.splits,
    )
    if split_payload:
        supabase.table("supplier_line_item_allocation_rule_splits").insert(split_payload).execute()

    return {"success": True, "rule": _rule_with_splits(rule)}


@router.patch("/allocation-rules/{rule_id}")
def update_supplier_allocation_rule(
    rule_id: str,
    payload: SupplierAllocationRuleUpdateRequest,
    auth: UserAuth,
):
    user_id, _db = auth
    organisation_id = _org_for_allocation_rule(rule_id)
    if not organisation_id:
        raise HTTPException(status_code=404, detail="Allocation rule not found")
    ensure_org_write(user_id, organisation_id)

    updates = _normalise_allocation_rule_payload(payload.model_dump(exclude={"splits"}, exclude_unset=True))
    rule = None
    if updates:
        res = (
            supabase
            .table("supplier_line_item_allocation_rules")
            .update(updates)
            .eq("id", rule_id)
            .execute()
        )
        rule = res.data[0] if res.data else {"id": rule_id, "organisation_id": organisation_id}
    else:
        res = (
            supabase
            .table("supplier_line_item_allocation_rules")
            .select("*")
            .eq("id", rule_id)
            .limit(1)
            .execute()
        )
        rule = res.data[0] if res.data else None

    if payload.splits is not None:
        _validate_rule_splits(supabase, organisation_id=organisation_id, splits=payload.splits)
        supabase.table("supplier_line_item_allocation_rule_splits").delete().eq("rule_id", rule_id).execute()
        split_payload = _normalise_allocation_split_payloads(
            rule_id=rule_id,
            organisation_id=organisation_id,
            splits=payload.splits,
        )
        if split_payload:
            supabase.table("supplier_line_item_allocation_rule_splits").insert(split_payload).execute()

    if not rule:
        raise HTTPException(status_code=404, detail="Allocation rule not found")
    return {"success": True, "rule": _rule_with_splits(rule)}


@router.delete("/allocation-rules/{rule_id}")
def delete_supplier_allocation_rule(rule_id: str, auth: UserAuth):
    user_id, _db = auth
    organisation_id = _org_for_allocation_rule(rule_id)
    if not organisation_id:
        raise HTTPException(status_code=404, detail="Allocation rule not found")
    ensure_org_write(user_id, organisation_id)
    supabase.table("supplier_line_item_allocation_rules").delete().eq("id", rule_id).execute()
    return {"success": True}


@router.post("/{supplier_id}/allocation-rules/from-invoice")
def create_supplier_allocation_rules_from_invoice(
    supplier_id: str,
    payload: SupplierAllocationRulesFromInvoiceRequest,
    auth: UserAuth,
):
    user_id, _db = auth
    invoice = get_extracted_invoice(payload.invoice_extracted_id)
    organisation_id = invoice.get("organisation_id")
    if not organisation_id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.get("supplier_id") and invoice.get("supplier_id") != supplier_id:
        raise HTTPException(status_code=400, detail="Invoice is linked to a different supplier")
    if supplier_id != payload.supplier_id:
        raise HTTPException(status_code=400, detail="Supplier id mismatch")
    supplier_org = _org_for_supplier(supplier_id)
    if supplier_org and supplier_org != organisation_id:
        raise HTTPException(status_code=400, detail="Supplier and invoice belong to different organisations")
    ensure_org_write(user_id, organisation_id)

    line_query = (
        supabase
        .table("invoice_line_items")
        .select("*")
        .eq("invoice_extracted_id", payload.invoice_extracted_id)
        .order("sort_order", desc=False)
        .order("id", desc=False)
    )
    if payload.line_item_ids:
        line_query = line_query.in_("id", payload.line_item_ids)
    line_items = line_query.execute().data or []
    if not line_items:
        raise HTTPException(status_code=400, detail="No invoice line items found to save as rules")

    line_ids = [line.get("id") for line in line_items if line.get("id")]
    allocations_by_line: dict[str, list[dict]] = {}
    if line_ids:
        allocation_rows = (
            supabase
            .table("invoice_line_item_allocations")
            .select("*")
            .in_("invoice_line_item_id", line_ids)
            .order("sort_order", desc=False)
            .execute()
            .data
            or []
        )
        for allocation in allocation_rows:
            line_id = allocation.get("invoice_line_item_id")
            if line_id:
                allocations_by_line.setdefault(line_id, []).append(allocation)

    created_rules: list[dict] = []
    for index, line_item in enumerate(line_items):
        splits = _splits_from_line_item(line_item, allocations_by_line.get(line_item.get("id"), []))
        if not splits:
            continue
        match_field, pattern = _line_rule_pattern(line_item.get("description"), line_item.get("code"))
        if not pattern:
            continue
        rule_payload = {
            "organisation_id": organisation_id,
            "supplier_id": supplier_id,
            "name": f"{line_item.get('description') or line_item.get('code') or 'Line item'} allocation",
            "active": True,
            "priority": payload.priority + index,
            "document_scope": payload.document_scope,
            "match_type": "contains",
            "match_field": match_field,
            "pattern": pattern,
            "source_invoice_extracted_id": payload.invoice_extracted_id,
        }
        res = supabase.table("supplier_line_item_allocation_rules").insert(rule_payload).execute()
        rule = res.data[0] if res.data else None
        if not rule:
            continue
        split_payload = _normalise_allocation_split_payloads(
            rule_id=rule["id"],
            organisation_id=organisation_id,
            splits=splits,
        )
        if split_payload:
            supabase.table("supplier_line_item_allocation_rule_splits").insert(split_payload).execute()
        created_rules.append(_rule_with_splits(rule))

    if not created_rules:
        raise HTTPException(status_code=400, detail="No coded line items or allocations were available to save as rules")

    log_invoice_event(
        supabase,
        organisation_id=organisation_id,
        invoice_raw_id=invoice.get("invoice_raw_id"),
        invoice_extracted_id=invoice.get("id"),
        event_type="supplier_allocation_rules_created",
        stage="supplier_processing_rules",
        actor_type="api",
        new_value={"supplier_id": supplier_id, "rules_created": len(created_rules)},
        notes="Supplier allocation rules were created from reviewed invoice line items.",
    )
    return {"success": True, "rules": created_rules}
