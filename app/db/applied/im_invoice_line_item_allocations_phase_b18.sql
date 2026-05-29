-- ============================================================
-- APPayPal Invoice Line Item Allocations Phase B18
-- Stores account/tracking splits for invoice line items.
--
-- Allocations are accounting distribution rows only. They do not
-- replace document line totals or OCR/extraction calculations.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

create table if not exists public.invoice_line_item_allocations (
  id uuid primary key default gen_random_uuid(),
  invoice_line_item_id uuid not null references public.invoice_line_items(id) on delete cascade,
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  expense_account text,
  tracking jsonb not null default '{}'::jsonb,
  amount numeric(14, 2) not null,
  percent numeric(9, 4),
  note text,
  sort_order smallint not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint invoice_line_item_allocations_amount_check check (amount >= 0),
  constraint invoice_line_item_allocations_percent_check check (percent is null or (percent >= 0 and percent <= 100))
);

create index if not exists invoice_line_item_allocations_line_idx
  on public.invoice_line_item_allocations(invoice_line_item_id, sort_order);

create index if not exists invoice_line_item_allocations_org_idx
  on public.invoice_line_item_allocations(organisation_id);

drop trigger if exists invoice_line_item_allocations_set_updated_at
  on public.invoice_line_item_allocations;
create trigger invoice_line_item_allocations_set_updated_at
  before update on public.invoice_line_item_allocations
  for each row execute function public.set_updated_at();

alter table public.invoice_line_item_allocations enable row level security;

drop policy if exists "invoice_line_item_allocations_select_member"
  on public.invoice_line_item_allocations;
create policy "invoice_line_item_allocations_select_member"
  on public.invoice_line_item_allocations for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "invoice_line_item_allocations_write_accountants"
  on public.invoice_line_item_allocations;
create policy "invoice_line_item_allocations_write_accountants"
  on public.invoice_line_item_allocations for all to authenticated
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

select 'invoice_line_item_allocations_phase_b18_applied' as migration_note;
