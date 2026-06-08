-- ============================================================
-- Persist extraction_strategy on document_processing_jobs — Phase B22
-- Replaces the in-memory EXTRACTION_STRATEGY_OVERRIDES dict in _queue.py.
-- Allows the extraction strategy override to survive server restarts
-- and hot-reloads without being lost.
-- ============================================================

alter table if exists public.document_processing_jobs
  add column if not exists extraction_strategy text default null;

-- Record migration application
select 'im_document_processing_jobs_strategy_phase_b22_applied' as migration_note;
