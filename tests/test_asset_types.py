from __future__ import annotations

from pathlib import Path

import pytest

import app.routers.asset_types as asset_router
from app.routers.asset_types import (
    AssetTypeRequest,
    create_asset_type_setting,
    preview_asset_type_setting_removal,
)
from app.services.asset_types import (
    create_asset_type,
    list_asset_types,
    managed_account_names,
)


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.ids = None

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, key, values):
        assert key == "id"
        self.ids = {str(value) for value in values}
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        rows = [
            row for row in self.rows
            if all(row.get(key) == value for key, value in self.filters)
            and (self.ids is None or str(row.get("id")) in self.ids)
        ]
        return _Result([row.copy() for row in rows])


class _Rpc:
    def __init__(self, db, name, params):
        self.db = db
        self.name = name
        self.params = params

    def execute(self):
        self.db.rpc_calls.append((self.name, self.params))
        if self.name == "create_asset_type_with_accounts":
            return _Result([{"id": "type-1"}])
        if self.name == "preview_asset_type_removal":
            return _Result({
                "asset_type_id": self.params["p_asset_type_id"],
                "asset_type_name": "Computer Equipment",
                "action": "archive",
                "active_assets": 0,
                "total_assets": 1,
                "journal_lines": 2,
                "account_mappings": 0,
                "has_history": True,
            })
        return _Result([])


class _DB:
    def __init__(self):
        self.rpc_calls = []
        self.tables = {
            "asset_types": [{
                "id": "type-1",
                "organisation_id": "org-1",
                "name": "Computer Equipment",
                "category": "tangible",
                "depreciation_method": "straight_line",
                "useful_life_months": 36,
                "residual_value_percent": 0,
                "depreciation_convention": "in_service_month",
                "active": True,
                "archived_at": None,
                "archived_by": None,
                "cost_account_id": "cost-1",
                "accumulated_account_id": "accum-1",
                "expense_account_id": "expense-1",
                "created_by": "user-1",
                "created_at": "2026-06-07T00:00:00+00:00",
                "updated_at": "2026-06-07T00:00:00+00:00",
            }],
            "accounts": [
                _account("cost-1", "Computer Equipment - At Cost", "asset", "cost"),
                _account(
                    "accum-1",
                    "Computer Equipment - Accumulated Depreciation",
                    "asset",
                    "accumulated",
                ),
                _account(
                    "expense-1",
                    "Depreciation on Computer Equipment",
                    "expense",
                    "expense",
                    income_statement_nature="depreciation_amortisation",
                ),
            ],
        }

    def table(self, name):
        return _Query(self.tables.get(name, []))

    def rpc(self, name, params):
        return _Rpc(self, name, params)


def _account(account_id, name, account_type, role, income_statement_nature=None):
    return {
        "id": account_id,
        "code": None,
        "name": name,
        "type": account_type,
        "group_name": None,
        "active": True,
        "vat_treatment": "full",
        "is_system": True,
        "system_key": f"asset_type:type-1:{role}",
        "managed_asset_type_id": "type-1",
        "asset_account_role": role,
        "income_statement_nature": income_statement_nature,
    }


def test_managed_account_names_use_depreciation_and_amortisation_terms():
    assert managed_account_names("Computer Equipment", "tangible") == {
        "cost": "Computer Equipment - At Cost",
        "accumulated": "Computer Equipment - Accumulated Depreciation",
        "expense": "Depreciation on Computer Equipment",
    }
    assert managed_account_names("Software", "intangible") == {
        "cost": "Software - At Cost",
        "accumulated": "Software - Accumulated Amortisation",
        "expense": "Amortisation of Software",
    }


def test_asset_type_request_normalises_and_validates_policy_defaults():
    payload = AssetTypeRequest(
        organisation_id="org-1",
        name="  Computer   Equipment ",
        category="tangible",
        useful_life_months=36,
    )
    assert payload.name == "Computer Equipment"
    assert payload.residual_value_percent == 0

    with pytest.raises(ValueError):
        AssetTypeRequest(
            organisation_id="org-1",
            name="Software",
            category="intangible",
            useful_life_months=0,
        )
    with pytest.raises(ValueError):
        AssetTypeRequest(
            organisation_id="org-1",
            name="Software",
            category="intangible",
            useful_life_months=36,
            residual_value_percent=101,
        )


def test_list_asset_types_enriches_the_three_managed_accounts():
    rows = list_asset_types(_DB(), organisation_id="org-1")
    assert len(rows) == 1
    assert rows[0]["accounts"]["cost"]["asset_account_role"] == "cost"
    assert rows[0]["accounts"]["accumulated"]["type"] == "asset"
    assert rows[0]["accounts"]["expense"]["income_statement_nature"] == "depreciation_amortisation"


def test_create_asset_type_uses_atomic_rpc_and_reloads_accounts():
    db = _DB()
    created = create_asset_type(
        db,
        organisation_id="org-1",
        name="Computer Equipment",
        category="tangible",
        useful_life_months=36,
        residual_value_percent=10,
    )
    assert db.rpc_calls == [(
        "create_asset_type_with_accounts",
        {
            "p_org_id": "org-1",
            "p_name": "Computer Equipment",
            "p_category": "tangible",
            "p_useful_life_months": 36,
            "p_residual_value_percent": 10,
        },
    )]
    assert created["accounts"]["expense"]["name"] == "Depreciation on Computer Equipment"


def test_asset_type_routes_require_admin_and_keep_organisation_scope(monkeypatch):
    checked = []
    monkeypatch.setattr(
        asset_router,
        "ensure_org_admin",
        lambda user_id, org_id: checked.append((user_id, org_id)),
    )
    db = _DB()
    created = create_asset_type_setting(
        AssetTypeRequest(
            organisation_id="org-1",
            name="Computer Equipment",
            category="tangible",
            useful_life_months=36,
        ),
        ("user-1", db),
    )
    assert checked == [("user-1", "org-1")]
    assert created["organisation_id"] == "org-1"

    preview = preview_asset_type_setting_removal(
        "type-1",
        "org-1",
        ("user-1", db),
    )
    assert preview["action"] == "archive"
    assert db.rpc_calls[-1][1] == {
        "p_org_id": "org-1",
        "p_asset_type_id": "type-1",
    }


def test_migration_defines_atomic_lifecycle_and_system_account_protection():
    sql = (
        Path(__file__).parents[1]
        / "app"
        / "db"
        / "applied"
        / "asset_types_managed_accounts_phase_c17.sql"
    ).read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION public.create_asset_type_with_accounts" in sql
    assert "CREATE OR REPLACE FUNCTION public.update_asset_type_with_accounts" in sql
    assert "CREATE OR REPLACE FUNCTION public.remove_asset_type_with_accounts" in sql
    assert "CREATE OR REPLACE FUNCTION public.restore_asset_type_with_accounts" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "depreciation_amortisation" in sql
    assert "managed_asset_type_id" in sql
    assert "app.asset_type_account_maintenance" in sql
