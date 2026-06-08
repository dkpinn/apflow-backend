from __future__ import annotations

from typing import Any, Optional


ASSET_TYPE_SELECT = (
    "id, organisation_id, name, category, depreciation_method, useful_life_months, "
    "residual_value_percent, depreciation_convention, active, archived_at, archived_by, "
    "cost_account_id, accumulated_account_id, expense_account_id, created_by, created_at, updated_at"
)

ACCOUNT_SELECT = (
    "id, code, name, type, group_name, active, vat_treatment, is_system, system_key, "
    "managed_asset_type_id, asset_account_role, income_statement_nature"
)


def managed_account_names(name: str, category: str) -> dict[str, str]:
    clean_name = " ".join(str(name or "").split())
    if not clean_name:
        raise ValueError("Asset type name is required")
    if category == "tangible":
        return {
            "cost": f"{clean_name} - At Cost",
            "accumulated": f"{clean_name} - Accumulated Depreciation",
            "expense": f"Depreciation on {clean_name}",
        }
    if category == "intangible":
        return {
            "cost": f"{clean_name} - At Cost",
            "accumulated": f"{clean_name} - Accumulated Amortisation",
            "expense": f"Amortisation of {clean_name}",
        }
    raise ValueError("Asset type category must be tangible or intangible")


def _rpc_data(result: Any) -> Any:
    data = getattr(result, "data", result)
    if isinstance(data, list) and len(data) == 1:
        return data[0]
    return data


def _account_ids(asset_types: list[dict[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(
            str(account_id)
            for asset_type in asset_types
            for account_id in (
                asset_type.get("cost_account_id"),
                asset_type.get("accumulated_account_id"),
                asset_type.get("expense_account_id"),
            )
            if account_id
        )
    )


def _with_accounts(
    db,
    asset_types: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    account_ids = _account_ids(asset_types)
    accounts: list[dict[str, Any]] = []
    if account_ids:
        accounts = (
            db.table("accounts")
            .select(ACCOUNT_SELECT)
            .in_("id", account_ids)
            .execute()
            .data
            or []
        )
    accounts_by_id = {str(account.get("id")): account for account in accounts}

    return [
        {
            **asset_type,
            "residual_value_percent": float(asset_type.get("residual_value_percent") or 0),
            "accounts": {
                "cost": accounts_by_id.get(str(asset_type.get("cost_account_id"))),
                "accumulated": accounts_by_id.get(str(asset_type.get("accumulated_account_id"))),
                "expense": accounts_by_id.get(str(asset_type.get("expense_account_id"))),
            },
        }
        for asset_type in asset_types
    ]


def list_asset_types(
    db,
    *,
    organisation_id: str,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    query = (
        db.table("asset_types")
        .select(ASSET_TYPE_SELECT)
        .eq("organisation_id", organisation_id)
    )
    if not include_archived:
        query = query.eq("active", True)
    rows = query.order("name").execute().data or []
    return _with_accounts(db, rows)


def get_asset_type(
    db,
    *,
    organisation_id: str,
    asset_type_id: str,
) -> Optional[dict[str, Any]]:
    rows = (
        db.table("asset_types")
        .select(ASSET_TYPE_SELECT)
        .eq("organisation_id", organisation_id)
        .eq("id", asset_type_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    enriched = _with_accounts(db, rows)
    return enriched[0] if enriched else None


def create_asset_type(
    db,
    *,
    organisation_id: str,
    name: str,
    category: str,
    useful_life_months: int,
    residual_value_percent: float,
) -> dict[str, Any]:
    result = db.rpc(
        "create_asset_type_with_accounts",
        {
            "p_org_id": organisation_id,
            "p_name": name,
            "p_category": category,
            "p_useful_life_months": useful_life_months,
            "p_residual_value_percent": residual_value_percent,
        },
    ).execute()
    created = _rpc_data(result)
    asset_type_id = str(created.get("id")) if isinstance(created, dict) else ""
    saved = get_asset_type(
        db,
        organisation_id=organisation_id,
        asset_type_id=asset_type_id,
    )
    if not saved:
        raise ValueError("Asset type was created but could not be reloaded")
    return saved


def update_asset_type(
    db,
    *,
    organisation_id: str,
    asset_type_id: str,
    name: str,
    category: str,
    useful_life_months: int,
    residual_value_percent: float,
) -> dict[str, Any]:
    db.rpc(
        "update_asset_type_with_accounts",
        {
            "p_org_id": organisation_id,
            "p_asset_type_id": asset_type_id,
            "p_name": name,
            "p_category": category,
            "p_useful_life_months": useful_life_months,
            "p_residual_value_percent": residual_value_percent,
        },
    ).execute()
    saved = get_asset_type(
        db,
        organisation_id=organisation_id,
        asset_type_id=asset_type_id,
    )
    if not saved:
        raise ValueError("Asset type not found")
    return saved


def preview_asset_type_removal(
    db,
    *,
    organisation_id: str,
    asset_type_id: str,
) -> dict[str, Any]:
    result = db.rpc(
        "preview_asset_type_removal",
        {
            "p_org_id": organisation_id,
            "p_asset_type_id": asset_type_id,
        },
    ).execute()
    data = _rpc_data(result)
    return data if isinstance(data, dict) else {}


def remove_asset_type(
    db,
    *,
    organisation_id: str,
    asset_type_id: str,
) -> dict[str, Any]:
    result = db.rpc(
        "remove_asset_type_with_accounts",
        {
            "p_org_id": organisation_id,
            "p_asset_type_id": asset_type_id,
        },
    ).execute()
    data = _rpc_data(result)
    return data if isinstance(data, dict) else {}


def restore_asset_type(
    db,
    *,
    organisation_id: str,
    asset_type_id: str,
) -> dict[str, Any]:
    db.rpc(
        "restore_asset_type_with_accounts",
        {
            "p_org_id": organisation_id,
            "p_asset_type_id": asset_type_id,
        },
    ).execute()
    saved = get_asset_type(
        db,
        organisation_id=organisation_id,
        asset_type_id=asset_type_id,
    )
    if not saved:
        raise ValueError("Asset type not found")
    return saved
