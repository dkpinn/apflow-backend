from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.services.accounting_locks import assert_accounting_period_unlocked, parse_accounting_date
from app.services.bank_statement_service import new_uuid
from app.services.money import money
from app.services.protected_accounts import protected_account_reason


MONEY = Decimal("0.01")
ZERO = Decimal("0.00")
CREDIT_NORMAL_TYPES = {"income", "liability", "equity"}
OPENING_BALANCE_SOURCE = "opening_balance"
RETAINED_EARNINGS_SYSTEM_KEY = "retained_earnings"


def _amount(value: Any) -> Decimal:
    return money(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def _out(value: Decimal) -> float:
    return float(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def _normal_side(account_type: str) -> str:
    return "credit" if str(account_type or "").lower() in CREDIT_NORMAL_TYPES else "debit"


def _normal_amount(account_type: str, debit: Decimal, credit: Decimal) -> Decimal:
    if _normal_side(account_type) == "credit":
        return credit - debit
    return debit - credit


def _side_amount(debit: Decimal, credit: Decimal) -> tuple[str, Decimal]:
    if debit >= credit:
        return "debit", debit - credit
    return "credit", credit - debit


def _line_tracking(line: dict[str, Any]) -> dict[str, Any]:
    tracking = line.get("tracking")
    return tracking if isinstance(tracking, dict) else {}


def _fetch_accounts(db, organisation_id: str) -> dict[str, dict[str, Any]]:
    rows = (
        db.table("accounts")
        .select("id, code, name, type, active, is_system, system_key")
        .eq("organisation_id", organisation_id)
        .execute()
        .data
        or []
    )
    return {str(row.get("id")): row for row in rows if row.get("id")}


def _resolve_account(
    accounts_by_id: dict[str, dict[str, Any]],
    *,
    account_id: str | None,
    account_code: str | None,
) -> dict[str, Any]:
    if account_id and str(account_id) in accounts_by_id:
        return accounts_by_id[str(account_id)]
    if account_code:
        code = str(account_code).strip()
        matches = [row for row in accounts_by_id.values() if str(row.get("code") or "").strip() == code]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Account code {code} matches more than one account")
    label = account_id or account_code or "blank account"
    raise ValueError(f"Account {label} was not found in this organisation")


def _line_amounts(line: dict[str, Any]) -> tuple[Decimal, Decimal]:
    debit = _amount(line.get("debit") or line.get("debit_amount") or 0)
    credit = _amount(line.get("credit") or line.get("credit_amount") or 0)
    if debit < ZERO or credit < ZERO:
        raise ValueError("Opening balance debits and credits cannot be negative")
    if debit and credit:
        raise ValueError("Each opening balance line may have either a debit or a credit, not both")
    return debit, credit


def preview_opening_balance(
    db,
    *,
    organisation_id: str,
    as_at_date: str,
    lines: list[dict[str, Any]],
) -> dict[str, Any]:
    parsed_date = parse_accounting_date(as_at_date, field="as_at_date")
    if not lines:
        raise ValueError("Add at least one opening balance line")

    accounts_by_id = _fetch_accounts(db, organisation_id)
    normalised_lines: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    total_debit = ZERO
    total_credit = ZERO

    for index, line in enumerate(lines):
        account = _resolve_account(
            accounts_by_id,
            account_id=line.get("account_id"),
            account_code=line.get("account_code") or line.get("code"),
        )
        reason = _opening_balance_protected_reason(
            db,
            organisation_id=organisation_id,
            account_id=str(account.get("id")),
        )
        if reason:
            raise ValueError(
                f"Opening balance import cannot use {account.get('code') or account.get('name')} because it is a {reason}"
            )
        if account.get("active") is False:
            warnings.append({
                "code": "inactive_account",
                "message": f"{account.get('code') or account.get('name')} is inactive but included in the import.",
            })
        debit, credit = _line_amounts(line)
        if not debit and not credit:
            warnings.append({
                "code": "zero_line",
                "message": f"Line {index + 1} has a zero balance and will be ignored.",
            })
            continue
        total_debit += debit
        total_credit += credit
        normalised_lines.append({
            "account_id": account["id"],
            "code": account.get("code"),
            "name": account.get("name"),
            "type": account.get("type"),
            "description": line.get("description") or f"Opening balance - {account.get('code') or account.get('name')}",
            "debit_amount": _out(debit),
            "credit_amount": _out(credit),
            "tracking": line.get("tracking") if isinstance(line.get("tracking"), dict) else {},
            "sort_order": len(normalised_lines),
        })

    difference = total_debit - total_credit
    in_balance = difference == ZERO
    if not in_balance:
        warnings.append({
            "code": "out_of_balance",
            "message": "Opening balance import is out of balance.",
            "difference": _out(difference),
        })

    return {
        "organisation_id": organisation_id,
        "as_at_date": parsed_date.isoformat(),
        "lines": normalised_lines,
        "summary": {
            "line_count": len(normalised_lines),
            "total_debit": _out(total_debit),
            "total_credit": _out(total_credit),
            "difference": _out(difference),
            "in_balance": in_balance,
        },
        "warnings": warnings,
    }


def _existing_opening_balance_journal(db, *, organisation_id: str, as_at_date: str) -> dict[str, Any] | None:
    rows = (
        db.table("gl_journals")
        .select("id, status, journal_date, description")
        .eq("organisation_id", organisation_id)
        .eq("source_type", OPENING_BALANCE_SOURCE)
        .eq("journal_date", as_at_date)
        .execute()
        .data
        or []
    )
    return next((row for row in rows if row.get("status") != "reversed"), None)


def _active_opening_balance_journals(db, *, organisation_id: str, as_at_date: str) -> list[dict[str, Any]]:
    rows = (
        db.table("gl_journals")
        .select("id, status, journal_date, description")
        .eq("organisation_id", organisation_id)
        .eq("source_type", OPENING_BALANCE_SOURCE)
        .eq("journal_date", as_at_date)
        .execute()
        .data
        or []
    )
    return [row for row in rows if row.get("status") != "reversed"]


def _active_opening_balance_journals_any_date(db, *, organisation_id: str) -> list[dict[str, Any]]:
    """All non-reversed opening-balance journals for the org, regardless of date.

    Opening balances are conceptually a single per-org journal (the conversion
    snapshot). The account-level editor must target that one journal wherever it
    lives, otherwise editing on a different day silently posts a SECOND opening
    journal and the trial balance double-counts. See _singleton_opening_balance_journal.
    """
    rows = (
        db.table("gl_journals")
        .select("id, status, journal_date, description")
        .eq("organisation_id", organisation_id)
        .eq("source_type", OPENING_BALANCE_SOURCE)
        .execute()
        .data
        or []
    )
    return [row for row in rows if row.get("status") != "reversed"]


def _singleton_opening_balance_journal(db, *, organisation_id: str) -> dict[str, Any] | None:
    """Return the org's single active opening-balance journal, or None.

    Raises when more than one exists so the caller refuses to edit (and the user
    is pointed at consolidation) rather than compounding a duplicate.
    """
    journals = _active_opening_balance_journals_any_date(db, organisation_id=organisation_id)
    if len(journals) > 1:
        dates = ", ".join(sorted({str(j.get("journal_date")) for j in journals}))
        raise ValueError(
            "More than one active opening balance journal exists "
            f"({dates}). Consolidate the duplicates before editing opening balances."
        )
    return journals[0] if journals else None


def get_opening_balance_summary(db, *, organisation_id: str) -> dict[str, Any]:
    """Org-level opening-balance snapshot for initialising the editor.

    Tolerant of the duplicate state (does not raise) so the settings page can
    still load and default its date to the real opening date while flagging that
    consolidation is needed.
    """
    journals = _active_opening_balance_journals_any_date(db, organisation_id=organisation_id)
    dates = sorted({str(j.get("journal_date")) for j in journals if j.get("journal_date")})
    return {
        "organisation_id": organisation_id,
        "exists": bool(journals),
        "as_at_date": dates[0] if dates else None,
        "journal_count": len(journals),
        "duplicate_dates": dates if len(dates) > 1 else [],
    }


def _fetch_journal_lines(db, *, organisation_id: str, journal_id: str) -> list[dict[str, Any]]:
    return (
        db.table("gl_journal_lines")
        .select("id, organisation_id, gl_journal_id, account_id, description, debit_amount, credit_amount, tracking, sort_order")
        .eq("organisation_id", organisation_id)
        .eq("gl_journal_id", journal_id)
        .order("sort_order")
        .execute()
        .data
        or []
    )


def _retained_earnings_account(accounts_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    matches = [
        account
        for account in accounts_by_id.values()
        if account.get("system_key") == RETAINED_EARNINGS_SYSTEM_KEY
    ]
    if not matches:
        raise ValueError("Retained earnings system account was not found for this organisation")
    return matches[0]


def _opening_line_for_account(
    lines: list[dict[str, Any]],
    *,
    account_id: str,
    label: str,
) -> dict[str, Any] | None:
    matches = [line for line in lines if str(line.get("account_id") or "") == account_id]
    if len(matches) > 1:
        raise ValueError(f"{label} has split opening-balance lines. Use the full opening-balance import to edit it.")
    if matches and _line_tracking(matches[0]):
        raise ValueError(f"{label} has tracked opening-balance detail. Use the full opening-balance import to edit it.")
    return matches[0] if matches else None


def _opening_balance_protected_reason(
    db,
    *,
    organisation_id: str,
    account_id: str,
) -> str | None:
    try:
        rows = (
            db.table("accounts")
            .select("id, is_system, system_key, managed_asset_type_id, asset_account_role")
            .eq("organisation_id", organisation_id)
            .eq("id", account_id)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    account = rows[0] if rows else {}
    if account.get("is_system") is True or account.get("system_key"):
        return "system account"
    if account.get("managed_asset_type_id") or account.get("asset_account_role"):
        return "module-controlled account"

    reason = protected_account_reason(
        db,
        organisation_id=organisation_id,
        account_id=account_id,
    )
    if reason == "bank/cash control account":
        return None
    return reason


def get_account_opening_balance(
    db,
    *,
    organisation_id: str,
    account_id: str,
    as_at_date: str,
) -> dict[str, Any]:
    parsed_date = parse_accounting_date(as_at_date, field="as_at_date").isoformat()
    accounts_by_id = _fetch_accounts(db, organisation_id)
    account = _resolve_account(accounts_by_id, account_id=account_id, account_code=None)
    retained = _retained_earnings_account(accounts_by_id)
    is_retained = str(account.get("id")) == str(retained.get("id"))
    protected_reason = _opening_balance_protected_reason(
        db,
        organisation_id=organisation_id,
        account_id=str(account.get("id")),
    )
    journal = _singleton_opening_balance_journal(db, organisation_id=organisation_id)
    # The opening-balance journal owns its own as-at date; surface that (not the
    # date the caller happened to ask with) so the editor shows the real date.
    if journal and journal.get("journal_date"):
        parsed_date = str(journal["journal_date"])

    debit = ZERO
    credit = ZERO
    line_id = None
    if journal:
        lines = _fetch_journal_lines(db, organisation_id=organisation_id, journal_id=str(journal["id"]))
        if is_retained:
            line = _opening_line_for_account(lines, account_id=str(account["id"]), label="Retained earnings")
        else:
            line = _opening_line_for_account(lines, account_id=str(account["id"]), label=account.get("name") or "Account")
        if line:
            line_id = line.get("id")
            debit = _amount(line.get("debit_amount"))
            credit = _amount(line.get("credit_amount"))

    side, amount = _side_amount(debit, credit)
    normal_amount = _normal_amount(str(account.get("type") or "other"), debit, credit)

    return {
        "organisation_id": organisation_id,
        "as_at_date": parsed_date,
        "journal": journal,
        "line_id": line_id,
        "account": {
            "id": account.get("id"),
            "code": account.get("code"),
            "name": account.get("name"),
            "type": account.get("type"),
            "active": account.get("active"),
            "system_key": account.get("system_key"),
        },
        "balancing_account": {
            "id": retained.get("id"),
            "code": retained.get("code"),
            "name": retained.get("name"),
            "system_key": retained.get("system_key"),
        },
        "editable": not is_retained and protected_reason is None,
        "protected_reason": protected_reason,
        "side": side,
        "amount": _out(amount),
        "normal_side": _normal_side(str(account.get("type") or "other")),
        "normal_amount": _out(normal_amount),
        "debit_amount": _out(debit),
        "credit_amount": _out(credit),
        "warnings": [],
    }


def _line_payload(
    *,
    organisation_id: str,
    journal_id: str,
    account_id: str,
    description: str,
    debit: Decimal,
    credit: Decimal,
    sort_order: int,
) -> dict[str, Any]:
    return {
        "organisation_id": organisation_id,
        "gl_journal_id": journal_id,
        "account_id": account_id,
        "description": description,
        "debit_amount": _out(debit),
        "credit_amount": _out(credit),
        "tracking": {},
        "sort_order": sort_order,
    }


def _journal_totals(lines: list[dict[str, Any]]) -> tuple[Decimal, Decimal]:
    debit = sum((_amount(line.get("debit_amount")) for line in lines), ZERO)
    credit = sum((_amount(line.get("credit_amount")) for line in lines), ZERO)
    return debit, credit


def upsert_account_opening_balance(
    db,
    *,
    organisation_id: str,
    account_id: str,
    as_at_date: str,
    side: str,
    amount: Any,
    user_id: str,
    description: str | None = None,
    allow_protected: bool = False,
) -> dict[str, Any]:
    parsed_date = parse_accounting_date(as_at_date, field="as_at_date").isoformat()
    side = str(side or "").lower().strip()
    if side not in {"debit", "credit"}:
        raise ValueError("side must be debit or credit")
    amount_dec = _amount(amount)
    if amount_dec < ZERO:
        raise ValueError("Opening balance amount cannot be negative")

    accounts_by_id = _fetch_accounts(db, organisation_id)
    account = _resolve_account(accounts_by_id, account_id=account_id, account_code=None)
    retained = _retained_earnings_account(accounts_by_id)
    if str(account["id"]) == str(retained["id"]):
        raise ValueError("Retained earnings is calculated from the other opening balances and cannot be edited here")
    if not allow_protected:
        reason = _opening_balance_protected_reason(
            db,
            organisation_id=organisation_id,
            account_id=str(account["id"]),
        )
        if reason:
            raise ValueError(f"Update opening balance cannot use a {reason}")
    if account.get("active") is False and amount_dec:
        raise ValueError("Inactive accounts cannot be given an opening balance")

    # Opening balances are a single per-org journal. Edit whichever one already
    # exists (at its own as-at date) instead of keying off the caller's date, so
    # editing on a different day never posts a duplicate that the TB double-counts.
    journal = _singleton_opening_balance_journal(db, organisation_id=organisation_id)
    effective_date = str(journal["journal_date"]) if journal and journal.get("journal_date") else parsed_date

    assert_accounting_period_unlocked(
        db,
        organisation_id=organisation_id,
        transaction_date=effective_date,
        action="Update opening balance",
    )

    debit = amount_dec if side == "debit" else ZERO
    credit = amount_dec if side == "credit" else ZERO
    target_description = (description or "").strip() or f"Opening balance - {account.get('code') or account.get('name')}"
    retained_description = f"Opening balance balancing entry - {retained.get('code') or retained.get('name')}"

    if journal is None:
        if not amount_dec:
            return {
                "success": True,
                "journal": None,
                "opening_balance": get_account_opening_balance(
                    db,
                    organisation_id=organisation_id,
                    account_id=account_id,
                    as_at_date=parsed_date,
                ),
            }
        journal_id = new_uuid()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        retained_debit = credit
        retained_credit = debit
        journal_lines = [
            _line_payload(
                organisation_id=organisation_id,
                journal_id=journal_id,
                account_id=str(account["id"]),
                description=target_description,
                debit=debit,
                credit=credit,
                sort_order=0,
            ),
            _line_payload(
                organisation_id=organisation_id,
                journal_id=journal_id,
                account_id=str(retained["id"]),
                description=retained_description,
                debit=retained_debit,
                credit=retained_credit,
                sort_order=1,
            ),
        ]
        total_debit, total_credit = _journal_totals(journal_lines)
        journal = {
            "id": journal_id,
            "organisation_id": organisation_id,
            "source_type": OPENING_BALANCE_SOURCE,
            "source_id": None,
            "journal_date": parsed_date,
            "description": f"Opening balances as at {parsed_date}",
            "status": "posted",
            "total_debit": _out(total_debit),
            "total_credit": _out(total_credit),
            "created_by": user_id,
            "posted_by": user_id,
            "posted_at": now,
        }
        db.table("gl_journals").insert(journal).execute()
        db.table("gl_journal_lines").insert(journal_lines).execute()
        return {
            "success": True,
            "journal": journal,
            "opening_balance": get_account_opening_balance(
                db,
                organisation_id=organisation_id,
                account_id=account_id,
                as_at_date=parsed_date,
            ),
        }

    if str(journal.get("status") or "").lower() != "posted":
        raise ValueError("Account-level opening balance editing only supports posted opening balance journals")
    journal_id = str(journal["id"])
    lines = _fetch_journal_lines(db, organisation_id=organisation_id, journal_id=journal_id)
    target_line = _opening_line_for_account(
        lines,
        account_id=str(account["id"]),
        label=account.get("name") or "Account",
    )
    retained_line = _opening_line_for_account(
        lines,
        account_id=str(retained["id"]),
        label="Retained earnings",
    )

    if target_line:
        if amount_dec:
            db.table("gl_journal_lines").update({
                "description": target_description,
                "debit_amount": _out(debit),
                "credit_amount": _out(credit),
                "tracking": {},
            }).eq("id", target_line["id"]).execute()
        else:
            db.table("gl_journal_lines").delete().eq("id", target_line["id"]).execute()
    elif amount_dec:
        db.table("gl_journal_lines").insert(_line_payload(
            organisation_id=organisation_id,
            journal_id=journal_id,
            account_id=str(account["id"]),
            description=target_description,
            debit=debit,
            credit=credit,
            sort_order=len(lines),
        )).execute()

    refreshed_lines = _fetch_journal_lines(db, organisation_id=organisation_id, journal_id=journal_id)
    non_retained = [
        line for line in refreshed_lines if str(line.get("account_id") or "") != str(retained["id"])
    ]
    non_retained_debit, non_retained_credit = _journal_totals(non_retained)
    difference = non_retained_debit - non_retained_credit
    retained_debit = ZERO
    retained_credit = ZERO
    if difference > ZERO:
        retained_credit = difference
    elif difference < ZERO:
        retained_debit = -difference

    if retained_line:
        if retained_debit or retained_credit:
            db.table("gl_journal_lines").update({
                "description": retained_description,
                "debit_amount": _out(retained_debit),
                "credit_amount": _out(retained_credit),
                "tracking": {},
                "sort_order": len(non_retained),
            }).eq("id", retained_line["id"]).execute()
        else:
            db.table("gl_journal_lines").delete().eq("id", retained_line["id"]).execute()
    elif retained_debit or retained_credit:
        db.table("gl_journal_lines").insert(_line_payload(
            organisation_id=organisation_id,
            journal_id=journal_id,
            account_id=str(retained["id"]),
            description=retained_description,
            debit=retained_debit,
            credit=retained_credit,
            sort_order=len(non_retained),
        )).execute()

    final_lines = _fetch_journal_lines(db, organisation_id=organisation_id, journal_id=journal_id)
    total_debit, total_credit = _journal_totals(final_lines)
    db.table("gl_journals").update({
        "total_debit": _out(total_debit),
        "total_credit": _out(total_credit),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }).eq("id", journal_id).execute()

    return {
        "success": True,
        "journal": {
            **journal,
            "total_debit": _out(total_debit),
            "total_credit": _out(total_credit),
        },
        "opening_balance": get_account_opening_balance(
            db,
            organisation_id=organisation_id,
            account_id=account_id,
            as_at_date=parsed_date,
        ),
    }


def post_opening_balance(
    db,
    *,
    organisation_id: str,
    as_at_date: str,
    lines: list[dict[str, Any]],
    user_id: str,
    description: str | None = None,
) -> dict[str, Any]:
    preview = preview_opening_balance(
        db,
        organisation_id=organisation_id,
        as_at_date=as_at_date,
        lines=lines,
    )
    summary = preview["summary"]
    if not summary["in_balance"]:
        raise ValueError("Opening balance import must balance before posting")
    if summary["line_count"] == 0:
        raise ValueError("Opening balance import has no non-zero lines to post")

    assert_accounting_period_unlocked(
        db,
        organisation_id=organisation_id,
        transaction_date=preview["as_at_date"],
        action="Post opening balance",
    )
    existing = _existing_opening_balance_journal(
        db,
        organisation_id=organisation_id,
        as_at_date=preview["as_at_date"],
    )
    if existing:
        raise ValueError(f"An opening balance journal already exists for {preview['as_at_date']}")

    journal_id = new_uuid()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    journal = {
        "id": journal_id,
        "organisation_id": organisation_id,
        "source_type": "opening_balance",
        "source_id": None,
        "journal_date": preview["as_at_date"],
        "description": (description or "").strip() or f"Opening balances as at {preview['as_at_date']}",
        "status": "posted",
        "total_debit": summary["total_debit"],
        "total_credit": summary["total_credit"],
        "created_by": user_id,
        "posted_by": user_id,
        "posted_at": now,
    }
    db.table("gl_journals").insert(journal).execute()
    db.table("gl_journal_lines").insert([
        {
            "organisation_id": organisation_id,
            "gl_journal_id": journal_id,
            "account_id": row["account_id"],
            "description": row["description"],
            "debit_amount": row["debit_amount"],
            "credit_amount": row["credit_amount"],
            "tracking": row["tracking"],
            "sort_order": row["sort_order"],
        }
        for row in preview["lines"]
    ]).execute()
    return {"success": True, "journal": journal, "preview": preview}
