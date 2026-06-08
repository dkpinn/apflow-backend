import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import themes as themes_router
from app.routers.themes import router
from app.services import themes as theme_service


class _Result:
    def __init__(self, data):
        self.data = data


class _MemoryQuery:
    def __init__(self, client, table_name):
        self.client = client
        self.table_name = table_name
        self.filters = []
        self.in_filters = []
        self.operation = "select"
        self.payload = None

    def select(self, *_args):
        self.operation = "select"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, key, values):
        self.in_filters.append((key, set(values)))
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = payload
        return self

    def insert(self, payload):
        self.operation = "insert"
        self.payload = payload
        return self

    def _matches(self, row):
        return (
            all(row.get(key) == value for key, value in self.filters)
            and all(row.get(key) in values for key, values in self.in_filters)
        )

    def execute(self):
        rows = self.client.tables.setdefault(self.table_name, [])
        if self.operation == "select":
            return _Result([row.copy() for row in rows if self._matches(row)])

        if self.operation == "update":
            updated = []
            for row in rows:
                if self._matches(row):
                    row.update(self.payload)
                    updated.append(row.copy())
            return _Result(updated)

        if self.operation == "insert":
            payload = self.payload if isinstance(self.payload, list) else [self.payload]
            inserted = []
            for item in payload:
                row = dict(item)
                row.setdefault("id", f"{self.table_name}-{len(rows) + len(inserted) + 1}")
                inserted.append(row)
            rows.extend(inserted)
            return _Result([row.copy() for row in inserted])

        return _Result([])


class _MemorySupabase:
    def __init__(self, tables, *, token_users=None):
        self.tables = tables
        self.auth = _MemoryAuth(token_users or {})

    def table(self, name):
        return _MemoryQuery(self, name)


class _MemoryAuth:
    def __init__(self, token_users):
        self.token_users = token_users

    def get_user(self, token):
        user_id = self.token_users.get(token)
        if user_id is None:
            raise ValueError("invalid token")
        user = type("User", (), {"id": user_id})()
        return type("UserResponse", (), {"user": user})()


def _theme(id_, slug, *, is_active=True, tokens=None):
    return {
        "id": id_,
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "description": None,
        "preview_image_url": None,
        "tokens": tokens or {},
        "is_active": is_active,
    }


def test_normalise_theme_tokens_keeps_only_cosmetic_allowlist():
    assert theme_service.normalise_theme_tokens(
        {
            "colors": {
                "background": "#FFFFFF",
                "primary": "#0f172a",
                "formula_multiplier": "#123456",
                "dangerous": "url(javascript:alert(1))",
            },
            "typography": {
                "family": "mono",
                "heading_weight": 600,
                "css": "* { display: none }",
            },
            "density": "compact",
            "radius": "lg",
            "shadow": "sm",
            "calculation_mode": "different",
        }
    ) == {
        "colors": {
            "background": "#ffffff",
            "primary": "#0f172a",
        },
        "typography": {
            "family": "mono",
            "heading_weight": 600,
        },
        "density": "compact",
        "radius": "lg",
        "shadow": "sm",
    }


def test_list_purchased_themes_only_returns_owned_active_themes():
    supabase = _MemorySupabase(
        {
            "user_theme_entitlements": [
                {"user_id": "user-1", "theme_id": "theme-1"},
                {"user_id": "user-1", "theme_id": "theme-2"},
                {"user_id": "user-2", "theme_id": "theme-3"},
            ],
            "themes": [
                _theme("theme-1", "fresh-ledger", tokens={"colors": {"background": "#fff"}}),
                _theme("theme-2", "retired-theme", is_active=False),
                _theme("theme-3", "other-user-theme"),
            ],
            "user_theme_preferences": [
                {"user_id": "user-1", "active_theme_id": "theme-1"},
            ],
        }
    )

    result = theme_service.list_purchased_themes(supabase, user_id="user-1")

    assert result["active_theme_id"] == "theme-1"
    assert [theme["id"] for theme in result["themes"]] == ["theme-1"]
    assert result["themes"][0]["tokens"] == {"colors": {"background": "#fff"}}


def test_set_active_theme_rejects_unpurchased_theme():
    supabase = _MemorySupabase(
        {
            "user_theme_entitlements": [
                {"user_id": "user-1", "theme_id": "theme-1"},
            ],
            "themes": [
                _theme("theme-1", "fresh-ledger"),
                _theme("theme-2", "not-owned"),
            ],
            "user_theme_preferences": [],
        }
    )

    with pytest.raises(theme_service.ThemeAccessError):
        theme_service.set_active_theme(supabase, user_id="user-1", theme_id="theme-2")

    assert supabase.tables["user_theme_preferences"] == []


def test_update_active_theme_endpoint_persists_preference(monkeypatch):
    supabase = _MemorySupabase(
        {
            "user_theme_entitlements": [
                {"user_id": "user-1", "theme_id": "theme-1"},
            ],
            "themes": [
                _theme("theme-1", "fresh-ledger", tokens={"density": "comfortable"}),
            ],
            "user_theme_preferences": [],
        },
        token_users={"valid-token": "user-1"},
    )

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[themes_router.authenticated_user] = lambda: ("user-1", supabase)
    client = TestClient(app)

    response = client.patch(
        "/api/themes/active",
        headers={"Authorization": "Bearer valid-token"},
        json={"theme_id": "theme-1"},
    )

    assert response.status_code == 200
    assert response.json()["active_theme_id"] == "theme-1"
    assert supabase.tables["user_theme_preferences"][0]["active_theme_id"] == "theme-1"


def test_update_active_theme_endpoint_requires_bearer_token(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.patch("/api/themes/active", json={"theme_id": "theme-1"})

    assert response.status_code == 401
