-- ============================================================
-- Bank/Cash Phase C3
-- Structured allocation rules, journal reversal linkage, and
-- audit-safe unpost support.
-- Idempotent.
-- ============================================================

alter table if exists public.bank_transaction_rules
  add column if not exists criteria jsonb not null default '[]'::jsonb,
  add column if not exists criteria_mode text not null default 'and'
    check (criteria_mode in ('and','or','only'));

alter table if exists public.gl_journals
  add column if not exists reversal_of_journal_id uuid references public.gl_journals(id) on delete set null;

create index if not exists bank_transaction_rules_criteria_idx
  on public.bank_transaction_rules using gin(criteria);

create index if not exists gl_journals_reversal_idx
  on public.gl_journals(organisation_id, reversal_of_journal_id)
  where reversal_of_journal_id is not null;

select 'bank_rules_journal_preview_unpost_phase_c3_applied' as migration_note;
