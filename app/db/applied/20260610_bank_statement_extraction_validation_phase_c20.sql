-- ============================================================
-- Bank/Cash Phase C20
-- Adds the bank statement extraction validation/benchmark layer:
--   - bank_statement_extraction_runs: results of comparing an
--     extraction (live or test) against a gold-standard file plus
--     internal consistency checks.
--   - bank_statement_gold_files: manually verified gold-standard
--     statement transactions used for benchmarking.
-- Idempotent. Does not modify existing bank tables.
-- ============================================================

create table if not exists public.bank_statement_extraction_runs (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid null references public.organisations(id) on delete cascade,
  bank_statement_upload_id uuid null references public.bank_statement_uploads(id) on delete cascade,
  document_id text not null,
  bank text null,
  account_type text null,
  document_variant text null,
  extractor_name text null,
  expected_transaction_count integer null,
  extracted_transaction_count integer null,
  matched_transaction_count integer null,
  missing_transaction_count integer null,
  extra_transaction_count integer null,
  amount_accuracy numeric null,
  date_accuracy numeric null,
  description_accuracy numeric null,
  balance_accuracy numeric null,
  running_balance_passed boolean null,
  closing_balance_passed boolean null,
  can_allocate boolean not null default false,
  overall_score numeric null,
  critical_errors jsonb not null default '[]'::jsonb,
  warnings jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists bank_statement_extraction_runs_org_idx
  on public.bank_statement_extraction_runs(organisation_id);
create index if not exists bank_statement_extraction_runs_document_idx
  on public.bank_statement_extraction_runs(document_id);
create index if not exists bank_statement_extraction_runs_upload_idx
  on public.bank_statement_extraction_runs(bank_statement_upload_id);

create table if not exists public.bank_statement_gold_files (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid null references public.organisations(id) on delete cascade,
  document_id text not null,
  bank text not null,
  account_type text null,
  document_variant text not null,
  statement_start_date date null,
  statement_end_date date null,
  gold_json jsonb null,
  gold_csv_path text null,
  verified_by uuid null references auth.users(id) on delete set null,
  verified_at timestamptz null,
  created_at timestamptz not null default now()
);

create index if not exists bank_statement_gold_files_org_idx
  on public.bank_statement_gold_files(organisation_id);
create index if not exists bank_statement_gold_files_document_idx
  on public.bank_statement_gold_files(document_id);

alter table public.bank_statement_extraction_runs enable row level security;
alter table public.bank_statement_extraction_runs force row level security;
alter table public.bank_statement_gold_files enable row level security;
alter table public.bank_statement_gold_files force row level security;

drop policy if exists "bank_extraction_runs_select_member" on public.bank_statement_extraction_runs;
create policy "bank_extraction_runs_select_member" on public.bank_statement_extraction_runs
  for select to authenticated using (
    organisation_id is null or public.is_org_member(organisation_id)
  );
drop policy if exists "bank_extraction_runs_write_accountants" on public.bank_statement_extraction_runs;
create policy "bank_extraction_runs_write_accountants" on public.bank_statement_extraction_runs
  for all to authenticated
  using (
    organisation_id is null or public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[])
  )
  with check (
    organisation_id is null or public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[])
  );

drop policy if exists "bank_gold_files_select_member" on public.bank_statement_gold_files;
create policy "bank_gold_files_select_member" on public.bank_statement_gold_files
  for select to authenticated using (
    organisation_id is null or public.is_org_member(organisation_id)
  );
drop policy if exists "bank_gold_files_write_accountants" on public.bank_statement_gold_files;
create policy "bank_gold_files_write_accountants" on public.bank_statement_gold_files
  for all to authenticated
  using (
    organisation_id is null or public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[])
  )
  with check (
    organisation_id is null or public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[])
  );
