"""
Shared FastAPI dependencies for authenticated routes.
"""
from __future__ import annotations

from typing import Optional, Tuple

from fastapi import Header, HTTPException
from supabase import Client

from app.db.supabase_client import get_supabase_client, get_user_supabase_client

# Service-role singleton — used only for JWT validation (auth.get_user).
# Never used for user-facing DB queries.
try:
    _svc = get_supabase_client()
except Exception:
    _svc = None  # type: ignore[assignment]


def authenticated_user(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Tuple[str, Client]:
    """
    Validate the Bearer token and return (user_id, user_scoped_db_client).

    The returned client has RLS enabled — all PostgREST queries run under
    the user's authenticated role, not the service role.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    if _svc is None:
        raise HTTPException(status_code=500, detail="Supabase credentials missing")

    # Validate token against Supabase Auth (service-role client, Auth API only — not a DB query)
    try:
        response = _svc.auth.get_user(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc

    user = response.get("user") if isinstance(response, dict) else getattr(response, "user", None)
    user_id = getattr(user, "id", None)
    if user_id is None and isinstance(user, dict):
        user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid bearer token")

    # Per-request user-scoped client (RLS enforced via user JWT)
    db = get_user_supabase_client(token)
    return str(user_id), db
