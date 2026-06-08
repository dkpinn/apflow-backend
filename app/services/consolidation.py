from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Optional


WRITE_ROLES = {"owner", "admin", "accountant"}
GROUP_WRITE_ROLES = {"owner", "admin", "accountant"}
GROUP_ADMIN_ROLES = {"owner", "admin"}


class ConsolidationAccessError(Exception):
    """Raised when a user cannot access or edit a reporting group."""


class ConsolidationValidationError(Exception):
    """Raised when consolidation input is invalid."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first(data: Any) -> Optional[dict]:
    return data[0] if data else None


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _pct_factor(value: Any) -> Decimal:
    return _decimal(value) / Decimal("100")


def _in(values: Iterable[Any]) -> list[Any]:
    return [value for value in values if value is not None]


def _fetch_by_id(supabase, table: str, id_: str) -> Optional[dict]:
    return _first((supabase.table(table).select("*").eq("id", id_).execute()).data)


def _fetch_user_memberships(supabase, *, user_id: str) -> list[dict]:
    return (
        supabase.table("organisation_users")
        .select("organisation_id, role, status")
        .eq("user_id", user_id)
        .eq("status", "active")
        .execute()
    ).data or []


def user_has_org_role(supabase, *, user_id: str, organisation_id: str, roles: set[str]) -> bool:
    memberships = _fetch_user_memberships(supabase, user_id=user_id)
    return any(
        row.get("organisation_id") == organisation_id and row.get("role") in roles
        for row in memberships
    )


def _group_user_role(supabase, *, user_id: str, reporting_group_id: str) -> Optional[str]:
    row = _first(
        (
            supabase.table("reporting_group_users")
            .select("role, status")
            .eq("reporting_group_id", reporting_group_id)
            .eq("user_id", user_id)
            .eq("status", "active")
            .execute()
        ).data
    )
    return row.get("role") if row else None


def user_can_read_reporting_group(supabase, *, user_id: str, reporting_group_id: str) -> bool:
    group = _fetch_by_id(supabase, "reporting_groups", reporting_group_id)
    if not group:
        return False

    memberships = _fetch_user_memberships(supabase, user_id=user_id)
    org_ids = {row.get("organisation_id") for row in memberships}
    if group.get("owner_organisation_id") in org_ids:
        return True

    if _group_user_role(supabase, user_id=user_id, reporting_group_id=reporting_group_id):
        return True

    if not org_ids:
        return False

    linked = (
        supabase.table("reporting_group_entities")
        .select("id")
        .eq("reporting_group_id", reporting_group_id)
        .in_("organisation_id", list(org_ids))
        .execute()
    ).data or []
    return bool(linked)


def user_can_write_reporting_group(supabase, *, user_id: str, reporting_group_id: str) -> bool:
    group = _fetch_by_id(supabase, "reporting_groups", reporting_group_id)
    if not group:
        return False

    if user_has_org_role(
        supabase,
        user_id=user_id,
        organisation_id=group["owner_organisation_id"],
        roles=WRITE_ROLES,
    ):
        return True

    return (_group_user_role(supabase, user_id=user_id, reporting_group_id=reporting_group_id) in GROUP_WRITE_ROLES)


def user_can_admin_reporting_group(supabase, *, user_id: str, reporting_group_id: str) -> bool:
    group = _fetch_by_id(supabase, "reporting_groups", reporting_group_id)
    if not group:
        return False

    if user_has_org_role(
        supabase,
        user_id=user_id,
        organisation_id=group["owner_organisation_id"],
        roles={"owner", "admin"},
    ):
        return True

    return (_group_user_role(supabase, user_id=user_id, reporting_group_id=reporting_group_id) in GROUP_ADMIN_ROLES)


def require_group_read(supabase, *, user_id: str, reporting_group_id: str) -> None:
    if not user_can_read_reporting_group(supabase, user_id=user_id, reporting_group_id=reporting_group_id):
        raise ConsolidationAccessError("Reporting group is not available to this user")


def require_group_write(supabase, *, user_id: str, reporting_group_id: str) -> None:
    if not user_can_write_reporting_group(supabase, user_id=user_id, reporting_group_id=reporting_group_id):
        raise ConsolidationAccessError("User cannot edit this reporting group")


def _period_allows_edit(supabase, *, user_id: str, period_id: str) -> None:
    period = _fetch_by_id(supabase, "consolidation_periods", period_id)
    if not period:
        raise ConsolidationValidationError("Consolidation period not found")

    status = period.get("status")
    if status in {"locked", "closed"} and not user_can_admin_reporting_group(
        supabase,
        user_id=user_id,
        reporting_group_id=period["reporting_group_id"],
    ):
        raise ConsolidationAccessError("Consolidation period is locked")


def list_reporting_groups(supabase, *, user_id: str) -> list[dict]:
    memberships = _fetch_user_memberships(supabase, user_id=user_id)
    org_ids = {row.get("organisation_id") for row in memberships if row.get("organisation_id")}

    group_ids: set[str] = set()
    if org_ids:
        owned = (
            supabase.table("reporting_groups")
            .select("*")
            .in_("owner_organisation_id", list(org_ids))
            .execute()
        ).data or []
        group_ids.update(row["id"] for row in owned)

        linked = (
            supabase.table("reporting_group_entities")
            .select("reporting_group_id")
            .in_("organisation_id", list(org_ids))
            .execute()
        ).data or []
        group_ids.update(row["reporting_group_id"] for row in linked)

    assigned = (
        supabase.table("reporting_group_users")
        .select("reporting_group_id")
        .eq("user_id", user_id)
        .eq("status", "active")
        .execute()
    ).data or []
    group_ids.update(row["reporting_group_id"] for row in assigned)

    if not group_ids:
        return []

    groups = (
        supabase.table("reporting_groups")
        .select("*")
        .in_("id", list(group_ids))
        .execute()
    ).data or []
    return sorted(groups, key=lambda row: (row.get("name") or "").lower())


def create_reporting_group(supabase, *, user_id: str, payload: dict) -> dict:
    owner_org_id = payload.get("owner_organisation_id")
    if not owner_org_id:
        raise ConsolidationValidationError("owner_organisation_id is required")

    if not user_has_org_role(supabase, user_id=user_id, organisation_id=owner_org_id, roles=WRITE_ROLES):
        raise ConsolidationAccessError("User cannot create reporting groups for this organisation")

    row = {
        "owner_organisation_id": owner_org_id,
        "name": payload.get("name"),
        "reporting_currency": payload.get("reporting_currency") or "ZAR",
        "country": payload.get("country"),
        "status": payload.get("status") or "active",
        "created_by": user_id,
    }
    if not row["name"]:
        raise ConsolidationValidationError("name is required")

    return _first((supabase.table("reporting_groups").insert(row).execute()).data) or row


def create_group_entity(supabase, *, user_id: str, reporting_group_id: str, payload: dict) -> dict:
    require_group_write(supabase, user_id=user_id, reporting_group_id=reporting_group_id)

    row = {
        "reporting_group_id": reporting_group_id,
        "parent_entity_id": payload.get("parent_entity_id"),
        "organisation_id": payload.get("organisation_id"),
        "entity_type": payload.get("entity_type"),
        "ownership_percent": payload.get("ownership_percent", 100),
        "consolidation_method": payload.get("consolidation_method"),
        "effective_from": payload.get("effective_from") or date.today().isoformat(),
        "effective_to": payload.get("effective_to"),
        "sort_order": payload.get("sort_order") or 0,
    }
    if not row["organisation_id"] or not row["entity_type"] or not row["consolidation_method"]:
        raise ConsolidationValidationError("organisation_id, entity_type, and consolidation_method are required")
    return _first((supabase.table("reporting_group_entities").insert(row).execute()).data) or row


def create_period(supabase, *, user_id: str, reporting_group_id: str, payload: dict) -> dict:
    require_group_write(supabase, user_id=user_id, reporting_group_id=reporting_group_id)
    group = _fetch_by_id(supabase, "reporting_groups", reporting_group_id) or {}
    row = {
        "reporting_group_id": reporting_group_id,
        "name": payload.get("name"),
        "start_date": payload.get("start_date"),
        "end_date": payload.get("end_date"),
        "reporting_currency": payload.get("reporting_currency") or group.get("reporting_currency") or "ZAR",
        "status": payload.get("status") or "draft",
    }
    if not row["name"] or not row["start_date"] or not row["end_date"]:
        raise ConsolidationValidationError("name, start_date, and end_date are required")
    return _first((supabase.table("consolidation_periods").insert(row).execute()).data) or row


def upsert_account_mapping(supabase, *, user_id: str, reporting_group_id: str, payload: dict) -> dict:
    require_group_write(supabase, user_id=user_id, reporting_group_id=reporting_group_id)
    row = {
        "reporting_group_id": reporting_group_id,
        "entity_organisation_id": payload.get("entity_organisation_id"),
        "local_account_id": payload.get("local_account_id"),
        "group_account_id": payload.get("group_account_id"),
        "effective_from": payload.get("effective_from") or date.today().isoformat(),
        "effective_to": payload.get("effective_to"),
    }
    if not row["entity_organisation_id"] or not row["local_account_id"] or not row["group_account_id"]:
        raise ConsolidationValidationError("entity_organisation_id, local_account_id, and group_account_id are required")
    return _first((supabase.table("consolidation_account_mappings").insert(row).execute()).data) or row


def create_exchange_rate(supabase, *, user_id: str, reporting_group_id: str, payload: dict) -> dict:
    require_group_write(supabase, user_id=user_id, reporting_group_id=reporting_group_id)
    row = {
        "reporting_group_id": reporting_group_id,
        "period_id": payload.get("period_id"),
        "from_currency": payload.get("from_currency"),
        "to_currency": payload.get("to_currency"),
        "rate_type": payload.get("rate_type") or "closing",
        "rate_date": payload.get("rate_date"),
        "rate": payload.get("rate"),
        "source": payload.get("source"),
    }
    if not row["from_currency"] or not row["to_currency"] or not row["rate_date"] or row["rate"] is None:
        raise ConsolidationValidationError("from_currency, to_currency, rate_date, and rate are required")
    return _first((supabase.table("exchange_rates").insert(row).execute()).data) or row


def create_adjustment(supabase, *, user_id: str, reporting_group_id: str, payload: dict) -> dict:
    require_group_write(supabase, user_id=user_id, reporting_group_id=reporting_group_id)
    period_id = payload.get("period_id")
    if not period_id:
        raise ConsolidationValidationError("period_id is required")
    _period_allows_edit(supabase, user_id=user_id, period_id=period_id)

    lines = payload.get("lines") or []
    if len(lines) < 2:
        raise ConsolidationValidationError("At least two adjustment lines are required")

    debit_total = sum(_decimal(line.get("debit_amount")) for line in lines)
    credit_total = sum(_decimal(line.get("credit_amount")) for line in lines)
    if debit_total != credit_total:
        raise ConsolidationValidationError("Consolidation adjustment does not balance")

    adjustment = _first(
        (
            supabase.table("consolidation_adjustments")
            .insert({
                "reporting_group_id": reporting_group_id,
                "period_id": period_id,
                "adjustment_type": payload.get("adjustment_type") or "manual",
                "description": payload.get("description"),
                "status": "draft",
                "created_by": user_id,
            })
            .execute()
        ).data
    )
    if not adjustment:
        raise ConsolidationValidationError("Failed to create adjustment")

    line_rows = []
    for idx, line in enumerate(lines, start=1):
        line_rows.append({
            "adjustment_id": adjustment["id"],
            "line_number": line.get("line_number") or idx,
            "account_id": line.get("account_id"),
            "entity_organisation_id": line.get("entity_organisation_id"),
            "description": line.get("description"),
            "debit_amount": line.get("debit_amount") or 0,
            "credit_amount": line.get("credit_amount") or 0,
        })
    supabase.table("consolidation_adjustment_lines").insert(line_rows).execute()
    adjustment["lines"] = line_rows
    return adjustment


def post_adjustment(supabase, *, user_id: str, adjustment_id: str) -> dict:
    adjustment = _fetch_by_id(supabase, "consolidation_adjustments", adjustment_id)
    if not adjustment:
        raise ConsolidationValidationError("Adjustment not found")
    require_group_write(supabase, user_id=user_id, reporting_group_id=adjustment["reporting_group_id"])
    _period_allows_edit(supabase, user_id=user_id, period_id=adjustment["period_id"])
    if adjustment.get("status") != "draft":
        raise ConsolidationValidationError("Only draft adjustments can be posted")

    updated = {
        "status": "posted",
        "posted_by": user_id,
        "posted_at": utc_now_iso(),
    }
    return _first(
        (
            supabase.table("consolidation_adjustments")
            .update(updated)
            .eq("id", adjustment_id)
            .execute()
        ).data
    ) or {**adjustment, **updated}


def _effective_entities(entities: list[dict], period: dict) -> list[dict]:
    start = str(period.get("start_date") or "")
    end = str(period.get("end_date") or "")
    result = []
    for entity in entities:
        effective_from = str(entity.get("effective_from") or "")
        effective_to = entity.get("effective_to")
        if effective_from and effective_from > end:
            continue
        if effective_to and str(effective_to) < start:
            continue
        result.append(entity)
    return result


def _method_factor(entity: dict) -> Decimal:
    method = entity.get("consolidation_method")
    if method == "full":
        return Decimal("1")
    if method == "proportionate":
        return _pct_factor(entity.get("ownership_percent"))
    return Decimal("0")


def _rate_lookup(rates: list[dict]) -> dict[tuple[str, str, str], Decimal]:
    lookup = {}
    for rate in rates:
        lookup[
            (
                rate.get("from_currency"),
                rate.get("to_currency"),
                rate.get("rate_type") or "closing",
            )
        ] = _decimal(rate.get("rate"))
    return lookup


def _get_rate(
    rates: dict[tuple[str, str, str], Decimal],
    *,
    from_currency: str,
    to_currency: str,
    rate_type: str,
) -> Decimal:
    if from_currency == to_currency:
        return Decimal("1")
    rate = rates.get((from_currency, to_currency, rate_type))
    if rate is None:
        raise ConsolidationValidationError(f"Missing {rate_type} FX rate {from_currency}->{to_currency}")
    return rate


def consolidated_trial_balance(
    supabase,
    *,
    user_id: str,
    reporting_group_id: str,
    period_id: str,
    rate_type: str = "closing",
) -> dict:
    require_group_read(supabase, user_id=user_id, reporting_group_id=reporting_group_id)

    group = _fetch_by_id(supabase, "reporting_groups", reporting_group_id)
    period = _fetch_by_id(supabase, "consolidation_periods", period_id)
    if not group or not period or period.get("reporting_group_id") != reporting_group_id:
        raise ConsolidationValidationError("Consolidation period not found for reporting group")

    reporting_currency = period.get("reporting_currency") or group.get("reporting_currency") or "ZAR"
    entities = _effective_entities(
        (
            supabase.table("reporting_group_entities")
            .select("*")
            .eq("reporting_group_id", reporting_group_id)
            .execute()
        ).data or [],
        period,
    )
    entity_by_org = {entity["organisation_id"]: entity for entity in entities}

    mappings = (
        supabase.table("consolidation_account_mappings")
        .select("*")
        .eq("reporting_group_id", reporting_group_id)
        .execute()
    ).data or []
    account_mapping = {
        (row["entity_organisation_id"], row["local_account_id"]): row["group_account_id"]
        for row in mappings
    }

    rates = _rate_lookup(
        (
            supabase.table("exchange_rates")
            .select("*")
            .eq("reporting_group_id", reporting_group_id)
            .eq("period_id", period_id)
            .execute()
        ).data or []
    )

    balances = (
        supabase.table("consolidation_entity_balances")
        .select("*")
        .eq("reporting_group_id", reporting_group_id)
        .eq("period_id", period_id)
        .execute()
    ).data or []

    totals: dict[str, Decimal] = defaultdict(Decimal)
    entity_contributions = []
    skipped_entities = []

    for row in balances:
        entity = entity_by_org.get(row.get("entity_organisation_id"))
        if not entity:
            continue

        factor = _method_factor(entity)
        if factor == 0:
            skipped_entities.append({
                "entity_organisation_id": row.get("entity_organisation_id"),
                "consolidation_method": entity.get("consolidation_method"),
                "account_id": row.get("account_id"),
            })
            continue

        fx_rate = _get_rate(
            rates,
            from_currency=row.get("currency") or reporting_currency,
            to_currency=reporting_currency,
            rate_type=rate_type,
        )
        balance = (_decimal(row.get("debit_amount")) - _decimal(row.get("credit_amount"))) * factor * fx_rate
        group_account_id = account_mapping.get(
            (row.get("entity_organisation_id"), row.get("account_id")),
            row.get("account_id"),
        )
        totals[group_account_id] += balance
        entity_contributions.append({
            "entity_organisation_id": row.get("entity_organisation_id"),
            "local_account_id": row.get("account_id"),
            "group_account_id": group_account_id,
            "consolidation_method": entity.get("consolidation_method"),
            "ownership_percent": _money(_decimal(entity.get("ownership_percent"))),
            "applied_factor": _money(factor),
            "fx_rate": _money(fx_rate),
            "balance": _money(balance),
            "non_controlling_interest_balance": _money(
                (_decimal(row.get("debit_amount")) - _decimal(row.get("credit_amount")))
                * (Decimal("1") - _pct_factor(entity.get("ownership_percent")))
                * fx_rate
            ) if entity.get("consolidation_method") == "full" else 0.0,
        })

    posted_adjustments = (
        supabase.table("consolidation_adjustments")
        .select("*")
        .eq("reporting_group_id", reporting_group_id)
        .eq("period_id", period_id)
        .eq("status", "posted")
        .execute()
    ).data or []
    adjustment_ids = _in(row.get("id") for row in posted_adjustments)
    adjustment_lines = []
    if adjustment_ids:
        adjustment_lines = (
            supabase.table("consolidation_adjustment_lines")
            .select("*")
            .in_("adjustment_id", adjustment_ids)
            .execute()
        ).data or []
        for line in adjustment_lines:
            totals[line["account_id"]] += _decimal(line.get("debit_amount")) - _decimal(line.get("credit_amount"))

    lines = []
    for account_id, balance in sorted(totals.items(), key=lambda item: item[0]):
        lines.append({
            "account_id": account_id,
            "debit_amount": _money(balance if balance > 0 else Decimal("0")),
            "credit_amount": _money(-balance if balance < 0 else Decimal("0")),
            "balance": _money(balance),
            "reporting_currency": reporting_currency,
        })

    debit_total = sum(_decimal(line["debit_amount"]) for line in lines)
    credit_total = sum(_decimal(line["credit_amount"]) for line in lines)
    return {
        "reporting_group_id": reporting_group_id,
        "period_id": period_id,
        "reporting_currency": reporting_currency,
        "rate_type": rate_type,
        "lines": lines,
        "summary": {
            "debit_total": _money(debit_total),
            "credit_total": _money(credit_total),
            "out_of_balance": _money(debit_total - credit_total),
            "entity_balance_rows": len(balances),
            "posted_adjustments": len(posted_adjustments),
            "posted_adjustment_lines": len(adjustment_lines),
        },
        "entity_contributions": entity_contributions,
        "skipped_entities": skipped_entities,
    }
