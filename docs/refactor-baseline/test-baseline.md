# Test Baseline

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Result:

- 444 passed
- 3 warnings

Stabilization fixes applied before refactoring:

1. `tests/test_bank_uploads.py::test_extract_bank_upload_happy_path`
   - Bank balance helpers now tolerate dict-shaped test doubles while preserving `ParsedBankLine` behavior.

2. `tests/test_database_security_migrations.py::test_cli_public_table_migrations_declare_data_api_access`
   - Recent deploy migrations now declare explicit RLS, revoke, and service-role grants in addition to dynamic policy setup.

3. `tests/test_integration_management.py::IntegrationManagementTests::test_lm_studio_adapter_uses_local_openai_compatible_chat_endpoint`
   - LM Studio response logging now tolerates fake response objects used by adapter tests.

Warnings:

- Supabase client deprecation warnings for `timeout` and `verify`.
- Pytest cache write warnings caused by denied access to `.pytest_cache`.

Refactor status:

- Step 2 may begin from this green baseline.
