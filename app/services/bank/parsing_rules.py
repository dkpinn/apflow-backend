from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException

from app.schemas.bank import ParsingRuleCreate, ParsingRuleUpdate


def _one(res, message: str):
    data = getattr(res, "data", None) or []
    if not data:
        raise HTTPException(status_code=404, detail=message)
    return data[0]


def lookup_parsing_hint(
    db,
    *,
    organisation_id: str,
    institution_name: Optional[str],
    account_type: Optional[str],
) -> Optional[str]:
    try:
        rules = (
            db.table("bank_parsing_rules")
            .select("institution_name, account_type, parsing_hint")
            .eq("organisation_id", organisation_id)
            .eq("active", True)
            .execute()
            .data
            or []
        )
    except Exception:
        return None

    institution = (institution_name or "").strip().lower()
    acct_type = (account_type or "").strip().lower()

    def _inst_match(rule_inst: str, acct_inst: str) -> bool:
        if not rule_inst or not acct_inst:
            return False
        return rule_inst == acct_inst or rule_inst in acct_inst or acct_inst in rule_inst

    def specificity(rule: dict[str, Any]) -> int:
        rule_institution = (rule.get("institution_name") or "").strip().lower()
        rule_account_type = (rule.get("account_type") or "").strip().lower()
        institution_match = _inst_match(rule_institution, institution)
        account_type_match = bool(rule_account_type) and rule_account_type == acct_type
        if institution_match and account_type_match:
            return 3
        if institution_match and not rule_account_type:
            return 2
        if account_type_match and not rule_institution:
            return 1
        if not rule_institution and not rule_account_type:
            return 0
        return -1

    candidates = [(specificity(rule), rule) for rule in rules]
    candidates = [(score, rule) for score, rule in candidates if score >= 0]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, best_rule = candidates[0]
    return best_rule.get("parsing_hint") or None


def list_rules(db, *, organisation_id: str) -> list[dict[str, Any]]:
    res = (
        db.table("bank_parsing_rules")
        .select("*")
        .eq("organisation_id", organisation_id)
        .order("institution_name")
        .execute()
    )
    return res.data or []


def create_rule(
    db,
    *,
    payload: ParsingRuleCreate,
    organisation_id: str,
    user_id: str,
) -> dict[str, Any]:
    row = {
        "organisation_id": organisation_id,
        "institution_name": payload.institution_name,
        "account_type": payload.account_type,
        "parsing_hint": payload.parsing_hint,
        "active": payload.active,
        "created_by": user_id,
    }
    return _one(db.table("bank_parsing_rules").insert(row).execute(), "Parsing rule create failed")


def update_rule(
    db,
    *,
    rule_id: str,
    payload: ParsingRuleUpdate,
    organisation_id: str,
    updated_at: str,
) -> dict[str, Any]:
    _one(
        db.table("bank_parsing_rules")
        .select("id")
        .eq("id", rule_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Parsing rule not found",
    )
    patch: dict[str, Any] = {"updated_at": updated_at}
    if payload.institution_name is not None:
        patch["institution_name"] = payload.institution_name
    if payload.account_type is not None:
        patch["account_type"] = payload.account_type
    if payload.parsing_hint is not None:
        patch["parsing_hint"] = payload.parsing_hint
    if payload.active is not None:
        patch["active"] = payload.active
    res = (
        db.table("bank_parsing_rules")
        .update(patch)
        .eq("id", rule_id)
        .eq("organisation_id", organisation_id)
        .execute()
    )
    return _one(res, "Parsing rule update failed")


def delete_rule(db, *, rule_id: str, organisation_id: str) -> None:
    _one(
        db.table("bank_parsing_rules")
        .select("id")
        .eq("id", rule_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Parsing rule not found",
    )
    db.table("bank_parsing_rules").delete().eq("id", rule_id).eq("organisation_id", organisation_id).execute()
