import sys
import types
import unittest
import importlib

try:
    import supabase  # noqa: F401
except ImportError:
    pass

if "supabase" not in sys.modules:
    supabase_stub = types.ModuleType("supabase")
    supabase_stub.Client = type("Client", (), {})
    supabase_stub.create_client = lambda url, key: object()
    sys.modules["supabase"] = supabase_stub

if "supabase" not in sys.modules:
    supabase_stub = types.ModuleType("supabase")
    supabase_stub.Client = type("Client", (), {})
    supabase_stub.create_client = lambda url, key: object()
    sys.modules["supabase"] = supabase_stub

try:
    import fastapi  # noqa: F401
except ImportError:
    pass

if "fastapi" not in sys.modules:
    fastapi_stub = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, *args, **kwargs):
            self.detail = kwargs.get("detail") if kwargs else (args[0] if args else None)
            super().__init__(self.detail)

        def __str__(self):
            return str(self.detail)

    class APIRouter:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            def decorator(fn):
                return fn

            return decorator

        def put(self, *args, **kwargs):
            def decorator(fn):
                return fn

            return decorator

    fastapi_stub.HTTPException = HTTPException
    fastapi_stub.APIRouter = APIRouter
    fastapi_stub.Depends = lambda dependency=None: dependency
    fastapi_stub.Header = lambda default=None, **_kwargs: default
    sys.modules["fastapi"] = fastapi_stub

from fastapi import HTTPException

if "app.services.invoice_extraction_service._helpers" not in sys.modules:
    helpers_mod = types.ModuleType("app.services.invoice_extraction_service._helpers")

    def get_organisation_extraction_settings(organisation_id: str):
        row = next(
            (org for org in helpers_mod.organisation_rows if org.get("id") == organisation_id),
            None,
        )
        return {
            "extraction_strategy": row.get("extraction_strategy") if row else "auto_group",
            "ask_per_upload": bool(row.get("ask_per_upload")) if row else False,
            "vlm_enabled": bool(row.get("vlm_enabled")) if row else False,
            "supplier_auto_link_min_matches": int(row.get("supplier_auto_link_min_matches", 2)) if row else 2,
            "reporting_standard": row.get("reporting_standard") if row else "ifrs",
            "income_statement_presentation": (
                "function"
                if row and row.get("reporting_standard") == "us_gaap"
                else row.get("income_statement_presentation") if row else "function"
            ),
        }

    def update_organisation_extraction_settings(organisation_id: str, updates: dict):
        row = next(
            (org for org in helpers_mod.organisation_rows if org.get("id") == organisation_id),
            None,
        )
        if not row:
            raise ValueError("Organisation not found")
        row.update(updates)
        return get_organisation_extraction_settings(organisation_id)

    helpers_mod.organisation_rows = []
    helpers_mod.get_organisation_extraction_settings = get_organisation_extraction_settings
    helpers_mod.update_organisation_extraction_settings = update_organisation_extraction_settings
    sys.modules["app.services.invoice_extraction_service._helpers"] = helpers_mod

from app.routers.organisations import (
    get_organisation_settings,
    update_organisation_settings,
    UpdateOrganisationSettingsRequest,
    router as organisations_router,
)
organisations_module = importlib.import_module("app.routers.organisations")


class _FakeQuery:
    def __init__(self, table_name: str, supabase: "_FakeSupabase"):
        self.table_name = table_name
        self.supabase = supabase
        self.operation = None
        self.payload = None
        self.eq_args = None

    def select(self, *_args):
        self.operation = "select"
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = payload
        return self

    def eq(self, *args, **_kwargs):
        self.eq_args = args
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        if self.table_name != "organisations":
            return type("Result", (), {"data": []})()

        if self.operation == "select":
            row = next(
                (org for org in self.supabase.organisation_rows if org.get("id") == self.eq_args[1]),
                None,
            )
            return type("Result", (), {"data": [row] if row else []})()

        if self.operation == "update":
            row = next(
                (org for org in self.supabase.organisation_rows if org.get("id") == self.eq_args[1]),
                None,
            )
            if not row:
                return type("Result", (), {"data": []})()
            row.update(self.payload)
            return type("Result", (), {"data": [row.copy()]})()

        return type("Result", (), {"data": []})()


class _FakeSupabase:
    def __init__(self, organisation_rows=None):
        self.organisation_rows = organisation_rows or []

    def table(self, name):
        return _FakeQuery(name, self)


class OrganisationSettingsAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake_supabase = _FakeSupabase()
        organisations_router.__dict__["supabase"] = self.fake_supabase
        organisations_module.ensure_org_read = lambda *_args, **_kwargs: None
        organisations_module.ensure_org_admin = lambda *_args, **_kwargs: None
        helpers_mod = sys.modules["app.services.invoice_extraction_service._helpers"]
        helpers_mod.organisation_rows = []
        self.helpers_mod = helpers_mod

    def test_get_organisation_settings_returns_defaults_when_missing(self):
        result = get_organisation_settings("org-123", ("user-1", self.fake_supabase))

        self.assertEqual(result, {
            "extraction_strategy": "auto_group",
            "ask_per_upload": False,
            "vlm_enabled": False,
            "supplier_auto_link_min_matches": 2,
            "reporting_standard": "ifrs",
            "income_statement_presentation": "function",
        })

    def test_get_organisation_settings_returns_existing_values(self):
        self.helpers_mod.organisation_rows = [
            {
                "id": "org-123",
                "extraction_strategy": "vlm",
                "ask_per_upload": True,
                "vlm_enabled": True,
                "supplier_auto_link_min_matches": 3,
                "reporting_standard": "uk_gaap_frs_102",
                "income_statement_presentation": "nature",
            }
        ]

        result = get_organisation_settings("org-123", ("user-1", self.fake_supabase))

        self.assertEqual(result, {
            "extraction_strategy": "vlm",
            "ask_per_upload": True,
            "vlm_enabled": True,
            "supplier_auto_link_min_matches": 3,
            "reporting_standard": "uk_gaap_frs_102",
            "income_statement_presentation": "nature",
        })

    def test_update_organisation_settings_updates_values(self):
        self.helpers_mod.organisation_rows = [
            {
                "id": "org-456",
                "extraction_strategy": "auto_group",
                "ask_per_upload": False,
                "vlm_enabled": False,
                "supplier_auto_link_min_matches": 2,
                "reporting_standard": "ifrs",
                "income_statement_presentation": "function",
            }
        ]

        payload = UpdateOrganisationSettingsRequest(
            extraction_strategy="vlm",
            ask_per_upload=True,
            supplier_auto_link_min_matches=4,
            reporting_standard="ifrs",
            income_statement_presentation="nature",
        )

        result = update_organisation_settings("org-456", payload, ("user-1", self.fake_supabase))

        self.assertEqual(result, {
            "extraction_strategy": "vlm",
            "ask_per_upload": True,
            "vlm_enabled": False,
            "supplier_auto_link_min_matches": 4,
            "reporting_standard": "ifrs",
            "income_statement_presentation": "nature",
        })

    def test_update_organisation_settings_forces_function_for_us_gaap(self):
        self.helpers_mod.organisation_rows = [
            {
                "id": "org-456",
                "extraction_strategy": "auto_group",
                "ask_per_upload": False,
                "vlm_enabled": False,
                "supplier_auto_link_min_matches": 2,
                "reporting_standard": "ifrs",
                "income_statement_presentation": "nature",
            }
        ]

        payload = UpdateOrganisationSettingsRequest(reporting_standard="us_gaap")

        result = update_organisation_settings("org-456", payload, ("user-1", self.fake_supabase))

        self.assertEqual(result["reporting_standard"], "us_gaap")
        self.assertEqual(result["income_statement_presentation"], "function")

    def test_update_organisation_settings_rejects_nature_for_us_gaap(self):
        payload = UpdateOrganisationSettingsRequest(
            reporting_standard="us_gaap",
            income_statement_presentation="nature",
        )

        with self.assertRaises(Exception) as ctx:
            update_organisation_settings("org-456", payload, ("user-1", self.fake_supabase))

        self.assertEqual(
            getattr(ctx.exception, "detail", str(ctx.exception)),
            "US GAAP requires the Income Statement presentation to be by function",
        )

    def test_update_organisation_settings_rejects_invalid_supplier_auto_link_threshold(self):
        with self.assertRaises(Exception):
            UpdateOrganisationSettingsRequest(supplier_auto_link_min_matches=5)

    def test_update_organisation_settings_rejects_empty_payload(self):
        payload = UpdateOrganisationSettingsRequest()

        with self.assertRaises(Exception) as ctx:
            update_organisation_settings("org-456", payload, ("user-1", self.fake_supabase))

        self.assertEqual(getattr(ctx.exception, "detail", str(ctx.exception)), "No settings were provided to update")

    def test_update_organisation_settings_returns_404_for_missing_org(self):
        payload = UpdateOrganisationSettingsRequest(vlm_enabled=True)

        with self.assertRaises(Exception) as ctx:
            update_organisation_settings("org-missing", payload, ("user-1", self.fake_supabase))

        self.assertEqual(getattr(ctx.exception, "detail", str(ctx.exception)), "Organisation not found")


if __name__ == "__main__":
    unittest.main()
