import importlib.util
import pathlib
import sys
import types
import unittest

# Stub standard dependencies not installed in the test environment.
if "fastapi" not in sys.modules:
    fastapi_stub = types.ModuleType("fastapi")
    fastapi_stub.HTTPException = type("HTTPException", (Exception,), {})
    sys.modules["fastapi"] = fastapi_stub

if "supabase" not in sys.modules:
    supabase_stub = types.ModuleType("supabase")
    supabase_stub.Client = type("Client", (), {})
    supabase_stub.create_client = lambda url, key: object()
    sys.modules["supabase"] = supabase_stub

# Stub app package modules required by _helpers.py without importing the full app package.
if "app" not in sys.modules:
    sys.modules["app"] = types.ModuleType("app")
if "app.db" not in sys.modules:
    sys.modules["app.db"] = types.ModuleType("app.db")
if "app.db.supabase_client" not in sys.modules:
    supabase_client_module = types.ModuleType("app.db.supabase_client")
    supabase_client_module.get_supabase_client = lambda: object()
    sys.modules["app.db.supabase_client"] = supabase_client_module
if "app.services" not in sys.modules:
    sys.modules["app.services"] = types.ModuleType("app.services")
if "app.services.audit_log" not in sys.modules:
    audit_log_module = types.ModuleType("app.services.audit_log")
    audit_log_module.log_invoice_event = lambda *args, **kwargs: None
    sys.modules["app.services.audit_log"] = audit_log_module
if "app.services.invoice_extraction" not in sys.modules:
    sys.modules["app.services.invoice_extraction"] = types.ModuleType("app.services.invoice_extraction")
if "app.services.invoice_extraction.file_naming" not in sys.modules:
    file_naming_module = types.ModuleType("app.services.invoice_extraction.file_naming")
    file_naming_module.build_invoice_storage_filename = lambda **kwargs: "invoice.pdf"
    sys.modules["app.services.invoice_extraction.file_naming"] = file_naming_module
if "app.services.invoice_data_builders" not in sys.modules:
    data_builders_module = types.ModuleType("app.services.invoice_data_builders")
    data_builders_module.utc_now_iso = lambda: "2026-05-27T00:00:00Z"
    sys.modules["app.services.invoice_data_builders"] = data_builders_module

helpers_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "invoice_extraction_service" / "_helpers.py"
spec = importlib.util.spec_from_file_location("invoice_extraction_helpers", helpers_path)
helpers = importlib.util.module_from_spec(spec)
sys.modules["invoice_extraction_helpers"] = helpers
spec.loader.exec_module(helpers)


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    def __init__(self, table_name: str, supabase: "_FakeSupabase"):
        self.table_name = table_name
        self.supabase = supabase
        self.payload = None

    def select(self, *_args):
        return self

    def update(self, payload):
        self.payload = payload
        return self

    def insert(self, payload):
        self.payload = payload
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        if self.table_name == "organisations":
            return _FakeResult(self.supabase.organisation_rows)
        if self.table_name == "invoices_raw":
            self.supabase.last_update_payload = self.payload
            return _FakeResult([self.payload])
        if self.table_name == "invoice_page_groups":
            payload = self.payload if isinstance(self.payload, list) else [self.payload]
            inserted = [{"id": "page-group-1", **payload[0]}]
            self.supabase.last_insert_payload = payload[0]
            return _FakeResult(inserted)
        return _FakeResult([])


class _FakeSupabase:
    def __init__(self, organisation_rows=None):
        self.organisation_rows = organisation_rows or []
        self.last_update_payload = None
        self.last_insert_payload = None

    def table(self, name):
        return _FakeTable(name, self)


class InvoiceExtractionGroupingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake_supabase = _FakeSupabase()
        setattr(helpers, "supabase", self.fake_supabase)

    def test_get_organisation_extraction_settings_returns_defaults_when_missing(self):
        self.fake_supabase.organisation_rows = []

        result = helpers.get_organisation_extraction_settings("org-1")

        self.assertEqual(result["extraction_strategy"], "auto_group")
        self.assertFalse(result["ask_per_upload"])
        self.assertFalse(result["vlm_enabled"])

    def test_get_organisation_extraction_settings_preserves_values(self):
        self.fake_supabase.organisation_rows = [
            {
                "extraction_strategy": "vlm",
                "ask_per_upload": True,
                "vlm_enabled": True,
            }
        ]

        result = helpers.get_organisation_extraction_settings("org-1")

        self.assertEqual(result["extraction_strategy"], "vlm")
        self.assertTrue(result["ask_per_upload"])
        self.assertTrue(result["vlm_enabled"])

    def test_update_invoice_raw_grouping_updates_invoices_raw_table(self):
        helpers.update_invoice_raw_grouping(
            invoice_raw_id="raw-1",
            page_numbers=[1, 2, 3],
            strategy="auto_group",
            total_pages=3,
        )

        self.assertIsNotNone(self.fake_supabase.last_update_payload)
        self.assertEqual(self.fake_supabase.last_update_payload["grouped_from_pages"], [1, 2, 3])
        self.assertEqual(self.fake_supabase.last_update_payload["page_grouping_strategy"], "auto_group")
        self.assertEqual(self.fake_supabase.last_update_payload["total_pages_in_upload"], 3)
        self.assertIn("updated_at", self.fake_supabase.last_update_payload)

    def test_persist_invoice_page_group_inserts_expected_metadata(self):
        group = helpers.persist_invoice_page_group(
            invoice_raw_id="raw-1",
            page_numbers=[1, 2],
            strategy="auto_group",
            supplier_detected="Example Supplier",
            confidence=0.92,
        )

        self.assertIsNotNone(self.fake_supabase.last_insert_payload)
        self.assertEqual(self.fake_supabase.last_insert_payload["invoice_raw_id"], "raw-1")
        self.assertEqual(self.fake_supabase.last_insert_payload["page_numbers"], [1, 2])
        self.assertEqual(self.fake_supabase.last_insert_payload["strategy"], "auto_group")
        self.assertEqual(self.fake_supabase.last_insert_payload["supplier_detected"], "Example Supplier")
        self.assertEqual(self.fake_supabase.last_insert_payload["confidence"], 0.92)
        self.assertEqual(group["id"], "page-group-1")


if __name__ == "__main__":
    unittest.main()
