from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.dependencies import UserAuth, ensure_org_admin, ensure_org_read
from app.services.asset_types import (
    create_asset_type,
    get_asset_type,
    list_asset_types,
    preview_asset_type_removal,
    remove_asset_type,
    restore_asset_type,
    update_asset_type,
)


router = APIRouter(prefix="/api/asset-types", tags=["asset-types"])


class ManagedAssetAccount(BaseModel):
    id: str
    code: Optional[str] = None
    name: str
    type: Literal["asset", "expense"]
    group_name: Optional[str] = None
    active: bool
    vat_treatment: Optional[str] = None
    is_system: bool
    system_key: Optional[str] = None
    managed_asset_type_id: str
    asset_account_role: Literal["cost", "accumulated", "expense"]
    income_statement_nature: Optional[str] = None


class AssetTypeAccounts(BaseModel):
    cost: ManagedAssetAccount
    accumulated: ManagedAssetAccount
    expense: ManagedAssetAccount


class AssetTypeResponse(BaseModel):
    id: str
    organisation_id: str
    name: str
    category: Literal["tangible", "intangible"]
    depreciation_method: Literal["straight_line"]
    useful_life_months: int
    residual_value_percent: float
    depreciation_convention: Literal["in_service_month"]
    active: bool
    archived_at: Optional[str] = None
    archived_by: Optional[str] = None
    cost_account_id: str
    accumulated_account_id: str
    expense_account_id: str
    created_by: Optional[str] = None
    created_at: str
    updated_at: str
    accounts: AssetTypeAccounts


class AssetTypeRequest(BaseModel):
    organisation_id: str
    name: str = Field(min_length=1, max_length=100)
    category: Literal["tangible", "intangible"]
    useful_life_months: int = Field(ge=1, le=1200)
    residual_value_percent: float = Field(default=0, ge=0, le=100)

    @field_validator("name")
    @classmethod
    def normalise_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Asset type name is required")
        return normalized


class AssetTypeRemovalPreview(BaseModel):
    asset_type_id: str
    asset_type_name: str
    action: Literal["blocked", "archive", "delete"]
    active_assets: int
    total_assets: int
    journal_lines: int
    account_mappings: int
    has_history: bool


def _asset_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if "not found" in message.lower():
        return HTTPException(status_code=404, detail=message)
    if "duplicate" in message.lower() or "already exists" in message.lower():
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=400, detail=message)


@router.get("", response_model=list[AssetTypeResponse])
def list_asset_type_settings(
    organisation_id: str,
    auth: UserAuth,
    include_archived: bool = False,
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        return list_asset_types(
            db,
            organisation_id=organisation_id,
            include_archived=include_archived,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unable to load asset types: {exc}") from exc


@router.post("", response_model=AssetTypeResponse, status_code=201)
def create_asset_type_setting(payload: AssetTypeRequest, auth: UserAuth):
    user_id, db = auth
    ensure_org_admin(str(user_id), payload.organisation_id)
    try:
        return create_asset_type(
            db,
            organisation_id=payload.organisation_id,
            name=payload.name,
            category=payload.category,
            useful_life_months=payload.useful_life_months,
            residual_value_percent=payload.residual_value_percent,
        )
    except Exception as exc:
        raise _asset_error(exc) from exc


@router.put("/{asset_type_id}", response_model=AssetTypeResponse)
def update_asset_type_setting(
    asset_type_id: str,
    payload: AssetTypeRequest,
    auth: UserAuth,
):
    user_id, db = auth
    ensure_org_admin(str(user_id), payload.organisation_id)
    try:
        return update_asset_type(
            db,
            organisation_id=payload.organisation_id,
            asset_type_id=asset_type_id,
            name=payload.name,
            category=payload.category,
            useful_life_months=payload.useful_life_months,
            residual_value_percent=payload.residual_value_percent,
        )
    except Exception as exc:
        raise _asset_error(exc) from exc


@router.post("/{asset_type_id}/removal-preview", response_model=AssetTypeRemovalPreview)
def preview_asset_type_setting_removal(
    asset_type_id: str,
    organisation_id: str,
    auth: UserAuth,
):
    user_id, db = auth
    ensure_org_admin(str(user_id), organisation_id)
    try:
        return preview_asset_type_removal(
            db,
            organisation_id=organisation_id,
            asset_type_id=asset_type_id,
        )
    except Exception as exc:
        raise _asset_error(exc) from exc


@router.delete("/{asset_type_id}", response_model=AssetTypeRemovalPreview)
def remove_asset_type_setting(
    asset_type_id: str,
    organisation_id: str,
    auth: UserAuth,
):
    user_id, db = auth
    ensure_org_admin(str(user_id), organisation_id)
    try:
        return remove_asset_type(
            db,
            organisation_id=organisation_id,
            asset_type_id=asset_type_id,
        )
    except Exception as exc:
        raise _asset_error(exc) from exc


@router.post("/{asset_type_id}/restore", response_model=AssetTypeResponse)
def restore_asset_type_setting(
    asset_type_id: str,
    organisation_id: str,
    auth: UserAuth,
):
    user_id, db = auth
    ensure_org_admin(str(user_id), organisation_id)
    try:
        return restore_asset_type(
            db,
            organisation_id=organisation_id,
            asset_type_id=asset_type_id,
        )
    except Exception as exc:
        raise _asset_error(exc) from exc
