import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import consolidation as consolidation_router
from app.routers.consolidation import router
from app.services.consolidation import (
    ConsolidationAccessError,
    ConsolidationValidationError,
    consolidated_trial_balance,
    create_adjustment,
    list_reporting_groups,
)


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

    def insert(self, payload):
        self.operation = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.operation = "update"
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
        if self.operation == "insert":
            payload = self.payload if isinstance(self.payload, list) else [self.payload]
            inserted = []
            for item in payload:
                row = dict(item)
                row.setdefault("id", f"{self.table_name}-{len(rows) + len(inserted) + 1}")
                inserted.append(row)
            rows.extend(inserted)
            return _Result([row.copy() for row in inserted])
        if self.operation == "update":
            updated = []
            for row in rows:
                if self._matches(row):
                    row.update(self.payload)
                    updated.append(row.copy())
            return _Result(updated)
        return _Result([])


class _MemoryAuth:
    def __init__(self, token_users):
        self.token_users = token_users

    def get_user(self, token):
        user_id = self.token_users.get(token)
        if user_id is None:
            raise ValueError("invalid token")
        user = type("User", (), {"id": user_id})()
        return type("UserResponse", (), {"user": user})()


class _MemorySupabase:
    def __init__(self, tables, *, token_users=None):
        self.tables = tables
        self.auth = _MemoryAuth(token_users or {})

    def table(self, name):
        return _MemoryQuery(self, name)


def _base_db():
    return _MemorySupabase(
        {
            "organisation_users": [
                {"organisation_id": "parent-org", "user_id": "owner-user", "role": "owner", "status": "active"},
                {"organisation_id": "sub-org", "user_id": "sub-user", "role": "viewer", "status": "active"},
                {"organisation_id": "parent-org", "user_id": "viewer-user", "role": "viewer", "status": "active"},
            ],
            "reporting_groups": [
                {
                    "id": "group-1",
                    "owner_organisation_id": "parent-org",
                    "name": "Demo Group",
                    "reporting_currency": "ZAR",
                    "country": "South Africa",
                    "status": "active",
                }
            ],
            "reporting_group_users": [
                {"reporting_group_id": "group-1", "user_id": "assigned-accountant", "role": "accountant", "status": "active"},
            ],
            "reporting_group_entities": [
                {
                    "id": "entity-parent",
                    "reporting_group_id": "group-1",
                    "organisation_id": "parent-org",
                    "entity_type": "parent",
                    "ownership_percent": 100,
                    "consolidation_method": "full",
                    "effective_from": "2026-01-01",
                    "effective_to": None,
                },
                {
                    "id": "entity-sub",
                    "reporting_group_id": "group-1",
                    "parent_entity_id": "entity-parent",
                    "organisation_id": "sub-org",
                    "entity_type": "subsidiary",
                    "ownership_percent": 80,
                    "consolidation_method": "full",
                    "effective_from": "2026-01-01",
                    "effective_to": None,
                },
                {
                    "id": "entity-jv",
                    "reporting_group_id": "group-1",
                    "parent_entity_id": "entity-parent",
                    "organisation_id": "jv-org",
                    "entity_type": "joint_venture",
                    "ownership_percent": 50,
                    "consolidation_method": "proportionate",
                    "effective_from": "2026-01-01",
                    "effective_to": None,
                },
                {
                    "id": "entity-associate",
                    "reporting_group_id": "group-1",
                    "parent_entity_id": "entity-parent",
                    "organisation_id": "associate-org",
                    "entity_type": "associate",
                    "ownership_percent": 30,
                    "consolidation_method": "equity",
                    "effective_from": "2026-01-01",
                    "effective_to": None,
                },
            ],
            "consolidation_periods": [
                {
                    "id": "period-1",
                    "reporting_group_id": "group-1",
                    "name": "Jan 2026",
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-31",
                    "reporting_currency": "ZAR",
                    "status": "open",
                }
            ],
            "consolidation_account_mappings": [
                {
                    "reporting_group_id": "group-1",
                    "entity_organisation_id": "sub-org",
                    "local_account_id": "sub-sales",
                    "group_account_id": "group-sales",
                    "effective_from": "2026-01-01",
                }
            ],
            "exchange_rates": [
                {
                    "reporting_group_id": "group-1",
                    "period_id": "period-1",
                    "from_currency": "USD",
                    "to_currency": "ZAR",
                    "rate_type": "closing",
                    "rate_date": "2026-01-31",
                    "rate": 18,
                }
            ],
            "consolidation_entity_balances": [
                {
                    "reporting_group_id": "group-1",
                    "period_id": "period-1",
                    "entity_organisation_id": "sub-org",
                    "account_id": "sub-sales",
                    "currency": "USD",
                    "debit_amount": 100,
                    "credit_amount": 0,
                },
                {
                    "reporting_group_id": "group-1",
                    "period_id": "period-1",
                    "entity_organisation_id": "jv-org",
                    "account_id": "jv-cash",
                    "currency": "ZAR",
                    "debit_amount": 100,
                    "credit_amount": 0,
                },
                {
                    "reporting_group_id": "group-1",
                    "period_id": "period-1",
                    "entity_organisation_id": "associate-org",
                    "account_id": "associate-revenue",
                    "currency": "ZAR",
                    "debit_amount": 1000,
                    "credit_amount": 0,
                },
            ],
            "consolidation_adjustments": [
                {
                    "id": "adj-1",
                    "reporting_group_id": "group-1",
                    "period_id": "period-1",
                    "adjustment_type": "elimination",
                    "description": "Eliminate intercompany sale",
                    "status": "posted",
                }
            ],
            "consolidation_adjustment_lines": [
                {
                    "adjustment_id": "adj-1",
                    "line_number": 1,
                    "account_id": "elim-account",
                    "debit_amount": 10,
                    "credit_amount": 0,
                },
                {
                    "adjustment_id": "adj-1",
                    "line_number": 2,
                    "account_id": "group-sales",
                    "debit_amount": 0,
                    "credit_amount": 10,
                },
            ],
        },
        token_users={"valid-token": "owner-user"},
    )


def test_consolidated_trial_balance_applies_methods_fx_mappings_and_adjustments():
    db = _base_db()

    result = consolidated_trial_balance(
        db,
        user_id="owner-user",
        reporting_group_id="group-1",
        period_id="period-1",
    )

    by_account = {line["account_id"]: line for line in result["lines"]}
    assert by_account["group-sales"]["balance"] == 1790.0
    assert by_account["jv-cash"]["balance"] == 50.0
    assert by_account["elim-account"]["balance"] == 10.0
    assert "associate-revenue" not in by_account
    assert result["skipped_entities"][0]["consolidation_method"] == "equity"

    sub_contribution = next(row for row in result["entity_contributions"] if row["entity_organisation_id"] == "sub-org")
    assert sub_contribution["applied_factor"] == 1.0
    assert sub_contribution["fx_rate"] == 18.0
    assert sub_contribution["non_controlling_interest_balance"] == 360.0


def test_missing_exchange_rate_is_report_validation_error():
    db = _base_db()
    db.tables["exchange_rates"] = []

    with pytest.raises(ConsolidationValidationError):
        consolidated_trial_balance(
            db,
            user_id="owner-user",
            reporting_group_id="group-1",
            period_id="period-1",
        )


def test_list_reporting_groups_includes_owner_linked_and_assigned_access():
    db = _base_db()

    assert [row["id"] for row in list_reporting_groups(db, user_id="owner-user")] == ["group-1"]
    assert [row["id"] for row in list_reporting_groups(db, user_id="sub-user")] == ["group-1"]
    assert [row["id"] for row in list_reporting_groups(db, user_id="assigned-accountant")] == ["group-1"]
    assert list_reporting_groups(db, user_id="no-access") == []


def test_locked_period_rejects_non_admin_adjustments():
    db = _base_db()
    db.tables["consolidation_periods"][0]["status"] = "locked"

    with pytest.raises(ConsolidationAccessError):
        create_adjustment(
            db,
            user_id="assigned-accountant",
            reporting_group_id="group-1",
            payload={
                "period_id": "period-1",
                "adjustment_type": "manual",
                "description": "Locked period edit",
                "lines": [
                    {"account_id": "a", "debit_amount": 1, "credit_amount": 0},
                    {"account_id": "b", "debit_amount": 0, "credit_amount": 1},
                ],
            },
        )


def test_unbalanced_adjustments_are_rejected():
    db = _base_db()

    with pytest.raises(ConsolidationValidationError):
        create_adjustment(
            db,
            user_id="owner-user",
            reporting_group_id="group-1",
            payload={
                "period_id": "period-1",
                "adjustment_type": "manual",
                "description": "Bad adjustment",
                "lines": [
                    {"account_id": "a", "debit_amount": 10, "credit_amount": 0},
                    {"account_id": "b", "debit_amount": 0, "credit_amount": 9},
                ],
            },
        )


def test_reporting_groups_endpoint_uses_bearer_token(monkeypatch):
    db = _base_db()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[consolidation_router.authenticated_user] = lambda: ("owner-user", db)
    client = TestClient(app)

    response = client.get("/api/consolidation/groups", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    assert response.json()["reporting_groups"][0]["id"] == "group-1"
