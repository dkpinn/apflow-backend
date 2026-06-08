-- ============================================================
-- APPayPal Document Preview Phase B3.2
-- Adds preview artifact and receipt/photo preprocessing metadata.
-- Apply manually in external Supabase.
-- Idempotent. Does not change RLS.
-- ============================================================

alter table public.invoices_raw
  add column if not exists preview_path text,
  add column if not exists processed_preview_path text;

alter table public.document_pages
  add column if not exists original_preview_path text,
  add column if not exists processed_preview_path text,
  add column if not exists preprocessing_notes text,
  add column if not exists crop_applied boolean default false,
  add column if not exists deskew_applied boolean default false;

select 'document_preview_phase_b3_2_applied' as migration_note;
