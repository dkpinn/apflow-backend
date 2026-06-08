-- ============================================================
-- Reconciliation results + statement_lines.match_status
-- Apply this migration via the Lovable Cloud migration tool.
-- ============================================================

-- 1. Add match_status to statement_lines
alter table public.statement_lines
  add column if not exists match_status text;

create index if not exists statement_lines_match_status_idx
  on public.statement_lines(match_status);

-- 2. reconciliation_results table
create table if not exists public.reconciliation_results (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid references public.organisations(id) on delete cascade,
  reconciliation_id text not null,
  statement_raw_id uuid references public.statements_raw(id) on delete cascade,
  line_id uuid references public.statement_lines(id) on delete set null,
  match_status text not null check (match_status in ('matched', 'unmatched', 'exception')),
  expected_amount numeric(14,2),
  matched_amount numeric(14,2),
  variance_amount numeric(14,2),
  matched_invoice_id uuid,
  matched_invoice_number text,
  notes text,
  created_at timestamptz not null default now()
);

create index if not exists reconciliation_results_statement_idx
  on public.reconciliation_results(statement_raw_id);
create index if not exists reconciliation_results_line_idx
  on public.reconciliation_results(line_id);
create index if not exists reconciliation_results_recon_idx
  on public.reconciliation_results(reconciliation_id);

-- 3. RLS
alter table public.reconciliation_results enable row level security;

drop policy if exists "reconciliation_results_select_own_org"
  on public.reconciliation_results;
create policy "reconciliation_results_select_own_org"
  on public.reconciliation_results for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "reconciliation_results_insert_own_org"
  on public.reconciliation_results;
create policy "reconciliation_results_insert_own_org"
  on public.reconciliation_results for insert to authenticated
  with check (
    public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[])
  );

drop policy if exists "reconciliation_results_update_own_org"
  on public.reconciliation_results;
create policy "reconciliation_results_update_own_org"
  on public.reconciliation_results for update to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop policy if exists "reconciliation_results_delete_own_org"
  on public.reconciliation_results;
create policy "reconciliation_results_delete_own_org"
  on public.reconciliation_results for delete to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));
