-- ============================================================
-- APPayPal Invoice Agent Suggestions Phase B24
-- Stores suggest-only invoice review/coding agent output.
--
-- Suggestions are advisory workflow records. They must not approve,
-- post, or alter invoice calculations by themselves.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

create table if not exists public.invoice_agent_suggestions (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  invoice_raw_id uuid references public.invoices_raw(id) on delete cascade,
  invoice_extracted_id uuid references public.invoices_extracted(id) on delete cascade,
  category text not null,
  severity text not null default 'info'
    check (severity in ('info', 'warning', 'critical')),
  message text not null,
  reason text,
  confidence numeric(5, 4) not null default 0
    check (confidence >= 0 and confidence <= 1),
  apply_payload jsonb,
  status text not null default 'open'
    check (status in ('open', 'applied', 'dismissed', 'checked')),
  fingerprint text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists invoice_agent_suggestions_org_idx
  on public.invoice_agent_suggestions(organisation_id, status, created_at desc);

create index if not exists invoice_agent_suggestions_raw_idx
  on public.invoice_agent_suggestions(invoice_raw_id, status);

create index if not exists invoice_agent_suggestions_extracted_idx
  on public.invoice_agent_suggestions(invoice_extracted_id, status);

create unique index if not exists invoice_agent_suggestions_extracted_fp_idx
  on public.invoice_agent_suggestions(organisation_id, invoice_extracted_id, fingerprint)
  where invoice_extracted_id is not null;

create unique index if not exists invoice_agent_suggestions_raw_fp_idx
  on public.invoice_agent_suggestions(organisation_id, invoice_raw_id, fingerprint)
  where invoice_extracted_id is null and invoice_raw_id is not null;

drop trigger if exists invoice_agent_suggestions_set_updated_at
  on public.invoice_agent_suggestions;
create trigger invoice_agent_suggestions_set_updated_at
  before update on public.invoice_agent_suggestions
  for each row execute function public.set_updated_at();

alter table public.invoice_agent_suggestions enable row level security;

drop policy if exists "invoice_agent_suggestions_select_member"
  on public.invoice_agent_suggestions;
create policy "invoice_agent_suggestions_select_member"
  on public.invoice_agent_suggestions for select to authenticated
  using (public.is_org_member(organisation_id));

drop policy if exists "invoice_agent_suggestions_insert_accountants"
  on public.invoice_agent_suggestions;
create policy "invoice_agent_suggestions_insert_accountants"
  on public.invoice_agent_suggestions for insert to authenticated
  with check (
    public.has_org_role(
      organisation_id,
      array['owner','admin','accountant']::public.organisation_role[]
    )
  );

drop policy if exists "invoice_agent_suggestions_update_accountants"
  on public.invoice_agent_suggestions;
create policy "invoice_agent_suggestions_update_accountants"
  on public.invoice_agent_suggestions for update to authenticated
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

select 'invoice_agent_suggestions_phase_b24_applied' as migration_note;
