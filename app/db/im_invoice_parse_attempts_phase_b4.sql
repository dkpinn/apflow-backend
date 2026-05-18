-- ============================================================
-- APPayPal Invoice Parse Attempts Phase B4
-- Stores temporary parse snapshots for review/selection.
-- Apply manually in external Supabase.
-- Idempotent. Does not change RLS.
-- ============================================================

create table if not exists public.invoice_parse_attempts (
  id uuid primary key default gen_random_uuid(),
  organisation_id uuid not null,
  invoice_raw_id uuid not null,
  invoice_extracted_id uuid,
  attempt_number integer not null,
  strategy text not null,
  dpi integer,
  ocr_variant text,
  ocr_psm text,
  ocr_used boolean default false,
  ocr_confidence numeric,
  image_quality_score numeric,
  candidate_score numeric,
  confidence_score numeric,
  parsed_data jsonb not null default '{}'::jsonb,
  line_items jsonb not null default '[]'::jsonb,
  text_preview text,
  selected boolean not null default false,
  accepted_at timestamptz,
  created_at timestamptz not null default now()
);

alter table public.invoice_parse_attempts
  add column if not exists organisation_id uuid,
  add column if not exists invoice_raw_id uuid,
  add column if not exists invoice_extracted_id uuid,
  add column if not exists attempt_number integer,
  add column if not exists strategy text,
  add column if not exists dpi integer,
  add column if not exists ocr_variant text,
  add column if not exists ocr_psm text,
  add column if not exists ocr_used boolean default false,
  add column if not exists ocr_confidence numeric,
  add column if not exists image_quality_score numeric,
  add column if not exists candidate_score numeric,
  add column if not exists confidence_score numeric,
  add column if not exists parsed_data jsonb default '{}'::jsonb,
  add column if not exists line_items jsonb default '[]'::jsonb,
  add column if not exists text_preview text,
  add column if not exists selected boolean default false,
  add column if not exists accepted_at timestamptz,
  add column if not exists created_at timestamptz default now();

create index if not exists invoice_parse_attempts_raw_idx
  on public.invoice_parse_attempts(invoice_raw_id, attempt_number);

create index if not exists invoice_parse_attempts_extracted_idx
  on public.invoice_parse_attempts(invoice_extracted_id);

create index if not exists invoice_parse_attempts_selected_idx
  on public.invoice_parse_attempts(invoice_raw_id, selected);

select 'invoice_parse_attempts_phase_b4_applied' as migration_note;
