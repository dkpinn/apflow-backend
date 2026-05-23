-- ============================================================
-- APPayPal Invoice Entity Validation — Phase B2
-- Adds issuer/recipient/direction validation fields.
-- Apply manually in external Supabase.
-- Idempotent.
-- ============================================================

alter table public.invoices_extracted
  add column if not exists issuer_name_extracted text,
  add column if not exists recipient_name_extracted text,
  add column if not exists document_direction text,
  add column if not exists organisation_match_status text,
  add column if not exists validation_status text,
  add column if not exists validation_notes text;

alter table public.document_pages
  add column if not exists issuer_guess text,
  add column if not exists recipient_guess text,
  add column if not exists document_direction text,
  add column if not exists organisation_match_status text,
  add column if not exists validation_status text;

create index if not exists invoices_extracted_document_direction_idx
  on public.invoices_extracted(document_direction);

create index if not exists invoices_extracted_validation_status_idx
  on public.invoices_extracted(validation_status);

create index if not exists document_pages_document_direction_idx
  on public.document_pages(document_direction);
