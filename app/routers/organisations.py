from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.db.supabase_client import get_supabase_client
from app.dependencies import UserAuth, ensure_org_admin, ensure_org_read
from app.services.invoice_extraction_service._helpers import (
    get_organisation_extraction_settings,
    update_organisation_extraction_settings,
)
from app.services.organisation_profile import get_organisation_profile
from app.services.organisation_module_settings import (
    MODULE_KEYS,
    get_module_settings,
    validate_required_dimensions,
)
from app.services.supplier_matching_config import (
    AutoLinkAmountTier,
    normalise_amount_tiers,
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


class ReportingStandard(str, Enum):
    ifrs = "ifrs"
    us_gaap = "us_gaap"
    uk_gaap_frs_102 = "uk_gaap_frs_102"
    aspe = "aspe"


class IncomeStatementPresentation(str, Enum):
    function = "function"
    nature = "nature"


class OrganisationSettingsResponse(BaseModel):
    extraction_strategy: ExtractionStrategy
    ask_per_upload: bool
    vlm_enabled: bool
    supplier_auto_link_min_matches: int = Field(default=2, ge=1, le=4)
    auto_link_amount_tiers: list[AutoLinkAmountTier] = Field(default_factory=list)
    reporting_standard: ReportingStandard = ReportingStandard.ifrs
    income_statement_presentation: IncomeStatementPresentation = IncomeStatementPresentation.function


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
    supplier_auto_link_min_matches: Optional[int] = Field(
        default=None,
        ge=1,
        le=4,
        description="How many supplier identity signals must match before auto-linking.",
    )
    auto_link_amount_tiers: Optional[list[AutoLinkAmountTier]] = Field(
        default=None,
        description="Per-amount-tier auto-link thresholds. Each entry: {max_amount: number|null, required_matches: 1-4}.",
    )
    reporting_standard: Optional[ReportingStandard] = Field(
        default=None,
        description="Default financial reporting framework for generated reports.",
    )
    income_statement_presentation: Optional[IncomeStatementPresentation] = Field(
        default=None,
        description="Default Income Statement expense presentation.",
    )

    @field_validator("auto_link_amount_tiers")
    @classmethod
    def validate_auto_link_amount_tiers(cls, value):
        if value is None:
            return None
        return normalise_amount_tiers(value)


ModuleKey = Literal[
    "supplier",
    "customer",
    "inventory",
    "bank_cash",
    "asset",
    "liability",
    "project",
]


class OrganisationModuleSettingResponse(BaseModel):
    module_key: ModuleKey
    tracking_enabled: bool
    required_tracking_dimension_ids: list[str] = Field(default_factory=list)


class UpdateOrganisationModuleSettingRequest(BaseModel):
    tracking_enabled: bool = False
    required_tracking_dimension_ids: list[str] = Field(default_factory=list)


class InvoiceBrandingResponse(BaseModel):
    logo_storage_path: Optional[str] = None
    primary_color: str = "#174EA6"
    accent_color: str = "#E8EEF9"
    text_color: str = "#111827"
    font_family: Literal["inter", "arial", "georgia", "times_new_roman", "roboto_mono"] = "inter"
    terms_and_conditions: str = ""
    bank_name: str = ""
    account_holder: str = ""
    account_number: str = ""
    account_type: str = ""
    branch_code: str = ""


class UpdateInvoiceBrandingRequest(BaseModel):
    logo_storage_path: Optional[str] = None
    primary_color: str = Field(default="#174EA6", pattern=r"^#[0-9A-Fa-f]{6}$")
    accent_color: str = Field(default="#E8EEF9", pattern=r"^#[0-9A-Fa-f]{6}$")
    text_color: str = Field(default="#111827", pattern=r"^#[0-9A-Fa-f]{6}$")
    font_family: Literal["inter", "arial", "georgia", "times_new_roman", "roboto_mono"] = "inter"
    terms_and_conditions: str = Field(default="", max_length=10000)
    bank_name: str = Field(default="", max_length=200)
    account_holder: str = Field(default="", max_length=200)
    account_number: str = Field(default="", max_length=100)
    account_type: str = Field(default="", max_length=100)
    branch_code: str = Field(default="", max_length=50)


def _db():
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase credentials missing")
    return supabase


@router.get("/{organisation_id}/settings", response_model=OrganisationSettingsResponse)
def get_organisation_settings(organisation_id: str, auth: UserAuth):
    user_id, _user_db = auth
    ensure_org_read(str(user_id), organisation_id)
    settings = get_organisation_extraction_settings(organisation_id)
    return settings


@router.put("/{organisation_id}/settings", response_model=OrganisationSettingsResponse)
def update_organisation_settings(
    organisation_id: str,
    payload: UpdateOrganisationSettingsRequest,
    auth: UserAuth,
):
    user_id, _user_db = auth
    ensure_org_admin(str(user_id), organisation_id)
    updates: dict = {}
    if payload.extraction_strategy is not None:
        updates["extraction_strategy"] = payload.extraction_strategy
    if payload.ask_per_upload is not None:
        updates["ask_per_upload"] = payload.ask_per_upload
    if payload.vlm_enabled is not None:
        updates["vlm_enabled"] = payload.vlm_enabled
    if payload.supplier_auto_link_min_matches is not None:
        updates["supplier_auto_link_min_matches"] = payload.supplier_auto_link_min_matches
    if payload.auto_link_amount_tiers is not None:
        updates["auto_link_amount_tiers"] = [
            tier.model_dump(mode="json") for tier in payload.auto_link_amount_tiers
        ]
    if payload.reporting_standard is not None:
        updates["reporting_standard"] = payload.reporting_standard
    if payload.income_statement_presentation is not None:
        updates["income_statement_presentation"] = payload.income_statement_presentation

    reporting_standard = updates.get("reporting_standard")
    presentation = updates.get("income_statement_presentation")
    if reporting_standard == ReportingStandard.us_gaap:
        if presentation == IncomeStatementPresentation.nature:
            raise HTTPException(
                status_code=400,
                detail="US GAAP requires the Income Statement presentation to be by function",
            )
        updates["income_statement_presentation"] = IncomeStatementPresentation.function
    elif presentation == IncomeStatementPresentation.nature and reporting_standard is None:
        current = get_organisation_extraction_settings(organisation_id)
        if current.get("reporting_standard") == ReportingStandard.us_gaap:
            raise HTTPException(
                status_code=400,
                detail="US GAAP requires the Income Statement presentation to be by function",
            )

    if not updates:
        raise HTTPException(status_code=400, detail="No settings were provided to update")

    try:
        return update_organisation_extraction_settings(organisation_id, updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/{organisation_id}/module-settings",
    response_model=list[OrganisationModuleSettingResponse],
)
def list_organisation_module_settings(organisation_id: str, auth: UserAuth):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        return get_module_settings(db, organisation_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unable to load module settings: {exc}") from exc


@router.put(
    "/{organisation_id}/module-settings/{module_key}",
    response_model=OrganisationModuleSettingResponse,
)
def update_organisation_module_setting(
    organisation_id: str,
    module_key: ModuleKey,
    payload: UpdateOrganisationModuleSettingRequest,
    auth: UserAuth,
):
    user_id, db = auth
    ensure_org_admin(str(user_id), organisation_id)
    if module_key not in MODULE_KEYS:
        raise HTTPException(status_code=400, detail="Unsupported organisation module")
    try:
        dimension_ids, _dimensions = validate_required_dimensions(
            db,
            organisation_id=organisation_id,
            tracking_enabled=payload.tracking_enabled,
            dimension_ids=payload.required_tracking_dimension_ids,
        )
        row = {
            "organisation_id": organisation_id,
            "module_key": module_key,
            "tracking_enabled": payload.tracking_enabled,
            "required_tracking_dimension_ids": dimension_ids,
        }
        result = (
            db.table("organisation_module_settings")
            .upsert(row, on_conflict="organisation_id,module_key")
            .execute()
        )
        saved = result.data[0] if result.data else row
        return {
            "module_key": module_key,
            "tracking_enabled": bool(saved.get("tracking_enabled")),
            "required_tracking_dimension_ids": [
                str(item) for item in (saved.get("required_tracking_dimension_ids") or [])
            ],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unable to save module settings: {exc}") from exc


def _default_invoice_branding() -> dict:
    return InvoiceBrandingResponse().model_dump()


@router.get(
    "/{organisation_id}/invoice-branding",
    response_model=InvoiceBrandingResponse,
)
def get_invoice_branding(organisation_id: str, auth: UserAuth):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        result = (
            db.table("organisation_invoice_branding")
            .select(
                "logo_storage_path, primary_color, accent_color, text_color, font_family, "
                "terms_and_conditions, bank_name, account_holder, account_number, "
                "account_type, branch_code"
            )
            .eq("organisation_id", organisation_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else _default_invoice_branding()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unable to load invoice branding: {exc}") from exc


@router.put(
    "/{organisation_id}/invoice-branding",
    response_model=InvoiceBrandingResponse,
)
def update_invoice_branding(
    organisation_id: str,
    payload: UpdateInvoiceBrandingRequest,
    auth: UserAuth,
):
    user_id, db = auth
    ensure_org_admin(str(user_id), organisation_id)
    logo_path = payload.logo_storage_path
    if logo_path and not logo_path.startswith(f"{organisation_id}/"):
        raise HTTPException(
            status_code=400,
            detail="The invoice logo must be stored inside this organisation's branding folder",
        )
    row = {
        "organisation_id": organisation_id,
        "logo_storage_path": logo_path,
        "primary_color": payload.primary_color.upper(),
        "accent_color": payload.accent_color.upper(),
        "text_color": payload.text_color.upper(),
        "font_family": payload.font_family,
        "terms_and_conditions": payload.terms_and_conditions.strip(),
        "bank_name": payload.bank_name.strip(),
        "account_holder": payload.account_holder.strip(),
        "account_number": payload.account_number.strip(),
        "account_type": payload.account_type.strip(),
        "branch_code": payload.branch_code.strip(),
    }
    try:
        result = (
            db.table("organisation_invoice_branding")
            .upsert(row, on_conflict="organisation_id")
            .execute()
        )
        saved = result.data[0] if result.data else row
        return {key: saved.get(key, value) for key, value in _default_invoice_branding().items()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unable to save invoice branding: {exc}") from exc


class OrganisationProfileResponse(BaseModel):
    legal_name: str = ""
    registration_number: str = ""
    vat_number: str = ""
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    postal_code: str = ""
    country: str = ""
    phone: str = ""
    email: str = ""


@router.get("/{organisation_id}/profile", response_model=OrganisationProfileResponse)
def get_org_profile(organisation_id: str, auth: UserAuth):
    user_id, _db = auth
    ensure_org_read(str(user_id), organisation_id)
    try:
        return get_organisation_profile(organisation_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unable to load organisation profile: {exc}") from exc
