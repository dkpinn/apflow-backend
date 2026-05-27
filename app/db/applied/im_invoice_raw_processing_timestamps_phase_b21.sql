-- ============================================================
-- Invoice processing timestamps — Phase B21
-- Adds parse_started_at and parse_completed_at to invoices_raw
-- so the job worker can record when processing begins and ends.
-- ============================================================

alter table if exists public.invoices_raw
  add column if not exists parse_started_at  timestamptz default null,
  add column if not exists parse_completed_at timestamptz default null;

-- Record migration application
select 'im_invoice_raw_processing_timestamps_phase_b21_applied' as migration_note;
