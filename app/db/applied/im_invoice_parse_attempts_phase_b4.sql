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

create index if not exists invoice_parse_attempts_raw_idx
  on public.invoice_parse_attempts(invoice_raw_id, attempt_number);

create index if not exists invoice_parse_attempts_extracted_idx
  on public.invoice_parse_attempts(invoice_extracted_id);

create index if not exists invoice_parse_attempts_selected_idx
  on public.invoice_parse_attempts(invoice_raw_id, selected);

select 'invoice_parse_attempts_phase_b4_applied' as migration_note;
