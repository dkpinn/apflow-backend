from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.dependencies import authenticated_user
from app.services.consolidation import (
    ConsolidationAccessError,
    ConsolidationValidationError,
    consolidated_trial_balance,
    create_adjustment,
    create_exchange_rate,
    create_group_entity,
    create_period,
    create_reporting_group,
    list_reporting_groups,
    post_adjustment,
    require_group_read,
    upsert_account_mapping,
)

router = APIRouter(prefix="/api/consolidation", tags=["consolidation"])

UserAuth = Annotated[tuple, Depends(authenticated_user)]


class ReportingGroupRequest(BaseModel):
    owner_organisation_id: str
    name: str
    reporting_currency: str = "ZAR"
    country: Optional[str] = None
    status: str = "active"


class ReportingGroupEntityRequest(BaseModel):
    parent_entity_id: Optional[str] = None
    organisation_id: str
    entity_type: str
    ownership_percent: float = 100
    consolidation_method: str
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    sort_order: int = 0


class ConsolidationPeriodRequest(BaseModel):
    name: str
    start_date: str
    end_date: str
    reporting_currency: Optional[str] = None
    status: str = "draft"


class AccountMappingRequest(BaseModel):
    entity_organisation_id: str
    local_account_id: str
    group_account_id: str
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None


class ExchangeRateRequest(BaseModel):
    period_id: Optional[str] = None
    from_currency: str
    to_currency: str
    rate_type: str = "closing"
    rate_date: str
    rate: float
    source: Optional[str] = None


class AdjustmentLineRequest(BaseModel):
    line_number: Optional[int] = None
    account_id: str
    entity_organisation_id: Optional[str] = None
    description: Optional[str] = None
    debit_amount: float = 0
    credit_amount: float = 0


class AdjustmentRequest(BaseModel):
    period_id: str
    adjustment_type: str = "manual"
    description: str
    lines: list[AdjustmentLineRequest]


def _handle_error(exc: Exception) -> None:
    if isinstance(exc, ConsolidationAccessError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, ConsolidationValidationError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("/groups")
def get_reporting_groups(auth: UserAuth) -> dict:
    user_id, db = auth
    return {"reporting_groups": list_reporting_groups(db, user_id=user_id)}


@router.post("/groups")
def create_group(
    payload: ReportingGroupRequest,
    auth: UserAuth,
) -> dict:
    user_id, db = auth
    try:
        return create_reporting_group(db, user_id=user_id, payload=payload.model_dump())
    except Exception as exc:
        _handle_error(exc)


@router.get("/groups/{reporting_group_id}/entities")
def get_group_entities(
    reporting_group_id: str,
    auth: UserAuth,
) -> dict:
    user_id, db = auth
    try:
        require_group_read(db, user_id=user_id, reporting_group_id=reporting_group_id)
        rows = (
            db.table("reporting_group_entities")
            .select("*")
            .eq("reporting_group_id", reporting_group_id)
            .execute()
        ).data or []
        return {"entities": rows}
    except Exception as exc:
        _handle_error(exc)


@router.post("/groups/{reporting_group_id}/entities")
def add_group_entity(
    reporting_group_id: str,
    payload: ReportingGroupEntityRequest,
    auth: UserAuth,
) -> dict:
    user_id, db = auth
    try:
        return create_group_entity(
            db,
            user_id=user_id,
            reporting_group_id=reporting_group_id,
            payload=payload.model_dump(),
        )
    except Exception as exc:
        _handle_error(exc)


@router.post("/groups/{reporting_group_id}/periods")
def add_period(
    reporting_group_id: str,
    payload: ConsolidationPeriodRequest,
    auth: UserAuth,
) -> dict:
    user_id, db = auth
    try:
        return create_period(db, user_id=user_id, reporting_group_id=reporting_group_id, payload=payload.model_dump())
    except Exception as exc:
        _handle_error(exc)


@router.post("/groups/{reporting_group_id}/account-mappings")
def add_account_mapping(
    reporting_group_id: str,
    payload: AccountMappingRequest,
    auth: UserAuth,
) -> dict:
    user_id, db = auth
    try:
        return upsert_account_mapping(
            db,
            user_id=user_id,
            reporting_group_id=reporting_group_id,
            payload=payload.model_dump(),
        )
    except Exception as exc:
        _handle_error(exc)


@router.post("/groups/{reporting_group_id}/exchange-rates")
def add_exchange_rate(
    reporting_group_id: str,
    payload: ExchangeRateRequest,
    auth: UserAuth,
) -> dict:
    user_id, db = auth
    try:
        return create_exchange_rate(
            db,
            user_id=user_id,
            reporting_group_id=reporting_group_id,
            payload=payload.model_dump(),
        )
    except Exception as exc:
        _handle_error(exc)


@router.post("/groups/{reporting_group_id}/adjustments")
def add_adjustment(
    reporting_group_id: str,
    payload: AdjustmentRequest,
    auth: UserAuth,
) -> dict:
    user_id, db = auth
    try:
        return create_adjustment(
            db,
            user_id=user_id,
            reporting_group_id=reporting_group_id,
            payload=payload.model_dump(),
        )
    except Exception as exc:
        _handle_error(exc)


@router.post("/adjustments/{adjustment_id}/post")
def post_consolidation_adjustment(
    adjustment_id: str,
    auth: UserAuth,
) -> dict:
    user_id, db = auth
    try:
        return post_adjustment(db, user_id=user_id, adjustment_id=adjustment_id)
    except Exception as exc:
        _handle_error(exc)


@router.get("/reports/trial-balance")
def trial_balance_report(
    auth: UserAuth,
    reporting_group_id: str = Query(...),
    period_id: str = Query(...),
    rate_type: str = Query(default="closing"),
) -> dict:
    user_id, db = auth
    try:
        return consolidated_trial_balance(
            db,
            user_id=user_id,
            reporting_group_id=reporting_group_id,
            period_id=period_id,
            rate_type=rate_type,
        )
    except Exception as exc:
        _handle_error(exc)
