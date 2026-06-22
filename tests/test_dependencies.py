"""Tests for app/dependencies.py — auth boundary and capability logic."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import dependencies
from app.dependencies import (
    CAPABILITY_KEYS,
    authenticated_user,
    effective_capabilities,
    is_platform_owner,
    org_role_for_user,
)


# ── fixture: clear org-role cache between tests ───────────────────────────────

@pytest.fixture(autouse=True)
def _clear_role_cache():
    dependencies._org_role_cache.clear()
    yield
    dependencies._org_role_cache.clear()


# ── effective_capabilities — pure function, no DB needed ─────────────────────

def test_platform_owner_flag_enables_every_capability():
    caps = effective_capabilities(None, platform_owner=True)
    assert set(caps) == set(CAPABILITY_KEYS), "All capability keys must be present"
    assert all(caps.values()), "Every capability must be True for platform owner"


def test_owner_role_has_full_write_and_admin_access():
    caps = effective_capabilities("owner")
    assert caps["manage_org"] is True
    assert caps["approve"] is True
    assert caps["edit_data"] is True
    assert caps["run_reconciliation"] is True


def test_reviewer_role_can_review_but_not_write():
    caps = effective_capabilities("reviewer")
    assert caps["review"] is True
    assert caps["edit_data"] is False
    assert caps["manage_org"] is False
    assert caps["approve"] is False


def test_viewer_role_has_no_access_by_default():
    caps = effective_capabilities("viewer")
    assert caps["view_reports"] is False
    assert caps["edit_data"] is False
    assert caps["manage_org"] is False


def test_explicit_reports_permission_overrides_role_restriction():
    caps = effective_capabilities("viewer", {"reports_view": True})
    assert caps["view_reports"] is True
    assert caps["edit_data"] is False


def test_none_role_with_no_permissions_returns_all_false():
    caps = effective_capabilities(None)
    assert not any(caps.values())


# ── org_role_for_user ─────────────────────────────────────────────────────────

def test_org_role_returns_none_for_empty_user_id():
    assert org_role_for_user("", "org-1") is None


def test_org_role_returns_none_for_none_org_id():
    assert org_role_for_user("user-1", None) is None


def test_org_role_reads_matching_role_from_db(monkeypatch, stub_db):
    db = stub_db({
        "organisation_users": [
            {"user_id": "u1", "organisation_id": "o1", "role": "accountant", "status": "active"},
            {"user_id": "u2", "organisation_id": "o1", "role": "viewer",     "status": "active"},
        ]
    })
    monkeypatch.setattr(dependencies, "get_supabase_client", lambda: db)
    assert org_role_for_user("u1", "o1") == "accountant"
    assert org_role_for_user("u2", "o1") == "viewer"


def test_org_role_returns_none_when_user_not_in_org(monkeypatch, stub_db):
    db = stub_db({"organisation_users": []})
    monkeypatch.setattr(dependencies, "get_supabase_client", lambda: db)
    assert org_role_for_user("u-unknown", "o1") is None


def test_org_role_caches_result_and_avoids_second_db_call(monkeypatch, stub_db):
    call_count = 0

    class _CountingDB:
        def table(self, _):
            return self
        def select(self, *_):
            return self
        def eq(self, *_):
            nonlocal call_count
            call_count += 1
            return self
        def limit(self, _):
            return self
        def execute(self):
            return type("R", (), {"data": [{"role": "admin"}]})()

    monkeypatch.setattr(dependencies, "get_supabase_client", _CountingDB)
    org_role_for_user("u-cache", "o-cache")
    org_role_for_user("u-cache", "o-cache")
    assert call_count < 6, "Second call should be served from cache, not re-querying DB"


def test_org_role_returns_none_on_db_error(monkeypatch):
    def _raise():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(dependencies, "get_supabase_client", _raise)
    assert org_role_for_user("u1", "o1") is None


# ── is_platform_owner ─────────────────────────────────────────────────────────

def test_is_platform_owner_rejects_empty_string():
    assert is_platform_owner("") is False


def test_is_platform_owner_grants_access_via_env_var(monkeypatch):
    monkeypatch.setenv("PLATFORM_OWNER_USER_IDS", "env-admin-abc,env-admin-def")
    assert is_platform_owner("env-admin-abc") is True
    assert is_platform_owner("env-admin-def") is True
    assert is_platform_owner("someone-else") is False


def test_is_platform_owner_grants_access_via_db(monkeypatch, stub_db):
    monkeypatch.setenv("PLATFORM_OWNER_USER_IDS", "")
    db = stub_db({
        "platform_admin_users": [
            {"user_id": "db-owner", "role": "owner", "status": "active"},
        ]
    })
    monkeypatch.setattr(dependencies, "get_supabase_client", lambda: db)
    assert is_platform_owner("db-owner") is True
    assert is_platform_owner("db-non-owner") is False


def test_is_platform_owner_returns_false_on_db_error(monkeypatch):
    monkeypatch.setenv("PLATFORM_OWNER_USER_IDS", "")

    def _raise():
        raise RuntimeError("db down")

    monkeypatch.setattr(dependencies, "get_supabase_client", _raise)
    assert is_platform_owner("any-user") is False


# ── authenticated_user — token guard paths ────────────────────────────────────

def test_missing_authorization_header_raises_401():
    with pytest.raises(HTTPException) as exc:
        authenticated_user(authorization=None)
    assert exc.value.status_code == 401
    assert "missing" in exc.value.detail.lower()


def test_non_bearer_scheme_raises_401():
    with pytest.raises(HTTPException) as exc:
        authenticated_user(authorization="Basic dXNlcjpwYXNz")
    assert exc.value.status_code == 401


def test_bearer_with_empty_token_raises_401():
    with pytest.raises(HTTPException) as exc:
        authenticated_user(authorization="Bearer ")
    assert exc.value.status_code == 401


def test_expired_token_raises_401_with_descriptive_message(monkeypatch):
    import jwt

    class _MockKey:
        key = "irrelevant"

    class _MockJWKS:
        def get_signing_key_from_jwt(self, _token):
            return _MockKey()

    def _raise_expired(*_a, **_kw):
        raise jwt.ExpiredSignatureError()

    monkeypatch.setattr(dependencies, "_jwks_client", _MockJWKS())
    monkeypatch.setattr(jwt, "decode", _raise_expired)

    with pytest.raises(HTTPException) as exc:
        authenticated_user(authorization="Bearer some.fake.token")
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


def test_invalid_token_signature_raises_401(monkeypatch):
    import jwt

    class _MockKey:
        key = "irrelevant"

    class _MockJWKS:
        def get_signing_key_from_jwt(self, _token):
            return _MockKey()

    def _raise_invalid(*_a, **_kw):
        raise jwt.PyJWTError("signature mismatch")

    monkeypatch.setattr(dependencies, "_jwks_client", _MockJWKS())
    monkeypatch.setattr(jwt, "decode", _raise_invalid)

    with pytest.raises(HTTPException) as exc:
        authenticated_user(authorization="Bearer forged.token.here")
    assert exc.value.status_code == 401
    assert "invalid" in exc.value.detail.lower()
