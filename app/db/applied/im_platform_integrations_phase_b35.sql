-- Phase B35: Platform and organisation integration configuration.
-- Platform integrations are service-role/backend only unless a user is listed
-- in platform_admin_users. Organisation integrations are scoped by org role.

create table if not exists public.platform_admin_users (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  role text not null default 'owner' check (role in ('owner')),
  status text not null default 'active' check (status in ('active','revoked')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id)
);

create table if not exists public.system_integration_configs (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  capability text not null,
  display_name text not null,
  enabled boolean not null default true,
  model text,
  base_url text,
  config jsonb not null default '{}'::jsonb,
  encrypted_api_key text,
  api_key_fingerprint text,
  api_key_mask_hint text,
  last_test_status text,
  last_test_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists system_integration_configs_capability_idx
  on public.system_integration_configs(capability, enabled);

create table if not exists public.system_integration_policies (
  id uuid primary key default gen_random_uuid(),
  task text not null unique,
  enabled boolean not null default true,
  ordered_integration_ids uuid[] not null default '{}'::uuid[],
  config jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.system_extraction_criteria_versions (
  id uuid primary key default gen_random_uuid(),
  task text not null,
  version integer not null,
  status text not null default 'draft' check (status in ('draft','published','archived')),
  prompt_template text,
  criteria jsonb not null default '{}'::jsonb,
  notes text,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  unique (task, version)
);

create index if not exists system_extraction_criteria_task_status_idx
  on public.system_extraction_criteria_versions(task, status, version desc);

create table if not exists public.organisation_integration_configs (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  provider text not null,
  capability text not null,
  display_name text not null,
  enabled boolean not null default true,
  model text,
  base_url text,
  config jsonb not null default '{}'::jsonb,
  encrypted_api_key text,
  api_key_fingerprint text,
  api_key_mask_hint text,
  last_test_status text,
  last_test_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists organisation_integration_configs_org_idx
  on public.organisation_integration_configs(organisation_id, provider, capability);

create table if not exists public.integration_audit_events (
  id uuid primary key default gen_random_uuid(),
  event_type text not null,
  actor_user_id uuid references auth.users(id) on delete set null,
  integration_scope text not null check (integration_scope in ('system','organisation')),
  integration_id uuid,
  organisation_id uuid references public.organisations(id) on delete cascade,
  provider text,
  capability text,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists integration_audit_events_org_idx
  on public.integration_audit_events(organisation_id, created_at desc);

create index if not exists integration_audit_events_scope_idx
  on public.integration_audit_events(integration_scope, created_at desc);

alter table public.platform_admin_users enable row level security;
alter table public.system_integration_configs enable row level security;
alter table public.system_integration_policies enable row level security;
alter table public.system_extraction_criteria_versions enable row level security;
alter table public.organisation_integration_configs enable row level security;
alter table public.integration_audit_events enable row level security;

-- Integration config tables intentionally have no authenticated-user policies.
-- The backend accesses them after ensure_platform_owner()/ensure_org_* checks,
-- using service-role, so encrypted credential blobs are never exposed through
-- direct Supabase client reads.
drop policy if exists "organisation_integrations_select_org_member" on public.organisation_integration_configs;
drop policy if exists "organisation_integrations_insert_admins" on public.organisation_integration_configs;
drop policy if exists "organisation_integrations_update_admins" on public.organisation_integration_configs;
drop policy if exists "organisation_integrations_delete_admins" on public.organisation_integration_configs;

drop policy if exists "integration_audit_events_select_org_member" on public.integration_audit_events;
create policy "integration_audit_events_select_org_member"
  on public.integration_audit_events for select to authenticated
  using (
    organisation_id is not null
    and public.is_org_member(organisation_id)
  );

drop trigger if exists platform_admin_users_set_updated_at on public.platform_admin_users;
create trigger platform_admin_users_set_updated_at
  before update on public.platform_admin_users
  for each row execute function public.set_updated_at();

drop trigger if exists system_integration_configs_set_updated_at on public.system_integration_configs;
create trigger system_integration_configs_set_updated_at
  before update on public.system_integration_configs
  for each row execute function public.set_updated_at();

drop trigger if exists system_integration_policies_set_updated_at on public.system_integration_policies;
create trigger system_integration_policies_set_updated_at
  before update on public.system_integration_policies
  for each row execute function public.set_updated_at();

drop trigger if exists organisation_integration_configs_set_updated_at on public.organisation_integration_configs;
create trigger organisation_integration_configs_set_updated_at
  before update on public.organisation_integration_configs
  for each row execute function public.set_updated_at();

select 'im_platform_integrations_phase_b35_applied' as migration_note;
