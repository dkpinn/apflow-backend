-- ============================================================
-- Bank/Cash Phase C16
-- Per-bank / per-account-type parsing hints fed to the VLM
-- statement extractor, configurable under Settings -> Bank/Cash.
-- Idempotent.
-- ============================================================

create table if not exists public.bank_parsing_rules (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  institution_name text,
  account_type text
    check (account_type is null or account_type in (
      'bank','cash','credit_card','loan','mortgage','vehicle_finance',
      'investment','call_account','money_market','paypal','paygate',
      'crypto','foreign_bank','other'
    )),
  parsing_hint text not null,
  active boolean not null default true,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists bank_parsing_rules_org_idx
  on public.bank_parsing_rules(organisation_id, active, institution_name, account_type);

alter table public.bank_parsing_rules enable row level security;

drop policy if exists "bank_parsing_rules_select_member" on public.bank_parsing_rules;
create policy "bank_parsing_rules_select_member" on public.bank_parsing_rules
  for select to authenticated using (public.is_org_member(organisation_id));
drop policy if exists "bank_parsing_rules_write_accountants" on public.bank_parsing_rules;
create policy "bank_parsing_rules_write_accountants" on public.bank_parsing_rules
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));
