"""Finance lease amortization calculator — Effective Interest Method.

All monetary amounts use Decimal to avoid float precision drift across long
schedules. The last period absorbs any rounding residual so the closing
balance is always exactly zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from dateutil.relativedelta import relativedelta

CENT = Decimal("0.01")
MICRO = Decimal("0.000001")


# ── Period helpers ─────────────────────────────────────────────────────────────

_PERIODS_PER_YEAR: dict[str, int] = {
    "monthly":    12,
    "quarterly":  4,
    "semi_annual": 2,
    "annual":     1,
}

_PERIOD_DELTA: dict[str, relativedelta] = {
    "monthly":    relativedelta(months=1),
    "quarterly":  relativedelta(months=3),
    "semi_annual": relativedelta(months=6),
    "annual":     relativedelta(years=1),
}


def periods_per_year(frequency: str) -> int:
    try:
        return _PERIODS_PER_YEAR[frequency]
    except KeyError:
        raise ValueError(f"Unknown payment frequency: {frequency!r}")


def period_date(commencement_date: date, frequency: str, n: int) -> date:
    """Return the date of the nth payment (1-based) from commencement_date."""
    delta = _PERIOD_DELTA[frequency]
    d = commencement_date
    for _ in range(n):
        d = d + delta
    return d


# ── Schedule row ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScheduleRow:
    period_number:   int
    period_date:     date
    opening_balance: Decimal
    payment_amount:  Decimal
    interest_amount: Decimal
    principal_amount: Decimal
    closing_balance: Decimal

    def as_dict(self) -> dict:
        return {
            "period_number":   self.period_number,
            "period_date":     self.period_date.isoformat(),
            "opening_balance":  str(self.opening_balance),
            "payment_amount":   str(self.payment_amount),
            "interest_amount":  str(self.interest_amount),
            "principal_amount": str(self.principal_amount),
            "closing_balance":  str(self.closing_balance),
        }


# ── Core amortization builder ──────────────────────────────────────────────────

def build_amortization_schedule(
    *,
    initial_liability: Decimal,
    payment_amount: Decimal,
    annual_rate: Decimal,
    frequency: str,
    commencement_date: date,
    end_date: date,
) -> list[ScheduleRow]:
    """Build a complete amortization schedule using the Effective Interest Method.

    The last period's payment is adjusted to close the balance exactly to zero,
    absorbing any accumulated rounding residual.

    Args:
        initial_liability: PV of future payments at inception (Decimal).
        payment_amount:    Regular instalment (Decimal).
        annual_rate:       Annual interest rate as a percentage, e.g. Decimal("12.5").
        frequency:         One of 'monthly', 'quarterly', 'semi_annual', 'annual'.
        commencement_date: Lease start date.
        end_date:          Lease expiry date (last payment on or before this date).

    Returns:
        List of ScheduleRow in period order, closing balance of last row == 0.
    """
    ppy = Decimal(str(periods_per_year(frequency)))
    period_rate = (annual_rate / Decimal("100") / ppy).quantize(MICRO, rounding=ROUND_HALF_UP)

    rows: list[ScheduleRow] = []
    balance = initial_liability.quantize(CENT)
    period = 1

    while True:
        pd = period_date(commencement_date, frequency, period)
        if pd > end_date:
            break
        if balance <= Decimal("0"):
            break

        interest = (balance * period_rate).quantize(CENT, rounding=ROUND_HALF_UP)

        # Detect last period
        next_pd = period_date(commencement_date, frequency, period + 1)
        is_last = next_pd > end_date or balance - (payment_amount - interest) <= Decimal("0")

        if is_last:
            principal = balance
            actual_payment = (principal + interest).quantize(CENT, rounding=ROUND_HALF_UP)
            closing = Decimal("0")
        else:
            principal = (payment_amount - interest).quantize(CENT, rounding=ROUND_HALF_UP)
            actual_payment = payment_amount.quantize(CENT)
            # Guard: principal cannot exceed remaining balance
            if principal >= balance:
                principal = balance
                actual_payment = (principal + interest).quantize(CENT, rounding=ROUND_HALF_UP)
                closing = Decimal("0")
                is_last = True
            else:
                closing = (balance - principal).quantize(CENT)

        rows.append(ScheduleRow(
            period_number=period,
            period_date=pd,
            opening_balance=balance,
            payment_amount=actual_payment,
            interest_amount=interest,
            principal_amount=principal,
            closing_balance=closing,
        ))

        balance = closing
        period += 1

        if is_last:
            break

    return rows


def validate_schedule_balances(rows: list[ScheduleRow]) -> None:
    """Raise ValueError if the schedule does not close to zero."""
    if not rows:
        raise ValueError("Empty amortization schedule")
    final = rows[-1].closing_balance
    if abs(final) > Decimal("0.05"):
        raise ValueError(
            f"Amortization schedule does not reduce to zero — "
            f"final closing balance: {final}"
        )


# ── Present value helper ───────────────────────────────────────────────────────

def compute_present_value(
    *,
    payment_amount: Decimal,
    annual_rate: Decimal,
    frequency: str,
    num_periods: int,
) -> Decimal:
    """PV of an ordinary annuity: PMT × [1 − (1+r)^−n] / r.

    Used to compute initial_liability when not provided explicitly by the user.
    """
    if num_periods <= 0:
        raise ValueError("num_periods must be positive")
    ppy = periods_per_year(frequency)
    r = float(annual_rate) / 100.0 / ppy
    pmt = float(payment_amount)
    if r == 0:
        pv = pmt * num_periods
    else:
        pv = pmt * (1.0 - math.pow(1.0 + r, -num_periods)) / r
    return Decimal(str(round(pv, 2)))


def estimate_num_periods(
    *,
    commencement_date: date,
    end_date: date,
    frequency: str,
) -> int:
    """Count how many payment periods fall on or before end_date."""
    count = 0
    while True:
        count += 1
        if period_date(commencement_date, frequency, count + 1) > end_date:
            break
    return count
