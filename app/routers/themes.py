from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.db.supabase_client import get_supabase_client
from app.services.themes import (
    ThemeAccessError,
    get_active_theme,
    list_purchased_themes,
    set_active_theme,
)

router = APIRouter(prefix="/api/themes", tags=["themes"])
try:
    supabase = get_supabase_client()
except Exception:
    supabase = None


class SetActiveThemeRequest(BaseModel):
    theme_id: str


def _db():
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase credentials missing")
    return supabase


def _current_user_id(supabase_client, authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    try:
        response = supabase_client.auth.get_user(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc

    user = response.get("user") if isinstance(response, dict) else getattr(response, "user", None)
    user_id = getattr(user, "id", None)
    if user_id is None and isinstance(user, dict):
        user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid bearer token")

    return str(user_id)


@router.get("/purchased")
def purchased_themes(authorization: Optional[str] = Header(default=None, alias="Authorization")) -> dict:
    db = _db()
    user_id = _current_user_id(db, authorization)
    return list_purchased_themes(db, user_id=user_id)


@router.get("/active")
def active_theme(authorization: Optional[str] = Header(default=None, alias="Authorization")) -> dict:
    db = _db()
    user_id = _current_user_id(db, authorization)
    return {
        "active_theme": get_active_theme(db, user_id=user_id),
    }


@router.patch("/active")
def update_active_theme(
    payload: SetActiveThemeRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> dict:
    db = _db()
    user_id = _current_user_id(db, authorization)
    try:
        theme = set_active_theme(db, user_id=user_id, theme_id=payload.theme_id)
    except ThemeAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return {
        "active_theme_id": theme["id"],
        "active_theme": theme,
    }
