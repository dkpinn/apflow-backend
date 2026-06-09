# Database Migrations

## Current Workflow

`app/db/applied/` is the historical record of SQL that was run manually before
Supabase CLI tracking was established. Do not replay or edit those files.

All new database changes belong in:

```text
supabase/migrations/<UTC timestamp>_<description>.sql
```

Use the pinned CLI wrapper from the repository root:

```powershell
scripts\supabase.cmd migration list
scripts\supabase.cmd db reset
scripts\supabase.cmd db push --dry-run
scripts\supabase.cmd db push
```

Production pushes are manual. Review `db push --dry-run` before every push.
Dashboard SQL Editor changes bypass migration history and are reserved for
emergency repairs that are immediately captured with `db pull`.

## Development Project

Development must use a separate Supabase project and untracked credentials in
`.env.development.local`, based on `.env.development.example`. Never point the
development bootstrap at the production project ref. The development project
ref is `ykhfrekhxdrsalwmalgt`.

After applying migrations to the development project, grant the demo account
Platform Owner access with:

```powershell
.\.venv\Scripts\python.exe scripts\seed_dev_test_data.py
.\.venv\Scripts\python.exe scripts\bootstrap_dev_platform_owner.py
```

Both scripts require `APFLOW_ENV=development`, verify
`DEV_SUPABASE_PROJECT_REF` against `SUPABASE_URL`, and refuses the production
project.

The production project ref is `arueantocclxnziipwdf`. CLI access tokens and the
database password must remain in the operating system credential store or
untracked environment variables.

The Dashboard's top-level **Connect** button configures external clients or
repository integration. It is not a database health or SQL connectivity
indicator.

## Data API Access

Supabase Data API access is controlled by both PostgreSQL grants and row-level
security (RLS). Every migration that creates a table in the exposed `public`
schema must declare both explicitly.

Use the minimum privileges required by the feature:

```sql
CREATE TABLE public.example_records (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id UUID NOT NULL REFERENCES public.organisations(id)
);

ALTER TABLE public.example_records ENABLE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON TABLE public.example_records FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE public.example_records FROM anon;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.example_records
  TO service_role;

-- Add only the operations used by authenticated Data API callers.
GRANT SELECT ON TABLE public.example_records TO authenticated;
```

Then add policies for every operation granted to `authenticated`. A grant makes
the object reachable through PostgREST; RLS determines which rows the caller
may access. Do not grant `anon` unless the table is intentionally public.

Functions exposed as RPCs need the same treatment:

```sql
REVOKE ALL ON FUNCTION public.example_rpc(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.example_rpc(UUID)
  TO authenticated, service_role;
```

Tables using identity or serial columns may also require explicit sequence
privileges. Existing applied migrations remain immutable; use a follow-up
migration when access for an applied object needs to change.

## Production Baseline

Production currently contains schema changes that predate CLI migration
tracking, including C19 and C20. The authoritative baseline must be generated
from the live database, reviewed, and assigned a version earlier than the two
existing remote migration records:

```text
20260530070000_production_public_baseline.sql
20260530071000_production_storage_baseline.sql
20260530074217_im_whatsapp_pending_selections_rls_phase_b33.sql
20260530080814_im_reconciliation_results_rls_phase_b34.sql
```

Back up first. Mark baseline versions as applied with `migration repair`; never
push a baseline into the production database that it describes.
