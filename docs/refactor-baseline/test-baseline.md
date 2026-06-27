# Test Baseline

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Result:

- 441 passed
- 3 failed
- 4 warnings

Current failures:

1. `tests/test_bank_uploads.py::test_extract_bank_upload_happy_path`
   - `correct_amounts_from_balance()` received dict lines from the patched extractor path and expected objects with `balance_amount`.
   - Failure surfaced as `HTTPException: 500: 'dict' object has no attribute 'balance_amount'`.

2. `tests/test_database_security_migrations.py::test_cli_public_table_migrations_declare_data_api_access`
   - Several recent CLI migrations are missing expected RLS/revoke/service-role grant declarations.
   - Affected migration files include inventory, finance leases, accounting periods, and supplier payment run drafts.

3. `tests/test_integration_management.py::IntegrationManagementTests::test_lm_studio_adapter_uses_local_openai_compatible_chat_endpoint`
   - `_run_lm_studio_provider()` returned `data=None`, so the test failed while reading `supplier_name_extracted`.

Warnings:

- Supabase client deprecation warnings for `timeout` and `verify`.
- Pytest cache write warnings caused by denied access to `.pytest_cache`.

Refactor status:

- Do not begin Step 2 until these baseline failures are either fixed or explicitly accepted as known pre-existing failures.
