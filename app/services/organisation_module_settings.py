from __future__ import annotations

from typing import Any, Iterable


MODULE_KEYS = (
    "supplier",
    "customer",
    "inventory",
    "bank_cash",
    "asset",
    "liability",
    "project",
)


def default_module_setting(module_key: str) -> dict[str, Any]:
    if module_key not in MODULE_KEYS:
        raise ValueError("Unsupported organisation module")
    return {
        "module_key": module_key,
        "tracking_enabled": False,
        "required_tracking_dimension_ids": [],
    }


def get_module_settings(db, organisation_id: str) -> list[dict[str, Any]]:
    rows = (
        db.table("organisation_module_settings")
        .select("module_key, tracking_enabled, required_tracking_dimension_ids")
        .eq("organisation_id", organisation_id)
        .execute()
        .data
        or []
    )
    by_key = {str(row.get("module_key")): row for row in rows}
    settings: list[dict[str, Any]] = []
    for module_key in MODULE_KEYS:
        row = by_key.get(module_key) or {}
        tracking_enabled = bool(row.get("tracking_enabled"))
        settings.append({
            "module_key": module_key,
            "tracking_enabled": tracking_enabled,
            "required_tracking_dimension_ids": (
                [str(item) for item in (row.get("required_tracking_dimension_ids") or [])]
                if tracking_enabled
                else []
            ),
        })
    return settings


def get_module_setting(db, organisation_id: str, module_key: str) -> dict[str, Any]:
    return next(
        setting
        for setting in get_module_settings(db, organisation_id)
        if setting["module_key"] == module_key
    )


def validate_required_dimensions(
    db,
    *,
    organisation_id: str,
    tracking_enabled: bool,
    dimension_ids: Iterable[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    if not tracking_enabled:
        return [], []

    normalized_ids = list(dict.fromkeys(str(item) for item in dimension_ids if item))
    if not normalized_ids:
        raise ValueError("Select at least one active tracking dimension")

    rows = (
        db.table("tracking_dimensions")
        .select("id, name, active")
        .eq("organisation_id", organisation_id)
        .in_("id", normalized_ids)
        .eq("active", True)
        .execute()
        .data
        or []
    )
    by_id = {str(row.get("id")): row for row in rows}
    invalid_ids = [dimension_id for dimension_id in normalized_ids if dimension_id not in by_id]
    if invalid_ids:
        raise ValueError(
            "Every required tracking dimension must be active and belong to this organisation"
        )
    return normalized_ids, [by_id[dimension_id] for dimension_id in normalized_ids]


def required_tracking_dimensions(
    db,
    *,
    organisation_id: str,
    module_key: str,
) -> list[dict[str, Any]]:
    setting = get_module_setting(db, organisation_id, module_key)
    if not setting["tracking_enabled"]:
        return []
    _, dimensions = validate_required_dimensions(
        db,
        organisation_id=organisation_id,
        tracking_enabled=True,
        dimension_ids=setting["required_tracking_dimension_ids"],
    )
    return dimensions


def missing_tracking_dimensions(
    tracking: Any,
    required_dimensions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    values = tracking if isinstance(tracking, dict) else {}
    return [
        dimension
        for dimension in required_dimensions
        if not str(values.get(str(dimension.get("id"))) or "").strip()
    ]


def validate_supplier_allocations_tracking(
    *,
    line_items: list[dict[str, Any]],
    allocations_by_line: dict[str, list[dict[str, Any]]],
    required_dimensions: list[dict[str, Any]],
) -> None:
    if not required_dimensions:
        return

    failures: list[str] = []
    for line_index, line_item in enumerate(line_items):
        description = str(line_item.get("description") or f"Line {line_index + 1}")
        line_tracking = line_item.get("tracking") or {}
        allocations = allocations_by_line.get(str(line_item.get("id")), [])
        effective_allocations = allocations or [{"tracking": line_tracking}]
        for allocation_index, allocation in enumerate(effective_allocations):
            effective_tracking = allocation.get("tracking") or line_tracking
            missing = missing_tracking_dimensions(effective_tracking, required_dimensions)
            if not missing:
                continue
            names = ", ".join(str(item.get("name") or item.get("id")) for item in missing)
            suffix = f" split {allocation_index + 1}" if allocations else ""
            failures.append(f"{description}{suffix}: {names}")

    if failures:
        raise ValueError(
            "Supplier posting requires tracking on every expense allocation. Missing "
            + "; ".join(failures[:8])
        )


def validate_bank_allocation_tracking(
    *,
    tracking: dict[str, Any],
    required_dimensions: list[dict[str, Any]],
) -> None:
    missing = missing_tracking_dimensions(tracking, required_dimensions)
    if missing:
        names = ", ".join(str(item.get("name") or item.get("id")) for item in missing)
        raise ValueError(f"Bank/Cash posting requires tracking for: {names}")
