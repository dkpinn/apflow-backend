from pathlib import Path

from app import dependencies
from app.routers import admin_integrations
from app.routers.admin_integrations import get_admin_identity
from scripts.bootstrap_dev_platform_owner import (
    PRODUCTION_PROJECT_REF,
    project_ref,
)
from scripts.seed_dev_test_data import USERS


class _Result:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)
        self.filters = []

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def limit(self, count):
        self.limit_count = count
        return self

    def execute(self):
        rows = [
            row
            for row in self.rows
            if all(row.get(key) == value for key, value in self.filters)
        ]
        return _Result(rows[: getattr(self, "limit_count", len(rows))])


class _DB:
    def __init__(self):
        self.tables = {
            "platform_admin_users": [
                {
                    "user_id": "platform-owner",
                    "role": "owner",
                    "status": "active",
                }
            ],
            "organisation_users": [
                {
                    "user_id": "platform-owner",
                    "organisation_id": "org-1",
                    "role": "owner",
                    "status": "active",
                    "permissions": {},
                    "platform_managed": True,
                }
            ],
        }

    def table(self, name):
        return _Query(self.tables.get(name, []))


def test_platform_owner_receives_every_effective_capability():
    capabilities = dependencies.effective_capabilities(
        None,
        {},
        platform_owner=True,
    )

    assert capabilities
    assert all(capabilities.values())


def test_explicit_reports_permission_is_preserved_for_non_owner():
    capabilities = dependencies.effective_capabilities(
        "viewer",
        {"reports_view": True},
    )

    assert capabilities["view_reports"] is True
    assert capabilities["manage_org"] is False


def test_admin_identity_reports_platform_owner_and_managed_membership(monkeypatch):
    db = _DB()
    monkeypatch.setattr(admin_integrations, "get_supabase_client", lambda: db)
    monkeypatch.setattr(dependencies, "get_supabase_client", lambda: db)

    result = get_admin_identity(
        ("platform-owner", object()),
        organisation_id="org-1",
    )

    assert result["platform_owner"] is True
    assert result["organisation_role"] == "owner"
    assert result["platform_managed_membership"] is True
    assert all(result["effective_capabilities"].values())


def test_platform_owner_migration_syncs_and_protects_memberships():
    sql = (
        Path(__file__).parents[1]
        / "supabase"
        / "migrations"
        / "20260608170000_platform_owner_global_access.sql"
    ).read_text(encoding="utf-8")

    assert "platform_managed BOOLEAN NOT NULL DEFAULT false" in sql
    assert "sync_platform_owner_memberships" in sql
    assert "organisations_add_platform_owners" in sql
    assert "organisation_users_protect_platform_managed" in sql
    assert "Platform Owner membership cannot be demoted or suspended" in sql
    assert "GRANT EXECUTE ON FUNCTION public.sync_platform_owner_memberships(UUID)" in sql
    assert "TO authenticated" not in sql.split(
        "GRANT EXECUTE ON FUNCTION public.sync_platform_owner_memberships(UUID)",
        1,
    )[1]


def test_platform_owner_guard_hardening_restores_sync_flag_and_allows_org_delete():
    sql = (
        Path(__file__).parents[1]
        / "supabase"
        / "migrations"
        / "20260609001000_platform_owner_membership_guard_hardening.sql"
    ).read_text(encoding="utf-8")

    assert "set_config('app.platform_owner_membership_sync', 'off', true)" in sql
    assert "WHERE id = OLD.organisation_id" in sql


def test_bootstrap_project_ref_identifies_and_blocks_production():
    assert (
        project_ref(f"https://{PRODUCTION_PROJECT_REF}.supabase.co")
        == PRODUCTION_PROJECT_REF
    )
    assert project_ref("https://development-ref.supabase.co") == "development-ref"


def test_development_personas_preserve_restricted_roles():
    roles = {email: role for _name, email, role in USERS}

    assert roles["demo@apflow.test"] == "owner"
    assert roles["kevin.barr@aeptest.co.za"] == "owner"
    assert roles["sandra.nkosi@aeptest.co.za"] == "admin"
    assert roles["nomsa.dlamini@aeptest.co.za"] == "accountant"
    assert roles["priya.govender@aeptest.co.za"] == "reviewer"
    assert roles["amahle.zulu@aeptest.co.za"] == "viewer"
    assert roles["fatima.ismail@aeptest.co.za"] == "client"
