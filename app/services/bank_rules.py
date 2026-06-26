from __future__ import annotations

from typing import Any

from app.services.bank_statement_service import (
    bank_rule_matches,
    normalize_rule_criteria,
    rule_criteria_rationale,
)


def list_bank_rules(
    db,
    *,
    organisation_id: str,
    bank_account_id: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    query = (
        db.table("bank_transaction_rules")
        .select("*")
        .eq("organisation_id", organisation_id)
        .order("priority")
        .order("name")
        .limit(1000)
    )
    if bank_account_id:
        query = query.eq("bank_account_id", bank_account_id)
    if not include_archived:
        query = query.eq("active", True)
    rules = query.execute().data or []

    accepted_rows = (
        db.table("bank_statement_lines")
        .select("accepted_rule_id, line_date, reviewed_at")
        .eq("organisation_id", organisation_id)
        .limit(10000)
        .execute()
        .data
        or []
    )
    usage: dict[str, dict[str, Any]] = {}
    for row in accepted_rows:
        rule_id = row.get("accepted_rule_id")
        if not rule_id:
            continue
        key = str(rule_id)
        stats = usage.setdefault(key, {"usage_count": 0, "last_used_at": None})
        stats["usage_count"] += 1
        used_at = row.get("reviewed_at") or row.get("line_date")
        if used_at and (not stats["last_used_at"] or str(used_at) > str(stats["last_used_at"])):
            stats["last_used_at"] = used_at

    enriched: list[dict[str, Any]] = []
    for rule in rules:
        stats = usage.get(str(rule.get("id"))) or {"usage_count": 0, "last_used_at": None}
        enriched.append(
            {
                **rule,
                "criteria": normalize_rule_criteria(rule.get("criteria")),
                "usage_count": stats["usage_count"],
                "last_used_at": stats["last_used_at"],
            }
        )
    return enriched


def preview_bank_rule_matches(
    db,
    *,
    organisation_id: str,
    rule: dict[str, Any],
    bank_account_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    query = (
        db.table("bank_statement_lines")
        .select(
            "id, bank_account_id, line_date, description, reference, bank_reference, "
            "counterparty, raw_text, signed_amount, match_status, allocation_status, posting_status"
        )
        .eq("organisation_id", organisation_id)
        .order("line_date", desc=True)
        .limit(max(1, min(limit, 500)))
    )
    if bank_account_id:
        query = query.eq("bank_account_id", bank_account_id)
    rows = query.execute().data or []
    matches = [
        {
            **row,
            "rationale": rule_criteria_rationale(rule),
        }
        for row in rows
        if bank_rule_matches(
            rule,
            bank_account_id=str(row.get("bank_account_id") or ""),
            line=row,
        )
    ]
    return {
        "rule_id": rule.get("id"),
        "tested_count": len(rows),
        "match_count": len(matches),
        "matches": matches,
    }
