"""
Shared FastAPI dependencies for authenticated routes.
"""
from __future__ import annotations

import os
from typing import Annotated, Optional, Tuple

from fastapi import Depends, Header, HTTPException
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


# ── Shared type alias ─────────────────────────────────────────────────────────

# Use this as the `auth: UserAuth` parameter type in any authenticated route.
# It resolves to (user_id: str, user_scoped_db_client: Client).
UserAuth = Annotated[tuple, Depends(authenticated_user)]

# ── Org-scoped authorisation helpers ─────────────────────────────────────────

WRITE_ROLES: frozenset[str] = frozenset({"owner", "admin", "accountant"})


def org_role_for_user(user_id: str, organisation_id: Optional[str]) -> Optional[str]:
    """
    Return the user's active role in the organisation, or None if not a member.

    Uses the service-role client so the check bypasses RLS — membership lookups
    must be reliable regardless of the user's own permissions.
    """
    if not user_id or not organisation_id:
        return None
    try:
        res = (
            get_supabase_client()
            .table("organisation_users")
            .select("role")
            .eq("user_id", user_id)
            .eq("organisation_id", organisation_id)
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        row = res.data[0] if res.data else None
        return row.get("role") if row else None
    except Exception:
        return None


def ensure_org_read(user_id: str, organisation_id: Optional[str]) -> None:
    """Raise 403 if the user is not an active member of the organisation."""
    if not org_role_for_user(user_id, organisation_id):
        raise HTTPException(
            status_code=403,
            detail="You do not have access to this organisation",
        )


def ensure_org_write(user_id: str, organisation_id: Optional[str]) -> None:
    """Raise 403 unless the user holds a write-capable role (owner/admin/accountant)."""
    role = org_role_for_user(user_id, organisation_id)
    if role not in WRITE_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only owners, admins, and accountants can perform this action",
        )


def _bootstrap_platform_owner_ids() -> set[str]:
    raw = os.getenv("PLATFORM_OWNER_USER_IDS") or os.getenv("PLATFORM_OWNER_USER_ID") or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


def is_platform_owner(user_id: str) -> bool:
    """
    Return True when the user is allowed to manage platform-level settings.

    PLATFORM_OWNER_USER_IDS is a bootstrap/backstop only. The normal source of
    truth is public.platform_admin_users, which lets ownership be managed
    without changing deployment environment variables.
    """
    if not user_id:
        return False
    if user_id in _bootstrap_platform_owner_ids():
        return True
    try:
        res = (
            get_supabase_client()
            .table("platform_admin_users")
            .select("role, status")
            .eq("user_id", user_id)
            .eq("role", "owner")
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception:
        return False


def ensure_platform_owner(user_id: str) -> None:
    """Raise 403 unless the user is a platform owner."""
    if not is_platform_owner(user_id):
        raise HTTPException(
            status_code=403,
            detail="Only the platform owner can manage platform settings",
        )
