-- ============================================================
-- Group Consolidation and Joint Venture Accounting - Phase 1
-- Adds management-reporting consolidation structures above the
-- entity-level ledger. Entity ledgers are not overwritten; group
-- adjustments are stored separately.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

create table if not exists public.reporting_groups (
  id uuid primary key default gen_random_uuid(),
  owner_organisation_id uuid not null references public.organisations(id) on delete cascade,
  name text not null,
  reporting_currency text not null default 'ZAR',
  country text,
  status text not null default 'active'
    check (status in ('active', 'archived')),
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (owner_organisation_id, name)
);

create table if not exists public.reporting_group_users (
  id uuid primary key default gen_random_uuid(),
  reporting_group_id uuid not null references public.reporting_groups(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  role text not null default 'viewer'
    check (role in ('owner', 'admin', 'accountant', 'reviewer', 'viewer')),
  status text not null default 'active'
    check (status in ('active', 'invited', 'suspended', 'revoked')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (reporting_group_id, user_id)
);

create table if not exists public.reporting_group_entities (
  id uuid primary key default gen_random_uuid(),
  reporting_group_id uuid not null references public.reporting_groups(id) on delete cascade,
  parent_entity_id uuid references public.reporting_group_entities(id) on delete set null,
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  entity_type text not null
    check (entity_type in ('parent', 'subsidiary', 'associate', 'joint_venture')),
  ownership_percent numeric(7,4) not null default 100
    check (ownership_percent >= 0 and ownership_percent <= 100),
  consolidation_method text not null default 'full'
    check (consolidation_method in ('full', 'proportionate', 'equity', 'none')),
  effective_from date not null default current_date,
  effective_to date,
  sort_order integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (effective_to is null or effective_to >= effective_from),
  unique (reporting_group_id, organisation_id, effective_from)
);

create table if not exists public.consolidation_periods (
  id uuid primary key default gen_random_uuid(),
  reporting_group_id uuid not null references public.reporting_groups(id) on delete cascade,
  name text not null,
  start_date date not null,
  end_date date not null,
  reporting_currency text not null default 'ZAR',
  status text not null default 'draft'
    check (status in ('draft', 'open', 'locked', 'closed')),
  locked_at timestamptz,
  locked_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (end_date >= start_date),
  unique (reporting_group_id, start_date, end_date)
);

create table if not exists public.exchange_rates (
  id uuid primary key default gen_random_uuid(),
  reporting_group_id uuid references public.reporting_groups(id) on delete cascade,
  period_id uuid references public.consolidation_periods(id) on delete cascade,
  from_currency text not null,
  to_currency text not null,
  rate_type text not null default 'closing'
    check (rate_type in ('closing', 'average', 'historical', 'spot')),
  rate_date date not null,
  rate numeric(20,10) not null check (rate > 0),
  source text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (reporting_group_id, period_id, from_currency, to_currency, rate_type, rate_date)
);

create table if not exists public.consolidation_account_mappings (
  id uuid primary key default gen_random_uuid(),
  reporting_group_id uuid not null references public.reporting_groups(id) on delete cascade,
  entity_organisation_id uuid not null references public.organisations(id) on delete cascade,
  local_account_id uuid not null references public.accounts(id) on delete cascade,
  group_account_id uuid not null references public.accounts(id) on delete cascade,
  effective_from date not null default current_date,
  effective_to date,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (effective_to is null or effective_to >= effective_from),
  unique (reporting_group_id, entity_organisation_id, local_account_id, effective_from)
);

-- Interim source table for consolidation reports. Once the GL layer exists,
-- this can be replaced by or populated from a posted GL trial-balance view.
create table if not exists public.consolidation_entity_balances (
  id uuid primary key default gen_random_uuid(),
  reporting_group_id uuid not null references public.reporting_groups(id) on delete cascade,
  period_id uuid not null references public.consolidation_periods(id) on delete cascade,
  entity_organisation_id uuid not null references public.organisations(id) on delete cascade,
  account_id uuid not null references public.accounts(id) on delete cascade,
  currency text not null,
  debit_amount numeric(20,4) not null default 0,
  credit_amount numeric(20,4) not null default 0,
  source_type text not null default 'trial_balance_import',
  source_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (debit_amount >= 0 and credit_amount >= 0),
  unique (reporting_group_id, period_id, entity_organisation_id, account_id, source_type, source_id)
);

create table if not exists public.consolidation_adjustments (
  id uuid primary key default gen_random_uuid(),
  reporting_group_id uuid not null references public.reporting_groups(id) on delete cascade,
  period_id uuid not null references public.consolidation_periods(id) on delete cascade,
  adjustment_type text not null default 'manual'
    check (adjustment_type in ('elimination', 'reclassification', 'minority_interest', 'manual', 'fx')),
  description text not null,
  status text not null default 'draft'
    check (status in ('draft', 'posted', 'reversed')),
  created_by uuid references auth.users(id) on delete set null,
  posted_by uuid references auth.users(id) on delete set null,
  posted_at timestamptz,
  reversed_by uuid references auth.users(id) on delete set null,
  reversed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.consolidation_adjustment_lines (
  id uuid primary key default gen_random_uuid(),
  adjustment_id uuid not null references public.consolidation_adjustments(id) on delete cascade,
  line_number integer not null,
  account_id uuid not null references public.accounts(id) on delete restrict,
  entity_organisation_id uuid references public.organisations(id) on delete set null,
  description text,
  debit_amount numeric(20,4) not null default 0,
  credit_amount numeric(20,4) not null default 0,
  created_at timestamptz not null default now(),
  check (debit_amount >= 0 and credit_amount >= 0),
  unique (adjustment_id, line_number)
);

create index if not exists reporting_groups_owner_org_idx
  on public.reporting_groups(owner_organisation_id);
create index if not exists reporting_group_users_user_idx
  on public.reporting_group_users(user_id);
create index if not exists reporting_group_entities_group_idx
  on public.reporting_group_entities(reporting_group_id);
create index if not exists reporting_group_entities_org_idx
  on public.reporting_group_entities(organisation_id);
create index if not exists consolidation_periods_group_idx
  on public.consolidation_periods(reporting_group_id);
create index if not exists exchange_rates_lookup_idx
  on public.exchange_rates(reporting_group_id, period_id, from_currency, to_currency, rate_type, rate_date);
create index if not exists consolidation_account_mappings_lookup_idx
  on public.consolidation_account_mappings(reporting_group_id, entity_organisation_id, local_account_id);
create index if not exists consolidation_entity_balances_report_idx
  on public.consolidation_entity_balances(reporting_group_id, period_id, entity_organisation_id, account_id);
create index if not exists consolidation_adjustments_period_idx
  on public.consolidation_adjustments(reporting_group_id, period_id, status);
create index if not exists consolidation_adjustment_lines_adjustment_idx
  on public.consolidation_adjustment_lines(adjustment_id);

create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

drop trigger if exists reporting_groups_set_updated_at on public.reporting_groups;
create trigger reporting_groups_set_updated_at
  before update on public.reporting_groups
  for each row execute function public.set_updated_at();

drop trigger if exists reporting_group_users_set_updated_at on public.reporting_group_users;
create trigger reporting_group_users_set_updated_at
  before update on public.reporting_group_users
  for each row execute function public.set_updated_at();

drop trigger if exists reporting_group_entities_set_updated_at on public.reporting_group_entities;
create trigger reporting_group_entities_set_updated_at
  before update on public.reporting_group_entities
  for each row execute function public.set_updated_at();

drop trigger if exists consolidation_periods_set_updated_at on public.consolidation_periods;
create trigger consolidation_periods_set_updated_at
  before update on public.consolidation_periods
  for each row execute function public.set_updated_at();

drop trigger if exists exchange_rates_set_updated_at on public.exchange_rates;
create trigger exchange_rates_set_updated_at
  before update on public.exchange_rates
  for each row execute function public.set_updated_at();

drop trigger if exists consolidation_account_mappings_set_updated_at on public.consolidation_account_mappings;
create trigger consolidation_account_mappings_set_updated_at
  before update on public.consolidation_account_mappings
  for each row execute function public.set_updated_at();

drop trigger if exists consolidation_entity_balances_set_updated_at on public.consolidation_entity_balances;
create trigger consolidation_entity_balances_set_updated_at
  before update on public.consolidation_entity_balances
  for each row execute function public.set_updated_at();

drop trigger if exists consolidation_adjustments_set_updated_at on public.consolidation_adjustments;
create trigger consolidation_adjustments_set_updated_at
  before update on public.consolidation_adjustments
  for each row execute function public.set_updated_at();

create or replace function public.can_read_reporting_group(_group_id uuid)
returns boolean
language sql stable security definer set search_path = public
as $$
  select exists (
    select 1
    from public.reporting_groups rg
    where rg.id = _group_id
      and public.is_org_member(rg.owner_organisation_id)
  )
  or exists (
    select 1
    from public.reporting_group_entities rge
    where rge.reporting_group_id = _group_id
      and public.is_org_member(rge.organisation_id)
  )
  or exists (
    select 1
    from public.reporting_group_users rgu
    where rgu.reporting_group_id = _group_id
      and rgu.user_id = auth.uid()
      and rgu.status = 'active'
  );
$$;

create or replace function public.can_write_reporting_group(_group_id uuid)
returns boolean
language sql stable security definer set search_path = public
as $$
  select exists (
    select 1
    from public.reporting_groups rg
    where rg.id = _group_id
      and public.has_org_role(rg.owner_organisation_id, array['owner','admin','accountant']::public.organisation_role[])
  )
  or exists (
    select 1
    from public.reporting_group_users rgu
    where rgu.reporting_group_id = _group_id
      and rgu.user_id = auth.uid()
      and rgu.status = 'active'
      and rgu.role in ('owner', 'admin', 'accountant')
  );
$$;

alter table public.reporting_groups enable row level security;
alter table public.reporting_group_users enable row level security;
alter table public.reporting_group_entities enable row level security;
alter table public.consolidation_periods enable row level security;
alter table public.exchange_rates enable row level security;
alter table public.consolidation_account_mappings enable row level security;
alter table public.consolidation_entity_balances enable row level security;
alter table public.consolidation_adjustments enable row level security;
alter table public.consolidation_adjustment_lines enable row level security;

drop policy if exists "reporting_groups_select" on public.reporting_groups;
create policy "reporting_groups_select"
  on public.reporting_groups for select to authenticated
  using (public.can_read_reporting_group(id));

drop policy if exists "reporting_groups_insert" on public.reporting_groups;
create policy "reporting_groups_insert"
  on public.reporting_groups for insert to authenticated
  with check (public.has_org_role(owner_organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop policy if exists "reporting_groups_update" on public.reporting_groups;
create policy "reporting_groups_update"
  on public.reporting_groups for update to authenticated
  using (public.can_write_reporting_group(id))
  with check (public.can_write_reporting_group(id));

drop policy if exists "reporting_group_users_select" on public.reporting_group_users;
create policy "reporting_group_users_select"
  on public.reporting_group_users for select to authenticated
  using (public.can_read_reporting_group(reporting_group_id));

drop policy if exists "reporting_group_users_write" on public.reporting_group_users;
create policy "reporting_group_users_write"
  on public.reporting_group_users for all to authenticated
  using (public.can_write_reporting_group(reporting_group_id))
  with check (public.can_write_reporting_group(reporting_group_id));

drop policy if exists "reporting_group_entities_select" on public.reporting_group_entities;
create policy "reporting_group_entities_select"
  on public.reporting_group_entities for select to authenticated
  using (public.can_read_reporting_group(reporting_group_id));

drop policy if exists "reporting_group_entities_write" on public.reporting_group_entities;
create policy "reporting_group_entities_write"
  on public.reporting_group_entities for all to authenticated
  using (public.can_write_reporting_group(reporting_group_id))
  with check (public.can_write_reporting_group(reporting_group_id));

drop policy if exists "consolidation_periods_select" on public.consolidation_periods;
create policy "consolidation_periods_select"
  on public.consolidation_periods for select to authenticated
  using (public.can_read_reporting_group(reporting_group_id));

drop policy if exists "consolidation_periods_write" on public.consolidation_periods;
create policy "consolidation_periods_write"
  on public.consolidation_periods for all to authenticated
  using (public.can_write_reporting_group(reporting_group_id))
  with check (public.can_write_reporting_group(reporting_group_id));

drop policy if exists "exchange_rates_select" on public.exchange_rates;
create policy "exchange_rates_select"
  on public.exchange_rates for select to authenticated
  using (reporting_group_id is null or public.can_read_reporting_group(reporting_group_id));

drop policy if exists "exchange_rates_write" on public.exchange_rates;
create policy "exchange_rates_write"
  on public.exchange_rates for all to authenticated
  using (reporting_group_id is not null and public.can_write_reporting_group(reporting_group_id))
  with check (reporting_group_id is not null and public.can_write_reporting_group(reporting_group_id));

drop policy if exists "consolidation_account_mappings_select" on public.consolidation_account_mappings;
create policy "consolidation_account_mappings_select"
  on public.consolidation_account_mappings for select to authenticated
  using (public.can_read_reporting_group(reporting_group_id));

drop policy if exists "consolidation_account_mappings_write" on public.consolidation_account_mappings;
create policy "consolidation_account_mappings_write"
  on public.consolidation_account_mappings for all to authenticated
  using (public.can_write_reporting_group(reporting_group_id))
  with check (public.can_write_reporting_group(reporting_group_id));

drop policy if exists "consolidation_entity_balances_select" on public.consolidation_entity_balances;
create policy "consolidation_entity_balances_select"
  on public.consolidation_entity_balances for select to authenticated
  using (public.can_read_reporting_group(reporting_group_id));

drop policy if exists "consolidation_entity_balances_write" on public.consolidation_entity_balances;
create policy "consolidation_entity_balances_write"
  on public.consolidation_entity_balances for all to authenticated
  using (public.can_write_reporting_group(reporting_group_id))
  with check (public.can_write_reporting_group(reporting_group_id));

drop policy if exists "consolidation_adjustments_select" on public.consolidation_adjustments;
create policy "consolidation_adjustments_select"
  on public.consolidation_adjustments for select to authenticated
  using (public.can_read_reporting_group(reporting_group_id));

drop policy if exists "consolidation_adjustments_write" on public.consolidation_adjustments;
create policy "consolidation_adjustments_write"
  on public.consolidation_adjustments for all to authenticated
  using (public.can_write_reporting_group(reporting_group_id))
  with check (public.can_write_reporting_group(reporting_group_id));

drop policy if exists "consolidation_adjustment_lines_select" on public.consolidation_adjustment_lines;
create policy "consolidation_adjustment_lines_select"
  on public.consolidation_adjustment_lines for select to authenticated
  using (
    exists (
      select 1 from public.consolidation_adjustments a
      where a.id = adjustment_id and public.can_read_reporting_group(a.reporting_group_id)
    )
  );

drop policy if exists "consolidation_adjustment_lines_write" on public.consolidation_adjustment_lines;
create policy "consolidation_adjustment_lines_write"
  on public.consolidation_adjustment_lines for all to authenticated
  using (
    exists (
      select 1 from public.consolidation_adjustments a
      where a.id = adjustment_id and public.can_write_reporting_group(a.reporting_group_id)
    )
  )
  with check (
    exists (
      select 1 from public.consolidation_adjustments a
      where a.id = adjustment_id and public.can_write_reporting_group(a.reporting_group_id)
    )
  );
