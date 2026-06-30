from __future__ import annotations

from typing import Any


def _rows(db, table: str, *, organisation_id: str, select: str) -> list[dict[str, Any]]:
    try:
        return (
            db.table(table)
            .select(select)
            .eq("organisation_id", organisation_id)
            .execute()
            .data
            or []
        )
    except Exception:
        return []


def protected_account_reasons(db, *, organisation_id: str) -> dict[str, str]:
    reasons: dict[str, str] = {}
    accounts = _rows(
        db,
        "accounts",
        organisation_id=organisation_id,
        select="id, is_system, system_key, managed_asset_type_id, asset_account_role",
    )
    for account in accounts:
        account_id = str(account.get("id") or "")
        if not account_id:
            continue
        if account.get("is_system") is True:
            reasons[account_id] = "system account"
        if account.get("system_key"):
            reasons[account_id] = "system account"
        if account.get("managed_asset_type_id") or account.get("asset_account_role"):
            reasons[account_id] = "module-controlled account"

    bank_accounts = _rows(
        db,
        "bank_accounts",
        organisation_id=organisation_id,
        select="id, gl_account_id, active",
    )
    for bank_account in bank_accounts:
        account_id = str(bank_account.get("gl_account_id") or "")
        if account_id and bank_account.get("active") is not False:
            reasons[account_id] = "bank/cash control account"

    return reasons


def protected_account_ids(db, *, organisation_id: str) -> set[str]:
    return set(protected_account_reasons(db, organisation_id=organisation_id))


def protected_account_reason(db, *, organisation_id: str, account_id: str) -> str | None:
    return protected_account_reasons(db, organisation_id=organisation_id).get(str(account_id))


def assert_manual_posting_account_allowed(
    db,
    *,
    organisation_id: str,
    account_id: str,
    action: str = "Post transaction",
) -> None:
    reason = protected_account_reason(db, organisation_id=organisation_id, account_id=str(account_id))
    if reason:
        raise ValueError(f"{action} cannot use a {reason} as the user-selected posting account")
