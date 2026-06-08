import os
import unittest

from fastapi import HTTPException

from app.routers import admin_integrations
from app import dependencies
from app.routers.admin_integrations import (
    ExtractionCriteriaRequest,
    SystemIntegrationCreateRequest,
    add_system_integration,
    get_platform_extraction_criteria,
    put_platform_extraction_criteria,
)
from app.services import ai_provider_fallback
from app.services.integration_secrets import decrypt_secret
from app.services.integration_service import (
    ORG_INTEGRATIONS_TABLE,
    SYSTEM_CRITERIA_TABLE,
    SYSTEM_INTEGRATIONS_TABLE,
    SYSTEM_POLICIES_TABLE,
)


class _Result:
    def __init__(self, data=None):
        self.data = data or []


class _FakeQuery:
    def __init__(self, db, table_name):
        self.db = db
        self.table_name = table_name
        self.operation = None
        self.payload = None
        self.filters = []
        self.limit_count = None
        self.order_field = None
        self.order_desc = False

    def select(self, *_args, **_kwargs):
        self.operation = "select"
        return self

    def insert(self, payload):
        self.operation = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = payload
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def limit(self, count):
        self.limit_count = count
        return self

    def order(self, field, desc=False, **_kwargs):
        self.order_field = field
        self.order_desc = desc
        return self

    def _matches(self, row):
        return all(row.get(key) == value for key, value in self.filters)

    def _rows(self):
        rows = [row for row in self.db.tables.setdefault(self.table_name, []) if self._matches(row)]
        if self.order_field:
            rows.sort(key=lambda row: row.get(self.order_field) or 0, reverse=self.order_desc)
        if self.limit_count is not None:
            rows = rows[: self.limit_count]
        return rows

    def execute(self):
        table = self.db.tables.setdefault(self.table_name, [])
        if self.operation == "insert":
            payload = dict(self.payload)
            payload.setdefault("id", f"{self.table_name}-{len(table) + 1}")
            table.append(payload)
            return _Result([payload])
        if self.operation == "update":
            updated = []
            for row in table:
                if self._matches(row):
                    row.update(self.payload)
                    updated.append(dict(row))
            return _Result(updated)
        if self.operation == "delete":
            deleted = [row for row in table if self._matches(row)]
            self.db.tables[self.table_name] = [row for row in table if not self._matches(row)]
            return _Result(deleted)
        return _Result([dict(row) for row in self._rows()])


class _FakeDB:
    def __init__(self):
        self.tables = {
            SYSTEM_INTEGRATIONS_TABLE: [],
            SYSTEM_POLICIES_TABLE: [],
            SYSTEM_CRITERIA_TABLE: [],
            ORG_INTEGRATIONS_TABLE: [],
            "integration_audit_events": [],
        }

    def table(self, name):
        return _FakeQuery(self, name)


class IntegrationManagementTests(unittest.TestCase):
    def setUp(self):
        self.old_secret = os.environ.get("INTEGRATION_SECRET_KEY")
        self.old_owner = os.environ.get("PLATFORM_OWNER_USER_IDS")
        os.environ["INTEGRATION_SECRET_KEY"] = "test-secret-for-integrations"
        os.environ["PLATFORM_OWNER_USER_IDS"] = "owner-user"
        self.db = _FakeDB()
        self.old_get_supabase_client = admin_integrations.get_supabase_client
        self.old_dependencies_get_supabase_client = dependencies.get_supabase_client
        admin_integrations.get_supabase_client = lambda: self.db
        dependencies.get_supabase_client = lambda: self.db

    def tearDown(self):
        if self.old_secret is None:
            os.environ.pop("INTEGRATION_SECRET_KEY", None)
        else:
            os.environ["INTEGRATION_SECRET_KEY"] = self.old_secret
        if self.old_owner is None:
            os.environ.pop("PLATFORM_OWNER_USER_IDS", None)
        else:
            os.environ["PLATFORM_OWNER_USER_IDS"] = self.old_owner
        admin_integrations.get_supabase_client = self.old_get_supabase_client
        dependencies.get_supabase_client = self.old_dependencies_get_supabase_client

    def test_platform_owner_can_create_system_integration_without_returning_secret(self):
        result = add_system_integration(
            SystemIntegrationCreateRequest(
                provider="gemini",
                capability="vlm",
                display_name="Gemini primary",
                api_key="secret-gemini-key",
                model="gemini-2.5-flash",
            ),
            ("owner-user", object()),
        )

        integration = result["integration"]
        stored = self.db.tables[SYSTEM_INTEGRATIONS_TABLE][0]
        self.assertIsNone(integration["api_key"])
        self.assertNotIn("encrypted_api_key", integration)
        self.assertNotEqual(stored["encrypted_api_key"], "secret-gemini-key")
        self.assertEqual(decrypt_secret(stored["encrypted_api_key"]), "secret-gemini-key")

    def test_non_platform_owner_cannot_create_system_integration(self):
        with self.assertRaises(HTTPException) as ctx:
            add_system_integration(
                SystemIntegrationCreateRequest(
                    provider="gemini",
                    capability="vlm",
                    display_name="Gemini primary",
                    api_key="secret-gemini-key",
                ),
                ("org-admin-user", object()),
            )

        self.assertEqual(ctx.exception.status_code, 403)

    def test_extraction_criteria_are_versioned_and_latest_published_is_returned(self):
        put_platform_extraction_criteria(
            "invoice_vlm_extraction",
            ExtractionCriteriaRequest(
                status="draft",
                prompt_template="Draft prompt",
                criteria={"supplier_exclusions": ["deliver to"]},
            ),
            ("owner-user", object()),
        )
        put_platform_extraction_criteria(
            "invoice_vlm_extraction",
            ExtractionCriteriaRequest(
                status="published",
                prompt_template="Published prompt",
                criteria={"supplier_exclusions": ["deliver to", "ship to"]},
            ),
            ("owner-user", object()),
        )

        result = get_platform_extraction_criteria("invoice_vlm_extraction", ("owner-user", object()))

        self.assertEqual(result["criteria"]["published"]["version"], 2)
        self.assertEqual(result["criteria"]["published"]["prompt_template"], "Published prompt")
        self.assertEqual(result["criteria"]["draft"]["version"], 1)

    def test_vlm_fallback_tries_policy_order_until_provider_succeeds(self):
        from app.services.integration_secrets import encrypt_secret, secret_fingerprint

        self.db.tables[SYSTEM_INTEGRATIONS_TABLE] = [
            {
                "id": "openai-1",
                "provider": "openai",
                "capability": "vlm",
                "enabled": True,
                "model": "gpt-test",
                "encrypted_api_key": encrypt_secret("openai-key"),
                "api_key_fingerprint": secret_fingerprint("openai-key"),
            },
            {
                "id": "gemini-1",
                "provider": "gemini",
                "capability": "vlm",
                "enabled": True,
                "model": "gemini-test",
                "encrypted_api_key": encrypt_secret("gemini-key"),
                "api_key_fingerprint": secret_fingerprint("gemini-key"),
            },
        ]
        self.db.tables[SYSTEM_POLICIES_TABLE] = [
            {
                "id": "policy-1",
                "task": "invoice_vlm_extraction",
                "enabled": True,
                "ordered_integration_ids": ["openai-1", "gemini-1"],
                "config": {},
            }
        ]

        old_runners = dict(ai_provider_fallback.PROVIDER_RUNNERS)
        try:
            ai_provider_fallback.PROVIDER_RUNNERS["openai"] = lambda **_kwargs: {
                "data": None,
                "reason": "rate_limited",
                "error": "rate limited",
            }
            ai_provider_fallback.PROVIDER_RUNNERS["gemini"] = lambda **_kwargs: {
                "data": {"supplier_name_extracted": "PRODEC PAINTS CC", "confidence_score": 0.95},
                "reason": None,
                "error": None,
            }

            result = ai_provider_fallback.extract_with_vlm_fallback(
                b"fake-pdf",
                "application/pdf",
                supabase=self.db,
            )
        finally:
            ai_provider_fallback.PROVIDER_RUNNERS.clear()
            ai_provider_fallback.PROVIDER_RUNNERS.update(old_runners)

        self.assertEqual(result["provider"], "gemini")
        self.assertEqual(result["data"]["supplier_name_extracted"], "PRODEC PAINTS CC")
        self.assertEqual([attempt["provider"] for attempt in result["attempts"]], ["openai", "gemini"])

    def test_openai_adapter_normalises_response_to_invoice_schema(self):
        calls = []

        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "output_text": '{"supplier_name_extracted":"OPENAI SUPPLIER","confidence_score":0.91}'
                }

        old_post = ai_provider_fallback.httpx.post
        try:
            ai_provider_fallback.httpx.post = lambda *args, **kwargs: calls.append((args, kwargs)) or _FakeResponse()
            result = ai_provider_fallback._run_openai_provider(
                integration={"provider": "openai", "model": "gpt-test", "config": {}},
                api_key="openai-key",
                file_bytes=b"fake-image",
                mime_type="image/png",
                prompt="Extract this invoice.",
            )
        finally:
            ai_provider_fallback.httpx.post = old_post

        self.assertEqual(result["data"]["supplier_name_extracted"], "OPENAI SUPPLIER")
        self.assertEqual(calls[0][0][0], "https://api.openai.com/v1/responses")
        self.assertEqual(calls[0][1]["json"]["input"][0]["content"][1]["type"], "input_image")

    def test_anthropic_adapter_normalises_response_to_invoice_schema(self):
        calls = []

        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": '{"supplier_name_extracted":"ANTHROPIC SUPPLIER","confidence_score":0.89}',
                        }
                    ]
                }

        old_post = ai_provider_fallback.httpx.post
        try:
            ai_provider_fallback.httpx.post = lambda *args, **kwargs: calls.append((args, kwargs)) or _FakeResponse()
            result = ai_provider_fallback._run_anthropic_provider(
                integration={"provider": "anthropic", "model": "claude-test", "config": {}},
                api_key="anthropic-key",
                file_bytes=b"fake-image",
                mime_type="image/png",
                prompt="Extract this invoice.",
            )
        finally:
            ai_provider_fallback.httpx.post = old_post

        self.assertEqual(result["data"]["supplier_name_extracted"], "ANTHROPIC SUPPLIER")
        self.assertEqual(calls[0][0][0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(calls[0][1]["json"]["messages"][0]["content"][1]["type"], "image")


if __name__ == "__main__":
    unittest.main()
