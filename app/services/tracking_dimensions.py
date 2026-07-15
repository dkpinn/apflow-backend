from __future__ import annotations

from typing import Any, Optional


MAX_TRACKING_DIMENSIONS = 5


def _rows(result: Any) -> list[dict[str, Any]]:
    return list(getattr(result, "data", None) or [])


def _tracking_dimension_select() -> str:
    return (
        "id, organisation_id, name, position, active, default_value_id, "
        "is_income_statement_function_driver"
    )


def _tracking_value_select() -> str:
    return "id, dimension_id, code, name, active, sort_order"


def _fetch_dimensions(db, organisation_id: str) -> list[dict[str, Any]]:
    return _rows(
        db.table("tracking_dimensions")
        .select(_tracking_dimension_select())
        .eq("organisation_id", organisation_id)
        .execute()
    )


def _sort_dimensions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            int(row.get("position") or MAX_TRACKING_DIMENSIONS + 1),
            str(row.get("name") or "").lower(),
        ),
    )


def _sort_values(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            int(row.get("sort_order") or 0),
            str(row.get("name") or row.get("code") or "").lower(),
        ),
    )


def hydrate_tracking_values(
    db,
    dimensions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not dimensions:
        return []

    dimension_ids = [str(row["id"]) for row in dimensions if row.get("id")]
    values_by_dimension: dict[str, list[dict[str, Any]]] = {dimension_id: [] for dimension_id in dimension_ids}
    if dimension_ids:
        values = _rows(
            db.table("tracking_values")
            .select(_tracking_value_select())
            .in_("dimension_id", dimension_ids)
            .execute()
        )
        for value in _sort_values(values):
            dimension_id = str(value.get("dimension_id"))
            if dimension_id in values_by_dimension:
                values_by_dimension[dimension_id].append(value)

    return [
        {
            **dimension,
            "values": values_by_dimension.get(str(dimension.get("id")), []),
        }
        for dimension in _sort_dimensions(dimensions)
    ]


def list_tracking_dimensions(
    db,
    *,
    organisation_id: str,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    dimensions = _fetch_dimensions(db, organisation_id)
    if not include_archived:
        dimensions = [row for row in dimensions if row.get("active") is not False]
    return hydrate_tracking_values(db, dimensions)


def _normalise_position(position: Optional[int]) -> Optional[int]:
    if position is None:
        return None
    position = int(position)
    if position < 1 or position > MAX_TRACKING_DIMENSIONS:
        raise ValueError("Tracking dimension position must be between 1 and 5")
    return position


def _next_available_position(existing: list[dict[str, Any]]) -> int:
    used_positions = {
        int(row.get("position"))
        for row in existing
        if row.get("position") is not None
    }
    for position in range(1, MAX_TRACKING_DIMENSIONS + 1):
        if position not in used_positions:
            return position
    raise ValueError("Tracking dimensions are limited to five positions per organisation")


def _assert_position_available(
    existing: list[dict[str, Any]],
    *,
    position: int,
    exclude_dimension_id: Optional[str] = None,
) -> None:
    for row in existing:
        if exclude_dimension_id and str(row.get("id")) == str(exclude_dimension_id):
            continue
        if int(row.get("position") or 0) == position:
            raise ValueError(f"Tracking dimension position {position} is already in use")


def create_tracking_dimension(
    db,
    *,
    organisation_id: str,
    name: str,
    position: Optional[int] = None,
) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Tracking dimension name is required")

    existing = _fetch_dimensions(db, organisation_id)
    selected_position = _normalise_position(position) or _next_available_position(existing)
    _assert_position_available(existing, position=selected_position)

    row = {
        "organisation_id": organisation_id,
        "name": clean_name,
        "position": selected_position,
        "active": True,
    }
    result = db.table("tracking_dimensions").insert(row).execute()
    saved = (_rows(result) or [row])[0]
    return hydrate_tracking_values(db, [saved])[0]


def update_tracking_dimension(
    db,
    *,
    organisation_id: str,
    dimension_id: str,
    name: Optional[str] = None,
    position: Optional[int] = None,
    active: Optional[bool] = None,
) -> dict[str, Any]:
    existing = _fetch_dimensions(db, organisation_id)
    current = next((row for row in existing if str(row.get("id")) == str(dimension_id)), None)
    if not current:
        raise LookupError("Tracking dimension not found")

    patch: dict[str, Any] = {}
    if name is not None:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Tracking dimension name is required")
        patch["name"] = clean_name
    if position is not None:
        selected_position = _normalise_position(position)
        _assert_position_available(
            existing,
            position=selected_position,
            exclude_dimension_id=dimension_id,
        )
        patch["position"] = selected_position
    if active is not None:
        patch["active"] = bool(active)

    if not patch:
        return hydrate_tracking_values(db, [current])[0]

    result = (
        db.table("tracking_dimensions")
        .update(patch)
        .eq("organisation_id", organisation_id)
        .eq("id", dimension_id)
        .execute()
    )
    saved = (_rows(result) or [{**current, **patch}])[0]
    return hydrate_tracking_values(db, [saved])[0]


def set_tracking_dimension_active(
    db,
    *,
    organisation_id: str,
    dimension_id: str,
    active: bool,
) -> dict[str, Any]:
    return update_tracking_dimension(
        db,
        organisation_id=organisation_id,
        dimension_id=dimension_id,
        active=active,
    )
