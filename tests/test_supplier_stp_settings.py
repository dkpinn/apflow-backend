import pytest

import app.routers.suppliers as suppliers_module
from app.routers.suppliers import (
    SupplierStpSettingsRequest,
    update_supplier_stp_settings,
)


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.patch = None

    def select(self, *_args):
        return self

    def update(self, patch):
        self.patch = patch
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        rows = [
            row for row in self.rows
            if all(row.get(key) == value for key, value in self.filters)
        ]
        if self.patch is not None:
            for row in rows:
                row.update(self.patch)
        return _Result([row.copy() for row in rows])


class _DB:
    def __init__(self, suppliers):
        self.suppliers = suppliers

    def table(self, name):
        return _Query(self.suppliers if name == "suppliers" else [])


def test_supplier_stp_settings_require_non_negative_limit():
    with pytest.raises(ValueError):
        SupplierStpSettingsRequest(
            organisation_id="org-1",
            stp_enabled=True,
            stp_max_amount=-1,
        )


def test_supplier_stp_settings_update_is_organisation_scoped(monkeypatch):
    monkeypatch.setattr(suppliers_module, "ensure_org_admin", lambda *_args: None)
    db = _DB([{
        "id": "supplier-1",
        "organisation_id": "org-1",
        "stp_enabled": False,
        "stp_max_amount": None,
    }])
    result = update_supplier_stp_settings(
        "supplier-1",
        SupplierStpSettingsRequest(
            organisation_id="org-1",
            stp_enabled=True,
            stp_max_amount=2500,
        ),
        ("user-1", db),
    )
    assert result["stp_enabled"] is True
    assert result["stp_max_amount"] == 2500

    with pytest.raises(Exception) as exc:
        update_supplier_stp_settings(
            "supplier-1",
            SupplierStpSettingsRequest(
                organisation_id="org-2",
                stp_enabled=True,
            ),
            ("user-1", db),
        )
    assert getattr(exc.value, "status_code", None) == 404
