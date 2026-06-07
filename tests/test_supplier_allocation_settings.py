import pytest

import app.routers.suppliers as suppliers_module
from app.routers.suppliers import (
    SupplierAllocationSettingsRequest,
    get_supplier_allocation_settings,
    update_supplier_allocation_settings,
)


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.ids = None
        self.patch = None

    def select(self, *_args):
        return self

    def update(self, patch):
        self.patch = patch
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, key, values):
        self.ids = (key, {str(value) for value in values})
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        rows = [
            row for row in self.rows
            if all(row.get(key) == value for key, value in self.filters)
        ]
        if self.ids:
            key, values = self.ids
            rows = [row for row in rows if str(row.get(key)) in values]
        if self.patch is not None:
            for row in rows:
                row.update(self.patch)
        return _Result([row.copy() for row in rows])


class _DB:
    def __init__(self):
        self.tables = {
            "suppliers": [{
                "id": "supplier-1",
                "organisation_id": "org-1",
                "default_expense_account": "4050",
                "default_tracking": {},
            }],
            "tracking_dimensions": [
                {"id": "dim-department", "organisation_id": "org-1", "active": True},
                {"id": "dim-other", "organisation_id": "org-2", "active": True},
            ],
            "tracking_values": [
                {"id": "value-maintenance", "dimension_id": "dim-department", "active": True},
                {"id": "value-other", "dimension_id": "dim-other", "active": True},
            ],
        }

    def table(self, name):
        return _Query(self.tables.get(name, []))


def test_supplier_allocation_settings_validate_and_persist_default_tracking(monkeypatch):
    monkeypatch.setattr(suppliers_module, "ensure_org_write", lambda *_args: None)
    monkeypatch.setattr(suppliers_module, "ensure_org_read", lambda *_args: None)
    db = _DB()

    result = update_supplier_allocation_settings(
        "supplier-1",
        SupplierAllocationSettingsRequest(
            organisation_id="org-1",
            default_expense_account="4050",
            default_tracking={"dim-department": "value-maintenance"},
        ),
        ("user-1", db),
    )

    assert result["default_tracking"] == {"dim-department": "value-maintenance"}
    assert get_supplier_allocation_settings(
        "supplier-1",
        organisation_id="org-1",
        auth=("user-1", db),
    )["default_tracking"] == {"dim-department": "value-maintenance"}


def test_supplier_allocation_settings_reject_cross_dimension_value(monkeypatch):
    monkeypatch.setattr(suppliers_module, "ensure_org_write", lambda *_args: None)
    db = _DB()

    with pytest.raises(Exception) as exc:
        update_supplier_allocation_settings(
            "supplier-1",
            SupplierAllocationSettingsRequest(
                organisation_id="org-1",
                default_tracking={"dim-department": "value-other"},
            ),
            ("user-1", db),
        )

    assert getattr(exc.value, "status_code", None) == 422


def test_supplier_allocation_settings_partial_patch_preserves_tracking(monkeypatch):
    monkeypatch.setattr(suppliers_module, "ensure_org_write", lambda *_args: None)
    db = _DB()
    db.tables["suppliers"][0]["default_tracking"] = {
        "dim-department": "value-maintenance",
    }

    result = update_supplier_allocation_settings(
        "supplier-1",
        SupplierAllocationSettingsRequest(
            organisation_id="org-1",
            default_expense_account=None,
        ),
        ("user-1", db),
    )

    assert result["default_expense_account"] is None
    assert result["default_tracking"] == {"dim-department": "value-maintenance"}


def test_supplier_allocation_settings_are_organisation_scoped(monkeypatch):
    monkeypatch.setattr(suppliers_module, "ensure_org_write", lambda *_args: None)
    db = _DB()

    with pytest.raises(Exception) as exc:
        update_supplier_allocation_settings(
            "supplier-1",
            SupplierAllocationSettingsRequest(
                organisation_id="org-2",
                default_tracking={},
            ),
            ("user-1", db),
        )

    assert getattr(exc.value, "status_code", None) == 404
