"""
Shared FastAPI dependencies for authenticated routes.
"""
from __future__ import annotations

import os
from typing import Annotated, Optional, Tuple

import jwt
from cachetools import TTLCache
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient
from supabase import Client

from app.db.supabase_client import get_supabase_client, get_user_supabase_client

_org_role_cache: TTLCache = TTLCache(maxsize=1024, ttl=60)
_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        if not supabase_url:
            raise HTTPException(status_code=500, detail="Supabase credentials missing")
        # Fetches Supabase's public JWKS once and caches it — works with any key type
        # (current ECC P-256 / ES256 and legacy HS256 shared secret).
        _jwks_client = PyJWKClient(f"{supabase_url}/auth/v1/.well-known/jwks.json", cache_keys=True)
    return _jwks_client


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

    # Verify JWT locally using Supabase's JWKS public keys — no network call per request
    # after the initial JWKS fetch (keys are cached in _jwks_client).
    # Only asymmetric algorithms are accepted: JWKS exposes public keys, and
    # allowing HS256 here would let a token signed with that public key (as an
    # HMAC secret) pass verification — a classic algorithm-confusion attack.
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc

    user_id = payload.get("sub")
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
ADMIN_ROLES: frozenset[str] = frozenset({"owner", "admin"})

CAPABILITY_KEYS: tuple[str, ...] = (
    "manage_org",
    "manage_users",
    "edit_data",
    "review",
    "run_reconciliation",
    "view_reports",
    "approve",
    "submit_for_approval",
)


def org_role_for_user(user_id: str, organisation_id: Optional[str]) -> Optional[str]:
    """
    Return the user's active role in the organisation, or None if not a member.

    Uses the service-role client so the check bypasses RLS — membership lookups
    must be reliable regardless of the user's own permissions.
    """
    if not user_id or not organisation_id:
        return None
    cache_key = (user_id, organisation_id)
    if cache_key in _org_role_cache:
        return _org_role_cache[cache_key]
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
        role = row.get("role") if row else None
    except Exception:
        return None
    _org_role_cache[cache_key] = role
    return role


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


def ensure_org_admin(user_id: str, organisation_id: Optional[str]) -> None:
    """Raise 403 unless the user is an organisation owner or admin."""
    role = org_role_for_user(user_id, organisation_id)
    if role not in ADMIN_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only organisation owners and admins can change these settings",
        )


def effective_capabilities(
    role: Optional[str],
    permissions: Optional[dict] = None,
    *,
    platform_owner: bool = False,
) -> dict[str, bool]:
    permissions = permissions if isinstance(permissions, dict) else {}
    if platform_owner:
        return {key: True for key in CAPABILITY_KEYS}

    return {
        "manage_org": role in ADMIN_ROLES,
        "manage_users": role in ADMIN_ROLES,
        "edit_data": role in WRITE_ROLES,
        "review": role in {"owner", "admin", "reviewer"},
        "run_reconciliation": role in WRITE_ROLES,
        "view_reports": role in WRITE_ROLES or bool(permissions.get("reports_view")),
        "approve": role in ADMIN_ROLES,
        "submit_for_approval": role in WRITE_ROLES,
    }


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
