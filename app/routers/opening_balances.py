from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.dependencies import UserAuth, ensure_org_read, ensure_org_write
from app.services.opening_balances import post_opening_balance, preview_opening_balance


router = APIRouter(prefix="/api/opening-balances", tags=["opening-balances"])


class OpeningBalanceLine(BaseModel):
    account_id: Optional[UUID] = None
    account_code: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    debit: Decimal = Field(default=Decimal("0"), ge=0)
    credit: Decimal = Field(default=Decimal("0"), ge=0)
    tracking: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def account_reference_required(self):
        if not self.account_id and not (self.account_code or "").strip():
            raise ValueError("Each line needs an account_id or account_code")
        if self.debit and self.credit:
            raise ValueError("Each line may have either a debit or a credit, not both")
        return self


class OpeningBalanceRequest(BaseModel):
    organisation_id: UUID
    as_at_date: str
    description: Optional[str] = Field(default=None, max_length=500)
    lines: list[OpeningBalanceLine] = Field(min_length=1, max_length=1000)


def _line_payloads(payload: OpeningBalanceRequest) -> list[dict[str, Any]]:
    return [line.model_dump(mode="json") for line in payload.lines]


@router.post("/preview")
def preview_opening_balances(payload: OpeningBalanceRequest, auth: UserAuth):
    user_id, db = auth
    organisation_id = str(payload.organisation_id)
    ensure_org_read(str(user_id), organisation_id)
    try:
        return {
            "success": True,
            "preview": preview_opening_balance(
                db,
                organisation_id=organisation_id,
                as_at_date=payload.as_at_date,
                lines=_line_payloads(payload),
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/post")
def post_opening_balances(payload: OpeningBalanceRequest, auth: UserAuth):
    user_id, db = auth
    organisation_id = str(payload.organisation_id)
    ensure_org_write(str(user_id), organisation_id)
    try:
        return post_opening_balance(
            db,
            organisation_id=organisation_id,
            as_at_date=payload.as_at_date,
            lines=_line_payloads(payload),
            user_id=str(user_id),
            description=payload.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
