"""Pure line-builder functions for finance lease GL journals.

Each function returns a list of journal line dicts ready to pass to the
Supabase RPC or direct insert into gl_journal_lines.  No DB calls here.
"""

from __future__ import annotations

from decimal import Decimal


def _f(v) -> float:
    """Convert Decimal/str/float to plain float for JSON serialisation."""
    return float(Decimal(str(v)))


# ── Inception ──────────────────────────────────────────────────────────────────

def build_inception_lines(
    lease: dict,
    *,
    doc_fee_bank_account_id: str | None = None,
) -> list[dict]:
    """IFRS 16 inception journal lines.

    Standard treatment (no bank-funded doc fees):
        Dr  ROU Asset              = rou_asset_cost
        Cr  Lease Liability LT     = initial_liability
        Cr  [Payable / contra]     = documentation_fees   (if doc_fees > 0 and no bank)

    If doc_fee_bank_account_id is given, the documentation fees are funded
    immediately from bank:
        Dr  ROU Asset              = rou_asset_cost
        Cr  Lease Liability LT     = initial_liability
        Cr  Bank                   = documentation_fees

    If documentation_fees == 0, the journal is always two lines.
    """
    rou_cost    = _f(lease["rou_asset_cost"])
    liability   = _f(lease["initial_liability"])
    doc_fees    = _f(lease.get("documentation_fees", 0))
    description = (
        f"Finance Lease Inception – {lease['lessor_name']} / {lease['asset_description']}"
    )

    lines: list[dict] = [
        {
            "account_id":    lease["rou_asset_account_id"],
            "description":   f"{description} – ROU Asset at cost",
            "debit_amount":  rou_cost,
            "credit_amount": 0.0,
            "sort_order":    0,
        },
        {
            "account_id":    lease["liability_lt_account_id"],
            "description":   f"{description} – Lease Liability (LT)",
            "debit_amount":  0.0,
            "credit_amount": liability,
            "sort_order":    1,
        },
    ]

    if doc_fees > 0:
        credit_account = doc_fee_bank_account_id or lease.get("liability_lt_account_id")
        lines.append({
            "account_id":    credit_account,
            "description":   f"{description} – Documentation fees",
            "debit_amount":  0.0,
            "credit_amount": doc_fees,
            "sort_order":    2,
        })

    return lines


# ── Monthly payment ────────────────────────────────────────────────────────────

def build_payment_lines(
    lease: dict,
    schedule_row: dict,
    bank_account_id: str,
    *,
    description: str | None = None,
) -> list[dict]:
    """Monthly payment journal lines.

        Dr  Lease Liability LT   = principal_amount
        Dr  Interest Expense     = interest_amount
        Cr  Bank                 = payment_amount  (principal + interest)
    """
    principal   = _f(schedule_row["principal_amount"])
    interest    = _f(schedule_row["interest_amount"])
    payment     = _f(schedule_row["payment_amount"])
    period      = schedule_row["period_number"]
    desc = description or (
        f"Finance Lease Payment – Period {period} – {lease['asset_description']}"
    )

    lines: list[dict] = [
        {
            "account_id":    lease["liability_lt_account_id"],
            "description":   f"{desc} – Principal repayment",
            "debit_amount":  principal,
            "credit_amount": 0.0,
            "sort_order":    0,
        },
        {
            "account_id":    lease["interest_expense_account_id"],
            "description":   f"{desc} – Finance charge",
            "debit_amount":  interest,
            "credit_amount": 0.0,
            "sort_order":    1,
        },
        {
            "account_id":    bank_account_id,
            "description":   desc,
            "debit_amount":  0.0,
            "credit_amount": payment,
            "sort_order":    2,
        },
    ]

    # Handle additional debits / credits from schedule row
    additional_debit  = _f(schedule_row.get("additional_debit",  0))
    additional_credit = _f(schedule_row.get("additional_credit", 0))

    if additional_debit > 0:
        lines.append({
            "account_id":    bank_account_id,
            "description":   f"{desc} – Additional debit",
            "debit_amount":  additional_debit,
            "credit_amount": 0.0,
            "sort_order":    10,
        })
    if additional_credit > 0:
        lines.append({
            "account_id":    bank_account_id,
            "description":   f"{desc} – Additional credit",
            "debit_amount":  0.0,
            "credit_amount": additional_credit,
            "sort_order":    11,
        })

    return lines


# ── Depreciation ───────────────────────────────────────────────────────────────

def compute_monthly_depreciation(lease: dict) -> float:
    """Straight-line depreciation: rou_asset_cost / lease_term_months."""
    cost   = _f(lease["rou_asset_cost"])
    months = int(lease["lease_term_months"])
    if months <= 0:
        raise ValueError("lease_term_months must be positive")
    # Round to 2dp; last period via the caller can adjust if needed
    return round(cost / months, 2)


def build_depreciation_lines(
    lease: dict,
    *,
    monthly_depreciation: float | None = None,
    description: str | None = None,
) -> list[dict]:
    """Straight-line depreciation journal lines.

        Dr  Depreciation Expense     = monthly_depreciation
        Cr  Accumulated Depreciation = monthly_depreciation
    """
    dep_amount = monthly_depreciation if monthly_depreciation is not None \
        else compute_monthly_depreciation(lease)
    desc = description or (
        f"ROU Asset Depreciation – {lease['asset_description']}"
    )

    return [
        {
            "account_id":    lease["depreciation_expense_account_id"],
            "description":   desc,
            "debit_amount":  dep_amount,
            "credit_amount": 0.0,
            "sort_order":    0,
        },
        {
            "account_id":    lease["accum_depreciation_account_id"],
            "description":   desc,
            "debit_amount":  0.0,
            "credit_amount": dep_amount,
            "sort_order":    1,
        },
    ]


# ── Journal balance assertion (used before direct inserts) ────────────────────

def assert_balanced(lines: list[dict]) -> None:
    """Raise ValueError if debit total ≠ credit total."""
    dr = round(sum(float(l.get("debit_amount",  0)) for l in lines), 2)
    cr = round(sum(float(l.get("credit_amount", 0)) for l in lines), 2)
    if dr != cr:
        raise ValueError(f"Journal does not balance: Dr {dr} ≠ Cr {cr}")
