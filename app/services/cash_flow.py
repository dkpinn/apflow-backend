from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.services.money import money


ZERO = Decimal("0.00")
MONEY = Decimal("0.01")
CHUNK_SIZE = 200

CASH_BANK_ACCOUNT_TYPES = {
    "bank",
    "cash",
    "call_account",
    "money_market",
    "paypal",
    "paygate",
    "foreign_bank",
}

OPERATING_SOURCE_TYPES = {
    "bank_transaction",
    "customer_receipt",
    "invoice",
    "manual",
}


def amount_out(value: Decimal) -> float:
    return float(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def _fetch_rows(query) -> list[dict]:
    result = query.execute()
    return list(result.data or [])


def _validate_dates(date_from: str, date_to: str) -> tuple[date, date]:
    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except ValueError as exc:
        raise ValueError("Dates must use YYYY-MM-DD format") from exc
    if start > end:
        raise ValueError("From date must be on or before To date")
    return start, end


def _chunked(items: list, size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _fetch_cash_accounts(db, organisation_id: str) -> list[dict]:
    bank_rows = _fetch_rows(
        db.table("bank_accounts")
        .select("id, organisation_id, name, account_type, gl_account_id, active")
        .eq("organisation_id", organisation_id)
        .eq("active", True)
    )
    linked_gl_ids = sorted(
        {
            str(row.get("gl_account_id"))
            for row in bank_rows
            if row.get("gl_account_id")
            and str(row.get("account_type") or "").lower() in CASH_BANK_ACCOUNT_TYPES
        }
    )
    if not linked_gl_ids:
        return []

    accounts: list[dict] = []
    for chunk in _chunked(linked_gl_ids, CHUNK_SIZE):
        accounts.extend(
            _fetch_rows(
                db.table("accounts")
                .select("id, code, name, type, group_name, active")
                .eq("organisation_id", organisation_id)
                .in_("id", chunk)
            )
        )

    bank_by_gl = {str(row.get("gl_account_id")): row for row in bank_rows if row.get("gl_account_id")}
    cash_accounts = []
    for account in accounts:
        bank = bank_by_gl.get(str(account.get("id"))) or {}
        cash_accounts.append({
            **account,
            "bank_account_id": bank.get("id"),
            "bank_account_name": bank.get("name"),
            "bank_account_type": bank.get("account_type"),
        })
    return cash_accounts


def _fetch_posted_journals(db, organisation_id: str, date_from: str, date_to: str) -> list[dict]:
    return _fetch_rows(
        db.table("gl_journals")
        .select("id, journal_date, description, source_type, source_id, created_at")
        .eq("organisation_id", organisation_id)
        .eq("status", "posted")
        .gte("journal_date", date_from)
        .lte("journal_date", date_to)
        .order("journal_date")
        .order("created_at")
    )


def _fetch_journal_lines(db, organisation_id: str, journal_ids: list[str]) -> list[dict]:
    if not journal_ids:
        return []
    lines: list[dict] = []
    for chunk in _chunked(journal_ids, CHUNK_SIZE):
        lines.extend(
            _fetch_rows(
                db.table("gl_journal_lines")
                .select("id, gl_journal_id, account_id, description, debit_amount, credit_amount, sort_order")
                .eq("organisation_id", organisation_id)
                .in_("gl_journal_id", chunk)
                .order("sort_order")
            )
        )
    return lines


def _fetch_accounts(db, organisation_id: str, account_ids: list[str]) -> dict[str, dict]:
    if not account_ids:
        return {}
    rows: list[dict] = []
    for chunk in _chunked(account_ids, CHUNK_SIZE):
        rows.extend(
            _fetch_rows(
                db.table("accounts")
                .select("id, code, name, type, group_name, system_key")
                .eq("organisation_id", organisation_id)
                .in_("id", chunk)
            )
        )
    return {str(row.get("id")): row for row in rows if row.get("id")}


def _source_label(source_type: str | None) -> str:
    if not source_type:
        return "Manual"
    return {
        "bank_transaction": "Bank",
        "customer_receipt": "Customer receipt",
        "invoice": "Supplier invoice",
        "finance_lease": "Finance lease",
    }.get(source_type, source_type.replace("_", " ").title())


def _classify_counterparty(journal: dict, counterparty_accounts: list[dict]) -> str:
    source_type = str(journal.get("source_type") or "").lower()
    if source_type in {"customer_receipt", "bank_transaction", "invoice"}:
        return "operating"
    if source_type in {"finance_lease", "lease_payment"}:
        return "financing"

    account_types = {str(account.get("type") or "").lower() for account in counterparty_accounts}
    group_names = " ".join(str(account.get("group_name") or "").lower() for account in counterparty_accounts)
    names = " ".join(str(account.get("name") or "").lower() for account in counterparty_accounts)

    if account_types & {"income", "expense"}:
        return "operating"
    if account_types & {"equity"}:
        return "financing"
    if account_types & {"liability"}:
        if "loan" in names or "lease" in names or "finance" in names or "borrow" in names:
            return "financing"
        return "operating"
    if "non-current" in group_names or "fixed asset" in group_names or "property" in names or "equipment" in names:
        return "investing"
    if account_types & {"asset"}:
        if "investment" in names or "vehicle" in names or "equipment" in names:
            return "investing"
        return "operating"
    if source_type in OPERATING_SOURCE_TYPES:
        return "operating"
    return "unclassified"


def _blank_section() -> dict:
    return {
        "cash_in": ZERO,
        "cash_out": ZERO,
        "net_cash_flow": ZERO,
        "rows": [],
    }


def _section_out(section: dict) -> dict:
    return {
        "cash_in": amount_out(section["cash_in"]),
        "cash_out": amount_out(section["cash_out"]),
        "net_cash_flow": amount_out(section["net_cash_flow"]),
        "rows": section["rows"],
    }


def generate_cash_flow(
    db,
    *,
    organisation_id: str,
    date_from: str,
    date_to: str,
) -> dict:
    start, end = _validate_dates(date_from, date_to)
    warnings: list[dict] = []

    cash_accounts = _fetch_cash_accounts(db, organisation_id)
    cash_account_ids = {str(row.get("id")) for row in cash_accounts if row.get("id")}
    if not cash_account_ids:
        warnings.append({
            "code": "no_cash_accounts",
            "message": "No active Bank/Cash accounts with linked GL accounts were found.",
        })
        return {
            "organisation_id": organisation_id,
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "cash_accounts": [],
            "sections": {
                "operating": _section_out(_blank_section()),
                "investing": _section_out(_blank_section()),
                "financing": _section_out(_blank_section()),
                "unclassified": _section_out(_blank_section()),
            },
            "summary": {
                "cash_in": 0.0,
                "cash_out": 0.0,
                "net_cash_flow": 0.0,
                "movement_count": 0,
            },
            "warnings": warnings,
            "disclaimer": "Calculated from posted journals against active Bank/Cash-linked GL accounts.",
        }

    journals = _fetch_posted_journals(db, organisation_id, start.isoformat(), end.isoformat())
    journal_ids = [str(row.get("id")) for row in journals if row.get("id")]
    journal_map = {str(row.get("id")): row for row in journals if row.get("id")}
    lines = _fetch_journal_lines(db, organisation_id, journal_ids)

    account_ids = sorted({str(row.get("account_id")) for row in lines if row.get("account_id")})
    accounts_by_id = _fetch_accounts(db, organisation_id, account_ids)

    lines_by_journal: dict[str, list[dict]] = {}
    for line in lines:
        lines_by_journal.setdefault(str(line.get("gl_journal_id")), []).append(line)

    sections = {
        "operating": _blank_section(),
        "investing": _blank_section(),
        "financing": _blank_section(),
        "unclassified": _blank_section(),
    }
    total_in = ZERO
    total_out = ZERO
    movement_count = 0
    transfer_count = 0

    for journal_id in journal_ids:
        journal = journal_map[journal_id]
        journal_lines = lines_by_journal.get(journal_id, [])
        cash_lines = [line for line in journal_lines if str(line.get("account_id")) in cash_account_ids]
        if not cash_lines:
            continue

        noncash_lines = [line for line in journal_lines if str(line.get("account_id")) not in cash_account_ids]
        if not noncash_lines:
            transfer_count += 1
            continue

        cash_in = sum((money(line.get("debit_amount")) for line in cash_lines), ZERO)
        cash_out = sum((money(line.get("credit_amount")) for line in cash_lines), ZERO)
        net = cash_in - cash_out
        if not net:
            transfer_count += 1
            continue

        counterparty_accounts = [
            accounts_by_id.get(str(line.get("account_id")), {})
            for line in noncash_lines
            if line.get("account_id")
        ]
        section_key = _classify_counterparty(journal, counterparty_accounts)
        section = sections[section_key]
        section["cash_in"] += cash_in
        section["cash_out"] += cash_out
        section["net_cash_flow"] += net
        total_in += cash_in
        total_out += cash_out
        movement_count += 1

        description = journal.get("description") or "Cash movement"
        counterparty_label = ", ".join(
            sorted(
                {
                    " ".join(
                        part
                        for part in [
                            str(account.get("code") or ""),
                            str(account.get("name") or ""),
                        ]
                        if part
                    )
                    for account in counterparty_accounts
                    if account
                }
            )
        )
        section["rows"].append({
            "journal_id": journal_id,
            "journal_date": journal.get("journal_date"),
            "source_type": journal.get("source_type") or "",
            "source_label": _source_label(journal.get("source_type")),
            "description": description,
            "counterparty": counterparty_label,
            "cash_in": amount_out(cash_in),
            "cash_out": amount_out(cash_out),
            "net_cash_flow": amount_out(net),
        })

    if transfer_count:
        warnings.append({
            "code": "cash_transfers_excluded",
            "message": f"{transfer_count} cash-only transfer journal(s) were excluded from external cash flow.",
        })

    net_total = total_in - total_out
    return {
        "organisation_id": organisation_id,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "cash_accounts": [
            {
                "account_id": account.get("id"),
                "code": account.get("code"),
                "name": account.get("name") or account.get("bank_account_name") or "",
                "bank_account_id": account.get("bank_account_id"),
                "bank_account_type": account.get("bank_account_type"),
            }
            for account in cash_accounts
        ],
        "sections": {key: _section_out(value) for key, value in sections.items()},
        "summary": {
            "cash_in": amount_out(total_in),
            "cash_out": amount_out(total_out),
            "net_cash_flow": amount_out(net_total),
            "movement_count": movement_count,
        },
        "warnings": warnings,
        "disclaimer": (
            "Calculated from posted journals against active Bank/Cash-linked GL accounts. "
            "Classification is based on journal source and the non-cash counterparty accounts."
        ),
    }
