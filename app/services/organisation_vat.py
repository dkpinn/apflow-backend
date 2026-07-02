from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class VatApplicability:
    registered: bool
    applicable: bool
    registration_date: str | None
    transaction_date: str
    reason: str | None = None


def parse_vat_date(value: Any, *, field: str = "transaction_date") -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD format") from exc


def _organisation_vat_row(db, *, organisation_id: str) -> dict[str, Any]:
    rows = (
        db.table("organisations")
        .select("id, vat_registered, vat_registration_date")
        .eq("id", organisation_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        raise ValueError("Organisation not found")
    return rows[0]


def vat_applicability(
    db,
    *,
    organisation_id: str,
    transaction_date: Any,
    action: str = "Use VAT",
) -> VatApplicability:
    tx_date = parse_vat_date(transaction_date)
    row = _organisation_vat_row(db, organisation_id=organisation_id)
    registered = row.get("vat_registered") is True
    registration_raw = row.get("vat_registration_date")
    if not registered:
        return VatApplicability(
            registered=False,
            applicable=False,
            registration_date=None,
            transaction_date=tx_date.isoformat(),
            reason="organisation_not_vat_registered",
        )
    if not registration_raw:
        raise ValueError(f"{action} requires a VAT registration date on the organisation profile")
    registration_date = parse_vat_date(registration_raw, field="vat_registration_date")
    if tx_date < registration_date:
        return VatApplicability(
            registered=True,
            applicable=False,
            registration_date=registration_date.isoformat(),
            transaction_date=tx_date.isoformat(),
            reason="before_vat_registration_date",
        )
    return VatApplicability(
        registered=True,
        applicable=True,
        registration_date=registration_date.isoformat(),
        transaction_date=tx_date.isoformat(),
    )


def vat_report_effective_period(
    db,
    *,
    organisation_id: str,
    date_from: str,
    date_to: str,
) -> tuple[str, str | None]:
    start = parse_vat_date(date_from, field="date_from")
    end = parse_vat_date(date_to, field="date_to")
    if start > end:
        raise ValueError("From date must be on or before To date")

    row = _organisation_vat_row(db, organisation_id=organisation_id)
    if row.get("vat_registered") is not True:
        raise ValueError("VAT report is unavailable because this organisation is not VAT registered")
    registration_raw = row.get("vat_registration_date")
    if not registration_raw:
        raise ValueError("VAT report requires a VAT registration date on the organisation profile")
    registration_date = parse_vat_date(registration_raw, field="vat_registration_date")
    if end < registration_date:
        raise ValueError(f"VAT report is unavailable before VAT registration date {registration_date.isoformat()}")
    if start < registration_date:
        return registration_date.isoformat(), registration_date.isoformat()
    return start.isoformat(), None
