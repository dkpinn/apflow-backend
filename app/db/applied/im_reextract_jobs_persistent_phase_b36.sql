-- ============================================================
-- Persist re-extraction job state in document_processing_jobs — Phase B36
-- Replaces the in-memory REEXTRACT_JOBS dict in _job_tracking.py.
-- Allows re-extraction job status to survive server restarts
-- and be accessible across multiple server instances.
-- ============================================================

alter table if exists public.document_processing_jobs
  add column if not exists job_type text not null default 'extraction',
  add column if not exists extracted_invoice_id uuid references public.invoices_extracted(id) on delete set null,
  add column if not exists diagnostic jsonb default '{}'::jsonb;

-- Record migration application
select 'im_reextract_jobs_persistent_phase_b36_applied' as migration_note;
