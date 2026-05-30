from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.integration_service import (
    create_organisation_integration,
    delete_organisation_integration,
    list_organisation_integrations,
    update_organisation_integration,
)

router = APIRouter(prefix="/api/organisations/{organisation_id}/integrations", tags=["organisation-integrations"])


class OrganisationIntegrationCreateRequest(BaseModel):
    provider: str
    capability: str
    display_name: str
    api_key: Optional[str] = Field(default=None, repr=False)
    enabled: bool = True
    model: Optional[str] = None
    base_url: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)


class OrganisationIntegrationUpdateRequest(BaseModel):
    provider: Optional[str] = None
    capability: Optional[str] = None
    display_name: Optional[str] = None
    api_key: Optional[str] = Field(default=None, repr=False)
    enabled: Optional[bool] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    config: Optional[dict[str, Any]] = None


@router.get("")
def get_organisation_integrations(organisation_id: str, auth: UserAuth) -> dict:
    user_id, _user_db = auth
    ensure_org_read(user_id, organisation_id)
    db = get_supabase_client()
    return {"integrations": list_organisation_integrations(db, organisation_id)}


@router.post("")
def add_organisation_integration(
    organisation_id: str,
    payload: OrganisationIntegrationCreateRequest,
    auth: UserAuth,
) -> dict:
    user_id, _user_db = auth
    ensure_org_write(user_id, organisation_id)
    db = get_supabase_client()
    integration = create_organisation_integration(
        db,
        organisation_id,
        payload.model_dump(exclude_unset=True),
        actor_user_id=user_id,
    )
    return {"integration": integration}


@router.patch("/{integration_id}")
def patch_organisation_integration(
    organisation_id: str,
    integration_id: str,
    payload: OrganisationIntegrationUpdateRequest,
    auth: UserAuth,
) -> dict:
    user_id, _user_db = auth
    ensure_org_write(user_id, organisation_id)
    db = get_supabase_client()
    integration = update_organisation_integration(
        db,
        organisation_id,
        integration_id,
        payload.model_dump(exclude_unset=True),
        actor_user_id=user_id,
    )
    return {"integration": integration}


@router.delete("/{integration_id}")
def remove_organisation_integration(organisation_id: str, integration_id: str, auth: UserAuth) -> dict:
    user_id, _user_db = auth
    ensure_org_write(user_id, organisation_id)
    db = get_supabase_client()
    return delete_organisation_integration(
        db,
        organisation_id,
        integration_id,
        actor_user_id=user_id,
    )
