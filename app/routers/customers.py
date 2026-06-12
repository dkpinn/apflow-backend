from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write


router = APIRouter(prefix="/api/customers", tags=["customers"])


class CustomerContactInput(BaseModel):
    id: Optional[str] = None
    contact_name: str = Field(min_length=1, max_length=200)
    email: Optional[str] = Field(default=None, max_length=320)
    phone: Optional[str] = Field(default=None, max_length=100)
    role_title: Optional[str] = Field(default=None, max_length=150)
    is_primary: bool = False


class CustomerInput(BaseModel):
    organisation_id: str
    customer_code: Optional[str] = Field(default=None, max_length=100)
    legal_name: str = Field(min_length=1, max_length=250)
    trading_name: Optional[str] = Field(default=None, max_length=250)
    vat_number: Optional[str] = Field(default=None, max_length=100)
    registration_number: Optional[str] = Field(default=None, max_length=100)
    billing_address: Optional[str] = Field(default=None, max_length=2000)
    delivery_address: Optional[str] = Field(default=None, max_length=2000)
    default_email: Optional[str] = Field(default=None, max_length=320)
    phone: Optional[str] = Field(default=None, max_length=100)
    payment_terms_days: int = Field(default=30, ge=0, le=3650)
    currency: str = Field(default="ZAR", min_length=3, max_length=3)
    default_revenue_account_id: Optional[str] = None
    default_vat_treatment: str = "standard"
    default_tracking: dict[str, str] = Field(default_factory=dict)
    active: bool = True
    contacts: list[CustomerContactInput] = Field(default_factory=list)

    @field_validator("legal_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("Customer legal name is required")
        return cleaned

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("default_vat_treatment")
    @classmethod
    def validate_vat_treatment(cls, value: str) -> str:
        if value not in {"standard", "zero_rated", "exempt"}:
            raise ValueError("Unsupported VAT treatment")
        return value


def _customer_row(payload: CustomerInput, user_id: str, *, create: bool) -> dict[str, Any]:
    row = payload.model_dump(exclude={"contacts"})
    row["customer_code"] = (payload.customer_code or "").strip() or None
    row["currency"] = payload.currency.upper()
    row["default_tracking"] = payload.default_tracking or {}
    row["updated_by"] = user_id
    if create:
        row.pop("updated_by", None)
        row["created_by"] = user_id
    return row


def _load_customer(db, organisation_id: str, customer_id: str) -> dict[str, Any]:
    result = (
        db.table("customers")
        .select("*")
        .eq("id", customer_id)
        .eq("organisation_id", organisation_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Customer not found")
    customer = result.data[0]
    customer["contacts"] = (
        db.table("customer_contacts")
        .select("*")
        .eq("customer_id", customer_id)
        .eq("organisation_id", organisation_id)
        .order("is_primary", desc=True)
        .order("contact_name")
        .execute()
        .data
        or []
    )
    return customer


def _replace_contacts(
    db,
    *,
    organisation_id: str,
    customer_id: str,
    contacts: list[CustomerContactInput],
) -> None:
    db.table("customer_contacts").delete().eq("customer_id", customer_id).eq(
        "organisation_id", organisation_id
    ).execute()
    rows = [
        {
            "organisation_id": organisation_id,
            "customer_id": customer_id,
            **contact.model_dump(exclude={"id"}),
        }
        for contact in contacts
    ]
    if rows:
        db.table("customer_contacts").insert(rows).execute()


@router.get("")
def list_customers(
    organisation_id: str,
    auth: UserAuth,
    include_archived: bool = False,
    search: Optional[str] = Query(default=None, max_length=200),
):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    query = (
        db.table("customers")
        .select("*")
        .eq("organisation_id", organisation_id)
        .order("legal_name")
        .limit(500)
    )
    if not include_archived:
        query = query.eq("active", True)
    rows = query.execute().data or []
    needle = (search or "").strip().lower()
    if needle:
        rows = [
            row
            for row in rows
            if needle
            in " ".join(
                str(row.get(key) or "").lower()
                for key in (
                    "legal_name",
                    "trading_name",
                    "customer_code",
                    "vat_number",
                    "default_email",
                )
            )
        ]
    return rows


@router.post("", status_code=201)
def create_customer(payload: CustomerInput, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    try:
        result = db.table("customers").insert(
            _customer_row(payload, str(user_id), create=True)
        ).execute()
        customer_id = str(result.data[0]["id"])
        _replace_contacts(
            db,
            organisation_id=payload.organisation_id,
            customer_id=customer_id,
            contacts=payload.contacts,
        )
        return _load_customer(db, payload.organisation_id, customer_id)
    except Exception as exc:
        message = str(exc)
        status = 409 if "duplicate" in message.lower() or "unique" in message.lower() else 400
        raise HTTPException(status_code=status, detail=message) from exc


@router.get("/{customer_id}")
def get_customer(customer_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = auth
    ensure_org_read(str(user_id), organisation_id)
    return _load_customer(db, organisation_id, customer_id)


@router.put("/{customer_id}")
def update_customer(customer_id: str, payload: CustomerInput, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), payload.organisation_id)
    current = _load_customer(db, payload.organisation_id, customer_id)
    if not current:
        raise HTTPException(status_code=404, detail="Customer not found")
    try:
        db.table("customers").update(
            _customer_row(payload, str(user_id), create=False)
        ).eq("id", customer_id).eq("organisation_id", payload.organisation_id).execute()
        _replace_contacts(
            db,
            organisation_id=payload.organisation_id,
            customer_id=customer_id,
            contacts=payload.contacts,
        )
        return _load_customer(db, payload.organisation_id, customer_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{customer_id}/archive")
def archive_customer(customer_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), organisation_id)
    db.table("customers").update(
        {
            "active": False,
            "archived_by": str(user_id),
            "archived_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", customer_id).eq("organisation_id", organisation_id).execute()
    return _load_customer(db, organisation_id, customer_id)


@router.post("/{customer_id}/restore")
def restore_customer(customer_id: str, organisation_id: str, auth: UserAuth):
    user_id, db = auth
    ensure_org_write(str(user_id), organisation_id)
    db.table("customers").update(
        {"active": True, "archived_by": None, "archived_at": None}
    ).eq("id", customer_id).eq("organisation_id", organisation_id).execute()
    return _load_customer(db, organisation_id, customer_id)
