-- ============================================================
-- Organisation Management — Phase 1
-- Adds richer organisation columns, an organisation_users
-- membership table with roles, RLS, and a backwards-compatible
-- user_organisations view so existing policies keep working.
-- Apply this migration via the Lovable Cloud migration tool.
-- ============================================================

-- 1. Extend organisations table -----------------------------------
alter table public.organisations
  add column if not exists legal_name text,
  add column if not exists registration_number text,
  add column if not exists vat_number text,
  add column if not exists tax_number text,
  add column if not exists country text,
  add column if not exists base_currency text,
  add column if not exists financial_year_end text,
  add column if not exists status text not null default 'active'
    check (status in ('active', 'suspended', 'archived')),
  add column if not exists created_at timestamptz not null default now(),
  add column if not exists updated_at timestamptz not null default now();

-- 2. Roles enum --------------------------------------------------
do $$ begin
  create type public.organisation_role as enum
    ('owner', 'admin', 'accountant', 'reviewer', 'viewer', 'client');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.membership_status as enum
    ('active', 'invited', 'suspended', 'revoked');
exception when duplicate_object then null; end $$;

-- 3. organisation_users membership table -------------------------
create table if not exists public.organisation_users (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  user_id uuid references auth.users(id) on delete cascade,
  role public.organisation_role not null default 'viewer',
  status public.membership_status not null default 'active',
  invited_email text,
  invited_at timestamptz,
  accepted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organisation_id, user_id)
);

create index if not exists organisation_users_org_idx
  on public.organisation_users(organisation_id);
create index if not exists organisation_users_user_idx
  on public.organisation_users(user_id);

-- 4. Backfill from legacy user_organisations (if it exists) -------
do $$
begin
  if exists (
    select 1 from information_schema.tables
    where table_schema = 'public' and table_name = 'user_organisations'
  ) then
    -- Only copy rows whose target table is the legacy one (not the view we create below)
    if (select c.relkind from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname='public' and c.relname='user_organisations') = 'r' then
      insert into public.organisation_users (organisation_id, user_id, role, status)
      select uo.organisation_id, uo.user_id,
             coalesce(nullif(uo.role, '')::public.organisation_role, 'owner'),
             'active'::public.membership_status
      from public.user_organisations uo
      where uo.user_id is not null and uo.organisation_id is not null
      on conflict (organisation_id, user_id) do nothing;

      -- Replace legacy table with a view for backwards compatibility
      drop table public.user_organisations cascade;
    end if;
  end if;
end $$;

-- 5. Backwards-compatible view -----------------------------------
create or replace view public.user_organisations as
  select id, organisation_id, user_id, role::text as role, created_at
  from public.organisation_users
  where status = 'active';

-- 6. Helper: is_org_member ---------------------------------------
create or replace function public.is_org_member(_org_id uuid)
returns boolean
language sql stable security definer set search_path = public
as $$
  select exists (
    select 1 from public.organisation_users
    where organisation_id = _org_id
      and user_id = auth.uid()
      and status = 'active'
  );
$$;

create or replace function public.has_org_role(_org_id uuid, _roles public.organisation_role[])
returns boolean
language sql stable security definer set search_path = public
as $$
  select exists (
    select 1 from public.organisation_users
    where organisation_id = _org_id
      and user_id = auth.uid()
      and status = 'active'
      and role = any(_roles)
  );
$$;

-- 7. RLS ---------------------------------------------------------
alter table public.organisations enable row level security;
alter table public.organisation_users enable row level security;

-- organisations: members can select; owners/admins can update; any authenticated user can insert (becomes owner via trigger below).
drop policy if exists "organisations_select_member" on public.organisations;
create policy "organisations_select_member"
  on public.organisations for select to authenticated
  using (public.is_org_member(id));

drop policy if exists "organisations_insert_authenticated" on public.organisations;
create policy "organisations_insert_authenticated"
  on public.organisations for insert to authenticated
  with check (auth.uid() is not null);

drop policy if exists "organisations_update_admins" on public.organisations;
create policy "organisations_update_admins"
  on public.organisations for update to authenticated
  using (public.has_org_role(id, array['owner','admin']::public.organisation_role[]))
  with check (public.has_org_role(id, array['owner','admin']::public.organisation_role[]));

-- organisation_users: members can see their org's memberships; owners/admins manage.
drop policy if exists "org_users_select_member" on public.organisation_users;
create policy "org_users_select_member"
  on public.organisation_users for select to authenticated
  using (
    user_id = auth.uid()
    or public.is_org_member(organisation_id)
  );

drop policy if exists "org_users_insert_self_or_admin" on public.organisation_users;
create policy "org_users_insert_self_or_admin"
  on public.organisation_users for insert to authenticated
  with check (
    -- owners/admins may add users to organisations they already manage
    public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[])
    or (
      -- bootstrap: a user may insert themselves as owner only when the
      -- organisation currently has no active members.
      user_id = auth.uid()
      and role = 'owner'::public.organisation_role
      and status = 'active'::public.membership_status
      and not exists (
        select 1
        from public.organisation_users ou
        where ou.organisation_id = organisation_users.organisation_id
          and ou.status = 'active'
      )
    )
  );

drop policy if exists "org_users_update_admin" on public.organisation_users;
create policy "org_users_update_admin"
  on public.organisation_users for update to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop policy if exists "org_users_delete_admin" on public.organisation_users;
create policy "org_users_delete_admin"
  on public.organisation_users for delete to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

-- 8. updated_at trigger ------------------------------------------
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

drop trigger if exists organisations_set_updated_at on public.organisations;
create trigger organisations_set_updated_at
  before update on public.organisations
  for each row execute function public.set_updated_at();

drop trigger if exists organisation_users_set_updated_at on public.organisation_users;
create trigger organisation_users_set_updated_at
  before update on public.organisation_users
  for each row execute function public.set_updated_at();

-- 9. Backfill memberships for existing data ----------------------
-- Idempotent: ensures every user that has previously uploaded an
-- invoice (or any row referencing an organisation) becomes an active
-- owner of that organisation. Safe to re-run.
insert into public.organisation_users (organisation_id, user_id, role, status, accepted_at)
select distinct i.organisation_id, i.uploaded_by, 'owner'::public.organisation_role, 'active'::public.membership_status, now()
from public.invoices_raw i
where i.uploaded_by is not null
  and i.organisation_id is not null
on conflict (organisation_id, user_id) do nothing;
