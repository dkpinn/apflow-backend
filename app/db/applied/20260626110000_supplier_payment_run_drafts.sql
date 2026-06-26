-- Supplier payment run draft persistence.
-- Captures selected, bank-ready supplier invoices as a draft batch without posting or marking paid.

create table if not exists public.supplier_payment_runs (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  status text not null default 'draft'
    check (status in ('draft', 'approved', 'exported', 'cancelled')),
  pay_on_date date not null,
  due_within_days integer not null default 7 check (due_within_days between 0 and 90),
  currency text not null default 'ZAR',
  invoice_count integer not null default 0 check (invoice_count >= 0),
  selected_total numeric(14, 2) not null default 0 check (selected_total >= 0),
  notes text,
  created_by uuid,
  approved_by uuid,
  approved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.supplier_payment_run_items (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  payment_run_id uuid not null references public.supplier_payment_runs(id) on delete cascade,
  invoice_extracted_id uuid not null references public.invoices_extracted(id) on delete restrict,
  supplier_id uuid references public.suppliers(id) on delete set null,
  supplier_name text not null,
  invoice_number text,
  invoice_date date,
  due_date date,
  currency text not null default 'ZAR',
  total_amount numeric(14, 2) not null default 0,
  paid_amount numeric(14, 2) not null default 0,
  outstanding_amount numeric(14, 2) not null check (outstanding_amount > 0),
  priority text not null,
  bank_ready boolean not null default false,
  created_at timestamptz not null default now(),
  unique (payment_run_id, invoice_extracted_id)
);

create index if not exists supplier_payment_runs_org_status_idx
  on public.supplier_payment_runs(organisation_id, status, pay_on_date desc);

create index if not exists supplier_payment_run_items_run_idx
  on public.supplier_payment_run_items(payment_run_id);

create index if not exists supplier_payment_run_items_invoice_idx
  on public.supplier_payment_run_items(invoice_extracted_id);

drop trigger if exists supplier_payment_runs_set_updated_at on public.supplier_payment_runs;
create trigger supplier_payment_runs_set_updated_at
  before update on public.supplier_payment_runs
  for each row execute function public.update_updated_at_column();

alter table public.supplier_payment_runs enable row level security;
alter table public.supplier_payment_run_items enable row level security;

drop policy if exists "supplier_payment_runs_select_member" on public.supplier_payment_runs;
create policy "supplier_payment_runs_select_member"
  on public.supplier_payment_runs for select to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant','reviewer','viewer','client']::public.organisation_role[]));

drop policy if exists "supplier_payment_runs_write_accountants" on public.supplier_payment_runs;
create policy "supplier_payment_runs_write_accountants"
  on public.supplier_payment_runs for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop policy if exists "supplier_payment_run_items_select_member" on public.supplier_payment_run_items;
create policy "supplier_payment_run_items_select_member"
  on public.supplier_payment_run_items for select to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant','reviewer','viewer','client']::public.organisation_role[]));

drop policy if exists "supplier_payment_run_items_write_accountants" on public.supplier_payment_run_items;
create policy "supplier_payment_run_items_write_accountants"
  on public.supplier_payment_run_items for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

grant select, insert, update, delete on table public.supplier_payment_runs to authenticated;
grant select, insert, update, delete on table public.supplier_payment_run_items to authenticated;
grant all on table public.supplier_payment_runs to service_role;
grant all on table public.supplier_payment_run_items to service_role;
