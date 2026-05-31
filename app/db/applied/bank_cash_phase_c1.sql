-- ============================================================
-- Bank/Cash Phase C1
-- Bank account, statement import, allocation, rule, GL posting,
-- and audit tables for file-based Bank/Cash automation.
-- Idempotent.
-- ============================================================

create table if not exists public.bank_accounts (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  name text not null,
  institution_name text,
  account_type text not null default 'bank'
    check (account_type in (
      'bank','cash','credit_card','loan','mortgage','vehicle_finance',
      'investment','call_account','money_market','paypal','paygate',
      'crypto','foreign_bank','other'
    )),
  currency text not null default 'ZAR',
  account_number_mask text,
  account_number_hash text,
  gl_account_id uuid references public.accounts(id) on delete set null,
  opening_balance numeric(14,2) not null default 0,
  current_reconciled_balance numeric(14,2) not null default 0,
  last_statement_upload_id uuid,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists bank_accounts_org_idx
  on public.bank_accounts(organisation_id, active, account_type);

create table if not exists public.bank_statement_uploads (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  bank_account_id uuid not null references public.bank_accounts(id) on delete cascade,
  original_filename text not null,
  mime_type text,
  storage_bucket text not null default 'statement-files',
  storage_path text not null,
  file_sha256 text,
  source_type text not null default 'upload'
    check (source_type in ('upload','email','mobile','bank_feed','api','other')),
  statement_period_from date,
  statement_period_to date,
  opening_balance numeric(14,2),
  closing_balance numeric(14,2),
  extracted_line_count integer not null default 0,
  duplicate_line_count integer not null default 0,
  balance_status text not null default 'unchecked'
    check (balance_status in ('unchecked','balanced','opening_mismatch','closing_mismatch','missing_balance')),
  duplicate_status text not null default 'unchecked'
    check (duplicate_status in ('unchecked','clear','possible_duplicates','duplicate_file')),
  extraction_status text not null default 'uploaded'
    check (extraction_status in ('uploaded','processing','extracted','failed')),
  confidence_score numeric(5,4),
  duplicate_summary jsonb not null default '{}'::jsonb,
  extraction_evidence jsonb not null default '{}'::jsonb,
  uploaded_by uuid references auth.users(id) on delete set null,
  uploaded_at timestamptz not null default now(),
  extracted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists bank_statement_uploads_org_idx
  on public.bank_statement_uploads(organisation_id, bank_account_id, uploaded_at desc);
create index if not exists bank_statement_uploads_file_sha_idx
  on public.bank_statement_uploads(organisation_id, bank_account_id, file_sha256)
  where file_sha256 is not null;

create table if not exists public.bank_statement_lines (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  bank_account_id uuid not null references public.bank_accounts(id) on delete cascade,
  bank_statement_upload_id uuid not null references public.bank_statement_uploads(id) on delete cascade,
  line_date date,
  value_date date,
  description text,
  reference text,
  counterparty text,
  debit_amount numeric(14,2) not null default 0,
  credit_amount numeric(14,2) not null default 0,
  signed_amount numeric(14,2) not null default 0,
  balance_amount numeric(14,2),
  currency text,
  transaction_hash text not null,
  duplicate_status text not null default 'clear'
    check (duplicate_status in ('clear','possible_duplicate','duplicate')),
  match_status text not null default 'unmatched'
    check (match_status in ('unmatched','suggested','matched','exception','ignored')),
  allocation_status text not null default 'unallocated'
    check (allocation_status in ('unallocated','suggested','allocated','split')),
  posting_status text not null default 'unposted'
    check (posting_status in ('unposted','draft','posted','reversed')),
  accepted_suggestion_id uuid,
  accepted_rule_id uuid,
  gl_journal_id uuid,
  review_status text not null default 'pending'
    check (review_status in ('pending','reviewed','approved','ignored')),
  reviewed_by uuid references auth.users(id) on delete set null,
  reviewed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists bank_statement_lines_upload_idx
  on public.bank_statement_lines(bank_statement_upload_id, line_date, id);
create index if not exists bank_statement_lines_hash_idx
  on public.bank_statement_lines(organisation_id, bank_account_id, transaction_hash);

create table if not exists public.bank_transaction_suggestions (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  bank_statement_line_id uuid not null references public.bank_statement_lines(id) on delete cascade,
  suggestion_type text not null
    check (suggestion_type in ('supplier_invoice','receivable_invoice','prior_transaction','rule','manual','vlm')),
  confidence_score numeric(5,4) not null default 0,
  rationale text,
  evidence jsonb not null default '{}'::jsonb,
  matched_invoice_id uuid references public.invoices_extracted(id) on delete set null,
  matched_invoice_number text,
  suggested_account_id uuid references public.accounts(id) on delete set null,
  suggested_tracking jsonb not null default '{}'::jsonb,
  suggested_tax_treatment text,
  status text not null default 'open'
    check (status in ('open','accepted','rejected','superseded')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists bank_transaction_suggestions_line_idx
  on public.bank_transaction_suggestions(bank_statement_line_id, confidence_score desc);

create table if not exists public.bank_transaction_rules (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  bank_account_id uuid references public.bank_accounts(id) on delete cascade,
  name text not null,
  active boolean not null default true,
  priority integer not null default 100,
  amount_direction text not null default 'any'
    check (amount_direction in ('any','money_in','money_out')),
  match_type text not null default 'contains'
    check (match_type in ('contains','exact','regex')),
  description_pattern text,
  reference_pattern text,
  counterparty_pattern text,
  min_amount numeric(14,2),
  max_amount numeric(14,2),
  gl_account_id uuid references public.accounts(id) on delete set null,
  tracking jsonb not null default '{}'::jsonb,
  tax_treatment text,
  notes text,
  source_bank_statement_line_id uuid references public.bank_statement_lines(id) on delete set null,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists bank_transaction_rules_org_idx
  on public.bank_transaction_rules(organisation_id, active, priority);

create table if not exists public.gl_journals (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  source_type text not null default 'bank_transaction',
  source_id uuid,
  journal_date date,
  description text,
  status text not null default 'draft'
    check (status in ('draft','posted','reversed')),
  total_debit numeric(14,2) not null default 0,
  total_credit numeric(14,2) not null default 0,
  created_by uuid references auth.users(id) on delete set null,
  posted_by uuid references auth.users(id) on delete set null,
  posted_at timestamptz,
  reversed_by uuid references auth.users(id) on delete set null,
  reversed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint gl_journals_balanced_check check (round(total_debit, 2) = round(total_credit, 2))
);

create table if not exists public.gl_journal_lines (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  gl_journal_id uuid not null references public.gl_journals(id) on delete cascade,
  account_id uuid references public.accounts(id) on delete restrict,
  description text,
  debit_amount numeric(14,2) not null default 0,
  credit_amount numeric(14,2) not null default 0,
  tracking jsonb not null default '{}'::jsonb,
  sort_order smallint not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists gl_journal_lines_journal_idx
  on public.gl_journal_lines(gl_journal_id, sort_order);

create table if not exists public.bank_audit_events (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null references public.organisations(id) on delete cascade,
  bank_account_id uuid references public.bank_accounts(id) on delete set null,
  bank_statement_upload_id uuid references public.bank_statement_uploads(id) on delete set null,
  bank_statement_line_id uuid references public.bank_statement_lines(id) on delete set null,
  gl_journal_id uuid references public.gl_journals(id) on delete set null,
  event_type text not null,
  actor_user_id uuid references auth.users(id) on delete set null,
  actor_type text not null default 'user',
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists bank_audit_events_org_idx
  on public.bank_audit_events(organisation_id, created_at desc);

alter table public.bank_accounts enable row level security;
alter table public.bank_statement_uploads enable row level security;
alter table public.bank_statement_lines enable row level security;
alter table public.bank_transaction_suggestions enable row level security;
alter table public.bank_transaction_rules enable row level security;
alter table public.gl_journals enable row level security;
alter table public.gl_journal_lines enable row level security;
alter table public.bank_audit_events enable row level security;

drop policy if exists "bank_accounts_select_member" on public.bank_accounts;
create policy "bank_accounts_select_member" on public.bank_accounts
  for select to authenticated using (public.is_org_member(organisation_id));
drop policy if exists "bank_accounts_write_accountants" on public.bank_accounts;
create policy "bank_accounts_write_accountants" on public.bank_accounts
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop policy if exists "bank_uploads_select_member" on public.bank_statement_uploads;
create policy "bank_uploads_select_member" on public.bank_statement_uploads
  for select to authenticated using (public.is_org_member(organisation_id));
drop policy if exists "bank_uploads_write_accountants" on public.bank_statement_uploads;
create policy "bank_uploads_write_accountants" on public.bank_statement_uploads
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop policy if exists "bank_lines_select_member" on public.bank_statement_lines;
create policy "bank_lines_select_member" on public.bank_statement_lines
  for select to authenticated using (public.is_org_member(organisation_id));
drop policy if exists "bank_lines_write_accountants" on public.bank_statement_lines;
create policy "bank_lines_write_accountants" on public.bank_statement_lines
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop policy if exists "bank_suggestions_select_member" on public.bank_transaction_suggestions;
create policy "bank_suggestions_select_member" on public.bank_transaction_suggestions
  for select to authenticated using (public.is_org_member(organisation_id));
drop policy if exists "bank_suggestions_write_accountants" on public.bank_transaction_suggestions;
create policy "bank_suggestions_write_accountants" on public.bank_transaction_suggestions
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop policy if exists "bank_rules_select_member" on public.bank_transaction_rules;
create policy "bank_rules_select_member" on public.bank_transaction_rules
  for select to authenticated using (public.is_org_member(organisation_id));
drop policy if exists "bank_rules_write_accountants" on public.bank_transaction_rules;
create policy "bank_rules_write_accountants" on public.bank_transaction_rules
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop policy if exists "gl_journals_select_member" on public.gl_journals;
create policy "gl_journals_select_member" on public.gl_journals
  for select to authenticated using (public.is_org_member(organisation_id));
drop policy if exists "gl_journals_write_accountants" on public.gl_journals;
create policy "gl_journals_write_accountants" on public.gl_journals
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop policy if exists "gl_journal_lines_select_member" on public.gl_journal_lines;
create policy "gl_journal_lines_select_member" on public.gl_journal_lines
  for select to authenticated using (public.is_org_member(organisation_id));
drop policy if exists "gl_journal_lines_write_accountants" on public.gl_journal_lines;
create policy "gl_journal_lines_write_accountants" on public.gl_journal_lines
  for all to authenticated
  using (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]))
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop policy if exists "bank_audit_events_select_member" on public.bank_audit_events;
create policy "bank_audit_events_select_member" on public.bank_audit_events
  for select to authenticated using (public.is_org_member(organisation_id));
drop policy if exists "bank_audit_events_insert_accountants" on public.bank_audit_events;
create policy "bank_audit_events_insert_accountants" on public.bank_audit_events
  for insert to authenticated
  with check (public.has_org_role(organisation_id, array['owner','admin','accountant']::public.organisation_role[]));

drop trigger if exists bank_accounts_set_updated_at on public.bank_accounts;
create trigger bank_accounts_set_updated_at
  before update on public.bank_accounts
  for each row execute function public.set_updated_at();

drop trigger if exists bank_statement_uploads_set_updated_at on public.bank_statement_uploads;
create trigger bank_statement_uploads_set_updated_at
  before update on public.bank_statement_uploads
  for each row execute function public.set_updated_at();

drop trigger if exists bank_statement_lines_set_updated_at on public.bank_statement_lines;
create trigger bank_statement_lines_set_updated_at
  before update on public.bank_statement_lines
  for each row execute function public.set_updated_at();

select 'bank_cash_phase_c1_applied' as migration_note;
