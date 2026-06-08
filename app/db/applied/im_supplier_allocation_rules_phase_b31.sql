-- ============================================================
-- Supplier Allocation Rules Phase B31
-- Explicit supplier-level account/tracking memory for future invoices.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

create table if not exists public.supplier_line_item_allocation_rules (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  supplier_id uuid not null references public.suppliers(id) on delete cascade,
  name text not null,
  active boolean not null default true,
  priority integer not null default 100,
  document_scope text not null default 'all'
    check (document_scope in ('all', 'invoice', 'credit_note')),
  match_type text not null default 'all_lines'
    check (match_type in ('all_lines', 'contains', 'exact', 'regex')),
  pattern text,
  match_field text not null default 'description'
    check (match_field in ('description', 'code', 'description_or_code')),
  notes text,
  source_invoice_extracted_id uuid references public.invoices_extracted(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.supplier_line_item_allocation_rule_splits (
  id uuid primary key default gen_random_uuid(),
  rule_id uuid not null references public.supplier_line_item_allocation_rules(id) on delete cascade,
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  expense_account text,
  tracking jsonb not null default '{}'::jsonb,
  percent numeric(9, 4) not null,
  note text,
  sort_order smallint not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint supplier_allocation_rule_splits_percent_check check (percent > 0 and percent <= 100)
);

create index if not exists supplier_allocation_rules_supplier_idx
  on public.supplier_line_item_allocation_rules(supplier_id, active, priority);

create index if not exists supplier_allocation_rules_org_idx
  on public.supplier_line_item_allocation_rules(organisation_id);

create index if not exists supplier_allocation_rule_splits_rule_idx
  on public.supplier_line_item_allocation_rule_splits(rule_id, sort_order);

drop trigger if exists supplier_allocation_rules_set_updated_at
  on public.supplier_line_item_allocation_rules;
create trigger supplier_allocation_rules_set_updated_at
  before update on public.supplier_line_item_allocation_rules
  for each row execute function public.set_updated_at();

drop trigger if exists supplier_allocation_rule_splits_set_updated_at
  on public.supplier_line_item_allocation_rule_splits;
create trigger supplier_allocation_rule_splits_set_updated_at
  before update on public.supplier_line_item_allocation_rule_splits
  for each row execute function public.set_updated_at();

alter table public.supplier_line_item_allocation_rules enable row level security;
alter table public.supplier_line_item_allocation_rule_splits enable row level security;

drop policy if exists "supplier_allocation_rules_select_member"
  on public.supplier_line_item_allocation_rules;
create policy "supplier_allocation_rules_select_member"
  on public.supplier_line_item_allocation_rules for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "supplier_allocation_rules_write_accountants"
  on public.supplier_line_item_allocation_rules;
create policy "supplier_allocation_rules_write_accountants"
  on public.supplier_line_item_allocation_rules for all to authenticated
  using (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  )
  with check (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );

drop policy if exists "supplier_allocation_rule_splits_select_member"
  on public.supplier_line_item_allocation_rule_splits;
create policy "supplier_allocation_rule_splits_select_member"
  on public.supplier_line_item_allocation_rule_splits for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "supplier_allocation_rule_splits_write_accountants"
  on public.supplier_line_item_allocation_rule_splits;
create policy "supplier_allocation_rule_splits_write_accountants"
  on public.supplier_line_item_allocation_rule_splits for all to authenticated
  using (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  )
  with check (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );

select 'supplier_allocation_rules_phase_b31_applied' as migration_note;
