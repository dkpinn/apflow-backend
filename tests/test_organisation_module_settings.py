import unittest

from app.services.organisation_module_settings import (
    get_module_settings,
    validate_bank_allocation_tracking,
    validate_required_dimensions,
    validate_supplier_allocations_tracking,
)


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)
        self.filters = []
        self.ids = None

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, key, values):
        self.ids = (key, {str(value) for value in values})
        return self

    def execute(self):
        rows = self.rows
        for key, value in self.filters:
            rows = [row for row in rows if row.get(key) == value]
        if self.ids:
            key, values = self.ids
            rows = [row for row in rows if str(row.get(key)) in values]
        return _Result(rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


class OrganisationModuleSettingsTests(unittest.TestCase):
    def test_missing_rows_return_disabled_defaults_for_every_module(self):
        settings = get_module_settings(_DB({}), "org-1")
        self.assertEqual(len(settings), 7)
        self.assertTrue(all(not row["tracking_enabled"] for row in settings))

    def test_enabled_module_requires_active_same_org_dimensions(self):
        db = _DB({
            "tracking_dimensions": [
                {"id": "dim-1", "organisation_id": "org-1", "name": "Department", "active": True},
                {"id": "dim-2", "organisation_id": "org-2", "name": "Other", "active": True},
            ]
        })
        ids, rows = validate_required_dimensions(
            db,
            organisation_id="org-1",
            tracking_enabled=True,
            dimension_ids=["dim-1"],
        )
        self.assertEqual(ids, ["dim-1"])
        self.assertEqual(rows[0]["name"], "Department")

        with self.assertRaisesRegex(ValueError, "belong to this organisation"):
            validate_required_dimensions(
                db,
                organisation_id="org-1",
                tracking_enabled=True,
                dimension_ids=["dim-2"],
            )

    def test_disabled_module_normalises_dimensions_to_empty(self):
        ids, rows = validate_required_dimensions(
            _DB({}),
            organisation_id="org-1",
            tracking_enabled=False,
            dimension_ids=["dim-1"],
        )
        self.assertEqual(ids, [])
        self.assertEqual(rows, [])

    def test_supplier_splits_inherit_line_tracking_but_each_split_is_checked(self):
        required = [
            {"id": "department", "name": "Department"},
            {"id": "branch", "name": "Branch"},
        ]
        line_items = [{
            "id": "line-1",
            "description": "Rent",
            "tracking": {"department": "admin", "branch": "jhb"},
        }]
        validate_supplier_allocations_tracking(
            line_items=line_items,
            allocations_by_line={
                "line-1": [
                    {"tracking": {}},
                    {"tracking": {"department": "sales", "branch": "cpt"}},
                ]
            },
            required_dimensions=required,
        )

        with self.assertRaisesRegex(ValueError, "Rent split 2: Branch"):
            validate_supplier_allocations_tracking(
                line_items=line_items,
                allocations_by_line={
                    "line-1": [
                        {"tracking": {}},
                        {"tracking": {"department": "sales"}},
                    ]
                },
                required_dimensions=required,
            )

    def test_bank_allocation_requires_every_configured_dimension(self):
        required = [{"id": "department", "name": "Department"}]
        with self.assertRaisesRegex(ValueError, "Department"):
            validate_bank_allocation_tracking(tracking={}, required_dimensions=required)


if __name__ == "__main__":
    unittest.main()
