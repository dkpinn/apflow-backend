from __future__ import annotations

from enum import Enum
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.supabase_client import get_supabase_client
from app.services.invoice_extraction_service._helpers import (
    get_organisation_extraction_settings,
    update_organisation_extraction_settings,
)

router = APIRouter(prefix="/api/organisations", tags=["organisations"])
try:
    supabase = get_supabase_client()
except Exception:
    supabase = None


class ExtractionStrategy(str, Enum):
    auto_group = "auto_group"
    vlm = "vlm"
    extract_all = "extract_all"


class OrganisationSettingsResponse(BaseModel):
    extraction_strategy: ExtractionStrategy
    ask_per_upload: bool
    vlm_enabled: bool


class UpdateOrganisationSettingsRequest(BaseModel):
    extraction_strategy: Optional[ExtractionStrategy] = Field(
        default=None,
        description="Which multi-document extraction strategy to use.",
    )
    ask_per_upload: Optional[bool] = Field(
        default=None,
        description="Whether to ask the user per upload which strategy to use.",
    )
    vlm_enabled: Optional[bool] = Field(
        default=None,
        description="Whether VLM-based boundary detection is enabled.",
    )


def _db():
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase credentials missing")
    return supabase


@router.get("/{organisation_id}/settings", response_model=OrganisationSettingsResponse)
def get_organisation_settings(organisation_id: str):
    db = _db()
    settings = get_organisation_extraction_settings(organisation_id)
    return settings


@router.put("/{organisation_id}/settings", response_model=OrganisationSettingsResponse)
def update_organisation_settings(
    organisation_id: str,
    payload: UpdateOrganisationSettingsRequest,
):
    updates: dict = {}
    if payload.extraction_strategy is not None:
        updates["extraction_strategy"] = payload.extraction_strategy
    if payload.ask_per_upload is not None:
        updates["ask_per_upload"] = payload.ask_per_upload
    if payload.vlm_enabled is not None:
        updates["vlm_enabled"] = payload.vlm_enabled

    if not updates:
        raise HTTPException(status_code=400, detail="No settings were provided to update")

    try:
        return update_organisation_extraction_settings(organisation_id, updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
