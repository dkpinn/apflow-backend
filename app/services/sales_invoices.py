from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from app.services.organisation_module_settings import (
    missing_tracking_dimensions,
    required_tracking_dimensions,
)


MONEY = Decimal("0.01")
QUANTITY = Decimal("0.0001")
STANDARD_VAT_RATE = Decimal("15")


def decimal_value(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    return Decimal(str(value))


def money(value: Any) -> Decimal:
    return decimal_value(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def calculate_sales_line(line: dict[str, Any]) -> dict[str, Any]:
    quantity = decimal_value(line.get("quantity"), "1").quantize(QUANTITY)
    unit_price = decimal_value(line.get("unit_price"))
    if quantity <= 0:
        raise ValueError("Line quantity must be greater than zero")
    if unit_price < 0:
        raise ValueError("Line unit price cannot be negative")

    extended = quantity * unit_price
    fixed_discount = decimal_value(line.get("discount_amount"))
    discount_percent = decimal_value(line.get("discount_percent"))
    if fixed_discount < 0:
        raise ValueError("Line discount cannot be negative")
    if discount_percent < 0 or discount_percent > 100:
        raise ValueError("Line discount percentage must be between 0 and 100")

    discount = (
        fixed_discount
        if fixed_discount > 0
        else extended * discount_percent / Decimal("100")
    )
    discount = min(discount, extended)
    discounted = extended - discount

    treatment = str(line.get("vat_treatment") or "standard")
    if treatment not in {"standard", "zero_rated", "exempt"}:
        raise ValueError("Unsupported VAT treatment")
    rate = decimal_value(
        line.get("vat_rate"),
        str(STANDARD_VAT_RATE if treatment == "standard" else 0),
    )
    if treatment != "standard":
        rate = Decimal("0")
    if rate < 0 or rate > 100:
        raise ValueError("VAT rate must be between 0 and 100")

    if bool(line.get("prices_include_vat")) and rate > 0:
        gross_amount = money(discounted)
        net_amount = money(discounted / (Decimal("1") + rate / Decimal("100")))
        tax_amount = money(gross_amount - net_amount)
    else:
        net_amount = money(discounted)
        tax_amount = money(net_amount * rate / Decimal("100"))
        gross_amount = money(net_amount + tax_amount)

    source_unit_cost = line.get("source_unit_cost")
    source_cost_total = (
        money(decimal_value(source_unit_cost) * quantity)
        if source_unit_cost not in (None, "")
        else None
    )
    margin_amount = (
        money(net_amount - source_cost_total)
        if source_cost_total is not None
        else None
    )

    return {
        **line,
        "quantity": float(quantity),
        "unit_price": float(unit_price.quantize(QUANTITY)),
        "discount_percent": float(discount_percent),
        "discount_amount": float(money(discount)),
        "vat_treatment": treatment,
        "vat_rate": float(rate),
        "net_amount": float(net_amount),
        "tax_amount": float(tax_amount),
        "gross_amount": float(gross_amount),
        "source_cost_total": float(source_cost_total) if source_cost_total is not None else None,
        "margin_amount": float(margin_amount) if margin_amount is not None else None,
    }


def calculate_sales_invoice(lines: Iterable[dict[str, Any]]) -> dict[str, Any]:
    calculated = [calculate_sales_line(line) for line in lines]
    if not calculated:
        raise ValueError("Add at least one invoice line")
    subtotal = money(sum(decimal_value(line["net_amount"]) for line in calculated))
    tax_total = money(sum(decimal_value(line["tax_amount"]) for line in calculated))
    total = money(sum(decimal_value(line["gross_amount"]) for line in calculated))
    discount_total = money(
        sum(decimal_value(line["discount_amount"]) for line in calculated)
    )
    if abs(total - (subtotal + tax_total)) > Decimal("0.02"):
        raise ValueError("Invoice totals do not balance")
    return {
        "lines": calculated,
        "subtotal": float(subtotal),
        "discount_total": float(discount_total),
        "tax_total": float(tax_total),
        "total_amount": float(total),
    }


def validate_customer_line_tracking(
    db,
    *,
    organisation_id: str,
    lines: Iterable[dict[str, Any]],
) -> None:
    required = required_tracking_dimensions(
        db,
        organisation_id=organisation_id,
        module_key="customer",
    )
    failures: list[str] = []
    for index, line in enumerate(lines):
        missing = missing_tracking_dimensions(line.get("tracking") or {}, required)
        if missing:
            names = ", ".join(str(item.get("name") or item.get("id")) for item in missing)
            failures.append(f"{line.get('description') or f'Line {index + 1}'}: {names}")
    if failures:
        raise ValueError(
            "Customer posting requires tracking on every revenue line. Missing "
            + "; ".join(failures[:8])
        )


def default_due_date(
    *,
    issue_date: date,
    customer_terms_days: int | None,
    organisation_terms_days: int | None,
) -> date:
    days = (
        customer_terms_days
        if customer_terms_days is not None
        else organisation_terms_days
        if organisation_terms_days is not None
        else 30
    )
    return issue_date + timedelta(days=max(int(days), 0))


def build_rebill_lines(
    source_lines: Iterable[dict[str, Any]],
    *,
    default_revenue_account_id: str,
    markup_percent: float = 0,
) -> list[dict[str, Any]]:
    markup = decimal_value(markup_percent)
    if markup < Decimal("-100"):
        raise ValueError("Markup percentage cannot be less than -100")

    result: list[dict[str, Any]] = []
    for index, source in enumerate(source_lines):
        quantity = decimal_value(source.get("quantity"), "1")
        if quantity <= 0:
            quantity = Decimal("1")
        line_total = decimal_value(source.get("line_total"))
        source_unit_cost = (
            decimal_value(source.get("unit_price"))
            if source.get("unit_price") not in (None, "")
            else line_total / quantity
        )
        selling_price = source_unit_cost * (Decimal("1") + markup / Decimal("100"))
        result.append(
            calculate_sales_line(
                {
                    "description": source.get("description") or "Rebilled cost",
                    "item_code": source.get("code"),
                    "quantity": quantity,
                    "unit_price": selling_price,
                    "prices_include_vat": False,
                    "discount_percent": 0,
                    "discount_amount": 0,
                    "vat_treatment": "standard",
                    "vat_rate": STANDARD_VAT_RATE,
                    "revenue_account_id": default_revenue_account_id,
                    "tracking": {},
                    "source_invoice_extracted_id": source.get("invoice_extracted_id"),
                    "source_invoice_line_id": source.get("id"),
                    "source_unit_cost": source_unit_cost,
                    "markup_percent": markup,
                    "sort_order": index,
                }
            )
        )
    return result


def rpc_object(result: Any) -> dict[str, Any]:
    data = getattr(result, "data", result)
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def issue_sales_invoice(
    db,
    *,
    organisation_id: str,
    sales_invoice_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    result = db.rpc(
        "issue_sales_invoice_atomic",
        {
            "p_org_id": organisation_id,
            "p_sales_invoice_id": sales_invoice_id,
            "p_actor_user_id": actor_user_id,
        },
    ).execute()
    return rpc_object(result)


def post_customer_receipt(
    db,
    *,
    organisation_id: str,
    customer_id: str,
    bank_account_id: str,
    receipt_date: str,
    amount: float,
    currency: str,
    reference: str | None,
    notes: str | None,
    allocations: list[dict[str, Any]],
    actor_user_id: str,
    bank_statement_line_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    result = db.rpc(
        "post_customer_receipt_atomic",
        {
            "p_org_id": organisation_id,
            "p_customer_id": customer_id,
            "p_bank_account_id": bank_account_id,
            "p_receipt_date": receipt_date,
            "p_amount": amount,
            "p_currency": currency,
            "p_reference": reference,
            "p_notes": notes,
            "p_allocations": allocations,
            "p_bank_statement_line_id": bank_statement_line_id,
            "p_idempotency_key": idempotency_key,
            "p_actor_user_id": actor_user_id,
        },
    ).execute()
    return rpc_object(result)

