-- ============================================================
-- Bank/Cash Phase C2
-- Adds extractor identity, raw extraction evidence, and richer
-- bank transaction detail fields without changing supplier flows.
-- Idempotent.
-- ============================================================

alter table if exists public.bank_statement_uploads
  add column if not exists extractor_type text not null default 'bank_statement',
  add column if not exists extractor_version text not null default 'v1',
  add column if not exists source_format text,
  add column if not exists raw_extraction jsonb not null default '{}'::jsonb,
  add column if not exists extraction_warnings jsonb not null default '[]'::jsonb;

alter table if exists public.bank_statement_lines
  add column if not exists transaction_type text,
  add column if not exists bank_reference text,
  add column if not exists raw_text text,
  add column if not exists raw_lines jsonb not null default '[]'::jsonb,
  add column if not exists source_page integer,
  add column if not exists source_row_index integer,
  add column if not exists extraction_confidence numeric(5,4),
  add column if not exists extraction_warnings jsonb not null default '[]'::jsonb;

create index if not exists bank_statement_uploads_extractor_idx
  on public.bank_statement_uploads(organisation_id, extractor_type, extractor_version, source_format);

create index if not exists bank_statement_lines_bank_reference_idx
  on public.bank_statement_lines(organisation_id, bank_account_id, bank_reference)
  where bank_reference is not null;

select 'bank_extraction_profiles_phase_c2_applied' as migration_note;
