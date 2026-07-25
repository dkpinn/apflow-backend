from __future__ import annotations

import math
import logging
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from dateutil.relativedelta import relativedelta

from app.services.aged_payables import generate_aged_payables

MONEY = Decimal("0.01")
ZERO = Decimal("0.00")
logger = logging.getLogger(__name__)


class CashFlowForecastDataError(RuntimeError):
    def __init__(self, source: str):
        self.source = source
        super().__init__(f"Required cash-flow forecast source is unavailable: {source}")

OUTFLOW_TYPES = {
    "supplier_invoice",
    "bank_standing_order",
    "finance_repayment",
    "operational_repayment",
    "journal_entry",
}
INFLOW_TYPES = {"sales_invoice"}


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except Exception:
        return ZERO


def _f(value: Decimal) -> float:
    return float(value)


def _fmt_week(start: date, end: date) -> str:
    months = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    s = f"{start.day} {months[start.month - 1]}"
    e = f"{end.day} {months[end.month - 1]}"
    return f"{s} – {e}"


def _next_occurrence(template: dict, from_date: date) -> date | None:
    """Return the next occurrence of a recurring template on or after from_date."""
    next_due_str = template.get("next_due_date")
    if not next_due_str:
        return None
    try:
        next_due = date.fromisoformat(str(next_due_str))
    except ValueError:
        return None
    # If next_due is already >= from_date, use it directly
    if next_due >= from_date:
        return next_due
    # Advance until we reach from_date
    schedule = template.get("schedule_type", "monthly")
    day = template.get("schedule_day") or next_due.day
    candidate = next_due
    while candidate < from_date:
        candidate = _advance(candidate, schedule, day)
    return candidate


def _advance(current: date, schedule: str, day: int | None) -> date:
    day = day or current.day
    if schedule == "weekly":
        return current + timedelta(weeks=1)
    if schedule == "monthly":
        target = current + relativedelta(months=1)
        return target.replace(day=min(day, monthrange(target.year, target.month)[1]))
    if schedule == "quarterly":
        target = current + relativedelta(months=3)
        return target.replace(day=min(day, monthrange(target.year, target.month)[1]))
    if schedule == "annually":
        target = current + relativedelta(years=1)
        return target.replace(day=min(day, monthrange(target.year, target.month)[1]))
    return current + timedelta(weeks=1)


def generate_cash_flow_forecast(
    db,
    organisation_id: str,
    as_at_date: str,
    forecast_days: int = 90,
) -> dict:
    try:
        start = date.fromisoformat(as_at_date)
    except ValueError:
        raise ValueError("as_at_date must be YYYY-MM-DD")

    end = start + timedelta(days=forecast_days)
    n_weeks = math.ceil(forecast_days / 7)

    warnings: list[dict[str, str]] = []
    is_partial = False

    # Build weekly buckets
    weeks: list[dict] = []
    for i in range(n_weeks):
        w_start = start + timedelta(weeks=i)
        w_end = min(w_start + timedelta(days=6), end)
        weeks.append({
            "week_label": _fmt_week(w_start, w_end),
            "week_start": w_start.isoformat(),
            "week_end": w_end.isoformat(),
            "inflows": ZERO,
            "outflows": ZERO,
            "items": [],
        })

    def _bucket_index(d: date) -> int | None:
        if d < start or d > end:
            return None
        return min(int((d - start).days // 7), n_weeks - 1)

    def _add_item(d: date, direction: str, item_type: str, description: str, amount: Decimal) -> None:
        idx = _bucket_index(d)
        if idx is None:
            return
        w = weeks[idx]
        if direction == "in":
            w["inflows"] += amount
        else:
            w["outflows"] += amount
        w["items"].append({
            "date": d.isoformat(),
            "type": item_type,
            "direction": direction,
            "description": description,
            "amount": _f(amount),
        })

    # ── Opening balance ────────────────────────────────────────────────────
    opening_balance = ZERO
    try:
        accounts = (
            db.table("bank_accounts")
            .select("current_reconciled_balance")
            .eq("organisation_id", organisation_id)
            .eq("active", True)
            .execute()
            .data or []
        )
        for acc in accounts:
            opening_balance += _d(acc.get("current_reconciled_balance"))
    except Exception as exc:
        logger.exception("Failed to load bank balances for cash-flow forecast")
        raise CashFlowForecastDataError("bank_balances") from exc

    # ── Payables due (AP invoices) ─────────────────────────────────────────
    # Use the same as-at reconciliation logic as the Aged Payables report so
    # partially and fully settled invoices are not forecast at their gross total.
    try:
        aged_payables = generate_aged_payables(
            db,
            organisation_id=organisation_id,
            as_at_date=start.isoformat(),
        )
        warnings.extend(aged_payables.get("warnings") or [])
        for supplier_group in aged_payables.get("suppliers", []):
            supplier = supplier_group.get("supplier_name") or "Supplier"
            for row in supplier_group.get("invoices", []):
                try:
                    due = date.fromisoformat(str(row["due_date"]))
                except (ValueError, TypeError):
                    continue
                if due < start or due > end:
                    continue
                amt = _d(row.get("outstanding_amount"))
                if amt <= ZERO:
                    continue
                inv_no = row.get("invoice_number") or ""
                desc = f"{supplier} – {inv_no}".strip(" –") if inv_no else supplier
                _add_item(due, "out", "payable", desc, amt)
    except CashFlowForecastDataError:
        raise
    except Exception as exc:
        logger.exception("Failed to load supplier payables for cash-flow forecast")
        raise CashFlowForecastDataError("supplier_payables") from exc

    # ── Receivables due (AR invoices) ──────────────────────────────────────
    try:
        receivables = (
            db.table("sales_invoices")
            .select("due_date, amount_outstanding, invoice_number, customer_id")
            .eq("organisation_id", organisation_id)
            .gt("amount_outstanding", 0)
            .gte("due_date", start.isoformat())
            .lte("due_date", end.isoformat())
            .execute()
            .data or []
        )
        for row in receivables:
            try:
                due = date.fromisoformat(str(row["due_date"]))
            except (ValueError, TypeError):
                continue
            amt = _d(row.get("amount_outstanding"))
            if amt <= ZERO:
                continue
            inv_no = row.get("invoice_number") or ""
            desc = f"Invoice {inv_no}".strip() if inv_no else "Sales invoice due"
            _add_item(due, "in", "receivable", desc, amt)
    except Exception as exc:
        logger.exception("Failed to load customer receivables for cash-flow forecast")
        raise CashFlowForecastDataError("customer_receivables") from exc

    # ── Recurring transactions ─────────────────────────────────────────────
    try:
        templates = (
            db.table("recurring_transaction_templates")
            .select("*")
            .eq("organisation_id", organisation_id)
            .eq("status", "active")
            .execute()
            .data or []
        )
        for tpl in templates:
            transaction_type = tpl.get("transaction_type", "")
            if transaction_type in OUTFLOW_TYPES:
                direction = "out"
                item_type = "recurring"
            elif transaction_type in INFLOW_TYPES:
                direction = "in"
                item_type = "recurring"
            else:
                continue

            amt = _d(tpl.get("amount"))
            if amt <= ZERO:
                continue

            name = tpl.get("name") or transaction_type.replace("_", " ").title()
            schedule = tpl.get("schedule_type", "monthly")
            day = tpl.get("schedule_day")

            # Project all occurrences in the forecast window
            end_date_str = tpl.get("end_date")
            tpl_end = date.fromisoformat(str(end_date_str)) if end_date_str else None

            candidate = _next_occurrence(tpl, start)
            if candidate is None:
                continue

            safety = 0
            while candidate <= end and safety < 60:
                safety += 1
                if tpl_end and candidate > tpl_end:
                    break
                _add_item(candidate, direction, item_type, name, amt)
                candidate = _advance(candidate, schedule, day)
    except Exception:
        logger.exception("Failed to load recurring transactions for cash-flow forecast")
        is_partial = True
        warnings.append({
            "code": "recurring_transactions_unavailable",
            "message": "Recurring transactions could not be loaded and are excluded from this forecast.",
        })

    # ── Build running balance ──────────────────────────────────────────────
    running = opening_balance
    total_inflows = ZERO
    total_outflows = ZERO
    result_weeks = []
    for w in weeks:
        inf = w["inflows"]
        out = w["outflows"]
        net = inf - out
        running += net
        total_inflows += inf
        total_outflows += out
        result_weeks.append({
            "week_label": w["week_label"],
            "week_start": w["week_start"],
            "week_end": w["week_end"],
            "inflows": _f(inf),
            "outflows": _f(out),
            "net": _f(net),
            "closing_balance": _f(running),
            "items": sorted(w["items"], key=lambda x: x["date"]),
        })

    return {
        "as_at_date": as_at_date,
        "forecast_days": forecast_days,
        "opening_balance": _f(opening_balance),
        "weeks": result_weeks,
        "summary": {
            "opening_balance": _f(opening_balance),
            "total_inflows": _f(total_inflows),
            "total_outflows": _f(total_outflows),
            "closing_balance": _f(running),
        },
        "is_partial": is_partial,
        "warnings": warnings,
        "disclaimer": (
            "Forecast is indicative only. Payables use outstanding balances after matched payments "
            "dated on or before the forecast start date. Receivables use outstanding balance. "
            "Recurring amounts are projected from templates. Actual timing may vary."
        ),
    }
