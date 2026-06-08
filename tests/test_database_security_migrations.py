import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI_MIGRATIONS = ROOT / "supabase/migrations"
LEGACY_REMOTE_MIGRATIONS = {
    "20260530074217_im_whatsapp_pending_selections_rls_phase_b33.sql",
    "20260530080814_im_reconciliation_results_rls_phase_b34.sql",
}
BASELINE_MIGRATIONS = {
    "20260530070000_production_public_baseline.sql",
    "20260530071000_production_storage_baseline.sql",
}


def test_c20_hardens_user_organisations_view() -> None:
    migration = (
        ROOT / "app/db/applied/user_organisations_security_invoker_phase_c20.sql"
    ).read_text(encoding="utf-8")

    assert (
        "ALTER VIEW public.user_organisations\n"
        "  SET (security_invoker = true);"
    ) in migration
    assert (
        "REVOKE ALL PRIVILEGES ON TABLE public.user_organisations FROM PUBLIC;"
    ) in migration
    assert (
        "REVOKE ALL PRIVILEGES ON TABLE public.user_organisations FROM anon;"
    ) in migration
    assert (
        "GRANT SELECT ON TABLE public.user_organisations\n"
        "  TO authenticated, service_role;"
    ) in migration
    assert (
        "GRANT SELECT ON TABLE public.organisation_users\n"
        "  TO authenticated, service_role;"
    ) in migration


def test_user_organisations_view_keeps_active_membership_filter() -> None:
    migration = (
        ROOT / "app/db/applied/organisation_management.sql"
    ).read_text(encoding="utf-8")

    assert "create or replace view public.user_organisations as" in migration
    assert "from public.organisation_users" in migration
    assert "where status = 'active';" in migration


def test_cli_public_table_migrations_declare_data_api_access() -> None:
    failures: list[str] = []

    for path in sorted(CLI_MIGRATIONS.glob("*.sql")):
        if path.name in LEGACY_REMOTE_MIGRATIONS or "baseline" in path.stem:
            continue

        sql = path.read_text(encoding="utf-8")
        table_names = re.findall(
            r"create\s+table(?:\s+if\s+not\s+exists)?\s+public\.([a-z0-9_]+)",
            sql,
            flags=re.IGNORECASE,
        )

        for table_name in table_names:
            qualified_table = rf"public\.{re.escape(table_name)}"
            required_patterns = {
                "RLS": (
                    rf"alter\s+table\s+{qualified_table}\s+"
                    r"enable\s+row\s+level\s+security\s*;"
                ),
                "anonymous revoke": (
                    rf"revoke\s+all(?:\s+privileges)?\s+on\s+"
                    rf"(?:table\s+)?{qualified_table}\s+from\s+"
                    r"(?:public\s*,\s*anon|anon\s*,\s*public|anon)\s*;"
                ),
                "service-role grant": (
                    rf"grant\s+[^;]+\s+on\s+(?:table\s+)?{qualified_table}\s+"
                    r"to\s+[^;]*\bservice_role\b[^;]*;"
                ),
            }

            for requirement, pattern in required_patterns.items():
                if not re.search(pattern, sql, flags=re.IGNORECASE):
                    failures.append(f"{path.name}: {table_name} is missing {requirement}")

    assert not failures, "\n".join(failures)


def test_supabase_cli_uses_explicit_data_api_grants() -> None:
    config = (ROOT / "supabase/config.toml").read_text(encoding="utf-8")

    assert "auto_expose_new_tables = false" in config


def test_known_remote_migrations_are_tracked_by_exact_version() -> None:
    tracked = {path.name for path in CLI_MIGRATIONS.glob("*.sql")}

    assert LEGACY_REMOTE_MIGRATIONS <= tracked
    assert BASELINE_MIGRATIONS <= tracked


def test_production_baseline_contains_c19_and_c20() -> None:
    baseline = (
        CLI_MIGRATIONS / "20260530070000_production_public_baseline.sql"
    ).read_text(encoding="utf-8")

    for function_name in (
        "delete_bank_statement_lines_atomic",
        "delete_bank_statement_uploads_atomic",
        "get_bank_account_balance_summary",
    ):
        assert f'FUNCTION "public"."{function_name}"' in baseline
    assert (
        'VIEW "public"."user_organisations" WITH ("security_invoker"=\'true\')'
        in baseline
    )


def test_storage_baseline_contains_live_buckets_and_policies() -> None:
    baseline = (
        CLI_MIGRATIONS / "20260530071000_production_storage_baseline.sql"
    ).read_text(encoding="utf-8")

    for bucket in (
        "invoices",
        "kyc-documents",
        "statement-files",
        "organisation-branding",
    ):
        assert f"'{bucket}'" in baseline
    for policy in (
        "Allow authenticated invoice reads",
        "organisation_branding_select_member",
        "statement_files_select_member",
    ):
        assert f'create policy "{policy}"' in baseline


def test_supabase_cli_wrapper_is_version_pinned() -> None:
    wrapper = (ROOT / "scripts/supabase.cmd").read_text(encoding="utf-8")

    assert "supabase@2.105.0" in wrapper


def test_seed_contains_configuration_not_business_data() -> None:
    seed = (ROOT / "supabase/seed.sql").read_text(encoding="utf-8").lower()

    assert "insert into public.themes" in seed
    for table in (
        "organisations",
        "organisation_users",
        "invoices_raw",
        "invoices_extracted",
        "suppliers",
        "bank_statement_lines",
    ):
        assert f"insert into public.{table}" not in seed
