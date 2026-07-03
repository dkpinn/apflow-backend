from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.routers.bank import _auth, _one, log_bank_event, now_iso
from app.services.bank_rules import list_bank_rules, preview_bank_rule_matches
from app.services.bank_statement_service import normalize_rule_criteria
from app.services.protected_accounts import assert_manual_posting_account_allowed


router = APIRouter(prefix="/api/bank", tags=["bank"])

AmountDirection = Literal["any", "money_in", "money_out"]
MatchType = Literal["contains", "exact", "regex"]
CriteriaMode = Literal["and", "or", "only"]


def _clean_name(value: str | None) -> str | None:
    if value is None:
        return value
    clean = " ".join(value.split())
    if not clean:
        raise ValueError("Rule name is required")
    return clean


class BankRuleCreate(BaseModel):
    organisation_id: UUID
    bank_account_id: Optional[UUID] = None
    name: str = Field(min_length=1, max_length=200)
    active: bool = True
    priority: int = Field(default=0, ge=0, le=10000)
    amount_direction: AmountDirection = "any"
    match_type: MatchType = "contains"
    description_pattern: Optional[str] = Field(default=None, max_length=500)
    reference_pattern: Optional[str] = Field(default=None, max_length=500)
    counterparty_pattern: Optional[str] = Field(default=None, max_length=500)
    min_amount: Optional[Decimal] = Field(default=None, ge=0)
    max_amount: Optional[Decimal] = Field(default=None, ge=0)
    gl_account_id: Optional[UUID] = None
    split_allocations: Optional[list[dict[str, Any]]] = None
    supplier_id: Optional[UUID] = None
    auto_post: bool = False
    tracking: dict[str, Any] = Field(default_factory=dict)
    tax_treatment: Optional[str] = Field(default=None, max_length=100)
    notes: Optional[str] = Field(default=None, max_length=2000)
    criteria: list[dict[str, Any]] = Field(default_factory=list)
    criteria_mode: CriteriaMode = "and"
    source_bank_statement_line_id: Optional[UUID] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        result = _clean_name(value)
        if result is None:
            raise ValueError("Rule name is required")
        return result


class BankRuleUpdate(BaseModel):
    organisation_id: UUID
    bank_account_id: Optional[UUID] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    active: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=0, le=10000)
    amount_direction: Optional[AmountDirection] = None
    match_type: Optional[MatchType] = None
    description_pattern: Optional[str] = Field(default=None, max_length=500)
    reference_pattern: Optional[str] = Field(default=None, max_length=500)
    counterparty_pattern: Optional[str] = Field(default=None, max_length=500)
    min_amount: Optional[Decimal] = Field(default=None, ge=0)
    max_amount: Optional[Decimal] = Field(default=None, ge=0)
    gl_account_id: Optional[UUID] = None
    split_allocations: Optional[list[dict[str, Any]]] = None
    supplier_id: Optional[UUID] = None
    auto_post: Optional[bool] = None
    tracking: Optional[dict[str, Any]] = None
    tax_treatment: Optional[str] = Field(default=None, max_length=100)
    notes: Optional[str] = Field(default=None, max_length=2000)
    criteria: Optional[list[dict[str, Any]]] = None
    criteria_mode: Optional[CriteriaMode] = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return _clean_name(value)


class BankRuleTestRequest(BaseModel):
    organisation_id: UUID
    bank_account_id: Optional[UUID] = None
    limit: int = Field(default=100, ge=1, le=500)


@router.post("/rules")
def create_rule(payload: BankRuleCreate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)

    criteria = normalize_rule_criteria(payload.criteria)
    if not criteria and payload.criteria_mode != "only":
        raise HTTPException(status_code=400, detail="Add at least one rule condition")

    account_id = str(payload.gl_account_id) if payload.gl_account_id else None
    if account_id and not payload.split_allocations:
        try:
            assert_manual_posting_account_allowed(
                db,
                organisation_id=organisation_id,
                account_id=account_id,
                action="Create bank rule",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    row = {
        "organisation_id": organisation_id,
        "bank_account_id": str(payload.bank_account_id) if payload.bank_account_id else None,
        "name": payload.name,
        "active": payload.active,
        "priority": payload.priority,
        "amount_direction": payload.amount_direction,
        "match_type": payload.match_type,
        "description_pattern": payload.description_pattern,
        "reference_pattern": payload.reference_pattern,
        "counterparty_pattern": payload.counterparty_pattern,
        "min_amount": float(payload.min_amount) if payload.min_amount is not None else None,
        "max_amount": float(payload.max_amount) if payload.max_amount is not None else None,
        "gl_account_id": account_id,
        "split_allocations": payload.split_allocations,
        "supplier_id": str(payload.supplier_id) if payload.supplier_id else None,
        "auto_post": payload.auto_post,
        "tracking": payload.tracking,
        "tax_treatment": payload.tax_treatment,
        "notes": payload.notes,
        "criteria": criteria,
        "criteria_mode": payload.criteria_mode,
        "source_bank_statement_line_id": (
            str(payload.source_bank_statement_line_id)
            if payload.source_bank_statement_line_id
            else None
        ),
        "created_by": user_id,
    }
    result = db.table("bank_transaction_rules").insert(row).execute()
    rule = _one(result, "Bank rule create failed")
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_rule_created",
        actor_user_id=user_id,
        bank_account_id=row.get("bank_account_id"),
        rule_id=rule["id"],
        criteria=criteria,
        criteria_mode=payload.criteria_mode,
    )
    return {"success": True, "rule": rule}


@router.get("/rules")
def list_rules(
    organisation_id: str,
    auth: UserAuth,
    bank_account_id: Optional[str] = None,
    include_archived: bool = Query(default=False),
):
    user_id, db = _auth(auth)
    ensure_org_read(user_id, organisation_id)
    return {
        "success": True,
        "rules": list_bank_rules(
            db,
            organisation_id=organisation_id,
            bank_account_id=bank_account_id,
            include_archived=include_archived,
        ),
    }


@router.patch("/rules/{rule_id}")
def update_rule(rule_id: str, payload: BankRuleUpdate, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_write(user_id, organisation_id)
    _one(
        db.table("bank_transaction_rules")
        .select("id")
        .eq("id", rule_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank rule not found",
    )

    patch = payload.model_dump(mode="json", exclude={"organisation_id"}, exclude_none=True)
    if "criteria" in patch:
        patch["criteria"] = normalize_rule_criteria(patch.get("criteria"))
    if patch.get("gl_account_id") and not patch.get("split_allocations"):
        try:
            assert_manual_posting_account_allowed(
                db,
                organisation_id=organisation_id,
                account_id=str(patch["gl_account_id"]),
                action="Update bank rule",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not patch:
        raise HTTPException(status_code=400, detail="No rule changes supplied")
    patch["updated_at"] = now_iso()

    result = (
        db.table("bank_transaction_rules")
        .update(patch)
        .eq("id", rule_id)
        .eq("organisation_id", organisation_id)
        .execute()
    )
    rule = _one(result, "Bank rule update failed")
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_rule_updated",
        actor_user_id=user_id,
        bank_account_id=rule.get("bank_account_id"),
        rule_id=rule_id,
        changed_fields=sorted(patch.keys()),
    )
    return {"success": True, "rule": rule}


@router.post("/rules/{rule_id}/test")
def test_rule(rule_id: str, payload: BankRuleTestRequest, auth: UserAuth):
    user_id, db = _auth(auth)
    organisation_id = str(payload.organisation_id)
    ensure_org_read(user_id, organisation_id)
    rule = _one(
        db.table("bank_transaction_rules")
        .select("*")
        .eq("id", rule_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute(),
        "Bank rule not found",
    )
    result = preview_bank_rule_matches(
        db,
        organisation_id=organisation_id,
        rule=rule,
        bank_account_id=str(payload.bank_account_id) if payload.bank_account_id else None,
        limit=payload.limit,
    )
    log_bank_event(
        db,
        organisation_id=organisation_id,
        event_type="bank_rule_tested",
        actor_user_id=user_id,
        bank_account_id=str(payload.bank_account_id) if payload.bank_account_id else rule.get("bank_account_id"),
        rule_id=rule_id,
        tested_count=result["tested_count"],
        match_count=result["match_count"],
    )
    return {"success": True, **result}
