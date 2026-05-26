-- ============================================================
-- APPayPal OCR Crop Diagnostics Phase B16
-- Stores the crop rectangle selected during receipt preprocessing.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

alter table public.document_pages
  add column if not exists crop_box jsonb,
  add column if not exists crop_area_ratio numeric;

select 'document_pages_crop_box_phase_b16_applied' as migration_note;
