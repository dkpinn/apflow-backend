-- Sprint P0: Accounting periods, close status, and lock dates.
-- Idempotent.

create table if not exists public.organisation_accounting_periods (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  period_start date not null,
  period_end date not null,
  status text not null default 'open'
    check (status in ('open', 'closed', 'locked')),
  lock_date date,
  checklist jsonb not null default '{}'::jsonb
    check (jsonb_typeof(checklist) = 'object'),
  notes text not null default '',
  closed_by uuid references auth.users(id) on delete set null,
  closed_at timestamptz,
  locked_by uuid references auth.users(id) on delete set null,
  locked_at timestamptz,
  reopened_by uuid references auth.users(id) on delete set null,
  reopened_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organisation_id, period_start),
  check (period_start <= period_end),
  check (lock_date is null or lock_date <= period_end)
);

create index if not exists organisation_accounting_periods_org_idx
  on public.organisation_accounting_periods(organisation_id, period_start desc);

alter table public.organisation_accounting_periods enable row level security;

drop policy if exists "organisation_accounting_periods_select_member"
  on public.organisation_accounting_periods;
create policy "organisation_accounting_periods_select_member"
  on public.organisation_accounting_periods for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "organisation_accounting_periods_write_admin"
  on public.organisation_accounting_periods;
create policy "organisation_accounting_periods_write_admin"
  on public.organisation_accounting_periods for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin']::public.organisation_role[]));

drop trigger if exists organisation_accounting_periods_set_updated_at
  on public.organisation_accounting_periods;
create trigger organisation_accounting_periods_set_updated_at
  before update on public.organisation_accounting_periods
  for each row execute function public.set_updated_at();
