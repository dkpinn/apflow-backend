-- ============================================================
-- Phase C18
-- General-ledger account budgets (organisation-wide, by account
-- and optional tracking value, over a date range), feeding the
-- "actual vs budget" comparison on the Trial Balance report.
-- Idempotent.
-- ============================================================

create table if not exists public.account_budgets (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  account_id uuid not null references public.accounts(id) on delete cascade,
  tracking_value_id uuid references public.tracking_values(id) on delete set null,
  period_start date not null,
  period_end date not null,
  amount numeric(14, 2) not null default 0 check (amount >= 0),
  notes text,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint account_budgets_period_check check (period_start <= period_end)
);

create index if not exists account_budgets_org_account_idx
  on public.account_budgets(organisation_id, account_id);

alter table public.account_budgets enable row level security;

drop policy if exists "account_budgets_select_member" on public.account_budgets;
create policy "account_budgets_select_member" on public.account_budgets
  for select to authenticated using (public.is_org_member(organisation_id));
drop policy if exists "account_budgets_write_accountants" on public.account_budgets;
create policy "account_budgets_write_accountants" on public.account_budgets
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop trigger if exists account_budgets_set_updated_at on public.account_budgets;
create trigger account_budgets_set_updated_at
  before update on public.account_budgets
  for each row execute function public.set_updated_at();
