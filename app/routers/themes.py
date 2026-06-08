from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.dependencies import authenticated_user
from app.services.themes import (
    ThemeAccessError,
    get_active_theme,
    list_purchased_themes,
    set_active_theme,
)

router = APIRouter(prefix="/api/themes", tags=["themes"])

UserAuth = Annotated[tuple, Depends(authenticated_user)]


class SetActiveThemeRequest(BaseModel):
    theme_id: str


@router.get("/purchased")
def purchased_themes(auth: UserAuth) -> dict:
    user_id, db = auth
    return list_purchased_themes(db, user_id=user_id)


@router.get("/active")
def active_theme(auth: UserAuth) -> dict:
    user_id, db = auth
    return {
        "active_theme": get_active_theme(db, user_id=user_id),
    }


@router.patch("/active")
def update_active_theme(
    payload: SetActiveThemeRequest,
    auth: UserAuth,
) -> dict:
    user_id, db = auth
    try:
        theme = set_active_theme(db, user_id=user_id, theme_id=payload.theme_id)
    except ThemeAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return {
        "active_theme_id": theme["id"],
        "active_theme": theme,
    }
