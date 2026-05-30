from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional


SUGGESTION_STATUSES = {"open", "applied", "dismissed", "checked"}

SAFE_INVOICE_FIELDS = {
    "supplier_name_extracted",
    "vat_number_extracted",
    "company_registration_number_extracted",
    "cus_code_extracted",
    "supplier_email_extracted",
    "supplier_acc_email_extracted",
    "supplier_telephone_extracted",
    "supplier_cell_extracted",
    "supplier_fax_extracted",
    "supplier_website_extracted",
    "supplier_del_address_extracted",
    "supplier_pos_address_extracted",
    "expense_account",
}

SAFE_LINE_ITEM_FIELDS = {"expense_account", "tracking"}

SAFE_SUPPLIER_FIELDS = {
    "supplier_code",
    "account_number",
    "vat_number",
    "tax_number",
    "registration_number",
    "company_registration_number",
    "default_email",
    "accounting_email",
    "phone",
    "fax",
    "cell",
    "website",
    "delivery_address",
    "postal_address",
    "bank_account_name",
    "bank_name",
    "bank_account_number",
    "bank_branch_code",
    "bank_swift_code",
    "bank_country",
    "bank_verified",
}


def agent_status_after_regeneration(existing_status: Optional[str]) -> Optional[str]:
    """
    A checked finding is only acknowledged for the current pass.

    If the same condition is generated again, it should become open again.
    Dismissed and applied findings remain closed.
    """
    if existing_status in {"open", "checked"}:
        return "open"
    return existing_status


def has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def money(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    clean = str(value).strip()
    if not clean:
        return None
    clean = clean.replace("R", "").replace("ZAR", "").replace(" ", "")
    if "," in clean and "." not in clean:
        clean = clean.replace(",", ".")
    else:
        clean = clean.replace(",", "")
    try:
        return round(float(clean), 2)
    except Exception:
        return None


def compact_string(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def digits_only(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def phone_matches(left: Any, right: Any) -> bool:
    left_digits = digits_only(left)
    right_digits = digits_only(right)
    if not left_digits or not right_digits:
        return False
    if left_digits == right_digits:
        return True
    longer, shorter = (left_digits, right_digits) if len(left_digits) >= len(right_digits) else (right_digits, left_digits)
    if len(shorter) < 7:
        return False
    return longer.startswith(shorter) or longer[-len(shorter):] == shorter


BANK_ALIASES = {
    "fnb": "firstnationalbank",
    "firstnationalbank": "firstnationalbank",
    "firstnational": "firstnationalbank",
    "absa": "absa",
    "absabank": "absa",
    "standardbank": "standardbank",
    "thestandardbankofsouthafrica": "standardbank",
    "nedbank": "nedbank",
    "capitec": "capitec",
    "capitecbank": "capitec",
    "investec": "investec",
}


def normalise_bank_name(value: Any) -> str:
    compact = compact_string(value)
    return BANK_ALIASES.get(compact, compact)


def bank_values_match(invoice_key: str, invoice_value: Any, supplier_value: Any) -> bool:
    if invoice_key == "bank_name_extracted":
        return normalise_bank_name(invoice_value) == normalise_bank_name(supplier_value)
    return compact_string(invoice_value) == compact_string(supplier_value)


def is_cash_or_card_document(invoice: dict) -> bool:
    document_type = compact_string(invoice.get("document_type"))
    payment_method = compact_string(invoice.get("payment_method"))
    return document_type in {"cardreceipt", "cashreceipt"} or payment_method in {"cash", "card", "creditcard", "debitcard", "cashcard"}


def branch_value(branch: Optional[dict], supplier: Optional[dict], key: str) -> Any:
    if branch and has_value(branch.get(key)):
        return branch.get(key)
    return (supplier or {}).get(key)


def find_branch_match(invoice: dict, branches: list[dict]) -> Optional[dict]:
    invoice_vat = compact_string(invoice.get("vat_number_extracted"))
    invoice_code = compact_string(invoice.get("cus_code_extracted"))
    invoice_address = compact_string(invoice.get("supplier_del_address_extracted"))
    invoice_name = compact_string(invoice.get("supplier_name_extracted") or invoice.get("issuer_name_extracted"))
    for branch in branches:
        if invoice_vat and invoice_vat == compact_string(branch.get("vat_number") or branch.get("tax_number")):
            return branch
        if invoice_code and invoice_code == compact_string(branch.get("branch_code")):
            return branch
        if invoice_address and compact_string(branch.get("delivery_address")) and invoice_address == compact_string(branch.get("delivery_address")):
            return branch
        branch_name = compact_string(branch.get("branch_name"))
        if invoice_name and branch_name and (branch_name in invoice_name or invoice_name in branch_name):
            return branch
    return None


def normalise_tracking(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items() if v not in (None, "")}


def suggestion_fingerprint(category: str, message: str, apply_payload: Optional[dict] = None) -> str:
    payload = {
        "category": category,
        "message": message,
        "apply_payload": apply_payload or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


@dataclass(frozen=True)
class AgentSuggestion:
    category: str
    severity: str
    message: str
    reason: str
    confidence: float
    apply_payload: Optional[dict] = None
    target: Optional[dict] = None

    def as_dict(self) -> dict:
        payload = {
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "reason": self.reason,
            "confidence": round(max(0.0, min(float(self.confidence), 1.0)), 4),
            "apply_payload": self.apply_payload,
            "target": self.target,
        }
        payload["fingerprint"] = suggestion_fingerprint(
            self.category,
            self.message,
            self.apply_payload,
        )
        return payload


def _supplier_value(invoice: dict, supplier: Optional[dict], supplier_key: str, invoice_key: str) -> Any:
    if supplier and has_value(supplier.get(supplier_key)):
        return supplier.get(supplier_key)
    return invoice.get(invoice_key)


def _supplier_identity_suggestions(
    invoice: dict,
    supplier: Optional[dict],
    supplier_branch: Optional[dict] = None,
    supplier_branches: Optional[list[dict]] = None,
) -> list[AgentSuggestion]:
    suggestions: list[AgentSuggestion] = []
    supplier_name = (
        invoice.get("supplier_name_extracted")
        or invoice.get("issuer_name_extracted")
        or (supplier or {}).get("supplier_name")
    )
    if not has_value(supplier_name):
        suggestions.append(AgentSuggestion(
            category="supplier_identity",
            severity="critical",
            message="Supplier name is missing.",
            reason="The invoice cannot be reliably coded or reviewed until a supplier is identified.",
            confidence=0.98,
            target={"tab": "supplier", "field": "supplier_name_extracted"},
        ))
    elif not invoice.get("supplier_id"):
        suggestions.append(AgentSuggestion(
            category="supplier_identity",
            severity="warning",
            message=f"Link or create supplier master for {supplier_name}.",
            reason="A linked supplier master lets supplier rules, VAT settings, banking checks, and account defaults run consistently.",
            confidence=0.86,
            target={"tab": "supplier", "section": "supplier_master"},
        ))

    tax_amount = money(invoice.get("tax_amount"))
    branches = supplier_branches or []
    matched_branch = supplier_branch or find_branch_match(invoice, branches)
    vat_value = branch_value(matched_branch, supplier, "vat_number") or invoice.get("vat_number_extracted")
    if tax_amount and tax_amount > 0 and not has_value(vat_value):
        suggestions.append(AgentSuggestion(
            category="vat_tax",
            severity="warning",
            message="VAT/tax amount is present but no supplier VAT number is recorded.",
            reason="The document shows tax, but neither the invoice extraction nor linked supplier master has a VAT number.",
            confidence=0.82,
            target={"tab": "supplier", "field": "vat_number_extracted"},
        ))
    elif (
        supplier
        and has_value(invoice.get("vat_number_extracted"))
        and has_value(supplier.get("vat_number"))
        and compact_string(invoice.get("vat_number_extracted")) != compact_string(supplier.get("vat_number"))
        and not matched_branch
    ):
        suggestions.append(AgentSuggestion(
            category="supplier_branch",
            severity="warning",
            message="Supplier VAT differs from the master and may belong to a branch.",
            reason="Create or link a supplier branch instead of overwriting the supplier-level VAT number.",
            confidence=0.84,
            target={"tab": "supplier", "field": "vat_number_extracted"},
        ))

    registration_value = _supplier_value(
        invoice,
        supplier,
        "company_registration_number",
        "company_registration_number_extracted",
    )
    if supplier and not has_value(registration_value):
        suggestions.append(AgentSuggestion(
            category="supplier_identity",
            severity="info",
            message="Supplier company registration number is missing.",
            reason="This is not always printed on receipts, but keeping it on the supplier master improves compliance checks.",
            confidence=0.58,
            target={"tab": "supplier", "field": "company_registration_number_extracted"},
        ))

    return suggestions


def _supplier_master_update_suggestions(invoice: dict, supplier: Optional[dict]) -> list[AgentSuggestion]:
    if not supplier or not supplier.get("id"):
        return []

    fields = [
        ("vat_number_extracted", "vat_number", "VAT number"),
        ("company_registration_number_extracted", "company_registration_number", "company registration number"),
        ("supplier_acc_email_extracted", "accounting_email", "accounts email"),
        ("supplier_email_extracted", "default_email", "email"),
        ("supplier_telephone_extracted", "phone", "telephone"),
        ("supplier_del_address_extracted", "delivery_address", "delivery address"),
        ("supplier_pos_address_extracted", "postal_address", "postal address"),
        ("bank_account_name_extracted", "bank_account_name", "bank account name"),
        ("bank_name_extracted", "bank_name", "bank name"),
        ("bank_account_number_extracted", "bank_account_number", "bank account number"),
        ("bank_branch_code_extracted", "bank_branch_code", "bank branch code"),
        ("bank_swift_code_extracted", "bank_swift_code", "bank SWIFT code"),
    ]
    suggestions: list[AgentSuggestion] = []
    for invoice_key, supplier_key, label in fields:
        extracted = invoice.get(invoice_key)
        current = supplier.get(supplier_key)
        if has_value(extracted) and not has_value(current):
            patch = {supplier_key: extracted}
            if supplier_key.startswith("bank_"):
                patch["bank_verified"] = False
            suggestions.append(AgentSuggestion(
                category="supplier_master_data",
                severity="info",
                message=f"Add extracted {label} to supplier master.",
                reason=f"The invoice contains {label}, but the linked supplier master field is blank.",
                confidence=0.74,
                apply_payload={
                    "type": "supplier_patch",
                    "supplier_id": supplier["id"],
                    "fields": patch,
                },
                target={"tab": "supplier", "field": invoice_key},
            ))
    return suggestions


def _banking_suggestions(invoice: dict, supplier: Optional[dict], supplier_branch: Optional[dict] = None) -> list[AgentSuggestion]:
    if not supplier:
        return []
    suggestions: list[AgentSuggestion] = []
    comparisons = [
        ("bank_account_name_extracted", "bank_account_name", "bank account name"),
        ("bank_name_extracted", "bank_name", "bank name"),
        ("bank_account_number_extracted", "bank_account_number", "bank account number"),
        ("bank_branch_code_extracted", "bank_branch_code", "bank branch code"),
        ("bank_swift_code_extracted", "bank_swift_code", "bank SWIFT code"),
    ]
    effective_supplier = {**supplier, **{k: v for k, v in (supplier_branch or {}).items() if has_value(v)}}
    any_invoice_bank = any(has_value(invoice.get(invoice_key)) for invoice_key, _, _ in comparisons)
    any_supplier_bank = any(has_value(effective_supplier.get(supplier_key)) for _, supplier_key, _ in comparisons)
    cash_or_card = is_cash_or_card_document(invoice)
    if not any_invoice_bank and not any_supplier_bank:
        if cash_or_card:
            return suggestions
        suggestions.append(AgentSuggestion(
            category="banking",
            severity="warning",
            message="No banking details are available on the invoice or supplier master.",
            reason="Banking cannot be checked before approval until at least one trusted source is populated.",
            confidence=0.78,
            target={"tab": "supplier", "section": "banking"},
        ))
        return suggestions

    for invoice_key, supplier_key, label in comparisons:
        invoice_value = invoice.get(invoice_key)
        supplier_value = effective_supplier.get(supplier_key)
        if not has_value(invoice_value) or not has_value(supplier_value):
            continue
        if bank_values_match(invoice_key, invoice_value, supplier_value):
            continue
        is_required_field = supplier_key in {"bank_name", "bank_account_number", "bank_branch_code"}
        severity = "info" if cash_or_card else ("critical" if is_required_field else "warning")
        if True:
            suggestions.append(AgentSuggestion(
                category="banking",
                severity=severity,
                message=f"Invoice {label} differs from supplier master.",
                reason=f"Invoice value is {invoice_value}; supplier/branch value is {supplier_value}. Confirm before approval.",
                confidence=0.9 if is_required_field else 0.74,
                target={"tab": "supplier", "field": invoice_key, "section": "banking"},
            ))
    return suggestions


def _coding_suggestions(
    invoice: dict,
    supplier: Optional[dict],
    line_items: list[dict],
    tracking_dimensions: list[dict],
) -> list[AgentSuggestion]:
    suggestions: list[AgentSuggestion] = []
    default_account = (supplier or {}).get("default_expense_account") or invoice.get("expense_account")
    has_cost_centre = bool(tracking_dimensions)

    if not line_items:
        if not has_value(invoice.get("expense_account")) and has_value(default_account):
            suggestions.append(AgentSuggestion(
                category="account_coding",
                severity="info",
                message=f"Use default account {default_account} on the invoice.",
                reason="No line items are available, but the supplier or invoice has a default expense account.",
                confidence=0.7,
                apply_payload={
                    "type": "invoice_patch",
                    "fields": {"expense_account": default_account},
                },
                target={"tab": "line_items", "section": "coding"},
            ))
        elif not has_value(invoice.get("expense_account")):
            suggestions.append(AgentSuggestion(
                category="account_coding",
                severity="warning",
                message="No account coding is present.",
                reason="There are no saved line items and no invoice-level expense account to support review.",
                confidence=0.82,
                target={"tab": "line_items", "section": "coding"},
            ))
        return suggestions

    missing_account_count = 0
    missing_cost_centre_count = 0
    for index, item in enumerate(line_items):
        allocations = item.get("allocations") or []
        description = item.get("description") or f"line {index + 1}"
        if not has_value(item.get("expense_account")) and not allocations:
            missing_account_count += 1
            if has_value(default_account) and item.get("id"):
                suggestions.append(AgentSuggestion(
                    category="account_coding",
                    severity="info",
                    message=f"Code {description} to {default_account}.",
                    reason="The line has no account and the linked supplier/invoice default account can be applied safely.",
                    confidence=0.76,
                    apply_payload={
                        "type": "line_item_patch",
                        "line_item_id": item["id"],
                        "fields": {"expense_account": default_account},
                    },
                    target={"tab": "line_items", "line_item_id": item["id"], "field": "expense_account"},
                ))

        tracking = normalise_tracking(item.get("tracking"))
        allocations_have_tracking = any(normalise_tracking(allocation.get("tracking")) for allocation in allocations)
        if has_cost_centre and not tracking and not allocations_have_tracking:
            missing_cost_centre_count += 1

    if missing_account_count and not has_value(default_account):
        suggestions.append(AgentSuggestion(
            category="account_coding",
            severity="warning",
            message=f"{missing_account_count} line item(s) have no account code.",
            reason="Missing account coding prevents a clean journal preview and later GL posting.",
            confidence=0.86,
            target={"tab": "line_items", "field": "expense_account"},
        ))

    if missing_cost_centre_count:
        first_missing = next((
            item for item in line_items
            if item.get("id")
            and not normalise_tracking(item.get("tracking"))
            and not any(normalise_tracking(allocation.get("tracking")) for allocation in (item.get("allocations") or []))
        ), None)
        suggestions.append(AgentSuggestion(
            category="cost_centre",
            severity="warning",
            message=f"{missing_cost_centre_count} line item(s) have no cost centre or tracking split.",
            reason="Tracking dimensions exist for this organisation, so uncoded lines may be incomplete for management reporting.",
            confidence=0.78,
            target={
                "tab": "line_items",
                "line_item_id": first_missing.get("id") if first_missing else None,
                "field": "cost_centre",
            },
        ))

    return suggestions


def _allocation_suggestions(line_items: list[dict]) -> list[AgentSuggestion]:
    suggestions: list[AgentSuggestion] = []
    for index, item in enumerate(line_items or []):
        allocations = item.get("allocations") or []
        if not allocations:
            continue
        line_total = money(item.get("line_total") if item.get("line_total") is not None else item.get("amount"))
        allocation_total = round(sum(money(row.get("amount")) or 0 for row in allocations), 2)
        if line_total is None:
            suggestions.append(AgentSuggestion(
                category="allocation_splits",
                severity="critical",
                message=f"Split line {index + 1} has no line total.",
                reason="The allocations cannot be checked against the source line amount.",
                confidence=0.9,
                target={"tab": "line_items", "line_item_id": item.get("id"), "section": "split"},
            ))
        elif abs(allocation_total - abs(line_total)) > 0.02:
            suggestions.append(AgentSuggestion(
                category="allocation_splits",
                severity="critical",
                message=f"Split line {index + 1} does not balance.",
                reason=f"Allocation total is {allocation_total:.2f}; line total is {line_total:.2f}.",
                confidence=0.98,
                target={"tab": "line_items", "line_item_id": item.get("id"), "section": "split"},
            ))
    return suggestions


def _total_suggestions(invoice: dict, line_items: list[dict]) -> list[AgentSuggestion]:
    suggestions: list[AgentSuggestion] = []
    subtotal = money(invoice.get("subtotal"))
    tax = money(invoice.get("tax_amount")) or 0.0
    total = money(invoice.get("total_amount"))

    if subtotal is not None and total is not None:
        expected = round(subtotal + tax, 2)
        if abs(expected - total) > 0.02:
            suggestions.append(AgentSuggestion(
                category="totals",
                severity="warning",
                message="Subtotal plus VAT/tax does not match invoice total.",
                reason=f"Subtotal plus tax is {expected:.2f}; invoice total is {total:.2f}.",
                confidence=0.92,
                target={"tab": "extracted", "field": "total_amount", "section": "totals"},
            ))

    if line_items and subtotal is not None:
        line_total = round(sum(money(item.get("line_total") if item.get("line_total") is not None else item.get("amount")) or 0 for item in line_items), 2)
        if abs(line_total - subtotal) > 0.02:
            suggestions.append(AgentSuggestion(
                category="totals",
                severity="warning",
                message="Line item total does not match invoice subtotal.",
                reason=f"Saved line items total {line_total:.2f}; extracted subtotal is {subtotal:.2f}.",
                confidence=0.9,
                target={"tab": "line_items", "section": "totals"},
            ))

    return suggestions


def _reference_suggestions(invoice: dict, duplicate_count: int = 0) -> list[AgentSuggestion]:
    suggestions: list[AgentSuggestion] = []
    if not has_value(invoice.get("invoice_number")):
        suggestions.append(AgentSuggestion(
            category="reference_date",
            severity="warning",
            message="Document number is missing.",
            reason="A missing invoice, receipt, or credit-note number increases duplicate and audit risk.",
            confidence=0.82,
            target={"tab": "extracted", "field": "invoice_number"},
        ))
    if not has_value(invoice.get("invoice_date")):
        suggestions.append(AgentSuggestion(
            category="reference_date",
            severity="warning",
            message="Document date is missing.",
            reason="The invoice cannot be aged, matched, or period checked reliably without a date.",
            confidence=0.84,
            target={"tab": "extracted", "field": "invoice_date"},
        ))
    if duplicate_count > 0:
        suggestions.append(AgentSuggestion(
            category="duplicate_risk",
            severity="critical",
            message="Possible duplicate document reference found.",
            reason=f"{duplicate_count} other invoice(s) in this organisation share the same supplier/reference.",
            confidence=0.88,
            target={"tab": "extracted", "field": "invoice_number"},
        ))
    return suggestions


def _clean_readiness_suggestion(suggestions: list[AgentSuggestion]) -> list[AgentSuggestion]:
    blocking = [item for item in suggestions if item.severity in {"critical", "warning"}]
    if blocking:
        return []
    return [AgentSuggestion(
        category="review_readiness",
        severity="info",
        message="No blocking review issues found.",
        reason="Supplier, totals, banking, coding, and allocations did not trigger any high-risk rule in this review pass.",
        confidence=0.68,
    )]


def generate_invoice_agent_suggestions(
    *,
    invoice: dict,
    supplier: Optional[dict] = None,
    supplier_branch: Optional[dict] = None,
    supplier_branches: Optional[list[dict]] = None,
    line_items: Optional[list[dict]] = None,
    accounts: Optional[list[dict]] = None,
    tracking_dimensions: Optional[list[dict]] = None,
    tracking_values: Optional[list[dict]] = None,
    audit_events: Optional[list[dict]] = None,
    parse_attempts: Optional[list[dict]] = None,
    duplicate_count: int = 0,
) -> list[dict]:
    """
    Deterministic v1 invoice review assistant.

    It only produces suggestions. Any apply_payload is intentionally narrow and
    must still be applied by an explicit reviewer action through the API.
    """
    _ = accounts, tracking_values, audit_events, parse_attempts
    source_line_items = line_items or []
    source_tracking_dimensions = tracking_dimensions or []
    effective_branch = supplier_branch or find_branch_match(invoice, supplier_branches or [])
    suggestions: list[AgentSuggestion] = []
    suggestions.extend(_supplier_identity_suggestions(invoice, supplier, effective_branch, supplier_branches or []))
    suggestions.extend(_supplier_master_update_suggestions(invoice, supplier))
    suggestions.extend(_banking_suggestions(invoice, supplier, effective_branch))
    suggestions.extend(_coding_suggestions(invoice, supplier, source_line_items, source_tracking_dimensions))
    suggestions.extend(_allocation_suggestions(source_line_items))
    suggestions.extend(_total_suggestions(invoice, source_line_items))
    suggestions.extend(_reference_suggestions(invoice, duplicate_count=duplicate_count))
    suggestions.extend(_clean_readiness_suggestion(suggestions))

    by_fingerprint: dict[str, dict] = {}
    for suggestion in suggestions:
        payload = suggestion.as_dict()
        by_fingerprint[payload["fingerprint"]] = payload
    return list(by_fingerprint.values())


def filter_safe_apply_payload(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    action_type = payload.get("type")
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        return None

    if action_type == "invoice_patch":
        safe = {key: value for key, value in fields.items() if key in SAFE_INVOICE_FIELDS}
        return {"type": action_type, "fields": safe} if safe else None
    if action_type == "line_item_patch":
        line_item_id = payload.get("line_item_id")
        safe = {key: value for key, value in fields.items() if key in SAFE_LINE_ITEM_FIELDS}
        if line_item_id and safe:
            return {"type": action_type, "line_item_id": line_item_id, "fields": safe}
    if action_type == "supplier_patch":
        supplier_id = payload.get("supplier_id")
        safe = {key: value for key, value in fields.items() if key in SAFE_SUPPLIER_FIELDS}
        if supplier_id and safe:
            return {"type": action_type, "supplier_id": supplier_id, "fields": safe}
    return None
