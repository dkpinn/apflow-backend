from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_platform_owner
from app.services.integration_service import (
    create_system_integration,
    delete_system_integration,
    get_extraction_criteria,
    get_system_policy,
    list_system_integrations,
    test_system_integration,
    update_system_integration,
    update_system_policy,
    upsert_extraction_criteria,
)

router = APIRouter(prefix="/api/admin", tags=["admin-integrations"])


class SystemIntegrationCreateRequest(BaseModel):
    provider: str
    capability: str
    display_name: str
    api_key: Optional[str] = Field(default=None, repr=False)
    enabled: bool = True
    model: Optional[str] = None
    base_url: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)


class SystemIntegrationUpdateRequest(BaseModel):
    provider: Optional[str] = None
    capability: Optional[str] = None
    display_name: Optional[str] = None
    api_key: Optional[str] = Field(default=None, repr=False)
    enabled: Optional[bool] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    config: Optional[dict[str, Any]] = None


class SystemPolicyRequest(BaseModel):
    enabled: bool = True
    ordered_integration_ids: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class ExtractionCriteriaRequest(BaseModel):
    status: str = Field(default="draft", pattern="^(draft|published|archived)$")
    prompt_template: Optional[str] = None
    criteria: dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None


def _platform_db(auth: UserAuth):
    user_id, _user_db = auth
    ensure_platform_owner(user_id)
    return user_id, get_supabase_client()


@router.get("/system-integrations")
def get_system_integrations(auth: UserAuth) -> dict:
    _user_id, db = _platform_db(auth)
    return {"integrations": list_system_integrations(db)}


@router.get("/platform-settings")
def get_platform_settings(auth: UserAuth) -> dict:
    _user_id, db = _platform_db(auth)
    task = "invoice_vlm_extraction"
    return {
        "platform_owner": True,
        "system_integrations": list_system_integrations(db),
        "ai_policies": {
            task: get_system_policy(db, task),
        },
        "extraction_criteria": {
            task: get_extraction_criteria(db, task),
        },
    }


@router.post("/system-integrations")
def add_system_integration(payload: SystemIntegrationCreateRequest, auth: UserAuth) -> dict:
    user_id, db = _platform_db(auth)
    integration = create_system_integration(
        db,
        payload.model_dump(exclude_unset=True),
        actor_user_id=user_id,
    )
    return {"integration": integration}


@router.patch("/system-integrations/{integration_id}")
def patch_system_integration(
    integration_id: str,
    payload: SystemIntegrationUpdateRequest,
    auth: UserAuth,
) -> dict:
    user_id, db = _platform_db(auth)
    integration = update_system_integration(
        db,
        integration_id,
        payload.model_dump(exclude_unset=True),
        actor_user_id=user_id,
    )
    return {"integration": integration}


@router.delete("/system-integrations/{integration_id}")
def remove_system_integration(integration_id: str, auth: UserAuth) -> dict:
    user_id, db = _platform_db(auth)
    return delete_system_integration(db, integration_id, actor_user_id=user_id)


@router.post("/system-integrations/{integration_id}/test")
def run_system_integration_test(integration_id: str, auth: UserAuth) -> dict:
    user_id, db = _platform_db(auth)
    return test_system_integration(db, integration_id, actor_user_id=user_id)


@router.get("/ai-policies/{task}")
def get_ai_policy(task: str, auth: UserAuth) -> dict:
    _user_id, db = _platform_db(auth)
    return {"policy": get_system_policy(db, task)}


@router.put("/ai-policies/{task}")
def put_ai_policy(task: str, payload: SystemPolicyRequest, auth: UserAuth) -> dict:
    user_id, db = _platform_db(auth)
    policy = update_system_policy(
        db,
        task,
        payload.model_dump(exclude_unset=True),
        actor_user_id=user_id,
    )
    return {"policy": policy}


@router.get("/extraction-criteria/{task}")
def get_platform_extraction_criteria(task: str, auth: UserAuth) -> dict:
    _user_id, db = _platform_db(auth)
    return {"criteria": get_extraction_criteria(db, task)}


@router.put("/extraction-criteria/{task}")
def put_platform_extraction_criteria(task: str, payload: ExtractionCriteriaRequest, auth: UserAuth) -> dict:
    user_id, db = _platform_db(auth)
    criteria = upsert_extraction_criteria(
        db,
        task,
        payload.model_dump(exclude_unset=True),
        actor_user_id=user_id,
    )
    return {"criteria": criteria}
